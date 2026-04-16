"""Sync local RoomPlan furniture catalog files into the furniture_models table.

Example:
    .venv/bin/python scripts/sync_furniture_catalog.py \
        --catalog-root /path/to/RoomPlanCatalog.bundle/Resources \
        --s3-prefix https://your-s3-bucket/models
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pxr import Usd, UsdGeom
from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from shared.db import SessionLocal
from shared.models import FurnitureModel

FURNITURE_TYPE_MAP = {
    "Chair": "chair",
    "Sofa": "sofa",
    "Storage": "shelf",
    "Table": "desk",
}


def load_env_file(env_path: Path) -> None:
    """Load simple KEY=VALUE pairs from a local .env file if present."""
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read RoomPlan USDC models and upsert furniture_models rows."
    )
    parser.add_argument(
        "--catalog-root",
        default=os.getenv("MODEL_CATALOG_ROOT"),
        help="Root directory containing category/model folders.",
    )
    parser.add_argument(
        "--s3-prefix",
        default=os.getenv("MODEL_CATALOG_S3_PREFIX"),
        help="Public or internal prefix used to build model usdc_url values.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print detected models without writing to the database.",
    )
    args = parser.parse_args()

    if not args.catalog_root:
        parser.error("--catalog-root or MODEL_CATALOG_ROOT is required")
    if not args.s3_prefix:
        parser.error("--s3-prefix or MODEL_CATALOG_S3_PREFIX is required")

    return args


def get_usdc_dimensions(file_path: Path) -> tuple[float, float, float] | None:
    """Extract width, depth, height in meters from a USDC model."""
    try:
        stage = Usd.Stage.Open(str(file_path))
        bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
        bbox = bbox_cache.ComputeWorldBound(stage.GetPseudoRoot())
        range_value = bbox.GetRange()

        min_v = range_value.GetMin()
        max_v = range_value.GetMax()

        width = round(float(max_v[0] - min_v[0]) / 100, 4)
        height = round(float(max_v[1] - min_v[1]) / 100, 4)
        depth = round(float(max_v[2] - min_v[2]) / 100, 4)
        return width, depth, height
    except Exception as exc:  # pragma: no cover - depends on USD assets/runtime
        print(f"  [WARN] Failed to inspect {file_path.name}: {exc}")
        return None


def normalize_furniture_type(category: str) -> str:
    return FURNITURE_TYPE_MAP.get(category, category.lower())


def build_model_key(category: str, model_name: str) -> str:
    """Return a stable unique key for a catalog entry."""
    return f"{normalize_furniture_type(category)}:{model_name}"


def build_virtual_url(s3_prefix: str, category: str, model_name: str, filename: str) -> str:
    prefix = s3_prefix.rstrip("/")
    return f"{prefix}/{category}/{model_name}/{filename}"


def upsert_furniture_model(
    db: Session,
    *,
    model_key: str,
    category: str,
    model_name: str,
    display_name: str,
    usdc_url: str,
    width: float,
    depth: float,
    height: float,
) -> None:
    furniture_type = normalize_furniture_type(category)
    existing = db.query(FurnitureModel).filter(FurnitureModel.model_key == model_key).one_or_none()

    if existing is None:
        db.add(
            FurnitureModel(
                model_key=model_key,
                name=display_name,
                furniture_type=furniture_type,
                usdc_url=usdc_url,
                width=width,
                depth=depth,
                height=height,
            )
        )
        print(f"  [CREATE] {model_key}: {width}m x {depth}m x {height}m")
        return

    existing.name = display_name
    existing.furniture_type = furniture_type
    existing.usdc_url = usdc_url
    existing.width = width
    existing.depth = depth
    existing.height = height
    print(f"  [UPDATE] {model_key}: {width}m x {depth}m x {height}m")


def sync_catalog(catalog_root: Path, s3_prefix: str, dry_run: bool = False) -> None:
    print("Starting furniture model catalog sync...")

    with SessionLocal() as db:
        for category_dir in sorted(catalog_root.iterdir()):
            if not category_dir.is_dir():
                continue

            category = category_dir.name

            for model_dir in sorted(category_dir.iterdir()):
                if not model_dir.is_dir():
                    continue

                target_file = next(
                    (item for item in sorted(model_dir.iterdir()) if item.suffix.lower() == ".usdc"),
                    None,
                )
                if target_file is None:
                    continue

                dims = get_usdc_dimensions(target_file)
                if dims is None:
                    continue

                width, depth, height = dims
                model_key = build_model_key(category, model_dir.name)
                display_name = target_file.stem
                virtual_url = build_virtual_url(
                    s3_prefix=s3_prefix,
                    category=category,
                    model_name=model_dir.name,
                    filename=target_file.name,
                )

                if dry_run:
                    print(
                        f"  [DRY RUN] {model_key}: {width}m x {depth}m x {height}m -> {virtual_url}"
                    )
                    continue

                upsert_furniture_model(
                    db,
                    model_key=model_key,
                    category=category,
                    model_name=model_dir.name,
                    display_name=display_name,
                    usdc_url=virtual_url,
                    width=width,
                    depth=depth,
                    height=height,
                )

        if dry_run:
            print("Dry run complete. No database changes were made.")
            return

        db.commit()
        print("Furniture model catalog sync completed.")


def main() -> None:
    load_env_file(Path(".env"))
    args = parse_args()
    sync_catalog(
        catalog_root=Path(args.catalog_root).expanduser().resolve(),
        s3_prefix=args.s3_prefix,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
