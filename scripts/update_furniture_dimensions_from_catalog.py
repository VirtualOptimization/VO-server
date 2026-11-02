"""Update base furniture model dimensions from the RoomPlan catalog USDC files.

seed_base_furniture_models_from_s3.py creates rows with placeholder 1 m
dimensions. Base catalog model keys mirror the bundle path
({Category}/{Variant}/{File}.rooms.usdc), so each row is measured from that
file in the bundle (centimeters converted to meters).

Example:
    .venv/bin/python scripts/update_furniture_dimensions_from_catalog.py \
        --catalog-root ~/RoomPlanCatalog.bundle --dry-run
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.prepare_roomplan_catalog import get_usdc_dimensions, resolve_resources_root  # noqa: E402
from shared.db import SessionLocal  # noqa: E402
from shared.models.furniture_model import FurnitureModel  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog-root",
        default=os.getenv("MODEL_CATALOG_ROOT"),
        help="RoomPlanCatalog.bundle path or its Resources directory.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.catalog_root:
        parser.error("--catalog-root or MODEL_CATALOG_ROOT is required")
    return args


def main() -> int:
    args = parse_args()
    resources_root = resolve_resources_root(Path(args.catalog_root))

    db = SessionLocal()
    try:
        models = (
            db.query(FurnitureModel)
            .filter(FurnitureModel.user_id.is_(None))
            .order_by(FurnitureModel.model_key)
            .all()
        )
        updated = 0
        missing: list[str] = []
        for model in models:
            usdc_path = resources_root / model.model_key
            dimensions = get_usdc_dimensions(usdc_path) if usdc_path.is_file() else None
            if not dimensions or not all(dimensions):
                missing.append(model.model_key)
                continue
            width, depth, height = dimensions
            print(f"  {model.model_key}: width={width}, depth={depth}, height={height}")
            if not args.dry_run:
                model.width, model.depth, model.height = width, depth, height
            updated += 1

        if args.dry_run:
            print(f"Dry run: {updated} rows would be updated; database was not changed.")
        else:
            db.commit()
            print(f"Updated DB rows: {updated}")
        if missing:
            print(f"Not measured (no USDC in bundle or unreadable): {len(missing)}")
            for model_key in missing:
                print(f"  {model_key}")
        return 0 if not missing else 1
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
