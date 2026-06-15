from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Any

import numpy as np


DEFAULT_WALL_THICKNESS = 0.08
DEFAULT_FLOOR_THICKNESS = 0.04
MIN_SEGMENT_SIZE = 0.02


def _round6(value: float) -> float:
    return round(float(value), 6)


def _rotate_xz(x: float, z: float, angle: float) -> tuple[float, float]:
    c, s = math.cos(-angle), math.sin(-angle)
    return (x * c - z * s), (x * s + z * c)


def _floor_normalization_context(room_data: dict[str, Any]) -> tuple[float, float, float, float]:
    floors = room_data.get("floors", [])
    if not floors:
        raise ValueError("RoomPlan payload must include at least one floor.")

    floor = floors[0]
    floor_transform = floor["transform"]
    floor_theta = math.atan2(floor_transform[0][2], floor_transform[0][0])
    floor_y = float(floor["center"][1])

    points: list[tuple[float, float]] = []
    for floor_item in floors:
        center = floor_item["center"]
        dimensions = floor_item["dimensions"]
        transform = floor_item["transform"]
        axis_x = np.array([transform[0][0], transform[0][2]], dtype=float)
        axis_z = np.array([transform[2][0], transform[2][2]], dtype=float)
        center_xz = np.array([center[0], center[2]], dtype=float)
        for sx in (-1.0, 1.0):
            for sz in (-1.0, 1.0):
                point = center_xz + axis_x * float(dimensions[0]) * 0.5 * sx + axis_z * float(dimensions[1]) * 0.5 * sz
                points.append(_rotate_xz(float(point[0]), float(point[1]), floor_theta))

    arr = np.array(points, dtype=float)
    min_x, min_z = np.min(arr, axis=0)
    return floor_theta, floor_y, float(min_x), float(min_z)


def _normalize_point(point: list[float], floor_theta: float, floor_y: float, min_x: float, min_z: float) -> list[float]:
    rx, rz = _rotate_xz(float(point[0]), float(point[2]), floor_theta)
    return [_round6(rx - min_x), _round6(float(point[1]) - floor_y), _round6(rz - min_z)]


def _normalize_vector(vector: list[float], floor_theta: float) -> list[float]:
    rx, rz = _rotate_xz(float(vector[0]), float(vector[2]), floor_theta)
    return [_round6(rx), _round6(float(vector[1])), _round6(rz)]


def normalize_roomplan_for_unity(room_data: dict[str, Any]) -> dict[str, Any]:
    floor_theta, floor_y, min_x, min_z = _floor_normalization_context(room_data)
    normalized = json.loads(json.dumps(room_data))
    normalized["coordinateSystem"] = "Unity normalized RoomPlan (Y-up, meters, floor aligned to +X/+Z)"

    for collection_name in ("floors", "walls", "doors", "windows", "objects"):
        for item in normalized.get(collection_name, []):
            if item.get("center"):
                item["center"] = _normalize_point(item["center"], floor_theta, floor_y, min_x, min_z)
            if item.get("transform") and len(item["transform"]) >= 4:
                for row in range(3):
                    if item["transform"][row] and len(item["transform"][row]) >= 3:
                        vector = _normalize_vector(item["transform"][row], floor_theta)
                        item["transform"][row][0] = vector[0]
                        item["transform"][row][1] = vector[1]
                        item["transform"][row][2] = vector[2]
                item["transform"][3][0:3] = item["center"]
            if item.get("obbVertices"):
                item["obbVertices"] = [
                    _normalize_point(vertex, floor_theta, floor_y, min_x, min_z)
                    for vertex in item["obbVertices"]
                ]
            for key in ("frontVector", "backVector", "leftVector", "rightVector", "upVector"):
                if item.get(key):
                    item[key] = _normalize_vector(item[key], floor_theta)

    normalized["unityNormalization"] = {
        "floorThetaRadians": _round6(floor_theta),
        "floorY": _round6(floor_y),
        "minX": _round6(min_x),
        "minZ": _round6(min_z),
    }
    return normalized


