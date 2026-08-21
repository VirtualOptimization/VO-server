"""Poll and execute queued conversion tasks.

Run this as a separate process/service from the FastAPI API service so heavy
asset conversions cannot take down the request server cgroup.
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from server.core.s3 import build_s3_uri
from server.services.conversion_tasks import (
    TASK_FURNITURE_GLB_TO_USDZ,
    TASK_FURNITURE_USDC_TO_GLB,
    TASK_MATERIAL_ASSET,
)
from server.services.furniture_conversion import (
    _run_furniture_conversion_sync,
    _run_furniture_texture_apply_sync,
    _run_furniture_usdz_conversion_sync,
)
from shared.db import SessionLocal
from shared.models.conversion_task import ConversionTask
from shared.models.furniture_model import FurnitureModel
from shared.models.version import Version

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
    _mark_related_material_failed(db, task, str(exc))
    db.commit()


def _mark_related_model_failed(db: Session, task: ConversionTask) -> None:
    if task.task_type != TASK_FURNITURE_USDC_TO_GLB:
        return
    if task.furniture_model_id is None:
        return
    model = db.get(FurnitureModel, task.furniture_model_id)
    if model is not None and model.status != "DELETED":
        model.status = "FAILED"


def _mark_related_material_failed(db: Session, task: ConversionTask, message: str) -> None:
    if task.task_type != TASK_MATERIAL_ASSET or task.version_id is None:
        return

    payload = task.payload or {}
    furniture_instance_id = payload.get("furniture_instance_id")
    if not isinstance(furniture_instance_id, str) or not furniture_instance_id:
        return

    version = db.get(Version, task.version_id)
    if version is None:
        return

    json_data = dict(version.json_data or {})
    material_assets = dict(json_data.get("material_assets") or {})
    asset = dict(material_assets.get(furniture_instance_id) or {})
    if not asset:
        return

    asset["status"] = "FAILED"
    asset["error_message"] = message[:1000]
    material_assets[furniture_instance_id] = asset
    json_data["material_assets"] = material_assets
    version.json_data = json_data


def _complete_furniture_glb_task(db: Session, task: ConversionTask) -> None:
    if not task.source_key or not task.output_glb_key:
        raise ValueError("FURNITURE_USDC_TO_GLB task requires source_key and output_glb_key")

    _run_furniture_conversion_sync(task.source_key, task.output_glb_key)

    if task.furniture_model_id is not None:
        model = db.get(FurnitureModel, task.furniture_model_id)
        if model is not None and model.status != "DELETED":
            model.status = "READY"
            model.glb_url = build_s3_uri(task.output_glb_key)


def _complete_furniture_usdz_task(db: Session, task: ConversionTask) -> None:
    if not task.source_key or not task.output_usdz_key:
        raise ValueError("FURNITURE_GLB_TO_USDZ task requires source_key and output_usdz_key")

    _run_furniture_usdz_conversion_sync(task.source_key, task.output_usdz_key)

    if task.furniture_model_id is not None:
        model = db.get(FurnitureModel, task.furniture_model_id)
        if model is not None and model.status != "DELETED":
            model.usdz_url = build_s3_uri(task.output_usdz_key)


def _complete_material_asset_task(db: Session, task: ConversionTask) -> None:
    if not task.source_key or not task.texture_key or not task.output_glb_key or not task.output_usdz_key:
        raise ValueError("MATERIAL_ASSET task requires source, texture, GLB output, and USDZ output")

    _run_furniture_texture_apply_sync(task.source_key, task.texture_key, task.output_glb_key)
    _run_furniture_usdz_conversion_sync(task.output_glb_key, task.output_usdz_key)
    _mark_material_asset_ready(db, task)


def _mark_material_asset_ready(db: Session, task: ConversionTask) -> None:
    if task.version_id is None:
        return

    payload: dict[str, Any] = task.payload or {}
    furniture_instance_id = payload.get("furniture_instance_id")
    if not isinstance(furniture_instance_id, str) or not furniture_instance_id:
        return

    version = db.get(Version, task.version_id)
    if version is None:
        return

    json_data = dict(version.json_data or {})
    material_assets = dict(json_data.get("material_assets") or {})
    asset = dict(material_assets.get(furniture_instance_id) or {})
    asset.update(
        {
            **{k: v for k, v in payload.items() if k != "furniture_instance_id"},
            "status": "READY",
            "version_id": version.id,
            "glb": build_s3_uri(task.output_glb_key),
            "usdz": build_s3_uri(task.output_usdz_key),
        }
    )
    asset.pop("error_message", None)
    material_assets[furniture_instance_id] = asset
    json_data["material_assets"] = material_assets
    version.json_data = json_data


def _execute_task(db: Session, task: ConversionTask) -> None:
    logger.info("conversion_task_started id=%s type=%s", task.id, task.task_type)
    if task.task_type == TASK_FURNITURE_USDC_TO_GLB:
        _complete_furniture_glb_task(db, task)
    elif task.task_type == TASK_FURNITURE_GLB_TO_USDZ:
        _complete_furniture_usdz_task(db, task)
    elif task.task_type == TASK_MATERIAL_ASSET:
        _complete_material_asset_task(db, task)
    else:
        raise ValueError(f"Unsupported conversion task type: {task.task_type}")

    _mark_done(db, task)
    logger.info("conversion_task_done id=%s type=%s", task.id, task.task_type)


def run_once() -> bool:
    with SessionLocal() as db:
        task = _claim_next_task(db)
        if task is None:
            return False

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
