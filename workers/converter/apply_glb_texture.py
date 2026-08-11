from __future__ import annotations

import argparse
import json
import mimetypes
import os
import struct
import tempfile
from pathlib import Path


GLB_MAGIC = 0x46546C67
GLB_VERSION = 2
GLB_JSON_CHUNK_TYPE = 0x4E4F534A
GLB_BIN_CHUNK_TYPE = 0x004E4942


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Embed a texture image into a GLB and apply it to all materials.")
    parser.add_argument("--input-s3-key", default=os.getenv("INPUT_S3_KEY", ""))
    parser.add_argument("--texture-s3-key", default=os.getenv("TEXTURE_S3_KEY", ""))
    parser.add_argument("--output-s3-key", default=os.getenv("OUTPUT_S3_KEY", ""))
    parser.add_argument("--bucket", default=os.getenv("S3_BUCKET_NAME", ""))
    parser.add_argument("--input-file", default=os.getenv("INPUT_FILE", ""))
    parser.add_argument("--texture-file", default=os.getenv("TEXTURE_FILE", ""))
    parser.add_argument("--output-file", default=os.getenv("OUTPUT_FILE", ""))
    parser.add_argument("--skip-upload", action="store_true")
    return parser.parse_args()


def require(value: str, name: str) -> str:
    if not value:
        raise ValueError(f"Missing required value: {name}")
    return value


def _pad_bytes(data: bytes, multiple: int, pad: bytes) -> bytes:
    padding = (-len(data)) % multiple
    return data + pad * padding


def _read_glb(path: str) -> tuple[dict, bytes]:
    data = Path(path).read_bytes()
    if len(data) < 20:
        raise ValueError(f"GLB is too small: {path}")

    magic, version, declared_length = struct.unpack_from("<III", data, 0)
    if magic != GLB_MAGIC or version != GLB_VERSION:
        raise ValueError(f"Unsupported GLB header: {path}")
    if declared_length != len(data):
        raise ValueError(f"GLB length mismatch: {path}")

    offset = 12
    json_chunk: bytes | None = None
    bin_chunk = b""
    while offset + 8 <= len(data):
        chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        chunk = data[offset : offset + chunk_length]
        offset += chunk_length
        if chunk_type == GLB_JSON_CHUNK_TYPE:
            json_chunk = chunk
        elif chunk_type == GLB_BIN_CHUNK_TYPE:
            bin_chunk = chunk

    if json_chunk is None:
        raise ValueError(f"GLB missing JSON chunk: {path}")

    gltf = json.loads(json_chunk.decode("utf-8").rstrip("\x00 \t\r\n"))
    return gltf, bin_chunk.rstrip(b"\x00")


def _write_glb(path: str, gltf: dict, bin_chunk: bytes) -> None:
    json_bytes = json.dumps(gltf, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    json_padded = _pad_bytes(json_bytes, 4, b" ")
    bin_padded = _pad_bytes(bin_chunk, 4, b"\x00")

    total_length = 12 + 8 + len(json_padded)
    chunks = [
        struct.pack("<II", len(json_padded), GLB_JSON_CHUNK_TYPE) + json_padded,
    ]
    if bin_padded:
        chunks.append(struct.pack("<II", len(bin_padded), GLB_BIN_CHUNK_TYPE) + bin_padded)
        total_length += 8 + len(bin_padded)

    header = struct.pack("<III", GLB_MAGIC, GLB_VERSION, total_length)
    Path(path).write_bytes(header + b"".join(chunks))


def _mime_type(texture_file: str) -> str:
    guessed, _encoding = mimetypes.guess_type(texture_file)
    if guessed in {"image/jpeg", "image/png", "image/webp"}:
        return guessed
    suffix = Path(texture_file).suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".png":
        return "image/png"
    return "image/jpeg"


def apply_texture_to_glb(input_file: str, texture_file: str, output_file: str) -> None:
    gltf, bin_chunk = _read_glb(input_file)
    texture_bytes = Path(texture_file).read_bytes()
    if not texture_bytes:
        raise ValueError(f"Texture file is empty: {texture_file}")

    texture_offset = len(_pad_bytes(bin_chunk, 4, b"\x00"))
    bin_chunk = _pad_bytes(bin_chunk, 4, b"\x00") + texture_bytes

    buffers = gltf.setdefault("buffers", [{}])
    if not buffers:
        buffers.append({})
    buffers[0]["byteLength"] = len(bin_chunk)

    buffer_views = gltf.setdefault("bufferViews", [])
    image_buffer_view_index = len(buffer_views)
    buffer_views.append(
        {
            "buffer": 0,
            "byteOffset": texture_offset,
            "byteLength": len(texture_bytes),
        }
    )

    images = gltf.setdefault("images", [])
    image_index = len(images)
    images.append(
        {
            "name": Path(texture_file).stem,
            "bufferView": image_buffer_view_index,
            "mimeType": _mime_type(texture_file),
        }
    )

    textures = gltf.setdefault("textures", [])
    texture_index = len(textures)
    textures.append({"source": image_index})

    materials = gltf.setdefault("materials", [])
    if not materials:
        materials.append({"name": "TexturedMaterial"})
    for material in materials:
        pbr = material.setdefault("pbrMetallicRoughness", {})
        pbr["baseColorTexture"] = {"index": texture_index}
        pbr.setdefault("baseColorFactor", [1, 1, 1, 1])

    _write_glb(output_file, gltf, bin_chunk)


def main() -> int:
    args = parse_args()

    if args.input_file and args.texture_file and args.output_file:
        input_file = args.input_file
        texture_file = args.texture_file
        output_file = args.output_file
        apply_texture_to_glb(input_file, texture_file, output_file)
    else:
        import boto3

        bucket = require(args.bucket, "bucket")
        input_key = require(args.input_s3_key, "input_s3_key")
        texture_key = require(args.texture_s3_key, "texture_s3_key")
        output_key = require(args.output_s3_key, "output_s3_key")
        s3 = boto3.client("s3")

        with tempfile.TemporaryDirectory() as tmpdir:
            input_file = os.path.join(tmpdir, Path(input_key).name)
            texture_file = os.path.join(tmpdir, Path(texture_key).name)
            output_file = os.path.join(tmpdir, Path(output_key).name)

            print(f"Downloading base GLB {input_key} to {input_file}...")
            s3.download_file(bucket, input_key, input_file)
            print(f"Downloading texture {texture_key} to {texture_file}...")
            s3.download_file(bucket, texture_key, texture_file)

            apply_texture_to_glb(input_file, texture_file, output_file)

            if not args.skip_upload:
                print(f"Uploading textured GLB {output_file} to {output_key}...")
                s3.upload_file(
                    output_file,
                    bucket,
                    output_key,
                    ExtraArgs={"ContentType": "model/gltf-binary"},
                )

    result = {
        "bucket": args.bucket,
        "converter": "apply_glb_texture",
        "input_s3_key": args.input_s3_key,
        "texture_s3_key": args.texture_s3_key,
        "output_s3_key": args.output_s3_key,
        "input_file": input_file,
        "texture_file": texture_file,
        "output_file": output_file,
        "uploaded": not args.skip_upload and bool(args.output_s3_key),
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
