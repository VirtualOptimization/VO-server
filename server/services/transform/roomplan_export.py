"""Export optimized canonical layout back into a RoomPlan-like JSON payload."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np


def _round6(value: float) -> float:
    return round(float(value), 6)


def _rotate_point(x: float, z: float, angle: float) -> tuple[float, float]:
    c, s = np.cos(-angle), np.sin(-angle)
    return (x * c - z * s), -(x * s + z * c)


def _inverse_rotate_point(rx: float, rz: float, angle: float) -> tuple[float, float]:
    # The transform used in normalization is involutory, so the same matrix inverts it.
    return _rotate_point(rx, rz, angle)


def _extract_floor_context(src: dict[str, Any]) -> tuple[float, float, float, float]:
    floors = src.get("floors", [])
    if not floors:
        raise ValueError("RoomPlan payload must include at least one floor.")

    floor_transform = floors[0]["transform"]
    floor_theta = math.atan2(floor_transform[0][2], floor_transform[0][0])
    floor_y = float(floors[0]["center"][1])

    all_elements = (
        src.get("objects", [])
        + src.get("doors", [])
        + src.get("windows", [])
        + src.get("walls", [])
    )
    if not all_elements:
        raise ValueError("RoomPlan payload must include at least one object, door, or wall.")

    rotated_pts: list[tuple[float, float]] = []
    for element in all_elements:
        obb_vertices = element.get("obbVertices", [])
        if obb_vertices:
            rotated_pts.extend(_rotate_point(vertex[0], vertex[2], floor_theta) for vertex in obb_vertices)
        else:
            center = element["center"]
            rotated_pts.append(_rotate_point(center[0], center[2], floor_theta))

    rotated_pts_np = np.array(rotated_pts, dtype=float)
    min_x, min_z = np.min(rotated_pts_np, axis=0)
    max_x, max_z = np.max(rotated_pts_np, axis=0)
    return floor_theta, floor_y, float(min_x), float(min_z)


def _basis_from_yaw(raw_theta: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    right = np.array([math.cos(raw_theta), 0.0, math.sin(raw_theta)], dtype=float)
    up = np.array([0.0, 1.0, 0.0], dtype=float)
    back = np.array([-math.sin(raw_theta), 0.0, math.cos(raw_theta)], dtype=float)
    return right, up, back


def _build_transform(center: list[float], raw_theta: float) -> list[list[float]]:
    c = math.cos(raw_theta)
    s = math.sin(raw_theta)
    return [
        [_round6(c), 0.0, _round6(s), 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [_round6(-s), 0.0, _round6(c), 0.0],
        [_round6(center[0]), _round6(center[1]), _round6(center[2]), 1.0],
    ]


def _build_rotation(raw_theta: float) -> list[float]:
    return [0.0, _round6(raw_theta), 0.0]


def _build_direction_vectors(raw_theta: float) -> dict[str, list[float]]:
    front = np.array([math.sin(raw_theta), 0.0, -math.cos(raw_theta)], dtype=float)
    back = -front
    left = np.array([front[2], 0.0, -front[0]], dtype=float)
    right = -left
    up = np.array([0.0, 1.0, 0.0], dtype=float)

    return {
        "frontVector": [_round6(v) for v in front],
        "backVector": [_round6(v) for v in back],
        "leftVector": [_round6(v) for v in left],
        "rightVector": [_round6(v) for v in right],
        "upVector": [_round6(v) for v in up],
    }


def _build_obb_vertices(center: list[float], dimensions: list[float], raw_theta: float) -> list[list[float]]:
    hx, hy, hz = dimensions[0] / 2.0, dimensions[1] / 2.0, dimensions[2] / 2.0
    right, up, back = _basis_from_yaw(raw_theta)
    center_v = np.array(center, dtype=float)

    vertices: list[list[float]] = []
    for sx in (1.0, -1.0):
        for sy in (-1.0, 1.0):
            for sz in (1.0, -1.0):
                point = center_v + (right * hx * sx) + (up * hy * sy) + (back * hz * sz)
                vertices.append([_round6(v) for v in point.tolist()])
    return vertices


def export_optimized_layout_to_roomplan(
    raw_roomplan: dict[str, Any],
    optimized_payload: dict[str, Any],
) -> dict[str, Any]:
    """Return a RoomPlan-like payload with optimized object poses applied."""
    result = json.loads(json.dumps(raw_roomplan))
    floor_theta, floor_y, min_x, min_z = _extract_floor_context(raw_roomplan)

    optimized_items = {
        item["id"]: item for item in optimized_payload.get("movable_items", [])
    }

    for obj in result.get("objects", []):
        optimized_item = optimized_items.get(obj.get("identifier"))
        if optimized_item is None:
            continue

        pos = optimized_item.get("optimized_pos") or optimized_item.get("pos")
        rotation_y_deg = float(
            optimized_item.get("optimized_rotation_y_deg", optimized_item.get("rotation_y_deg", 0.0))
        )

        rx = float(pos[0]) + min_x
        rz = float(pos[1]) + min_z
        world_x, world_z = _inverse_rotate_point(rx, rz, floor_theta)

        dimensions = obj["dimensions"]
        center_y = (float(pos[2]) - (float(dimensions[1]) / 2.0)) + floor_y
        new_center = [_round6(world_x), _round6(center_y), _round6(world_z)]

        raw_theta = math.radians(rotation_y_deg) + floor_theta

        obj["center"] = new_center
        obj["rotation"] = _build_rotation(raw_theta)
        obj["transform"] = _build_transform(new_center, raw_theta)
        obj.update(_build_direction_vectors(raw_theta))
        obj["obbVertices"] = _build_obb_vertices(new_center, dimensions, raw_theta)

        if optimized_item.get("source_model_file"):
            obj["modelFileName"] = optimized_item["source_model_file"]
        if optimized_item.get("model_key"):
            obj["model_key"] = optimized_item["model_key"]

    return result


def export_optimized_layout_to_roomplan_file(
    raw_roomplan_path: str | Path,
    optimized_payload_path: str | Path,
    output_path: str | Path,
) -> Path:
    raw_path = Path(raw_roomplan_path).expanduser().resolve()
    optimized_path = Path(optimized_payload_path).expanduser().resolve()
    out_path = Path(output_path).expanduser().resolve()

    raw_roomplan = json.loads(raw_path.read_text(encoding="utf-8"))
    optimized_payload = json.loads(optimized_path.read_text(encoding="utf-8"))
    exported = export_optimized_layout_to_roomplan(raw_roomplan, optimized_payload)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(exported, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path
