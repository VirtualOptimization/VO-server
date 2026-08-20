"""Canonicalize model_key values inside stored RoomPlan Unity JSON files.

This migrates old Unity JSON values such as:
    chair:Swivel_wBack_starLegs_wArms

to the current furniture_models.model_key format:
    Chair/Swivel_wBack_starLegs_wArms/Swivel_wBack_starLegs_wArms.rooms.usdc

Examples:
    .venv/bin/python scripts/canonicalize_room_model_keys.py --dry-run
    .venv/bin/python scripts/canonicalize_room_model_keys.py --prefix user01/scans --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import boto3

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(PROJECT_ROOT / ".env")

from shared.db import SessionLocal  # noqa: E402
from shared.models import FurnitureModel  # noqa: E402


UNITY_JSON_SUFFIXES = (
    "origin/room_data.unity.json",
    "optimized/room_data.roomplan_optimized.unity.json",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replace legacy RoomPlan model_key values in S3 Unity JSON files."
    )
    parser.add_argument(
        "--bucket",
        default=os.getenv("S3_BUCKET_NAME"),
        help="S3 bucket containing room JSON files.",
    )
    parser.add_argument(
        "--prefix",
        default="",
        help="Only scan S3 keys under this prefix. Empty means whole bucket.",
    )
    parser.add_argument(
        "--region",
        default=os.getenv("AWS_REGION", "ap-southeast-2"),
        help="AWS region.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changed JSON back to S3. Without this, the script is dry-run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing to S3. This is the default.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum number of Unity JSON files to scan. 0 means no limit.",
    )
    args = parser.parse_args()

    if not args.bucket:
        parser.error("--bucket or S3_BUCKET_NAME is required")

    return args


def _category_aliases(model: FurnitureModel) -> set[str]:
    aliases = {
        alias
        for alias in {
            (model.furniture_type or "").strip().lower(),
            (
                model.model_key.split("/", 1)[0].strip().lower()
                if "/" in model.model_key
                else ""
            ),
        }
        if alias
    }

    if "storage" in aliases:
        aliases.add("shelf")
    if "shelf" in aliases:
        aliases.add("storage")

    return aliases


def _model_key_aliases(model: FurnitureModel) -> set[str]:
    model_key = model.model_key
    parts = model_key.split("/")
    filename = parts[-1]
    stem = filename.removesuffix(".rooms.usdc").removesuffix(".usdc").removesuffix(".glb")
    variant = parts[-2] if len(parts) >= 2 else stem

    aliases = {model_key, filename, stem}
    for category in _category_aliases(model):
        aliases.add(f"{category}:{variant}")
        aliases.add(f"{category}:{stem}")

    return {alias for alias in aliases if alias}


def build_alias_map() -> dict[str, str]:
    """Return legacy_or_alias_key -> canonical model_key."""
    with SessionLocal() as db:
        models = (
            db.query(FurnitureModel)
            .filter(FurnitureModel.status != "DELETED")
            .all()
        )

    alias_map: dict[str, str] = {}
    for model in models:
        for alias in _model_key_aliases(model):
            alias_map.setdefault(alias, model.model_key)
    return alias_map


def iter_unity_json_keys(s3_client, *, bucket: str, prefix: str, limit: int) -> list[str]:
    normalized_prefix = prefix.strip("/")
    request_prefix = f"{normalized_prefix}/" if normalized_prefix else ""
    paginator = s3_client.get_paginator("list_objects_v2")
    keys: list[str] = []

    for page in paginator.paginate(Bucket=bucket, Prefix=request_prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            if key.endswith(UNITY_JSON_SUFFIXES):
                keys.append(key)
                if limit and len(keys) >= limit:
                    return keys

    return keys


def canonicalize_objects(data: dict[str, Any], alias_map: dict[str, str]) -> int:
    raw_objects = data.get("objects")
    if not isinstance(raw_objects, list):
        return 0

    changed = 0
    for obj in raw_objects:
        if not isinstance(obj, dict):
            continue

        for field in ("model_key", "modelKey"):
            model_key = obj.get(field)
            if not isinstance(model_key, str):
                continue

            canonical = alias_map.get(model_key)
            if canonical and canonical != model_key:
                obj[field] = canonical
                changed += 1

    return changed


def main() -> None:
    args = parse_args()
    s3_client = boto3.client("s3", region_name=args.region)
    alias_map = build_alias_map()
    keys = iter_unity_json_keys(
        s3_client,
        bucket=args.bucket,
        prefix=args.prefix,
        limit=args.limit,
    )

    if not keys:
        print(f"No Unity JSON files found under s3://{args.bucket}/{args.prefix.strip('/')}")
        return

    scanned = 0
    updated_files = 0
    updated_objects = 0

    for key in keys:
        scanned += 1
        response = s3_client.get_object(Bucket=args.bucket, Key=key)
        data = json.loads(response["Body"].read().decode("utf-8"))
        changed = canonicalize_objects(data, alias_map)

        if changed == 0:
            print(f"[OK] {key}: no changes")
            continue

        updated_files += 1
        updated_objects += changed
        action = "UPDATE" if args.apply else "DRY RUN"
        print(f"[{action}] {key}: {changed} model_key value(s)")

        if args.apply:
            s3_client.put_object(
                Bucket=args.bucket,
                Key=key,
                Body=json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
                ContentType="application/json",
            )

    mode = "applied" if args.apply else "dry-run"
    print(
        f"Done ({mode}). scanned={scanned}, "
        f"updated_files={updated_files}, updated_objects={updated_objects}"
    )


if __name__ == "__main__":
    main()
