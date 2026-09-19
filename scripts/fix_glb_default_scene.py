"""Patch GLB files so loaders can instantiate a default scene.

Some converters write valid GLB files with scenes but without the top-level
``scene`` field. glTFast's InstantiateMainSceneAsync expects that default scene,
so catalog models can load as empty GameObjects in Unity. This script adds
``scene: 0`` when a GLB has at least one scene and no default scene.
"""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

GLB_JSON_CHUNK_TYPE = 0x4E4F534A
GLB_BIN_CHUNK_TYPE = 0x004E4942


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        default="tmp/roomplan-catalog/glb",
        help="GLB file or directory to patch.",
    )
    parser.add_argument("--check", action="store_true", help="Check only; do not write files.")
    return parser.parse_args()


def pad_json_chunk(raw: bytes) -> bytes:
    return raw + (b" " * ((-len(raw)) % 4))


def pad_binary_chunk(raw: bytes) -> bytes:
    return raw + (b"\x00" * ((-len(raw)) % 4))


def read_glb_chunks(path: Path) -> tuple[int, list[tuple[int, bytes]]]:
    data = path.read_bytes()
    if len(data) < 20:
        raise ValueError("file is too small to be a GLB")

    magic, version, _length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2:
        raise ValueError("file is not GLB v2")

    chunks: list[tuple[int, bytes]] = []
    offset = 12
    while offset + 8 <= len(data):
        chunk_length, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        chunks.append((chunk_type, data[offset : offset + chunk_length]))
        offset += chunk_length

    return version, chunks


def patch_glb(path: Path, *, check: bool) -> str:
    _version, chunks = read_glb_chunks(path)
    json_index = next(
        (index for index, (chunk_type, _chunk) in enumerate(chunks) if chunk_type == GLB_JSON_CHUNK_TYPE),
        None,
    )
    if json_index is None:
        return "bad:no_json"

    gltf = json.loads(chunks[json_index][1].decode("utf-8").rstrip("\x00 \t\r\n"))
    scenes = gltf.get("scenes") or []
    meshes = gltf.get("meshes") or []

    if not meshes:
        return "bad:no_meshes"
    if not scenes:
        return "bad:no_scenes"
    if gltf.get("scene") is not None:
        return "ok"
    if check:
        return "needs_fix"

    gltf["scene"] = 0
    json_bytes = json.dumps(gltf, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    chunks[json_index] = (GLB_JSON_CHUNK_TYPE, pad_json_chunk(json_bytes))

    rebuilt_chunks = bytearray()
    for chunk_type, chunk in chunks:
        padded_chunk = pad_binary_chunk(chunk) if chunk_type == GLB_BIN_CHUNK_TYPE else chunk
        rebuilt_chunks.extend(struct.pack("<II", len(padded_chunk), chunk_type))
        rebuilt_chunks.extend(padded_chunk)

    total_length = 12 + len(rebuilt_chunks)
    path.write_bytes(struct.pack("<4sII", b"glTF", 2, total_length) + rebuilt_chunks)
    return "fixed"


def iter_glbs(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(root.rglob("*.glb"))


def main() -> None:
    args = parse_args()
    root = Path(args.root)
    counts: dict[str, int] = {}

    for path in iter_glbs(root):
        try:
            status = patch_glb(path, check=args.check)
        except Exception as exc:
            status = f"bad:{exc}"

        counts[status] = counts.get(status, 0) + 1
        if status not in {"ok"}:
            print(f"[{status}] {path}")

    print("Summary:", counts)


if __name__ == "__main__":
    main()
