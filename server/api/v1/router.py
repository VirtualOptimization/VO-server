from fastapi import APIRouter

from server.api.v1.endpoints import scans, rooms

router = APIRouter()

router.include_router(scans.router, prefix="/scans", tags=["scans"])
router.include_router(rooms.router, prefix="/rooms", tags=["rooms"])
