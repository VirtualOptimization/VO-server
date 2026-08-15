from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path
import subprocess

from build_room_shell_glb import build_room_shell_glb


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run RoomPlan shell GLB conversion.")
    parser.add_argument("--input-s3-key", default=os.getenv("INPUT_S3_KEY", ""))
    parser.add_argument("--room-data-s3-key", default=os.getenv("ROOM_DATA_S3_KEY", ""))
    parser.add_argument("--output-s3-key", default=os.getenv("OUTPUT_S3_KEY", ""))
    parser.add_argument("--bucket", default=os.getenv("S3_BUCKET_NAME", ""))
    parser.add_argument("--input-file", default=os.getenv("INPUT_FILE", ""))
    parser.add_argument("--room-data-file", default=os.getenv("ROOM_DATA_FILE", ""))
    parser.add_argument("--output-file", default=os.getenv("OUTPUT_FILE", ""))
    parser.add_argument("--wall-thickness", type=float, default=float(os.getenv("WALL_THICKNESS", "0.08")))
    parser.add_argument("--floor-thickness", type=float, default=float(os.getenv("FLOOR_THICKNESS", "0.04")))
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
    
    # systemd's PATH does not include `.venv/bin`, even though this script is
    # launched by the virtual-environment Python. Resolve the console script
    # beside that interpreter instead of relying on a global `usd2gltf`.
    usd2gltf_bin = Path(sys.executable).with_name("usd2gltf")
    if not usd2gltf_bin.is_file():
        raise RuntimeError(
            f"usd2gltf is not installed next to the active Python: {usd2gltf_bin}"
        )
    command = [
        str(usd2gltf_bin),
        "-i", input_file,
        "-o", output_file
    ]
    subprocess.run(command, check=True)
    
    print("--- USDZ -> GLB conversion done ---")


def normalize_usd_input_extension(input_file: str) -> str:
    """Correct an old `.usdc` key when the uploaded bytes are a USDZ archive."""
    path = Path(input_file)
    if path.suffix.lower() == ".usdz" or not zipfile.is_zipfile(path):
        return input_file
    corrected = path.with_suffix(".usdz")
    path.rename(corrected)
    print(f"Detected USDZ archive; using normalized input filename: {corrected}")
    return str(corrected)


def run_room_data_to_glb(
    room_data_file: str,
    output_file: str,
    *,
    wall_thickness: float,
    floor_thickness: float,
) -> None:
    print("--- RoomPlan JSON -> shell GLB conversion start ---")
    print(f"Room data file: {room_data_file}")
    print(f"Output file: {output_file}")
    print(f"Wall thickness: {wall_thickness}")
    print(f"Floor thickness: {floor_thickness}")

    build_room_shell_glb(
        room_data_file,
        output_file,
        wall_thickness=wall_thickness,
        floor_thickness=floor_thickness,
    )

    print("--- RoomPlan JSON -> shell GLB conversion done ---")


def main() -> int:
    args = parse_args()
    converter = "room_data_json" if args.room_data_file or args.room_data_s3_key else "usd2gltf"

    if args.output_file and (args.room_data_file or args.input_file):
        bucket = args.bucket
        input_file = args.input_file
        room_data_file = args.room_data_file
        output_file = args.output_file

        if room_data_file:
            run_room_data_to_glb(
                room_data_file,
                output_file,
                wall_thickness=args.wall_thickness,
                floor_thickness=args.floor_thickness,
            )
        else:
            input_file = normalize_usd_input_extension(input_file)
            run_usd2gltf(input_file, output_file)
    else:
        import boto3

        bucket = require(args.bucket, "bucket")
        s3 = boto3.client("s3")
        input_key = args.input_s3_key
        room_data_key = args.room_data_s3_key
        output_key = require(args.output_s3_key, "output_s3_key")
        with tempfile.TemporaryDirectory() as tmpdir:
            input_file = os.path.join(tmpdir, Path(input_key).name) if input_key else ""
            room_data_file = os.path.join(tmpdir, Path(room_data_key).name) if room_data_key else ""
            output_file = os.path.join(tmpdir, Path(output_key).name)

            if room_data_key:
                print(f"Downloading {room_data_key} to {room_data_file}...")
                s3.download_file(bucket, room_data_key, room_data_file)
                run_room_data_to_glb(
                    room_data_file,
                    output_file,
                    wall_thickness=args.wall_thickness,
                    floor_thickness=args.floor_thickness,
                )
            else:
                input_key = require(input_key, "input_s3_key")
                print(f"Downloading {input_key} to {input_file}...")
                s3.download_file(bucket, input_key, input_file)
                input_file = normalize_usd_input_extension(input_file)
                run_usd2gltf(input_file, output_file)

            if not args.skip_upload:
                print(f"Uploading {output_file} to {output_key}...")
                s3.upload_file(output_file, bucket, output_key)

    result = {
        "bucket": bucket,
        "converter": converter,
        "input_s3_key": args.input_s3_key,
        "room_data_s3_key": args.room_data_s3_key,
        "output_s3_key": args.output_s3_key,
        "input_file": input_file,
        "room_data_file": room_data_file,
        "output_file": output_file,
        "uploaded": not args.skip_upload and bool(args.output_s3_key),
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
