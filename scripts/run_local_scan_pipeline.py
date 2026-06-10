"""Run a local end-to-end scan pipeline against Docker PostgreSQL.

Example:
    .venv/bin/python scripts/run_local_scan_pipeline.py \
        "/path/to/ScanExport_1776303796"
"""

from __future__ import annotations

import argparse
import json
import os
import random
import string
import sys
from pathlib import Path
from typing import Any

import boto3

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_env_file(env_path: Path) -> None:
    """Load simple KEY=VALUE pairs from a local .env file if present."""
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(PROJECT_ROOT / ".env")
database_url = os.getenv("DATABASE_URL", "").strip()
if database_url.startswith("postgresql://") and "+psycopg" not in database_url:
    os.environ["DATABASE_URL"] = "postgresql+psycopg://vo_user:vo_password@localhost:5432/vo_db"

from server.services.transform import (  # noqa: E402
    build_layout_problem,
    convert_roomplan_to_optimizer_payload,
    export_optimized_layout_to_roomplan,
)
from shared.db import SessionLocal  # noqa: E402
from shared.models import FurnitureItem, FurnitureModel, Room, Version  # noqa: E402
from workers.optimizer.layout import CanonicalLayoutOptimizer  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scan_dir", help="Path to a ScanExport_* folder")
    parser.add_argument(
        "--output-dir",
        default="workers/optimizer/examples/generated/local_pipeline",
        help="Directory to write normalized/problem/optimized artifacts",
    )
    parser.add_argument("--global-maxiter", type=int, default=40, help="DE max iterations")
    parser.add_argument("--global-popsize", type=int, default=10, help="DE population size")
    parser.add_argument("--local-maxiter", type=int, default=200, help="SLSQP max iterations")
    parser.add_argument(
        "--skip-s3-upload",
        action="store_true",
        help="Skip uploading raw assets and generated outputs to S3.",
    )
    parser.add_argument(
        "--s3-prefix",
        default="scans/local-tests",
        help="Base S3 prefix used when uploading raw assets and outputs.",
    )
    return parser.parse_args()


def ensure_scan_set(scan_dir: Path) -> tuple[Path, Path | None, Path | None, list[Path]]:
    room_data = scan_dir / "room_data.json"
    if not room_data.exists():
        raise FileNotFoundError(f"room_data.json not found in {scan_dir}")

    room_usdz = scan_dir / "Room.usdz"
    room_empty_usdz = scan_dir / "Room_empty.usdz"
    model_paths = sorted((scan_dir / "Models").glob("*.usdc")) if (scan_dir / "Models").exists() else []
    return room_data, room_usdz if room_usdz.exists() else None, room_empty_usdz if room_empty_usdz.exists() else None, model_paths


def generate_confirm_code(existing_codes: set[str]) -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "".join(random.choices(alphabet, k=6))
        if code not in existing_codes:
            return code


def build_artifact_paths(output_dir: Path, scan_name: str) -> dict[str, Path]:
    base = output_dir / scan_name
    return {
        "raw": base / "room_data.raw.json",
        "normalized": base / "room_data.normalized.json",
        "problem": base / "room_data.problem.json",
        "optimized": base / "room_data.optimized.json",
        "roomplan_optimized": base / "room_data.roomplan_optimized.json",
        "plot": base / "room_data.optimized.svg",
    }


def build_s3_client():
    return boto3.client(
        "s3",
        region_name=os.getenv("AWS_REGION", "ap-northeast-2"),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    )


def build_s3_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "application/json"
    if suffix == ".svg":
        return "image/svg+xml"
    if suffix == ".usdz":
        return "model/vnd.usdz+zip"
    if suffix == ".usdc":
        return "application/octet-stream"
    return "application/octet-stream"


def upload_file_to_s3(s3_client: Any, bucket: str, key: str, local_path: Path) -> str:
    s3_client.upload_file(
        str(local_path),
        bucket,
        key,
        ExtraArgs={"ContentType": guess_content_type(local_path)},
    )
    return build_s3_uri(bucket, key)


