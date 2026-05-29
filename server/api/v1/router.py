from fastapi import APIRouter

from server.api.v1.endpoints import scan, rooms, optimize, rooms_edit

router = APIRouter()

router.include_router(scan.router,       prefix="/rooms", tags=["공간 스캔"])
router.include_router(rooms.router,      prefix="/rooms", tags=["공간 조회"])
router.include_router(optimize.router,   prefix="/rooms", tags=["공간 최적화"])
router.include_router(rooms_edit.router, prefix="/rooms", tags=["공간 편집"])