def _as_vec3(value: list[float]) -> np.ndarray:
    if len(value) < 3:
        raise ValueError(f"Expected 3D vector, got: {value}")
    return np.array(value[:3], dtype=float)


def _normalize(value: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(value))
    if norm < 1e-9:
        raise ValueError("Cannot normalize a zero-length vector.")
    return value / norm


def _matrix_axes(transform: list[list[float]]) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    if len(transform) < 4:
        raise ValueError(f"Expected a 4x4 transform, got: {transform}")

    axis_x = _normalize(_as_vec3(transform[0]))
    axis_y = _normalize(_as_vec3(transform[1]))
    axis_z = _normalize(_as_vec3(transform[2]))
    center = _as_vec3(transform[3])
    return axis_x, axis_y, axis_z, center


def _box_vertices(
    center: np.ndarray,
    axes: tuple[np.ndarray, np.ndarray, np.ndarray],
    extents: tuple[float, float, float],
) -> list[list[float]]:
    hx, hy, hz = (max(float(value), MIN_SEGMENT_SIZE) / 2.0 for value in extents)
    axis_x, axis_y, axis_z = axes
    vertices: list[list[float]] = []
    for sx, sy, sz in (
        (-1, -1, -1),
        (1, -1, -1),
        (1, 1, -1),
        (-1, 1, -1),
        (-1, -1, 1),
        (1, -1, 1),
        (1, 1, 1),
        (-1, 1, 1),
    ):
        vertex = center + axis_x * hx * sx + axis_y * hy * sy + axis_z * hz * sz
        vertices.append(vertex.tolist())
    return vertices


BOX_FACES = [
    (0, 1, 2),
    (0, 2, 3),
    (4, 6, 5),
    (4, 7, 6),
    (0, 4, 5),
    (0, 5, 1),
    (1, 5, 6),
    (1, 6, 2),
    (2, 6, 7),
    (2, 7, 3),
    (3, 7, 4),
    (3, 4, 0),
]


class MeshBuilder:
    def __init__(self) -> None:
        self.vertices: list[list[float]] = []
        self.faces: list[tuple[int, int, int]] = []

    def add_box(
        self,
        center: np.ndarray,
        axes: tuple[np.ndarray, np.ndarray, np.ndarray],
        extents: tuple[float, float, float],
    ) -> None:
        if any(float(value) < MIN_SEGMENT_SIZE for value in extents):
            return
        base = len(self.vertices)
        self.vertices.extend(_box_vertices(center, axes, extents))
        self.faces.extend((base + a, base + b, base + c) for a, b, c in BOX_FACES)

    def to_mesh(self) -> tuple[np.ndarray, np.ndarray]:
        if not self.vertices or not self.faces:
            raise ValueError("Room shell mesh is empty.")

        return (
            np.array(self.vertices, dtype=np.float32),
            np.array(self.faces, dtype=np.uint32),
        )


def _element_dimensions(element: dict[str, Any], fallback_depth: float) -> tuple[float, float, float]:
    dimensions = [float(value) for value in element.get("dimensions", [])]
    if len(dimensions) == 2:
        return dimensions[0], dimensions[1], fallback_depth
    if len(dimensions) >= 3:
        return dimensions[0], dimensions[1], dimensions[2]
    raise ValueError(f"Element is missing dimensions: {element}")