def upload_scan_set_to_s3(
    scan_name: str,
    scan_dir: Path,
    room_data_path: Path,
    room_usdz_path: Path | None,
    room_empty_usdz_path: Path | None,
    model_paths: list[Path],
    *,
    bucket: str,
    s3_prefix: str,
) -> dict[str, Any]:
    s3_client = build_s3_client()
    base_prefix = f"{s3_prefix.rstrip('/')}/{scan_name}/raw"

    uploaded: dict[str, Any] = {
        "scan_dir": str(scan_dir),
        "room_data_json": upload_file_to_s3(
            s3_client, bucket, f"{base_prefix}/room_data.json", room_data_path
        ),
        "room_usdz": None,
        "room_empty_usdz": None,
        "model_files": [],
    }

    if room_usdz_path:
        uploaded["room_usdz"] = upload_file_to_s3(
            s3_client, bucket, f"{base_prefix}/Room.usdz", room_usdz_path
        )
    if room_empty_usdz_path:
        uploaded["room_empty_usdz"] = upload_file_to_s3(
            s3_client, bucket, f"{base_prefix}/Room_empty.usdz", room_empty_usdz_path
        )

    for model_path in model_paths:
        uploaded["model_files"].append(
            {
                "local_path": str(model_path),
                "s3_uri": upload_file_to_s3(
                    s3_client,
                    bucket,
                    f"{base_prefix}/Models/{model_path.name}",
                    model_path,
                ),
            }
        )

    return uploaded


def upload_artifacts_to_s3(
    scan_name: str,
    artifact_paths: dict[str, Path],
    *,
    bucket: str,
    s3_prefix: str,
) -> dict[str, str]:
    s3_client = build_s3_client()
    base_prefix = f"{s3_prefix.rstrip('/')}/{scan_name}/generated"
    uploaded: dict[str, str] = {}

    for key, path in artifact_paths.items():
        s3_key = f"{base_prefix}/{path.name}"
        uploaded[key] = upload_file_to_s3(s3_client, bucket, s3_key, path)

    return uploaded


