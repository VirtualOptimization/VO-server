from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run GLB to USDZ conversion.")
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


def _candidate_commands(input_file: str, output_file: str) -> list[list[str]]:
    commands: list[list[str]] = []

    custom_command = _custom_converter_command(input_file, output_file)
    if custom_command:
        commands.append(custom_command)

    webusd_command = _webusd_converter_command(input_file, output_file)
    if webusd_command:
        commands.append(webusd_command)

    if shutil.which("usd_from_gltf"):
        commands.append(["usd_from_gltf", input_file, output_file])

    if shutil.which("usdzconvert"):
        commands.append(["usdzconvert", input_file, output_file])

    if shutil.which("xcrun") and _xcrun_tool_exists("usdz_converter"):
        commands.append(["xcrun", "usdz_converter", input_file, output_file])

    return commands


def _custom_converter_command(input_file: str, output_file: str) -> list[str] | None:
    template = os.getenv("GLB_TO_USDZ_COMMAND", "").strip()
    if not template:
        return None

    return [
        part.format(input=input_file, output=output_file)
        for part in shlex.split(template)
    ]


def _webusd_converter_command(input_file: str, output_file: str) -> list[str] | None:
    bun = _find_bun()
    if not bun:
        return None

    script_path = Path(__file__).with_name("gltf2usdz_webusd_cli.ts")
    repo_dir = Path(
        os.getenv(
            "GLTF2USDZ_REPO_DIR",
            str(Path(__file__).resolve().parents[3] / "gltf2usdz.online"),
        )
    )
    if not script_path.exists():
        return None
    if not (repo_dir / "src/vendor/webusd/src/index.ts").exists():
        return None

    return [
        bun,
        str(script_path),
        "--input",
        input_file,
        "--output",
        output_file,
        "--repo",
        str(repo_dir),
    ]


def _find_bun() -> str | None:
    configured = os.getenv("BUN_BINARY", "").strip()
    if configured and Path(configured).exists():
        return configured

    found = shutil.which("bun")
    if found:
        return found

    home_bun = Path.home() / ".bun/bin/bun"
    if home_bun.exists():
        return str(home_bun)

    return None


def _xcrun_tool_exists(tool_name: str) -> bool:
    try:
        subprocess.run(
            ["xcrun", "-find", tool_name],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def run_glb_to_usdz(input_file: str, output_file: str) -> None:
    print("--- GLB -> USDZ conversion start ---")
    print(f"Input file: {input_file}")
    print(f"Output file: {output_file}")

    errors: list[str] = []
    for command in _candidate_commands(input_file, output_file):
        try:
            print(f"Trying converter: {' '.join(command)}")
            subprocess.run(command, check=True)
            if Path(output_file).exists():
                print("--- GLB -> USDZ conversion done ---")
                return
            errors.append(f"{command[0]} finished but output file was not created")
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            errors.append(f"{command[0]} failed: {exc}")

    detail = "; ".join(errors) if errors else "No GLB to USDZ converter was found"
    raise RuntimeError(
        f"{detail}. Install Bun with the gltf2usdz.online dependencies, "
        "set GLB_TO_USDZ_COMMAND, or install a converter such as usd_from_gltf, "
        "usdzconvert, or Apple's usdz_converter."
    )


def main() -> int:
    args = parse_args()

    if args.output_file and args.input_file:
        bucket = args.bucket
        input_file = args.input_file
        output_file = args.output_file
        run_glb_to_usdz(input_file, output_file)
    else:
        import boto3

        bucket = require(args.bucket, "bucket")
        input_key = require(args.input_s3_key, "input_s3_key")
        output_key = require(args.output_s3_key, "output_s3_key")
        s3 = boto3.client("s3")

        with tempfile.TemporaryDirectory() as tmpdir:
            input_file = os.path.join(tmpdir, Path(input_key).name)
            output_file = os.path.join(tmpdir, Path(output_key).name)

            print(f"Downloading {input_key} to {input_file}...")
            s3.download_file(bucket, input_key, input_file)
            run_glb_to_usdz(input_file, output_file)

            if not args.skip_upload:
                print(f"Uploading {output_file} to {output_key}...")
                s3.upload_file(
                    output_file,
                    bucket,
                    output_key,
                    ExtraArgs={"ContentType": "model/vnd.usdz+zip"},
                )

    result = {
        "bucket": bucket,
        "converter": "glb_to_usdz",
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
