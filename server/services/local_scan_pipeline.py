"""Run the scan processing pipeline inside the FastAPI host.

This replaces the Step Functions + ECS + Lambda path for low-cost deployments.
"""

from __future__ import annotations

import asyncio
import json
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


def _mark_room_status(db: Session, room_id: int, status: str) -> None:
    room = db.get(Room, room_id)
    if not room:
        raise ValueError(f"Room not found: {room_id}")
    room.status = status
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

    room.status = "COMPLETED"

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


def _run_local_scan_pipeline_sync(event: dict[str, Any]) -> dict[str, Any]:
    bucket = event.get("bucket") or settings.s3_bucket_name
    if not bucket:
        raise ValueError("S3 bucket is missing. Set S3_BUCKET_NAME.")

    inputs = event["inputs"]
    outputs = event["outputs"]
    room_id = event["room_id"]

    with SessionLocal() as db:
        _mark_room_status(db, room_id, "PROCESSING")

    try:
        _run_python_script(
            "workers/converter/run_glb_conversion.py",
            {
                "S3_BUCKET_NAME": bucket,
                "ROOM_DATA_S3_KEY": inputs["room_data_json"],
                "INPUT_S3_KEY": inputs.get("room_usdz") or "",
                "OUTPUT_S3_KEY": outputs["glb"],
            },
        )
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
            _mark_room_status(db, room_id, "FAILED")
        raise


async def run_local_scan_pipeline(event: dict[str, Any]) -> dict[str, Any]:
    return await asyncio.to_thread(_run_local_scan_pipeline_sync, event)
