from fastapi import APIRouter

from server.api.v1.endpoints import auth, furniture, materials, scan, rooms, rooms_edit

router = APIRouter()

router.include_router(scan.router,       prefix="/rooms", tags=["공간 스캔"])
router.include_router(rooms.router,      prefix="/rooms", tags=["공간 조회"])
router.include_router(rooms_edit.router, prefix="/rooms", tags=["공간 편집"])
router.include_router(auth.router, prefix="/auth", tags=["인증"])
router.include_router(furniture.router, prefix="/furniture", tags=["가구 보관함"])
router.include_router(materials.router, prefix="/materials", tags=["AI 텍스처"])
