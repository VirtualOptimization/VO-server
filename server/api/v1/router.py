from fastapi import APIRouter

from server.api.v1.endpoints import scans

router = APIRouter()

router.include_router(scans.router, prefix="/scans", tags=["scans"])
