"""Seed base furniture model metadata from S3 GLB catalog objects.

Expected S3 layout:
    asset/{Category}/{Variant}/{ModelName}.rooms.glb

DB model_key layout:
    {Category}/{Variant}/{ModelName}.rooms.usdc

Example:
    .venv/bin/python scripts/seed_base_furniture_models_from_s3.py \
        --bucket project7-65-sydney-vo-s3 \
        --prefix asset
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import boto3
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.db import SessionLocal
from shared.models import FurnitureModel

CATEGORY_TYPE_MAP = {
    "Bed": "bed",
    "Chair": "chair",
    "Sofa": "sofa",
    "Storage": "storage",
    "Table": "table",
}


@dataclass(frozen=True)
class CatalogModel:
    model_key: str
    name: str
    furniture_type: str | None
    glb_url: str


def load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upsert base furniture_models rows from S3 GLB catalog assets."
    )
    parser.add_argument(
        "--bucket",
        default=os.getenv("MODEL_CATALOG_S3_BUCKET") or os.getenv("S3_BUCKET_NAME"),
        help="S3 bucket containing base furniture GLB assets.",
    )
    parser.add_argument(
        "--prefix",
        default=os.getenv("MODEL_CATALOG_S3_KEY_PREFIX", "asset"),
        help="S3 prefix containing base furniture GLB assets.",
    )
    parser.add_argument(
        "--region",
        default=os.getenv("AWS_REGION", "ap-southeast-2"),
        help="AWS region for S3.",
    )
    parser.add_argument("--default-width", type=float, default=1.0)
    parser.add_argument("--default-depth", type=float, default=1.0)
    parser.add_argument("--default-height", type=float, default=1.0)
    parser.add_argument(
        "--overwrite-dimensions",
        action="store_true",
        help="Overwrite width/depth/height on existing rows with default values.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned changes without writing to the database.",
    )
    args = parser.parse_args()

    if not args.bucket:
        parser.error("--bucket or S3_BUCKET_NAME is required")

    return args


def iter_glb_keys(s3_client, *, bucket: str, prefix: str) -> list[str]:
    normalized_prefix = prefix.strip("/")
    request_prefix = f"{normalized_prefix}/" if normalized_prefix else ""
    paginator = s3_client.get_paginator("list_objects_v2")
    keys: list[str] = []

    for page in paginator.paginate(Bucket=bucket, Prefix=request_prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            if key.lower().endswith(".glb"):
                keys.append(key)

    return sorted(keys)


def strip_prefix_parts(key: str, prefix: str) -> tuple[str, ...]:
    key_parts = PurePosixPath(key).parts
    prefix_parts = PurePosixPath(prefix.strip("/")).parts if prefix.strip("/") else ()

    if prefix_parts and key_parts[: len(prefix_parts)] == prefix_parts:
        return key_parts[len(prefix_parts) :]

    return key_parts


def model_key_from_relative_parts(rel_parts: tuple[str, ...]) -> str:
    """Build a unique model key that mirrors the RoomPlan catalog path.

    File names are duplicated across several RoomPlan catalog folders, so using
    only the file name would collapse multiple catalog entries into one DB row.
    """
    relative_path = PurePosixPath(*rel_parts).as_posix()
    if relative_path.endswith(".rooms.glb"):
        return relative_path.removesuffix(".rooms.glb") + ".rooms.usdc"
    return relative_path.removesuffix(".glb")


def display_name_from_filename(filename: str) -> str:
    return filename.removesuffix(".glb").removesuffix(".rooms")


def catalog_model_from_key(*, bucket: str, prefix: str, key: str) -> CatalogModel:
    rel_parts = strip_prefix_parts(key, prefix)
    category = rel_parts[0] if len(rel_parts) >= 2 else None
    filename = PurePosixPath(key).name

    return CatalogModel(
        model_key=model_key_from_relative_parts(rel_parts),
        name=display_name_from_filename(filename),
        furniture_type=CATEGORY_TYPE_MAP.get(category, category.lower() if category else None),
        glb_url=f"s3://{bucket}/{key}",
    )


def upsert_model(
    db: Session,
    model: CatalogModel,
    *,
    default_width: float,
    default_depth: float,
    default_height: float,
    overwrite_dimensions: bool,
    dry_run: bool,
) -> str:
    existing = db.query(FurnitureModel).filter(FurnitureModel.model_key == model.model_key).one_or_none()

    if existing and existing.user_id is not None:
        return f"[SKIP] {model.model_key}: already used by user-owned furniture"

    if dry_run:
        action = "UPDATE" if existing else "CREATE"
        return f"[DRY RUN {action}] {model.model_key} -> {model.glb_url}"

    if existing is None:
        db.add(
            FurnitureModel(
                user_id=None,
                model_key=model.model_key,
                name=model.name,
                furniture_type=model.furniture_type,
                status="READY",
                glb_url=model.glb_url,
                width=default_width,
                depth=default_depth,
                height=default_height,
            )
        )
        return f"[CREATE] {model.model_key}"

    existing.user_id = None
    existing.name = model.name
    existing.furniture_type = model.furniture_type
    existing.status = "READY"
    existing.glb_url = model.glb_url

    if overwrite_dimensions:
        existing.width = default_width
        existing.depth = default_depth
        existing.height = default_height

    return f"[UPDATE] {model.model_key}"


def seed_catalog(args: argparse.Namespace) -> None:
    s3_client = boto3.client("s3", region_name=args.region)
    keys = iter_glb_keys(s3_client, bucket=args.bucket, prefix=args.prefix)

    if not keys:
        print(f"No GLB files found under s3://{args.bucket}/{args.prefix.strip('/')}/")
        return

    seen_model_keys: set[str] = set()
    skipped_duplicates = 0

    with SessionLocal() as db:
        for key in keys:
            model = catalog_model_from_key(bucket=args.bucket, prefix=args.prefix, key=key)

            if model.model_key in seen_model_keys:
                skipped_duplicates += 1
                print(f"[SKIP] duplicate model_key {model.model_key} from {key}")
                continue

            seen_model_keys.add(model.model_key)
            print(
                upsert_model(
                    db,
                    model,
                    default_width=args.default_width,
                    default_depth=args.default_depth,
                    default_height=args.default_height,
                    overwrite_dimensions=args.overwrite_dimensions,
                    dry_run=args.dry_run,
                )
            )

        if args.dry_run:
            print("Dry run complete. No database changes were made.")
            return

        db.commit()

    print(f"Seed complete. Models={len(seen_model_keys)}, Duplicates skipped={skipped_duplicates}")


def main() -> None:
    load_env_file(Path(".env"))
    seed_catalog(parse_args())


if __name__ == "__main__":
    main()
