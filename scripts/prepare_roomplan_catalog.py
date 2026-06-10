"""Convert RoomPlan furniture catalog USDC files to GLB, upload to S3, and sync DB.

Example:
    .venv/bin/python scripts/prepare_roomplan_catalog.py \
        --catalog-root /path/to/RoomPlanCatalog.bundle \
        --bucket aja-vo-server-bucket \
        --prefix assets/roomplan-catalog/v1
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

FURNITURE_TYPE_MAP = {
    "Chair": "chair",
    "Sofa": "sofa",
    "Storage": "shelf",
    "Table": "desk",
}

_WARNED_MISSING_PXR = False


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog-root",
        default=os.getenv("MODEL_CATALOG_ROOT"),
        help="RoomPlanCatalog.bundle path or its Resources directory.",
    )
    parser.add_argument(
        "--bucket",
        default=os.getenv("MODEL_CATALOG_S3_BUCKET") or os.getenv("S3_BUCKET_NAME"),
        help="Destination S3 bucket.",
    )
    parser.add_argument(
        "--prefix",
        default=os.getenv("MODEL_CATALOG_S3_KEY_PREFIX", "assets/roomplan-catalog/v1"),
        help="S3 key prefix. USDC, GLB, and manifest objects are stored under it.",
    )
    parser.add_argument(
        "--work-dir",
        default="tmp/roomplan-catalog",
        help="Local working directory for converted GLB files and manifest.",
    )
    parser.add_argument(
        "--region",
        default=os.getenv("AWS_REGION", "ap-northeast-2"),
        help="AWS region for the S3 client.",
    )
    parser.add_argument(
        "--usd2gltf",
        default=os.getenv("USD2GLTF_BIN", "usd2gltf"),
        help="usd2gltf executable path.",
    )
    parser.add_argument(
        "--blender",
        default=os.getenv("BLENDER_BIN", "blender"),
        help="Blender executable path used as a fallback converter.",
    )
    parser.add_argument(
        "--fallback-converter",
        choices=("auto", "blender", "none"),
        default=os.getenv("MODEL_CATALOG_FALLBACK_CONVERTER", "auto"),
        help="Fallback converter to use when usd2gltf fails.",
    )
    parser.add_argument(
        "--force-convert",
        action="store_true",
        help="Recreate GLB files even when an up-to-date local output already exists.",
    )
    parser.add_argument("--skip-upload", action="store_true", help="Convert and sync DB only.")
    parser.add_argument("--skip-db", action="store_true", help="Convert/upload only.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing.")
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop on the first conversion failure instead of recording it in manifest.",
    )
    args = parser.parse_args()

    if not args.catalog_root:
        parser.error("--catalog-root or MODEL_CATALOG_ROOT is required")
    if not args.bucket:
        parser.error("--bucket or MODEL_CATALOG_S3_BUCKET/S3_BUCKET_NAME is required")

    return args


def resolve_resources_root(path: Path) -> Path:
    path = path.expanduser().resolve()
    resources = path / "Resources"
    if resources.is_dir():
        return resources
    return path


def normalize_furniture_type(category: str) -> str:
    return FURNITURE_TYPE_MAP.get(category, category.lower())


def build_model_key(category: str, model_name: str) -> str:
    return f"{normalize_furniture_type(category)}:{model_name}"


def build_s3_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def iter_catalog_files(resources_root: Path) -> list[Path]:
    return sorted(path for path in resources_root.rglob("*.usdc") if path.is_file())


def get_usdc_dimensions(file_path: Path, *, required: bool = True) -> tuple[float, float, float] | None:
    try:
        from pxr import Usd, UsdGeom

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
    except ModuleNotFoundError as exc:
        if exc.name == "pxr":
            global _WARNED_MISSING_PXR
            if not _WARNED_MISSING_PXR:
                print("  [WARN] pxr module is not installed. Using 0.0 dimensions for catalog rows.")
                _WARNED_MISSING_PXR = True
            return 0.0, 0.0, 0.0
        raise
    except Exception as exc:
        print(f"  [WARN] Failed to inspect {file_path}: {exc}")
        return None


def run_blender_usd_to_glb(blender_bin: str, input_path: Path, output_path: Path) -> str | None:
    script = f"""
import bpy

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()

bpy.ops.wm.usd_import(filepath={str(input_path)!r})

