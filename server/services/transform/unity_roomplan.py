"""Export RoomPlan-like payloads into a Unity-friendly normalized coordinate frame."""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np

from .mappers import infer_catalog_model_key


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


def _attach_model_keys(room_data: dict[str, Any]) -> None:
    for obj in room_data.get("objects", []):
        if obj.get("model_key"):
            continue

        category = obj.get("category")
        if not category:
            continue

        model_key = infer_catalog_model_key(category, obj.get("modelFileName"))
        if model_key:
            obj["model_key"] = model_key


def normalize_roomplan_for_unity(room_data: dict[str, Any]) -> dict[str, Any]:
    """Return a RoomPlan-like payload aligned to a Unity-friendly +X/+Z floor frame."""
    floor_theta, floor_y, min_x, min_z = _floor_normalization_context(room_data)
    normalized = json.loads(json.dumps(room_data))
    normalized["coordinateSystem"] = "Unity normalized RoomPlan (Y-up, meters, floor aligned to +X/+Z)"
    _attach_model_keys(normalized)

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
