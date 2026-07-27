"""Local furniture asset conversion helpers."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from server.core.config import settings


REPO_ROOT = Path(__file__).resolve().parents[2]


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
    subprocess.run(
        [sys.executable, "workers/converter/run_glb_conversion.py"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        timeout=settings.local_pipeline_timeout_seconds,
    )


async def convert_furniture_usdc_to_glb(source_key: str, output_key: str) -> None:
    """Convert an uploaded furniture USDC/USD file in S3 into a GLB file."""
    if not settings.s3_bucket_name:
        raise ValueError("S3_BUCKET_NAME is not configured")

    await asyncio.to_thread(_run_furniture_conversion_sync, source_key, output_key)


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
    subprocess.run(
        [sys.executable, "workers/converter/run_usdz_conversion.py"],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        timeout=settings.local_pipeline_timeout_seconds,
    )


async def convert_furniture_glb_to_usdz(source_key: str, output_key: str) -> None:
    """Convert a furniture GLB file in S3 into a USDZ file for iOS."""
    if not settings.s3_bucket_name:
        raise ValueError("S3_BUCKET_NAME is not configured")

    await asyncio.to_thread(_run_furniture_usdz_conversion_sync, source_key, output_key)
