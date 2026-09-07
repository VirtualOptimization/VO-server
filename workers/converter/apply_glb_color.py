"""Apply a solid base color to the materials in a GLB 2.0 file."""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path


GLB_JSON_CHUNK_TYPE = 0x4E4F534A
GLB_BIN_CHUNK_TYPE = 0x004E4942


def parse_hex_color(value: str) -> tuple[float, float, float]:
    normalized = value.strip().removeprefix("#")
    if len(normalized) != 6:
        raise ValueError("color must use the #RRGGBB format")

    try:
        channels = tuple(int(normalized[index : index + 2], 16) / 255 for index in (0, 2, 4))
    except ValueError as exc:
        raise ValueError("color must use the #RRGGBB format") from exc

    return channels  # type: ignore[return-value]


def srgb_to_linear(value: float) -> float:
    if value <= 0.04045:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def read_glb(path: Path) -> tuple[int, list[tuple[int, bytes]]]:
    data = path.read_bytes()
    if len(data) < 20:
        raise ValueError("file is too small to be a GLB")

    magic, version, declared_length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2:
        raise ValueError("file is not GLB v2")
    if declared_length != len(data):
        raise ValueError("GLB length header does not match the file size")

    chunks: list[tuple[int, bytes]] = []
    offset = 12
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("GLB contains a truncated chunk header")
        chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        chunk_end = offset + chunk_length
        if chunk_end > len(data):
            raise ValueError("GLB contains a truncated chunk")
        chunks.append((chunk_type, data[offset:chunk_end]))
        offset = chunk_end

    return version, chunks


def write_glb(path: Path, version: int, chunks: list[tuple[int, bytes]]) -> None:
    body = bytearray()
    for chunk_type, chunk in chunks:
        padding = b"\x00" if chunk_type == GLB_BIN_CHUNK_TYPE else b" "
        padded = chunk + (padding * ((-len(chunk)) % 4))
        body.extend(struct.pack("<II", len(padded), chunk_type))
        body.extend(padded)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<4sII", b"glTF", version, 12 + len(body)) + body)


def apply_color(
    input_path: Path,
    output_path: Path,
    color: str,
    *,
    keep_base_color_texture: bool = False,
) -> int:
    version, chunks = read_glb(input_path)
    json_index = next(
        (index for index, (chunk_type, _) in enumerate(chunks) if chunk_type == GLB_JSON_CHUNK_TYPE),
        None,
    )
    if json_index is None:
        raise ValueError("GLB does not contain a JSON chunk")

    gltf = json.loads(chunks[json_index][1].decode("utf-8").rstrip("\x00 \t\r\n"))
    materials = gltf.get("materials") or []
    if not materials:
        raise ValueError("GLB does not contain any materials")

    linear_rgb = [srgb_to_linear(channel) for channel in parse_hex_color(color)]
    for material in materials:
        pbr = material.setdefault("pbrMetallicRoughness", {})
        existing_factor = pbr.get("baseColorFactor") or [1.0, 1.0, 1.0, 1.0]
        alpha = existing_factor[3] if len(existing_factor) >= 4 else 1.0
        pbr["baseColorFactor"] = [*linear_rgb, alpha]
        if not keep_base_color_texture:
            pbr.pop("baseColorTexture", None)

    json_bytes = json.dumps(gltf, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    chunks[json_index] = (GLB_JSON_CHUNK_TYPE, json_bytes)
    write_glb(output_path, version, chunks)
    return len(materials)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_file", type=Path)
    parser.add_argument("output_file", type=Path)
    parser.add_argument("--color", required=True, help="Color in #RRGGBB format.")
    parser.add_argument(
        "--keep-base-color-texture",
        action="store_true",
        help="Tint an existing base color texture instead of replacing it with a solid color.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    count = apply_color(
        args.input_file,
        args.output_file,
        args.color,
        keep_base_color_texture=args.keep_base_color_texture,
    )
    print(json.dumps({"output_file": str(args.output_file), "color": args.color, "materials": count}))


if __name__ == "__main__":
    main()
