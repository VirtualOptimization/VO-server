from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from server.api.v1.router import router as v1_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 실행
    print("VO-server starting up...")
    yield
    # 서버 종료 시 실행
    print("VO-server shutting down...")


app = FastAPI(
    title="VO-server",
    description="iOS LiDAR 기반 가구 배치 최적화 백엔드 서버",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(v1_router, prefix="/api")


@app.get("/health")
async def health_check():
    return {"status": "ok"}