def save_artifacts(
    paths: dict[str, Path],
    raw_payload: dict,
    normalized: dict,
    problem: dict,
    optimized: dict,
    exported_roomplan: dict,
    optimizer: CanonicalLayoutOptimizer,
) -> None:
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)

    paths["raw"].write_text(json.dumps(raw_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["normalized"].write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["problem"].write_text(json.dumps(problem, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["optimized"].write_text(json.dumps(optimized, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["roomplan_optimized"].write_text(
        json.dumps(exported_roomplan, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    optimizer.save_comparison_svg(optimized, paths["plot"])


def build_model_lookup(db) -> dict[str, int]:
    rows = db.query(FurnitureModel).all()
    lookup: dict[str, int] = {}
    for model in rows:
        if model.name:
            lookup[str(model.name)] = int(model.id)
            if str(model.name).endswith(".rooms"):
                lookup[str(model.name).removesuffix(".rooms")] = int(model.id)
        lookup[str(model.model_key)] = int(model.id)
    return lookup


def add_furniture_items(db, version_id: int, items: list[dict], model_lookup: dict[str, int], *, optimized: bool) -> None:
    for item in items:
        pos_key = "optimized_pos" if optimized and "optimized_pos" in item else "pos"
        rot_key = "optimized_rotation_y_deg" if optimized and "optimized_rotation_y_deg" in item else "rotation_y_deg"

        model_id = None
        model_key = item.get("model_key")
        if model_key:
            model_id = model_lookup.get(model_key)

        source_model_file = item.get("source_model_file")
        if model_id is None and source_model_file:
            model_name = Path(source_model_file).stem
            model_id = model_lookup.get(model_name)

        pos = item[pos_key]
        db.add(
            FurnitureItem(
                version_id=version_id,
                model_id=model_id,
                item_key=item["id"],
                pos_x=float(pos[0]),
                pos_y=float(pos[1]),
                pos_z=float(pos[2]),
                rot_x=0.0,
                rot_y=float(item.get(rot_key, 0.0)),
                rot_z=0.0,
                scale_x=1.0,
                scale_y=1.0,
                scale_z=1.0,
                footprint_polygon=None,
            )
        )


def main() -> None:
    load_env_file(PROJECT_ROOT / ".env")
    args = parse_args()

    scan_dir = Path(args.scan_dir).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    room_data_path, room_usdz_path, room_empty_usdz_path, model_paths = ensure_scan_set(scan_dir)
    bucket_name = os.getenv("S3_BUCKET_NAME", "").strip()

    raw_payload = json.loads(room_data_path.read_text(encoding="utf-8"))
    normalized = convert_roomplan_to_optimizer_payload(raw_payload)
    problem = build_layout_problem(normalized)
    optimizer = CanonicalLayoutOptimizer(problem)
    optimized = optimizer.optimize(
        global_maxiter=args.global_maxiter,
        global_popsize=args.global_popsize,
        local_maxiter=args.local_maxiter,
    )
    exported_roomplan = export_optimized_layout_to_roomplan(raw_payload, optimized)

    artifact_paths = build_artifact_paths(output_dir, scan_dir.name)
    save_artifacts(
        artifact_paths,
        raw_payload,
        normalized,
        problem,
        optimized,
        exported_roomplan,
        optimizer,
    )

    raw_asset_manifest: dict[str, Any]
    uploaded_artifacts: dict[str, str] | None = None
    if args.skip_s3_upload:
        raw_asset_manifest = {
            "scan_dir": str(scan_dir),
            "room_data_json": str(room_data_path),
            "room_usdz": str(room_usdz_path) if room_usdz_path else None,
            "room_empty_usdz": str(room_empty_usdz_path) if room_empty_usdz_path else None,
            "model_files": [str(path) for path in model_paths],
        }
    else:
        if not bucket_name:
            raise ValueError("S3_BUCKET_NAME must be configured to upload scan assets.")
        raw_asset_manifest = upload_scan_set_to_s3(
            scan_name=scan_dir.name,
            scan_dir=scan_dir,
            room_data_path=room_data_path,
            room_usdz_path=room_usdz_path,
            room_empty_usdz_path=room_empty_usdz_path,
            model_paths=model_paths,
            bucket=bucket_name,
            s3_prefix=args.s3_prefix,
        )
        uploaded_artifacts = upload_artifacts_to_s3(
            scan_name=scan_dir.name,
            artifact_paths=artifact_paths,
            bucket=bucket_name,
            s3_prefix=args.s3_prefix,
        )

    with SessionLocal() as db:
        existing_codes = {code for (code,) in db.query(Room.confirm_code).all()}
        confirm_code = generate_confirm_code(existing_codes)

        room = Room(
            confirm_code=confirm_code,
            status="COMPLETED",
            room_shell_usdc_url=raw_asset_manifest.get("room_usdz"),
        )
        db.add(room)
        db.flush()

        original_version = Version(
            room_id=room.id,
            version_type="ORIGINAL",
            version_no=0,
            s3_json_url=(uploaded_artifacts or {}).get("raw", str(artifact_paths["raw"])),
            json_data={
                "raw_assets": raw_asset_manifest,
                "raw_payload": raw_payload,
                "normalized_scan": normalized,
            },
        )
        db.add(original_version)
        db.flush()

        model_lookup = build_model_lookup(db)
        add_furniture_items(
            db,
            original_version.id,
            normalized.get("scanned_objects", []),
            model_lookup,
            optimized=False,
        )

        optimized_version = Version(
            room_id=room.id,
            parent_version_id=original_version.id,
            version_type="OPTIMIZED",
            version_no=1,
            s3_json_url=(uploaded_artifacts or {}).get("optimized", str(artifact_paths["optimized"])),
            json_data={
                "problem": problem,
                "optimized": optimized,
                "roomplan_optimized": uploaded_artifacts.get("roomplan_optimized") if uploaded_artifacts else None,
            },
        )
        db.add(optimized_version)
        db.flush()

        add_furniture_items(
            db,
            optimized_version.id,
            optimized.get("movable_items", []),
            model_lookup,
            optimized=True,
        )

        db.commit()

        print(f"Room created: id={room.id}, confirm_code={confirm_code}")
        print(f"Original version saved: id={original_version.id}")
        print(f"Optimized version saved: id={optimized_version.id}")

    print(f"Raw JSON saved to: {artifact_paths['raw']}")
    print(f"Normalized JSON saved to: {artifact_paths['normalized']}")
    print(f"Problem JSON saved to: {artifact_paths['problem']}")
    print(f"Optimized JSON saved to: {artifact_paths['optimized']}")
    print(f"RoomPlan-like optimized JSON saved to: {artifact_paths['roomplan_optimized']}")
    print(f"Comparison plot saved to: {artifact_paths['plot']}")
    if uploaded_artifacts:
        print(f"Raw assets uploaded under: {args.s3_prefix.rstrip('/')}/{scan_dir.name}/raw")
        print(f"Generated outputs uploaded under: {args.s3_prefix.rstrip('/')}/{scan_dir.name}/generated")


if __name__ == "__main__":
    main()
