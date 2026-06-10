from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

import boto3


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run USDZ -> GLB conversion.")
    parser.add_argument("--input-s3-key", default=os.getenv("INPUT_S3_KEY", ""))
    parser.add_argument("--output-s3-key", default=os.getenv("OUTPUT_S3_KEY", ""))
    parser.add_argument("--bucket", default=os.getenv("S3_BUCKET_NAME", ""))
    parser.add_argument("--input-file", default=os.getenv("INPUT_FILE", ""))
    parser.add_argument("--output-file", default=os.getenv("OUTPUT_FILE", ""))
    parser.add_argument("--skip-upload", action="store_true")
    return parser.parse_args()


def require(value: str, name: str) -> str:
    if not value:
        raise ValueError(f"Missing required value: {name}")
    return value


def run_usd2gltf(input_file: str, output_file: str) -> None:
    print(f"--- USDZ -> GLB conversion start ---")
    print(f"Input file: {input_file}")
    print(f"Output file: {output_file}")
    
    command = [
        "usd2gltf",
        "-i", input_file,
        "-o", output_file
    ]
    subprocess.run(command, check=True)
    
    print("--- USDZ -> GLB conversion done ---")


def main() -> int:
    args = parse_args()

    if args.input_file and args.output_file:
        bucket = args.bucket
        input_file = args.input_file
        output_file = args.output_file
    else:
        bucket = require(args.bucket, "bucket")
        s3 = boto3.client("s3")
        input_key = require(args.input_s3_key, "input_s3_key")
        output_key = require(args.output_s3_key, "output_s3_key")
        with tempfile.TemporaryDirectory() as tmpdir:
            input_file = os.path.join(tmpdir, Path(input_key).name)
            output_file = os.path.join(tmpdir, Path(output_key).name)
            
            print(f"Downloading {input_key} to {input_file}...")
            s3.download_file(bucket, input_key, input_file)
            
            run_usd2gltf(input_file, output_file)
            
            if not args.skip_upload:
                print(f"Uploading {output_file} to {output_key}...")
                s3.upload_file(output_file, bucket, output_key)
                
    if args.input_file and args.output_file:
        run_usd2gltf(input_file, output_file)

    result = {
        "bucket": bucket,
        "input_s3_key": args.input_s3_key,
        "output_s3_key": args.output_s3_key,
        "input_file": input_file,
        "output_file": output_file,
        "uploaded": not args.skip_upload and bool(args.output_s3_key),
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
