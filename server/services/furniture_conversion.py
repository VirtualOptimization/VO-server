"""Local furniture asset conversion helpers."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

from server.core.config import settings


REPO_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)


class FurnitureConversionError(RuntimeError):
    """Converter failure whose detailed diagnostics remain in server logs."""


def _run_converter(command: list[str], *, env: dict[str, str]) -> None:
    """Run one converter process and preserve stdout/stderr on failure."""
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            check=True,
            timeout=settings.local_pipeline_timeout_seconds,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        logger.error(
            "furniture_converter_failed command=%s returncode=%s stdout=%s stderr=%s",
            command,
            exc.returncode,
            (exc.stdout or "").strip()[-4000:],
            (exc.stderr or "").strip()[-4000:],
        )
        raise FurnitureConversionError("가구 변환기가 실패했습니다. 서버 로그를 확인해 주세요.") from exc
    except subprocess.TimeoutExpired as exc:
        logger.error("furniture_converter_timed_out command=%s", command)
        raise FurnitureConversionError("가구 변환 시간이 초과되었습니다.") from exc
    else:
        logger.info(
            "furniture_converter_succeeded command=%s stdout=%s",
            command,
            (completed.stdout or "").strip()[-1000:],
        )


def _run_furniture_conversion_sync(source_key: str, output_key: str) -> None:
    env = os.environ.copy()
    env.update(
        {
            "S3_BUCKET_NAME": settings.s3_bucket_name,
            "INPUT_S3_KEY": source_key,
            "ROOM_DATA_S3_KEY": "",
            "OUTPUT_S3_KEY": output_key,
            "PYTHONPATH": str(REPO_ROOT),
        }
    )
    _run_converter(
        [sys.executable, "workers/converter/run_glb_conversion.py"],
        env=env,
    )


async def convert_furniture_usdc_to_glb(source_key: str, output_key: str) -> None:
    """Convert an uploaded furniture USDC/USD file in S3 into a GLB file."""
    if not settings.s3_bucket_name:
        raise ValueError("S3_BUCKET_NAME is not configured")

    await asyncio.to_thread(_run_furniture_conversion_sync, source_key, output_key)


def _run_furniture_texture_apply_sync(source_key: str, texture_key: str, output_key: str) -> None:
    env = os.environ.copy()
    env.update(
        {
            "S3_BUCKET_NAME": settings.s3_bucket_name,
            "INPUT_S3_KEY": source_key,
            "TEXTURE_S3_KEY": texture_key,
            "OUTPUT_S3_KEY": output_key,
            "PYTHONPATH": str(REPO_ROOT),
        }
    )
    _run_converter(
        [sys.executable, "workers/converter/apply_glb_texture.py"],
        env=env,
    )


async def apply_furniture_texture_to_glb(source_key: str, texture_key: str, output_key: str) -> None:
    """Apply a texture image from S3 to a base furniture GLB and upload a new GLB."""
    if not settings.s3_bucket_name:
        raise ValueError("S3_BUCKET_NAME is not configured")

    await asyncio.to_thread(_run_furniture_texture_apply_sync, source_key, texture_key, output_key)


def _run_furniture_usdz_conversion_sync(source_key: str, output_key: str) -> None:
    env = os.environ.copy()
    env.update(
        {
            "S3_BUCKET_NAME": settings.s3_bucket_name,
            "INPUT_S3_KEY": source_key,
            "OUTPUT_S3_KEY": output_key,
            "PYTHONPATH": str(REPO_ROOT),
        }
    )
    _run_converter(
        [sys.executable, "workers/converter/run_usdz_conversion.py"],
        env=env,
    )


async def convert_furniture_glb_to_usdz(source_key: str, output_key: str) -> None:
    """Convert a furniture GLB file in S3 into a USDZ file for iOS."""
    if not settings.s3_bucket_name:
        raise ValueError("S3_BUCKET_NAME is not configured")

    await asyncio.to_thread(_run_furniture_usdz_conversion_sync, source_key, output_key)
