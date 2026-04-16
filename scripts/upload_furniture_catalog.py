"""Upload RoomPlan furniture catalog assets to S3 while preserving folder structure.

Example:
    .venv/bin/python scripts/upload_furniture_catalog.py \
        --catalog-root /path/to/RoomPlanCatalog.bundle/Resources \
        --bucket aja-vo-server-bucket \
        --prefix models
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

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
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload RoomPlan catalog .usdc files to S3 with stable keys."
    )
    parser.add_argument(
        "--catalog-root",
        default=os.getenv("MODEL_CATALOG_ROOT"),
        help="Root directory containing category/model folders.",
    )
    parser.add_argument(
        "--bucket",
        default=os.getenv("MODEL_CATALOG_S3_BUCKET") or os.getenv("S3_BUCKET_NAME"),
        help="Destination S3 bucket name.",
    )
    parser.add_argument(
        "--prefix",
        default=os.getenv("MODEL_CATALOG_S3_KEY_PREFIX", "models"),
        help="S3 key prefix under the bucket.",
    )
    parser.add_argument(
        "--region",
        default=os.getenv("AWS_REGION", "ap-northeast-2"),
        help="AWS region for the S3 client.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print upload targets without sending files.",
    )
    args = parser.parse_args()

    if not args.catalog_root:
        parser.error("--catalog-root or MODEL_CATALOG_ROOT is required")
    if not args.bucket:
        parser.error("--bucket or MODEL_CATALOG_S3_BUCKET/S3_BUCKET_NAME is required")

    return args


def iter_usdc_files(catalog_root: Path) -> list[Path]:
    return sorted(path for path in catalog_root.rglob("*.usdc") if path.is_file())


def build_s3_key(catalog_root: Path, file_path: Path, prefix: str) -> str:
    relative_path = file_path.relative_to(catalog_root).as_posix()
    return f"{prefix.rstrip('/')}/{relative_path}"


def object_matches_local(s3_client, bucket: str, key: str, file_path: Path) -> bool:
    try:
        response = s3_client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise

    return response.get("ContentLength") == file_path.stat().st_size


def upload_catalog(
    *,
    catalog_root: Path,
    bucket: str,
    prefix: str,
    region: str,
    dry_run: bool,
) -> None:
    s3_client = boto3.client("s3", region_name=region)
    usdc_files = iter_usdc_files(catalog_root)

    if not usdc_files:
        print("No .usdc files found under the catalog root.")
        return

    print(f"Uploading catalog assets to s3://{bucket}/{prefix.rstrip('/')}/ ...")

    uploaded = 0
    skipped = 0

    for file_path in usdc_files:
        key = build_s3_key(catalog_root, file_path, prefix)

        if dry_run:
            print(f"  [DRY RUN] {file_path} -> s3://{bucket}/{key}")
            continue

        if object_matches_local(s3_client, bucket, key, file_path):
            skipped += 1
            print(f"  [SKIP] {file_path.name} already matches s3://{bucket}/{key}")
            continue

        s3_client.upload_file(str(file_path), bucket, key)
        uploaded += 1
        print(f"  [UPLOAD] {file_path.name} -> s3://{bucket}/{key}")

    if dry_run:
        print("Dry run complete. No files were uploaded.")
        return

    print(f"Catalog upload complete. Uploaded={uploaded}, Skipped={skipped}")


def main() -> None:
    load_env_file(Path(".env"))
    args = parse_args()
    upload_catalog(
        catalog_root=Path(args.catalog_root).expanduser().resolve(),
        bucket=args.bucket,
        prefix=args.prefix,
        region=args.region,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
