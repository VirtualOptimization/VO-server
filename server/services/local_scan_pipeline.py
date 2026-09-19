"""Run the scan processing pipeline inside the FastAPI host.

This replaces the Step Functions + ECS + Lambda path for low-cost deployments.

Save (GLB shell conversion) and optimize (furniture layout) run as two
independent background steps: saving a scan must not block on or fail because
of optimization, and optimization is only triggered later when the user asks
for it from the room-view screen.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from server.core.config import settings
from shared.db import SessionLocal
from shared.models.room import Room
from shared.models.version import Version


logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _s3_url(bucket: str, key: str | None) -> str | None:
    return f"s3://{bucket}/{key}" if key else None


def _run_python_script(script: str, env_updates: dict[str, str]) -> None:
    env = os.environ.copy()
    env.update(env_updates)
    env["PYTHONPATH"] = str(REPO_ROOT)
    subprocess.run(
        [sys.executable, script],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        timeout=settings.local_pipeline_timeout_seconds,
    )


def _mark_optimization_status(db: Session, room_id: int, status: str) -> None:
    room = db.get(Room, room_id)
    if not room:
        raise ValueError(f"Room not found: {room_id}")
    room.optimization_status = status
    db.commit()


def _upsert_pipeline_versions(db: Session, event: dict[str, Any]) -> dict[str, Any]:
    room_id = event["room_id"]
    inputs = event["inputs"]
    outputs = event["outputs"]
    bucket = event.get("bucket") or settings.s3_bucket_name

    raw_json_key = inputs["room_data_json"]
    normalized_json_key = outputs.get("normalized_json")
    problem_json_key = outputs.get("problem_json")
    optimized_json_key = outputs.get("optimized_json")
    roomplan_optimized_key = outputs["roomplan_optimized_json"]
    unity_roomplan_optimized_key = outputs.get("unity_roomplan_optimized_json")
    glb_key = outputs.get("glb")

    original_s3_url = _s3_url(bucket, raw_json_key)
    normalized_s3_url = _s3_url(bucket, normalized_json_key)
    problem_s3_url = _s3_url(bucket, problem_json_key)
    optimized_s3_url = _s3_url(bucket, optimized_json_key)
    roomplan_optimized_s3_url = _s3_url(bucket, roomplan_optimized_key)
    unity_roomplan_optimized_s3_url = _s3_url(bucket, unity_roomplan_optimized_key)
    glb_s3_url = _s3_url(bucket, glb_key)

    room = db.get(Room, room_id)
    if not room:
        raise ValueError(f"Room not found: {room_id}")

    room.optimization_status = "COMPLETED"

    original_json_data = {
        "source": "ios_upload",
        "room_data_json": original_s3_url,
    }
    original = (
        db.query(Version)
        .filter(
            Version.room_id == room_id,
            Version.version_type == "ORIGINAL",
            Version.version_no == 0,
        )
        .first()
    )
    if original:
        original.s3_json_url = original_s3_url
        original.converted_glb_url = glb_s3_url
        original.json_data = original.json_data or original_json_data
    else:
        original = Version(
            room_id=room_id,
            parent_version_id=None,
            version_type="ORIGINAL",
            version_no=0,
            s3_json_url=original_s3_url,
            converted_glb_url=glb_s3_url,
            json_data=original_json_data,
        )
        db.add(original)
        db.flush()

    optimized_json_data = {
        "source": "local_pipeline",
        "normalized_json": normalized_s3_url,
        "problem_json": problem_s3_url,
        "optimized_json": optimized_s3_url,
        "roomplan_optimized_json": roomplan_optimized_s3_url,
        "unity_roomplan_optimized_json": unity_roomplan_optimized_s3_url,
    }
    optimized = (
        db.query(Version)
        .filter(
            Version.room_id == room_id,
            Version.version_type == "OPTIMIZED",
            Version.version_no == 1,
        )
        .first()
    )
    if optimized:
        optimized.parent_version_id = original.id
        optimized.s3_json_url = roomplan_optimized_s3_url
        optimized.converted_glb_url = glb_s3_url
        optimized.json_data = optimized_json_data
    else:
        optimized = Version(
            room_id=room_id,
            parent_version_id=original.id,
            version_type="OPTIMIZED",
            version_no=1,
            s3_json_url=roomplan_optimized_s3_url,
            converted_glb_url=glb_s3_url,
            json_data=optimized_json_data,
        )
        db.add(optimized)
        db.flush()

    db.commit()
    return {
        "status": "ok",
        "room_id": room_id,
        "original_version_id": original.id,
        "optimized_version_id": optimized.id,
        "original_json": raw_json_key,
        "normalized_json": normalized_json_key,
        "problem_json": problem_json_key,
        "optimized_json": optimized_json_key,
        "roomplan_optimized_json": roomplan_optimized_key,
        "unity_roomplan_optimized_json": unity_roomplan_optimized_key,
        "glb": glb_key,
    }


def _run_glb_conversion_sync(event: dict[str, Any]) -> dict[str, Any]:
    """Build the room-shell GLB. Runs at save time; never touches optimization_status."""
    bucket = event.get("bucket") or settings.s3_bucket_name
    if not bucket:
        raise ValueError("S3 bucket is missing. Set S3_BUCKET_NAME.")

    inputs = event["inputs"]
    outputs = event["outputs"]

    _run_python_script(
        "workers/converter/run_glb_conversion.py",
        {
            "S3_BUCKET_NAME": bucket,
            "ROOM_DATA_S3_KEY": inputs["room_data_json"],
            "INPUT_S3_KEY": inputs.get("room_usdz") or "",
            "OUTPUT_S3_KEY": outputs["glb"],
        },
    )
    return event


async def run_glb_conversion_background(event: dict[str, Any]) -> None:
    """Fire-and-forget GLB conversion for POST /complete. Failures are logged, not raised."""
    try:
        await asyncio.to_thread(_run_glb_conversion_sync, event)
    except Exception:
        logger.exception("GLB 변환 백그라운드 작업 실패 room_id=%s", event.get("room_id"))


def _run_local_optimize_pipeline_sync(event: dict[str, Any]) -> dict[str, Any]:
    bucket = event.get("bucket") or settings.s3_bucket_name
    if not bucket:
        raise ValueError("S3 bucket is missing. Set S3_BUCKET_NAME.")

    inputs = event["inputs"]
    outputs = event["outputs"]
    room_id = event["room_id"]

    try:
        _run_python_script(
            "workers/optimizer/run_s3_optimizer.py",
            {
                "S3_BUCKET_NAME": bucket,
                "INPUT_S3_KEY": inputs["room_data_json"],
                "NORMALIZED_OUT_S3_KEY": outputs["normalized_json"],
                "PROBLEM_OUT_S3_KEY": outputs["problem_json"],
                "OPTIMIZED_OUT_S3_KEY": outputs["optimized_json"],
                "ROOMPLAN_OPTIMIZED_OUT_S3_KEY": outputs["roomplan_optimized_json"],
                "UNITY_ROOMPLAN_OPTIMIZED_OUT_S3_KEY": outputs["unity_roomplan_optimized_json"],
                "UPLOAD_DEBUG_ARTIFACTS": "true",
            },
        )

        with SessionLocal() as db:
            event["update_db"] = _upsert_pipeline_versions(db, event)
        event["local_pipeline"] = {"status": "ok"}
        return event
    except Exception:
        with SessionLocal() as db:
            _mark_optimization_status(db, room_id, "FAILED")
        raise


async def run_local_optimize_pipeline(event: dict[str, Any]) -> dict[str, Any]:
    """Awaited from a FastAPI BackgroundTask by POST /optimize, after the response is sent."""
    try:
        return await asyncio.to_thread(_run_local_optimize_pipeline_sync, event)
    except Exception:
        logger.exception("최적화 파이프라인 실패 room_id=%s", event.get("room_id"))
        raise
