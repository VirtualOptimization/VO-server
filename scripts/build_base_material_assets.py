"""Queue reusable GLB/USDZ variants for every base furniture and texture preset.

Run this on the EC2 instance after ``alembic upgrade head``. The instance
profile supplies S3 access, so no local AWS access key is required.

Examples:
    .venv/bin/python scripts/build_base_material_assets.py --dry-run
    .venv/bin/python scripts/build_base_material_assets.py
    .venv/bin/python scripts/build_base_material_assets.py --preset-key wood_01
    .venv/bin/python scripts/build_base_material_assets.py --force

The conversion worker processes the queued tasks. Shared output paths are:
    asset/materials/{catalog_model_path}/{preset_key}.glb
    asset/materials/{catalog_model_path}/{preset_key}.usdz
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path, PurePosixPath

from sqlalchemy.orm import Session

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.core.s3 import build_s3_uri, parse_s3_uri
from server.services.conversion_tasks import TASK_BASE_MATERIAL_ASSET, enqueue_conversion_task
from shared.db import SessionLocal
from shared.models.furniture_material_asset import FurnitureMaterialAsset
from shared.models.furniture_model import FurnitureModel
from shared.models.texture_preset import TexturePreset


def load_env_file(env_path: Path) -> None:
    """Load local .env only when script execution is not managed by systemd."""
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
        description="Queue shared GLB/USDZ material assets for base furniture models."
    )
    parser.add_argument("--model-id", action="append", type=int, help="Only process this base model ID.")
    parser.add_argument("--preset-key", action="append", help="Only process this texture preset key.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned tasks without database writes.")
    parser.add_argument("--force", action="store_true", help="Requeue assets already marked READY or FAILED.")
    return parser.parse_args()


def s3_key_from_uri(uri: str | None) -> str | None:
    parsed = parse_s3_uri(uri)
    return parsed[1] if parsed else None


def output_key(model_key: str, preset_key: str, extension: str) -> str:
    relative = model_key.removesuffix(".rooms.usdc").removesuffix(".usdc").removesuffix(".glb")
    normalized = PurePosixPath(relative).as_posix().strip("/")
    return f"asset/materials/{normalized}/{preset_key}.{extension}"


def queue_asset(
    db: Session,
    *,
    model: FurnitureModel,
    preset: TexturePreset,
    force: bool,
    dry_run: bool,
) -> str:
    source_key = s3_key_from_uri(model.glb_url)
    if not source_key:
        return f"[SKIP] {model.model_key} x {preset.preset_key}: base GLB S3 URI 없음"

    existing = (
        db.query(FurnitureMaterialAsset)
        .filter(
            FurnitureMaterialAsset.furniture_model_id == model.id,
            FurnitureMaterialAsset.texture_preset_id == preset.id,
        )
        .one_or_none()
    )
    if existing is not None and existing.status == "READY" and not force:
        return f"[SKIP READY] {model.model_key} x {preset.preset_key}"
    if existing is not None and existing.status in {"PENDING", "RUNNING"} and not force:
        return f"[SKIP {existing.status}] {model.model_key} x {preset.preset_key}"

    glb_key = output_key(model.model_key, preset.preset_key, "glb")
    usdz_key = output_key(model.model_key, preset.preset_key, "usdz")
    if dry_run:
        action = "REQUEUE" if existing else "CREATE"
        return f"[DRY RUN {action}] {model.model_key} x {preset.preset_key} -> {glb_key}"

    asset = existing or FurnitureMaterialAsset(
        furniture_model_id=model.id,
        texture_preset_id=preset.id,
    )
    if existing is None:
        db.add(asset)
    asset.status = "PENDING"
    asset.glb_url = build_s3_uri(glb_key)
    asset.usdz_url = build_s3_uri(usdz_key)
    asset.error_message = None
    db.flush()

    task = enqueue_conversion_task(
        db,
        task_type=TASK_BASE_MATERIAL_ASSET,
        source_key=source_key,
        texture_key=preset.texture_s3_key,
        output_glb_key=glb_key,
        output_usdz_key=usdz_key,
        furniture_model_id=model.id,
        payload={
            "material_asset_id": asset.id,
            "base_model_id": model.id,
            "texture_preset_id": preset.id,
            "material_preset_id": preset.preset_key,
        },
    )
    return f"[QUEUED task={task.id}] {model.model_key} x {preset.preset_key}"


def main() -> None:
    load_env_file(PROJECT_ROOT / ".env")
    args = parse_args()

    with SessionLocal() as db:
        models_query = db.query(FurnitureModel).filter(
            FurnitureModel.user_id.is_(None),
            FurnitureModel.status == "READY",
            FurnitureModel.glb_url.is_not(None),
        )
        if args.model_id:
            models_query = models_query.filter(FurnitureModel.id.in_(args.model_id))
        models = models_query.order_by(FurnitureModel.id.asc()).all()

        presets_query = db.query(TexturePreset).filter(TexturePreset.user_id.is_(None))
        if args.preset_key:
            presets_query = presets_query.filter(TexturePreset.preset_key.in_(args.preset_key))
        presets = presets_query.order_by(TexturePreset.id.asc()).all()

        if not models:
            raise SystemExit("기본 가구 모델이 없습니다. 먼저 seed_base_furniture_models_from_s3.py를 실행하세요.")
        if not presets:
            raise SystemExit("공용 텍스처 preset이 없습니다.")

        for model in models:
            for preset in presets:
                print(queue_asset(db, model=model, preset=preset, force=args.force, dry_run=args.dry_run))

        if args.dry_run:
            print("Dry run complete. No database changes were made.")
            return
        db.commit()

    print(f"Queued base material assets: models={len(models)}, presets={len(presets)}")


if __name__ == "__main__":
    main()
