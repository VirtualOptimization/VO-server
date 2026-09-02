"""Move shared texture preset files to the canonical S3 prefix.

This is safe to run on EC2 because its instance profile performs both the S3
copy and the database update. Existing source objects are deliberately kept so
rollback remains simple.

Canonical path:
    textures/presets/{preset_key}.{original_extension}

Examples:
    .venv/bin/python scripts/normalize_shared_texture_presets.py --dry-run
    .venv/bin/python scripts/normalize_shared_texture_presets.py
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.core.config import settings
from server.core.s3 import get_s3_client
from shared.db import SessionLocal
from shared.models.texture_preset import TexturePreset


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
    parser = argparse.ArgumentParser(description="Normalize shared texture preset S3 paths.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned copies without changing S3 or DB.")
    return parser.parse_args()


def target_key(preset: TexturePreset) -> str:
    suffix = PurePosixPath(preset.texture_s3_key).suffix.lower() or ".jpg"
    return f"textures/presets/{preset.preset_key}{suffix}"


def main() -> None:
    load_env_file(PROJECT_ROOT / ".env")
    args = parse_args()
    if not settings.s3_bucket_name:
        raise SystemExit("S3_BUCKET_NAME이 필요합니다.")

    with SessionLocal() as db:
        presets = (
            db.query(TexturePreset)
            .filter(TexturePreset.user_id.is_(None))
            .order_by(TexturePreset.id.asc())
            .all()
        )
        s3 = get_s3_client()

        for preset in presets:
            destination = target_key(preset)
            if preset.texture_s3_key == destination:
                print(f"[SKIP] {preset.preset_key}: already {destination}")
                continue
            print(f"[{'DRY RUN ' if args.dry_run else ''}COPY] {preset.texture_s3_key} -> {destination}")
            if args.dry_run:
                continue
            s3.copy_object(
                Bucket=settings.s3_bucket_name,
                CopySource={"Bucket": settings.s3_bucket_name, "Key": preset.texture_s3_key},
                Key=destination,
                ContentType="image/jpeg" if destination.endswith(".jpg") else "image/png",
                MetadataDirective="REPLACE",
            )
            preset.texture_s3_key = destination

        if args.dry_run:
            print("Dry run complete. No S3 or database changes were made.")
            return
        db.commit()

    print("Shared texture preset paths normalized.")


if __name__ == "__main__":
    main()
