"""Recolor base furniture GLBs and replace their S3 GLB/USDZ files safely.

The script is intended to run on the EC2 instance that has S3 access and the
GLB-to-USDZ converter installed. It is a dry run unless ``--apply`` is given.

Example:
    python scripts/recolor_base_furniture_assets.py \
        --bucket project7-65-sydney-vo-s3 \
        --region ap-southeast-2

    python scripts/recolor_base_furniture_assets.py \
        --bucket project7-65-sydney-vo-s3 \
        --region ap-southeast-2 \
        --apply
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import boto3
from botocore.exceptions import ClientError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from workers.converter.apply_glb_color import apply_color
from workers.converter.run_usdz_conversion import run_glb_to_usdz


@dataclass(frozen=True)
class PaletteColor:
    name: str
    hex: str


CATEGORY_PALETTES = {
    "chair": (
        PaletteColor("Chair Sky 1", "#7FA6DC"),
        PaletteColor("Chair Sky 2", "#A3BFE8"),
        PaletteColor("Chair Sky 3", "#CEDDF2"),
    ),
    "sofa": (
        PaletteColor("Sofa Green 1", "#77D69A"),
        PaletteColor("Sofa Green 2", "#A1E4B8"),
        PaletteColor("Sofa Green 3", "#D0F0D9"),
    ),
    "table": (
        PaletteColor("Table Brown 1", "#B9894D"),
        PaletteColor("Table Brown 2", "#C7A87A"),
        PaletteColor("Table Brown 3", "#DDC9AA"),
    ),
    "storage": (
        PaletteColor("Shelf Yellow 1", "#F4E256"),
        PaletteColor("Shelf Yellow 2", "#F8ED8A"),
        PaletteColor("Shelf Yellow 3", "#FCF6BE"),
    ),
}

CATEGORY_ALIASES = {
    "chair": "chair",
    "sofa": "sofa",
    "table": "table",
    "desk": "table",
    "storage": "storage",
    "shelf": "storage",
}

FALLBACK_PALETTE = (
    PaletteColor("Neutral Pastel 1", "#B8C6D9"),
    PaletteColor("Neutral Pastel 2", "#C8D1DE"),
    PaletteColor("Neutral Pastel 3", "#D8DEE7"),
)


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default=os.getenv("S3_BUCKET_NAME"))
    parser.add_argument("--region", default=os.getenv("AWS_REGION", "ap-southeast-2"))
    parser.add_argument("--prefix", default="asset")
    parser.add_argument(
        "--backup-prefix",
        default="backups/base-furniture-before-color",
        help="Permanent backup location for the original assets.",
    )
    parser.add_argument(
        "--staging-prefix",
        default="staging/base-furniture-colors",
        help="Temporary upload location used before replacing live keys.",
    )
    parser.add_argument("--seed", type=int, default=20260907)
    parser.add_argument(
        "--color-scope",
        choices=("model", "category"),
        default="model",
        help="Assign colors per model (default) or per top-level furniture category.",
    )
    parser.add_argument("--limit", type=int, help="Process only the first N GLB files.")
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=PROJECT_ROOT / "tmp/base-furniture-color-reports",
        help="Directory for the JSON execution report.",
    )
    parser.add_argument("--apply", action="store_true", help="Write backups and replace S3 files.")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument(
        "--skip-db-update",
        action="store_true",
        help="Do not populate furniture_models.usdz_url after a successful replacement.",
    )
    parser.add_argument(
        "--keep-base-color-texture",
        action="store_true",
        help="Tint existing textures instead of replacing them with solid colors.",
    )
    args = parser.parse_args()
    if not args.bucket:
        parser.error("--bucket or S3_BUCKET_NAME is required")
    return args


def iter_source_glb_keys(s3, *, bucket: str, prefix: str) -> list[str]:
    normalized = prefix.strip("/")
    request_prefix = f"{normalized}/" if normalized else ""
    excluded = {"materials", "backup", "backups", "staging"}
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=request_prefix):
        for item in page.get("Contents", []):
            key = item["Key"]
            relative = PurePosixPath(key).parts[len(PurePosixPath(request_prefix).parts) :]
            if not relative or relative[0].lower() in excluded:
                continue
            if key.lower().endswith(".glb"):
                keys.append(key)
    return sorted(keys)


def furniture_type_from_key(key: str, prefix: str) -> str:
    key_parts = PurePosixPath(key).parts
    prefix_parts = PurePosixPath(prefix.strip("/")).parts
    relative = key_parts[len(prefix_parts) :]
    if len(relative) < 2:
        return "Uncategorized"
    return relative[0]


def model_name_from_key(key: str, prefix: str) -> str:
    relative = relative_to_prefix(key, prefix)
    if relative.endswith(".rooms.glb"):
        return relative.removesuffix(".rooms.glb")
    return relative.removesuffix(".glb")


def color_group_from_key(key: str, prefix: str, scope: str) -> str:
    if scope == "category":
        return furniture_type_from_key(key, prefix)
    return model_name_from_key(key, prefix)


def assign_colors(types: list[str], seed: int) -> dict[str, PaletteColor]:
    del seed

    color_by_group: dict[str, PaletteColor] = {}
    groups_by_category: dict[str, list[str]] = {}
    for group in sorted(set(types)):
        category = category_from_group(group)
        groups_by_category.setdefault(category, []).append(group)

    for category, groups in groups_by_category.items():
        palette = build_category_palette(category, len(groups))
        for group, color in zip(groups, palette, strict=True):
            color_by_group[group] = color

    return color_by_group


def category_from_group(group: str) -> str:
    first_part = PurePosixPath(group).parts[0] if PurePosixPath(group).parts else group
    return CATEGORY_ALIASES.get(first_part.lower(), first_part.lower())


def build_category_palette(category: str, count: int) -> list[PaletteColor]:
    base_palette = CATEGORY_PALETTES.get(category, FALLBACK_PALETTE)
    if count <= len(base_palette):
        return list(base_palette[:count])

    anchors = [hex_to_rgb(color.hex) for color in base_palette]
    colors: list[PaletteColor] = []
    for index in range(count):
        position = index / max(count - 1, 1)
        scaled = position * (len(anchors) - 1)
        left = min(int(scaled), len(anchors) - 2)
        right = left + 1
        t = scaled - left
        rgb = tuple(
            round(anchors[left][channel] * (1 - t) + anchors[right][channel] * t)
            for channel in range(3)
        )
        colors.append(PaletteColor(f"{category.title()} Pastel {index + 1}", rgb_to_hex(rgb)))
    return colors


def hex_to_rgb(value: str) -> tuple[int, int, int]:
    normalized = value.strip().removeprefix("#")
    if len(normalized) != 6:
        raise ValueError(f"invalid color value: {value}")
    return tuple(int(normalized[index : index + 2], 16) for index in (0, 2, 4))


def rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{max(0, min(255, channel)):02X}" for channel in rgb)


def object_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def replace_extension(key: str, extension: str) -> str:
    return str(PurePosixPath(key).with_suffix(extension))


def relative_to_prefix(key: str, prefix: str) -> str:
    normalized = prefix.strip("/")
    return key.removeprefix(f"{normalized}/") if normalized else key


def backup_if_needed(s3, *, bucket: str, source_key: str, backup_key: str) -> str:
    if object_exists(s3, bucket, backup_key):
        return backup_key
    s3.copy_object(Bucket=bucket, CopySource={"Bucket": bucket, "Key": source_key}, Key=backup_key)
    return backup_key


def update_base_model_usdz_url(*, bucket: str, glb_key: str, usdz_key: str) -> int:
    from shared.db import SessionLocal
    from shared.models import FurnitureModel

    glb_uri = f"s3://{bucket}/{glb_key}"
    usdz_uri = f"s3://{bucket}/{usdz_key}"
    with SessionLocal() as db:
        models = (
            db.query(FurnitureModel)
            .filter(FurnitureModel.user_id.is_(None), FurnitureModel.glb_url == glb_uri)
            .all()
        )
        for model in models:
            model.usdz_url = usdz_uri
            model.status = "READY"
        db.commit()
        return len(models)


def process_asset(
    s3,
    *,
    args: argparse.Namespace,
    source_key: str,
    color: PaletteColor,
    run_id: str,
) -> dict[str, object]:
    relative_key = relative_to_prefix(source_key, args.prefix)
    live_usdz_key = replace_extension(source_key, ".usdz")
    backup_glb_key = f"{args.backup_prefix.strip('/')}/{relative_key}"
    backup_usdz_key = replace_extension(backup_glb_key, ".usdz")
    stage_glb_key = f"{args.staging_prefix.strip('/')}/{run_id}/{relative_key}"
    stage_usdz_key = replace_extension(stage_glb_key, ".usdz")

    if not args.apply:
        return {
            "source": source_key,
            "color": asdict(color),
            "backup": backup_glb_key,
            "output_glb": source_key,
            "output_usdz": live_usdz_key,
        }

    backup_if_needed(s3, bucket=args.bucket, source_key=source_key, backup_key=backup_glb_key)
    if object_exists(s3, args.bucket, live_usdz_key):
        backup_if_needed(
            s3,
            bucket=args.bucket,
            source_key=live_usdz_key,
            backup_key=backup_usdz_key,
        )

    with tempfile.TemporaryDirectory(prefix="vo-base-color-") as tmpdir:
        tmp = Path(tmpdir)
        original_file = tmp / "original.glb"
        colored_file = tmp / "colored.glb"
        usdz_file = tmp / "colored.usdz"

        # Always recolor the preserved original so reruns do not compound colors.
        s3.download_file(args.bucket, backup_glb_key, str(original_file))
        material_count = apply_color(
            original_file,
            colored_file,
            color.hex,
            keep_base_color_texture=args.keep_base_color_texture,
        )
        run_glb_to_usdz(str(colored_file), str(usdz_file))
        if colored_file.stat().st_size == 0 or usdz_file.stat().st_size == 0:
            raise RuntimeError("converter created an empty output file")

        s3.upload_file(
            str(colored_file),
            args.bucket,
            stage_glb_key,
            ExtraArgs={"ContentType": "model/gltf-binary"},
        )
        s3.upload_file(
            str(usdz_file),
            args.bucket,
            stage_usdz_key,
            ExtraArgs={"ContentType": "model/vnd.usdz+zip"},
        )
        s3.head_object(Bucket=args.bucket, Key=stage_glb_key)
        s3.head_object(Bucket=args.bucket, Key=stage_usdz_key)

        s3.copy_object(
            Bucket=args.bucket,
            CopySource={"Bucket": args.bucket, "Key": stage_glb_key},
            Key=source_key,
            ContentType="model/gltf-binary",
            MetadataDirective="REPLACE",
        )
        s3.copy_object(
            Bucket=args.bucket,
            CopySource={"Bucket": args.bucket, "Key": stage_usdz_key},
            Key=live_usdz_key,
            ContentType="model/vnd.usdz+zip",
            MetadataDirective="REPLACE",
        )
        s3.delete_objects(
            Bucket=args.bucket,
            Delete={"Objects": [{"Key": stage_glb_key}, {"Key": stage_usdz_key}]},
        )

    db_rows_updated = 0
    if not args.skip_db_update:
        db_rows_updated = update_base_model_usdz_url(
            bucket=args.bucket,
            glb_key=source_key,
            usdz_key=live_usdz_key,
        )

    return {
        "source": source_key,
        "color": asdict(color),
        "materials": material_count,
        "backup": backup_glb_key,
        "output_glb": source_key,
        "output_usdz": live_usdz_key,
        "db_rows_updated": db_rows_updated,
    }


def main() -> int:
    load_env_file(PROJECT_ROOT / ".env")
    args = parse_args()
    s3 = boto3.client("s3", region_name=args.region)
    all_keys = iter_source_glb_keys(s3, bucket=args.bucket, prefix=args.prefix)
    if not all_keys:
        print(f"No source GLBs found under s3://{args.bucket}/{args.prefix.strip('/')}/")
        return 0

    groups = [color_group_from_key(key, args.prefix, args.color_scope) for key in all_keys]
    color_by_group = assign_colors(groups, args.seed)
    keys = all_keys[: args.limit] if args.limit is not None else all_keys
    print("Color assignment:")
    for group, color in color_by_group.items():
        print(f"  {group}: {color.name} {color.hex}")
    print(f"Mode: {'APPLY' if args.apply else 'DRY RUN'}; assets={len(keys)}")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    results: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    for index, key in enumerate(keys, start=1):
        group = color_group_from_key(key, args.prefix, args.color_scope)
        print(f"[{index}/{len(keys)}] {key} -> {color_by_group[group].name}")
        try:
            results.append(
                process_asset(
                    s3,
                    args=args,
                    source_key=key,
                    color=color_by_group[group],
                    run_id=run_id,
                )
            )
        except Exception as exc:
            failures.append({"source": key, "error": str(exc)})
            print(f"[FAILED] {key}: {exc}", file=sys.stderr)
            if args.fail_fast:
                break

    report = {
        "mode": "apply" if args.apply else "dry-run",
        "bucket": args.bucket,
        "prefix": args.prefix,
        "seed": args.seed,
        "color_scope": args.color_scope,
        "color_by_group": {key: asdict(value) for key, value in color_by_group.items()},
        "completed": len(results),
        "failed": len(failures),
        "results": results,
        "failures": failures,
    }
    args.report_dir.mkdir(parents=True, exist_ok=True)
    report_path = args.report_dir / f"base-furniture-color-report-{run_id}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Report: {report_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
