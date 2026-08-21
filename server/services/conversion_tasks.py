"""Helpers for queuing conversion work outside API request handling."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from shared.models.conversion_task import ConversionTask

ACTIVE_STATUSES = {"PENDING", "RUNNING"}

TASK_FURNITURE_USDC_TO_GLB = "FURNITURE_USDC_TO_GLB"
TASK_FURNITURE_GLB_TO_USDZ = "FURNITURE_GLB_TO_USDZ"
TASK_MATERIAL_ASSET = "MATERIAL_ASSET"


def enqueue_conversion_task(
    db: Session,
    *,
    task_type: str,
    source_key: str | None = None,
    texture_key: str | None = None,
    output_glb_key: str | None = None,
    output_usdz_key: str | None = None,
    furniture_model_id: int | None = None,
    version_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> ConversionTask:
    """Create a conversion task unless the same active task already exists."""
    existing = (
        db.query(ConversionTask)
        .filter(
            ConversionTask.task_type == task_type,
            ConversionTask.source_key == source_key,
            ConversionTask.texture_key == texture_key,
            ConversionTask.output_glb_key == output_glb_key,
            ConversionTask.output_usdz_key == output_usdz_key,
            ConversionTask.furniture_model_id == furniture_model_id,
            ConversionTask.version_id == version_id,
            ConversionTask.status.in_(ACTIVE_STATUSES),
        )
        .order_by(ConversionTask.id.desc())
        .first()
    )
    if existing is not None:
        return existing

    task = ConversionTask(
        task_type=task_type,
        status="PENDING",
        source_key=source_key,
        texture_key=texture_key,
        output_glb_key=output_glb_key,
        output_usdz_key=output_usdz_key,
        furniture_model_id=furniture_model_id,
        version_id=version_id,
        payload=payload,
    )
    db.add(task)
    return task
