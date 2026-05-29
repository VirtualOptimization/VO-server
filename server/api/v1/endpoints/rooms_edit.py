"""공간 편집 — USER_EDITED 버전 삭제 (origin·optimized는 삭제 불가)"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from shared.db import SessionLocal
from shared.models.furniture_item import FurnitureItem
from shared.models.room import Room
from shared.models.version import Version

logger = logging.getLogger(__name__)
router = APIRouter()


# ── DELETE /rooms/{confirm_code}/versions/{version_id} ────────────────────────
# origin(ORIGINAL) / optimized(OPTIMIZED) 는 삭제 불가
# USER_EDITED 버전만 삭제 가능

@router.delete("/{confirm_code}/versions/{version_id}", status_code=204)
def delete_user_version(confirm_code: str, version_id: int):
    db = SessionLocal()
    try:
        room = db.query(Room).filter(Room.confirm_code == confirm_code).first()
        if not room:
            raise HTTPException(status_code=404, detail="확인 코드를 찾을 수 없습니다.")

        version = db.query(Version).filter(
            Version.id == version_id,
            Version.room_id == room.id,
        ).first()

        if not version:
            raise HTTPException(status_code=404, detail="버전을 찾을 수 없습니다.")

        if version.version_type != "USER_EDITED":
            raise HTTPException(
                status_code=403,
                detail="origin 및 optimized 버전은 삭제할 수 없습니다. USER_EDITED 버전만 삭제 가능합니다.",
            )

        db.query(FurnitureItem).filter(FurnitureItem.version_id == version.id).delete()
        db.delete(version)
        db.commit()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete version {version_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        db.close()