for obj in bpy.context.scene.objects:
    obj.select_set(True)

bpy.ops.export_scene.gltf(
    filepath={str(output_path)!r},
    export_format="GLB",
    export_yup=True,
    export_apply=True,
)
"""
    result = subprocess.run(
        [blender_bin, "--background", "--python-expr", script],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0 and output_path.exists():
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip())
        return None

    return "\n".join(part for part in [result.stdout.strip(), result.stderr.strip()] if part)


def convert_usdc_to_glb(
    usd2gltf_bin: str,
    input_path: Path,
    output_path: Path,
    *,
    blender_bin: str,
    fallback_converter: str,
    force: bool,
) -> str | None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if (
        not force
        and output_path.exists()
        and output_path.stat().st_size > 0
        and output_path.stat().st_mtime >= input_path.stat().st_mtime
    ):
        print(f"  [SKIP] Local GLB is up to date: {output_path}")
        return None

    result = subprocess.run(
        [usd2gltf_bin, "-i", str(input_path), "-o", str(output_path)],
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        if result.stdout:
            print(result.stdout.strip())
        if result.stderr:
            print(result.stderr.strip())
        return None

    usd2gltf_error = "\n".join(
        part for part in [result.stdout.strip(), result.stderr.strip()] if part
    )

    if fallback_converter == "none":
        return usd2gltf_error

    if fallback_converter in {"auto", "blender"}:
        print("  [FALLBACK] usd2gltf failed; retrying with Blender...")
        blender_error = run_blender_usd_to_glb(blender_bin, input_path, output_path)
        if blender_error is None:
            return None

        return (
            "usd2gltf failed:\n"
            f"{usd2gltf_error}\n\n"
            "Blender fallback failed:\n"
            f"{blender_error}"
        )

    return usd2gltf_error


def content_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".glb":
        return "model/gltf-binary"
    if suffix == ".json":
        return "application/json"
    if suffix == ".usdc":
        return "application/octet-stream"
    return "application/octet-stream"


def object_matches_local(s3_client: Any, bucket: str, key: str, file_path: Path) -> bool:
    from botocore.exceptions import ClientError

    try:
        response = s3_client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise

    return response.get("ContentLength") == file_path.stat().st_size


def upload_file(s3_client: Any, bucket: str, key: str, file_path: Path) -> str:
    if object_matches_local(s3_client, bucket, key, file_path):
        print(f"  [SKIP] s3://{bucket}/{key}")
        return build_s3_uri(bucket, key)

    s3_client.upload_file(
        str(file_path),
        bucket,
        key,
        ExtraArgs={"ContentType": content_type_for(file_path)},
    )
    print(f"  [UPLOAD] {file_path.name} -> s3://{bucket}/{key}")
    return build_s3_uri(bucket, key)


def upsert_furniture_model(
    db: "Session",
    *,
    model_key: str,
    category: str,
    display_name: str,
    usdc_uri: str,
    glb_uri: str,
    width: float,
    depth: float,
    height: float,
) -> None:
    from shared.models import FurnitureModel

    furniture_type = normalize_furniture_type(category)
    model = db.query(FurnitureModel).filter(FurnitureModel.model_key == model_key).one_or_none()

    if model is None:
        model = FurnitureModel(model_key=model_key)
        db.add(model)
        action = "CREATE"
    else:
        action = "UPDATE"

    model.name = display_name
    model.furniture_type = furniture_type
    model.usdc_url = usdc_uri
    model.glb_url = glb_uri
    model.width = width
    model.depth = depth
    model.height = height
    print(f"  [{action}] {model_key} -> {glb_uri}")


def prepare_catalog(
    *,
    catalog_root: Path,
    bucket: str,
    prefix: str,
    work_dir: Path,
    region: str,
    usd2gltf_bin: str,
    blender_bin: str,
    fallback_converter: str,
    force_convert: bool,
    skip_upload: bool,
    skip_db: bool,
    dry_run: bool,
    fail_fast: bool,
) -> None:
    resources_root = resolve_resources_root(catalog_root)
    catalog_files = iter_catalog_files(resources_root)
    if not catalog_files:
        raise FileNotFoundError(f"No .usdc files found under {resources_root}")

    if skip_upload or dry_run:
        s3_client = None
    else:
        import boto3

        s3_client = boto3.client("s3", region_name=region)
    manifest_items: list[dict[str, Any]] = []
    failed_items: list[dict[str, str]] = []
    if skip_db or dry_run:
        db = None
    else:
        from shared.db import SessionLocal

        db = SessionLocal()

    try:
        for usdc_path in catalog_files:
            relative_path = usdc_path.relative_to(resources_root)
            if len(relative_path.parts) < 3:
                print(f"  [SKIP] Unexpected catalog path: {relative_path}")
                continue

            category, model_name = relative_path.parts[0], relative_path.parts[1]
            glb_relative_path = relative_path.with_suffix(".glb")
            glb_path = work_dir / "glb" / glb_relative_path
            usdc_key = f"{prefix.rstrip('/')}/usdc/{relative_path.as_posix()}"
            glb_key = f"{prefix.rstrip('/')}/glb/{glb_relative_path.as_posix()}"
            model_key = build_model_key(category, model_name)

            dims = get_usdc_dimensions(usdc_path, required=not dry_run)
            if dims is None:
                continue
            width, depth, height = dims

            if dry_run:
                print(f"  [DRY RUN] {usdc_path} -> {glb_path} -> s3://{bucket}/{glb_key}")
            else:
                print(f"  [CONVERT] {relative_path}")
                error = convert_usdc_to_glb(
                    usd2gltf_bin,
                    usdc_path,
                    glb_path,
                    blender_bin=blender_bin,
                    fallback_converter=fallback_converter,
                    force=force_convert,
                )
                if error is not None:
                    last_line = error.splitlines()[-1] if error else "unknown error"
                    print(f"  [FAIL] {relative_path}: {last_line}")
                    failed_items.append(
                        {
                            "source": relative_path.as_posix(),
                            "glbKey": glb_key,
                            "error": error,
                        }
                    )
                    if fail_fast:
                        raise RuntimeError(f"Failed to convert {relative_path}:\n{error}")
                    continue

            usdc_uri = build_s3_uri(bucket, usdc_key)
            glb_uri = build_s3_uri(bucket, glb_key)

            if s3_client is not None:
                upload_file(s3_client, bucket, usdc_key, usdc_path)
                upload_file(s3_client, bucket, glb_key, glb_path)

            manifest_items.append(
                {
                    "modelKey": model_key,
                    "category": category,
                    "variant": model_name,
                    "name": usdc_path.stem,
                    "source": relative_path.as_posix(),
                    "usdcKey": usdc_key,
                    "glbKey": glb_key,
                    "width": width,
                    "depth": depth,
                    "height": height,
                }
            )

            if db is not None:
                upsert_furniture_model(
                    db,
                    model_key=model_key,
                    category=category,
                    display_name=usdc_path.stem,
                    usdc_uri=usdc_uri,
                    glb_uri=glb_uri,
                    width=width,
                    depth=depth,
                    height=height,
                )

        manifest = {
            "version": prefix.rstrip("/").split("/")[-1],
            "items": manifest_items,
            "failedItems": failed_items,
        }
        manifest_path = work_dir / "manifest.json"

        if dry_run:
            print(f"Dry run complete. Items={len(manifest_items)}")
            return

        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        if s3_client is not None:
            manifest_key = f"{prefix.rstrip('/')}/manifest.json"
            upload_file(s3_client, bucket, manifest_key, manifest_path)

        if db is not None:
            db.commit()

        print(f"Catalog prepare complete. Items={len(manifest_items)}, Failed={len(failed_items)}")
        print(f"Manifest: {manifest_path}")
    except Exception:
        if db is not None:
            db.rollback()
        raise
    finally:
        if db is not None:
            db.close()


def main() -> None:
    load_env_file(PROJECT_ROOT / ".env")
    args = parse_args()
    prepare_catalog(
        catalog_root=Path(args.catalog_root),
        bucket=args.bucket,
        prefix=args.prefix,
        work_dir=Path(args.work_dir),
        region=args.region,
        usd2gltf_bin=args.usd2gltf,
        blender_bin=args.blender,
        fallback_converter=args.fallback_converter,
        force_convert=args.force_convert,
        skip_upload=args.skip_upload,
        skip_db=args.skip_db,
        dry_run=args.dry_run,
        fail_fast=args.fail_fast,
    )


if __name__ == "__main__":
    main()
