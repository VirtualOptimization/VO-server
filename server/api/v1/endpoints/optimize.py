"""공간 최적화 — 최적화 레이아웃 저장"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from shared.db import SessionLocal
from shared.models.furniture_item import FurnitureItem
from shared.models.room import Room
from shared.models.version import Version

logger = logging.getLogger(__name__)
router = APIRouter()


# ── 내부 헬퍼 ────────────────────────────────────────────────────────────────

def _save_optimized_to_db(
    room_id: int,
    optimized_data: dict[str, Any],
    optimized_s3_url: str | None = None,
    converted_glb_url: str | None = None,
) -> None:
    db = SessionLocal()
    try:
        existing = db.query(Version).filter(
            Version.room_id == room_id,
            Version.version_type == "OPTIMIZED",
            Version.version_no == 1,
        ).first()

        if existing:
            db.query(FurnitureItem).filter(FurnitureItem.version_id == existing.id).delete()
            version = existing
            version.json_data = optimized_data
            if optimized_s3_url:
                version.s3_json_url = optimized_s3_url
            if converted_glb_url:
                version.converted_glb_url = converted_glb_url
        else:
            version = Version(
                room_id=room_id, version_no=1, version_type="OPTIMIZED",
                json_data=optimized_data, s3_json_url=optimized_s3_url,
                converted_glb_url=converted_glb_url,
            )
            db.add(version)
            db.flush()

        for elem in optimized_data.get("elements", []):
            t = elem.get("transform_dict", {})
            db.add(FurnitureItem(
                version_id=version.id,
                model_id=elem.get("model_id"),
                item_key=elem.get("item_key", f"item_{uuid.uuid4().hex[:8]}"),
                pos_x=t.get("pos_x", 0.0), pos_y=t.get("pos_y", 0.0), pos_z=t.get("pos_z", 0.0),
                rot_x=t.get("rot_x", 0.0), rot_y=t.get("rot_y", 0.0), rot_z=t.get("rot_z", 0.0),
                scale_x=t.get("scale_x", 1.0), scale_y=t.get("scale_y", 1.0), scale_z=t.get("scale_z", 1.0),
                footprint_polygon=elem.get("footprint_polygon"),
            ))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to persist optimized layout: {e}")
        raise
    finally:
        db.close()


# ── POST /rooms/{confirm_code}/optimize ───────────────────────────────────────

@router.post("/{confirm_code}/optimize")
async def save_optimized_layout(
    confirm_code: str,
    optimized_result: dict[str, Any],
    optimized_s3_url: str | None = None,
    converted_glb_url: str | None = None,
):
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.confirm_code == confirm_code).first()
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")
        await asyncio.to_thread(_save_optimized_to_db, room.id, optimized_result, optimized_s3_url, converted_glb_url)
        room.status = "COMPLETED"
        db.commit()
        return {"message": "Optimized layout saved"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to save optimized layout: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
