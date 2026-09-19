"""Poll and execute queued conversion tasks.

Run this as a separate process/service from the FastAPI API service so heavy
asset conversions cannot take down the request server cgroup.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from botocore.exceptions import ClientError

from server.core.config import settings
from server.core.s3 import build_s3_uri, get_s3_client
from server.services.conversion_tasks import (
    TASK_FURNITURE_GLB_TO_USDZ,
    TASK_FURNITURE_USDC_TO_GLB,
)
from server.services.furniture_conversion import (
    _run_furniture_conversion_sync,
    _run_furniture_usdz_conversion_sync,
)
from shared.db import SessionLocal
from shared.models.conversion_task import ConversionTask
from shared.models.furniture_model import FurnitureModel

logger = logging.getLogger("conversion_task_worker")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _claim_next_task(db: Session) -> ConversionTask | None:
    task = (
        db.query(ConversionTask)
        .filter(ConversionTask.status == "PENDING")
        .order_by(ConversionTask.created_at.asc(), ConversionTask.id.asc())
        .with_for_update(skip_locked=True)
        .first()
    )
    if task is None:
        return None

    task.status = "RUNNING"
    task.started_at = _utcnow()
    task.attempts = (task.attempts or 0) + 1
    task.error_message = None
    db.commit()
    db.refresh(task)
    return task


def _mark_done(db: Session, task: ConversionTask) -> None:
    task.status = "DONE"
    task.finished_at = _utcnow()
    task.error_message = None
    db.commit()


def _mark_failed(db: Session, task: ConversionTask, exc: BaseException) -> None:
    task.status = "FAILED"
    task.finished_at = _utcnow()
    task.error_message = str(exc)[:4000]
    _mark_related_model_failed(db, task)
    db.commit()


def _mark_related_model_failed(db: Session, task: ConversionTask) -> None:
    if task.task_type != TASK_FURNITURE_USDC_TO_GLB:
        return
    if task.furniture_model_id is None:
        return
    model = db.get(FurnitureModel, task.furniture_model_id)
    if model is not None and model.status != "DELETED":
        model.status = "FAILED"


def _mark_furniture_glb_ready(db: Session, task: ConversionTask) -> None:
    if task.furniture_model_id is None or not task.output_glb_key:
        return

    model = db.get(FurnitureModel, task.furniture_model_id)
    if model is not None and model.status != "DELETED":
        model.status = "READY"
        model.glb_url = build_s3_uri(task.output_glb_key)


def _mark_furniture_usdz_ready(db: Session, task: ConversionTask) -> None:
    if task.furniture_model_id is None or not task.output_usdz_key:
        return

    model = db.get(FurnitureModel, task.furniture_model_id)
    if model is not None and model.status != "DELETED":
        model.usdz_url = build_s3_uri(task.output_usdz_key)


def _complete_furniture_glb_task(db: Session, task: ConversionTask) -> None:
    if not task.source_key or not task.output_glb_key:
        raise ValueError("FURNITURE_USDC_TO_GLB task requires source_key and output_glb_key")

    _run_furniture_conversion_sync(task.source_key, task.output_glb_key)


def _complete_furniture_usdz_task(db: Session, task: ConversionTask) -> None:
    if not task.source_key or not task.output_usdz_key:
        raise ValueError("FURNITURE_GLB_TO_USDZ task requires source_key and output_usdz_key")

    _run_furniture_usdz_conversion_sync(task.source_key, task.output_usdz_key)


def _expected_output_keys(task: ConversionTask) -> list[str]:
    if task.task_type == TASK_FURNITURE_USDC_TO_GLB:
        return [task.output_glb_key] if task.output_glb_key else []
    if task.task_type == TASK_FURNITURE_GLB_TO_USDZ:
        return [task.output_usdz_key] if task.output_usdz_key else []
    return []


def _outputs_exist(task: ConversionTask) -> bool:
    """Return whether all artifacts required by a task are already in S3."""
    keys = _expected_output_keys(task)
    if not keys or not settings.s3_bucket_name:
        return False

    s3 = get_s3_client()
    try:
        for key in keys:
            s3.head_object(Bucket=settings.s3_bucket_name, Key=key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    return True


def _apply_completed_task_result(db: Session, task: ConversionTask) -> None:
    """Persist DB state for a task whose output was produced successfully."""
    if task.task_type == TASK_FURNITURE_USDC_TO_GLB:
        _mark_furniture_glb_ready(db, task)
    elif task.task_type == TASK_FURNITURE_GLB_TO_USDZ:
        _mark_furniture_usdz_ready(db, task)
    else:
        raise ValueError(f"Unsupported conversion task type: {task.task_type}")


def _reconcile_finished_running_tasks(db: Session) -> int:
    """Finalize interrupted tasks when their expected S3 output already exists.

    The converter uploads its result before this worker writes DONE. Therefore a
    process interruption in that narrow window used to leave tasks RUNNING
    forever even though the conversion itself was successful.
    """
    tasks = (
        db.query(ConversionTask)
        .filter(ConversionTask.status == "RUNNING")
        .order_by(ConversionTask.started_at.asc(), ConversionTask.id.asc())
        .with_for_update(skip_locked=True)
        .all()
    )
    reconciled = 0
    for task in tasks:
        try:
            if not _outputs_exist(task):
                continue
            _apply_completed_task_result(db, task)
            _mark_done(db, task)
            logger.info("conversion_task_reconciled id=%s type=%s", task.id, task.task_type)
            reconciled += 1
        except Exception:  # noqa: BLE001 - leave task RUNNING for a later retry if reconciliation cannot verify it
            logger.exception("conversion_task_reconcile_failed id=%s type=%s", task.id, task.task_type)
            db.rollback()
    return reconciled


def _execute_task(db: Session, task: ConversionTask) -> None:
    logger.info("conversion_task_started id=%s type=%s", task.id, task.task_type)
    if task.task_type == TASK_FURNITURE_USDC_TO_GLB:
        _complete_furniture_glb_task(db, task)
    elif task.task_type == TASK_FURNITURE_GLB_TO_USDZ:
        _complete_furniture_usdz_task(db, task)
    else:
        raise ValueError(f"Unsupported conversion task type: {task.task_type}")

    _apply_completed_task_result(db, task)
    _mark_done(db, task)
    logger.info("conversion_task_done id=%s type=%s", task.id, task.task_type)


def run_once() -> bool:
    with SessionLocal() as db:
        reconciled = _reconcile_finished_running_tasks(db)
        task = _claim_next_task(db)
        if task is None:
            return reconciled > 0

        try:
            _execute_task(db, task)
        except Exception as exc:  # noqa: BLE001 - worker must persist failures instead of crashing API state
            logger.exception("conversion_task_failed id=%s type=%s", task.id, task.task_type)
            _mark_failed(db, task, exc)
        return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Run queued V-O conversion tasks")
    parser.add_argument("--once", action="store_true", help="Process at most one pending task")
    parser.add_argument("--poll-seconds", type=float, default=3.0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.once:
        run_once()
        return

    while True:
        processed = run_once()
        if not processed:
            time.sleep(args.poll_seconds)


if __name__ == "__main__":
    main()
