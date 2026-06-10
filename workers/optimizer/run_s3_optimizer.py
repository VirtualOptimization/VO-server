"""Run the RoomPlan optimizer against S3 input and upload the Unity-ready result."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import boto3

from server.services.transform import (
    build_layout_problem,
    convert_roomplan_to_optimizer_payload,
    export_optimized_layout_to_roomplan,
    normalize_roomplan_for_unity,
)
from workers.optimizer.layout import CanonicalLayoutOptimizer


def require_env(name: str) -> str:
    value = os.getenv(name, "")
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def optional_env(name: str) -> str | None:
    value = os.getenv(name, "")
    return value or None


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def upload_json(s3_client, bucket: str, key: str, path: Path) -> None:
    print(f"Uploading {path} to s3://{bucket}/{key}...")
    s3_client.upload_file(
        str(path),
        bucket,
        key,
        ExtraArgs={"ContentType": "application/json"},
    )


def main() -> int:
    bucket = require_env("S3_BUCKET_NAME")
    input_key = require_env("INPUT_S3_KEY")
    roomplan_optimized_key = require_env("ROOMPLAN_OPTIMIZED_OUT_S3_KEY")
    upload_debug_artifacts = env_bool("UPLOAD_DEBUG_ARTIFACTS")

    normalized_key = optional_env("NORMALIZED_OUT_S3_KEY")
    problem_key = optional_env("PROBLEM_OUT_S3_KEY")
    optimized_key = optional_env("OPTIMIZED_OUT_S3_KEY")

    global_maxiter = int(os.getenv("OPTIMIZER_GLOBAL_MAXITER", "40"))
    global_popsize = int(os.getenv("OPTIMIZER_GLOBAL_POPSIZE", "10"))
    local_maxiter = int(os.getenv("OPTIMIZER_LOCAL_MAXITER", "200"))

    s3 = boto3.client("s3")

    with tempfile.TemporaryDirectory() as tmpdir:
        base = Path(tmpdir)
        input_path = base / "room_data.json"
        normalized_path = base / "room_data.normalized.json"
        problem_path = base / "room_data.problem.json"
        optimized_path = base / "room_data.optimized.json"
        roomplan_optimized_path = base / "room_data.roomplan_optimized.json"

        print(f"Downloading s3://{bucket}/{input_key} to {input_path}...")
        s3.download_file(bucket, input_key, str(input_path))

        raw_payload = json.loads(input_path.read_text(encoding="utf-8"))
        normalized = convert_roomplan_to_optimizer_payload(raw_payload)
        problem = build_layout_problem(normalized)

        optimizer = CanonicalLayoutOptimizer(problem)
        optimized = optimizer.optimize(
            global_maxiter=global_maxiter,
            global_popsize=global_popsize,
            local_maxiter=local_maxiter,
        )
        roomplan_optimized = normalize_roomplan_for_unity(
            export_optimized_layout_to_roomplan(raw_payload, optimized)
        )

        write_json(roomplan_optimized_path, roomplan_optimized)

        if upload_debug_artifacts:
            if normalized_key:
                write_json(normalized_path, normalized)
                upload_json(s3, bucket, normalized_key, normalized_path)
            if problem_key:
                write_json(problem_path, problem)
                upload_json(s3, bucket, problem_key, problem_path)
            if optimized_key:
                write_json(optimized_path, optimized)
                upload_json(s3, bucket, optimized_key, optimized_path)

        upload_json(s3, bucket, roomplan_optimized_key, roomplan_optimized_path)

    result = {
        "bucket": bucket,
        "input_s3_key": input_key,
        "outputs": {
            "roomplan_optimized_json": roomplan_optimized_key,
        },
        "upload_debug_artifacts": upload_debug_artifacts,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