def _opening_on_wall(wall: dict[str, Any], opening: dict[str, Any], wall_thickness: float) -> dict[str, float] | None:
    if not opening.get("center") or not opening.get("dimensions") or not opening.get("transform"):
        return None

    wall_x, wall_y, wall_z, wall_center = _matrix_axes(wall["transform"])
    opening_x, _, opening_z, opening_center = _matrix_axes(opening["transform"])

    # Same wall plane, allowing opposite normals.
    if abs(float(np.dot(wall_z, opening_z))) < 0.85:
        return None

    relative = opening_center - wall_center
    plane_distance = abs(float(np.dot(relative, wall_z)))
    if plane_distance > max(wall_thickness * 2.5, 0.25):
        return None

    wall_length, wall_height, _ = _element_dimensions(wall, wall_thickness)
    opening_width, opening_height, _ = _element_dimensions(opening, wall_thickness)
    opening_center_x = float(np.dot(relative, wall_x))
    opening_center_y = float(np.dot(relative, wall_y))

    if abs(float(np.dot(wall_x, opening_x))) < 0.5:
        return None

    min_x = max(-wall_length / 2.0, opening_center_x - opening_width / 2.0)
    max_x = min(wall_length / 2.0, opening_center_x + opening_width / 2.0)
    min_y = max(-wall_height / 2.0, opening_center_y - opening_height / 2.0)
    max_y = min(wall_height / 2.0, opening_center_y + opening_height / 2.0)

    if max_x - min_x < MIN_SEGMENT_SIZE or max_y - min_y < MIN_SEGMENT_SIZE:
        return None

    return {"min_x": min_x, "max_x": max_x, "min_y": min_y, "max_y": max_y}


def _wall_segments(wall: dict[str, Any], openings: list[dict[str, float]]) -> list[tuple[float, float, float, float]]:
    wall_length, wall_height, _ = _element_dimensions(wall, DEFAULT_WALL_THICKNESS)
    x_breaks = {-wall_length / 2.0, wall_length / 2.0}
    y_breaks = {-wall_height / 2.0, wall_height / 2.0}

    for opening in openings:
        x_breaks.add(opening["min_x"])
        x_breaks.add(opening["max_x"])
        y_breaks.add(opening["min_y"])
        y_breaks.add(opening["max_y"])

    segments: list[tuple[float, float, float, float]] = []
    xs = sorted(x_breaks)
    ys = sorted(y_breaks)
    for x0, x1 in zip(xs, xs[1:]):
        if x1 - x0 < MIN_SEGMENT_SIZE:
            continue
        for y0, y1 in zip(ys, ys[1:]):
            if y1 - y0 < MIN_SEGMENT_SIZE:
                continue
            cx = (x0 + x1) / 2.0
            cy = (y0 + y1) / 2.0
            inside_opening = any(
                opening["min_x"] <= cx <= opening["max_x"]
                and opening["min_y"] <= cy <= opening["max_y"]
                for opening in openings
            )
            if not inside_opening:
                segments.append((x0, x1, y0, y1))

    return segments


def _add_floor(builder: MeshBuilder, floor: dict[str, Any], floor_thickness: float) -> None:
    axis_x, axis_z, axis_y, center = _matrix_axes(floor["transform"])
    dimensions = [float(value) for value in floor.get("dimensions", [])]
    if len(dimensions) < 2:
        raise ValueError(f"Floor is missing dimensions: {floor}")

    # RoomPlan floors use row 0 and row 1 as the floor plane axes, with row 2 as up.
    center = center - axis_y * (floor_thickness / 2.0)
    builder.add_box(
        center=center,
        axes=(axis_x, axis_z, axis_y),
        extents=(dimensions[0], dimensions[1], floor_thickness),
    )


def _add_wall(
    builder: MeshBuilder,
    wall: dict[str, Any],
    all_openings: list[dict[str, Any]],
    wall_thickness: float,
) -> None:
    wall_x, wall_y, wall_z, wall_center = _matrix_axes(wall["transform"])
    wall_length, wall_height, wall_depth = _element_dimensions(wall, wall_thickness)
    depth = max(min(wall_depth, wall_thickness), wall_thickness)
    openings = [
        opening
        for opening in (_opening_on_wall(wall, item, wall_thickness) for item in all_openings)
        if opening is not None
    ]

    if not openings:
        builder.add_box(
            center=wall_center,
            axes=(wall_x, wall_y, wall_z),
            extents=(wall_length, wall_height, depth),
        )
        return

    for x0, x1, y0, y1 in _wall_segments(wall, openings):
        center = wall_center + wall_x * ((x0 + x1) / 2.0) + wall_y * ((y0 + y1) / 2.0)
        builder.add_box(
            center=center,
            axes=(wall_x, wall_y, wall_z),
            extents=(x1 - x0, y1 - y0, depth),
        )


def build_room_shell_mesh(
    room_data: dict[str, Any],
    *,
    wall_thickness: float = DEFAULT_WALL_THICKNESS,
    floor_thickness: float = DEFAULT_FLOOR_THICKNESS,
) -> tuple[np.ndarray, np.ndarray]:
    builder = MeshBuilder()

    for floor in room_data.get("floors", []):
        _add_floor(builder, floor, floor_thickness)

    openings = list(room_data.get("doors", [])) + list(room_data.get("windows", []))
    for wall in room_data.get("walls", []):
        _add_wall(builder, wall, openings, wall_thickness)

    mesh = builder.to_mesh()
    vertices, _ = mesh
    if not math.isfinite(float(vertices.sum())):
        raise ValueError("Generated room shell mesh contains invalid coordinates.")
    return mesh


def _padded(data: bytes, padding_byte: bytes) -> bytes:
    padding = (4 - (len(data) % 4)) % 4
    return data + (padding_byte * padding)


def _write_glb(vertices: np.ndarray, faces: np.ndarray, output_path: Path) -> None:
    position_bytes = vertices.astype("<f4", copy=False).tobytes()
    index_bytes = faces.reshape(-1).astype("<u4", copy=False).tobytes()
    bin_blob = _padded(position_bytes + index_bytes, b"\x00")

    index_offset = len(position_bytes)
    mins = vertices.min(axis=0).astype(float).tolist()
    maxs = vertices.max(axis=0).astype(float).tolist()

    gltf = {
        "asset": {"version": "2.0", "generator": "VO room_data shell builder"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "RoomShell"}],
        "meshes": [
            {
                "name": "RoomShellMesh",
                "primitives": [
                    {
                        "attributes": {"POSITION": 0},
                        "indices": 1,
                        "material": 0,
                        "mode": 4,
                    }
                ],
            }
        ],
        "materials": [
            {
                "name": "RoomShellMaterial",
                "doubleSided": True,
                "pbrMetallicRoughness": {
                    "baseColorFactor": [0.82, 0.84, 0.86, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 0.7,
                },
            }
        ],
        "buffers": [{"byteLength": len(bin_blob)}],
        "bufferViews": [
            {
                "buffer": 0,
                "byteOffset": 0,
                "byteLength": len(position_bytes),
                "byteStride": 12,
                "target": 34962,
            },
            {
                "buffer": 0,
                "byteOffset": index_offset,
                "byteLength": len(index_bytes),
                "target": 34963,
            },
        ],
        "accessors": [
            {
                "bufferView": 0,
                "byteOffset": 0,
                "componentType": 5126,
                "count": int(vertices.shape[0]),
                "type": "VEC3",
                "min": mins,
                "max": maxs,
            },
            {
                "bufferView": 1,
                "byteOffset": 0,
                "componentType": 5125,
                "count": int(faces.size),
                "type": "SCALAR",
            },
        ],
    }

    json_blob = _padded(json.dumps(gltf, separators=(",", ":")).encode("utf-8"), b" ")
    total_length = 12 + 8 + len(json_blob) + 8 + len(bin_blob)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as file:
        file.write(struct.pack("<III", 0x46546C67, 2, total_length))
        file.write(struct.pack("<I4s", len(json_blob), b"JSON"))
        file.write(json_blob)
        file.write(struct.pack("<I4s", len(bin_blob), b"BIN\x00"))
        file.write(bin_blob)


def build_room_shell_glb(
    input_json_path: str | Path,
    output_glb_path: str | Path,
    *,
    wall_thickness: float = DEFAULT_WALL_THICKNESS,
    floor_thickness: float = DEFAULT_FLOOR_THICKNESS,
) -> None:
    input_path = Path(input_json_path)
    output_path = Path(output_glb_path)
    room_data = json.loads(input_path.read_text(encoding="utf-8"))
    room_data = normalize_roomplan_for_unity(room_data)
    vertices, faces = build_room_shell_mesh(
        room_data,
        wall_thickness=wall_thickness,
        floor_thickness=floor_thickness,
    )
    _write_glb(vertices, faces, output_path)
