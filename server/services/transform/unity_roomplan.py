"""Export RoomPlan-like payloads into a Unity-friendly normalized coordinate frame."""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np

from .mappers import infer_catalog_model_key


def _round6(value: float) -> float:
    return round(float(value), 6)


def _yaw_from_rotation(rotation: list[Any]) -> float | None:
    if not isinstance(rotation, list) or not rotation:
        return None
    if len(rotation) >= 3:
        return float(rotation[1])
    return float(rotation[0])


def _ensure_transform(item: dict[str, Any]) -> list[list[float]]:
    transform = item.get("transform")
    if isinstance(transform, list) and len(transform) >= 4:
        return transform

    center = item.get("center") or [0.0, 0.0, 0.0]
    transform = [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [_round6(center[0]), _round6(center[1]), _round6(center[2]), 1.0],
    ]
    item["transform"] = transform
    return transform


def _sync_unity_obb_vertices(item: dict[str, Any]) -> None:
    center = item.get("center")
    dimensions = item.get("dimensions")
    transform = item.get("transform")
    if not (
        isinstance(center, list) and len(center) >= 3
        and isinstance(dimensions, list) and len(dimensions) >= 3
        and isinstance(transform, list) and len(transform) >= 3
    ):
        return

    hx, hy, hz = float(dimensions[0]) / 2.0, float(dimensions[1]) / 2.0, float(dimensions[2]) / 2.0
    right = [float(value) for value in transform[0][0:3]]
    up = [float(value) for value in transform[1][0:3]]
    back = [float(value) for value in transform[2][0:3]]
    center_v = [float(value) for value in center[0:3]]

    vertices = []
    for sx in (1.0, -1.0):
        for sy in (-1.0, 1.0):
            for sz in (1.0, -1.0):
                vertices.append([
                    _round6(center_v[axis] + right[axis] * hx * sx + up[axis] * hy * sy + back[axis] * hz * sz)
                    for axis in range(3)
                ])
    item["obbVertices"] = vertices


def fix_unity_transforms_from_rotation(room_data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy whose Unity object transforms match rotation[].

    Unity clients may send an updated rotation[] while leaving transform[][] and
    direction vectors stale. The server denormalizes using transform[][], so make
    rotation[] the source of truth before saving or converting to RoomPlan.
    """
    data = json.loads(json.dumps(room_data))
    for item in data.get("objects", []):
        yaw = _yaw_from_rotation(item.get("rotation"))
        if yaw is None:
            continue

        center = item.get("center")
        transform = _ensure_transform(item)
        c = math.cos(yaw)
        s = math.sin(yaw)
        right = [_round6(c), 0.0, _round6(-s)]
        up = [0.0, 1.0, 0.0]
        back = [_round6(s), 0.0, _round6(c)]

        transform[0][0:3] = right
        transform[1][0:3] = up
        transform[2][0:3] = back
        if isinstance(center, list) and len(center) >= 3:
            transform[3][0:3] = [_round6(center[0]), _round6(center[1]), _round6(center[2])]

        item["transform"] = transform
        item["rotation"] = [0.0, _round6(yaw), 0.0]
        item["rightVector"] = right
        item["leftVector"] = [_round6(-right[0]), _round6(-right[1]), _round6(-right[2])]
        item["upVector"] = up
        item["backVector"] = back
        item["frontVector"] = [_round6(-back[0]), _round6(-back[1]), _round6(-back[2])]
        _sync_unity_obb_vertices(item)

    return data


def _rotate_xz(x: float, z: float, angle: float) -> tuple[float, float]:
    c, s = math.cos(-angle), math.sin(-angle)
    return (x * c - z * s), (x * s + z * c)


def _inverse_rotate_xz(rx: float, rz: float, angle: float) -> tuple[float, float]:
    c, s = math.cos(angle), math.sin(angle)
    return (rx * c - rz * s), (rx * s + rz * c)


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


def _unity_rotation_from_transform(transform: list[list[float]]) -> list[float]:
    # Catalog GLB furniture faces local -Z in Unity, while RoomPlan transform row 2 is the back axis.
    yaw = math.atan2(float(transform[2][0]), float(transform[2][2]))
    return [0.0, _round6(yaw), 0.0]


def _roomplan_rotation_from_transform(transform: list[list[float]]) -> list[float]:
    yaw = math.atan2(float(transform[0][2]), float(transform[0][0]))
    return [0.0, _round6(yaw), 0.0]


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
                item["rotation"] = _unity_rotation_from_transform(item["transform"])
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


def normalize_roomplan_for_ios_view(room_data: dict[str, Any]) -> dict[str, Any]:
    """Return an iOS RoomPlan-frame payload without Unity floor normalization."""
    payload = json.loads(json.dumps(room_data))
    if isinstance(payload.get("unityNormalization"), dict):
        return denormalize_roomplan_from_unity(payload)

    payload.pop("unityNormalization", None)
    payload["coordinateSystem"] = payload.get("coordinateSystem") or "RoomPlan"
    return payload


def _denormalize_point(point: list[float], floor_theta: float, floor_y: float, min_x: float, min_z: float) -> list[float]:
    rx = float(point[0]) + min_x
    rz = float(point[2]) + min_z
    x, z = _inverse_rotate_xz(rx, rz, floor_theta)
    return [_round6(x), _round6(float(point[1]) + floor_y), _round6(z)]


def _denormalize_vector(vector: list[float], floor_theta: float) -> list[float]:
    x, z = _inverse_rotate_xz(float(vector[0]), float(vector[2]), floor_theta)
    return [_round6(x), _round6(float(vector[1])), _round6(z)]


def denormalize_roomplan_from_unity(room_data: dict[str, Any]) -> dict[str, Any]:
    """Return a Unity-normalized RoomPlan payload back in the original RoomPlan frame."""
    context = room_data.get("unityNormalization")
    if not isinstance(context, dict):
        raise ValueError("Unity-normalized payload must include unityNormalization.")

    floor_theta = float(context["floorThetaRadians"])
    floor_y = float(context["floorY"])
    min_x = float(context["minX"])
    min_z = float(context["minZ"])

    denormalized = json.loads(json.dumps(room_data))
    denormalized["coordinateSystem"] = "RoomPlan"
    denormalized.pop("unityNormalization", None)

    for collection_name in ("floors", "walls", "doors", "windows", "objects"):
        for item in denormalized.get(collection_name, []):
            if item.get("center"):
                item["center"] = _denormalize_point(item["center"], floor_theta, floor_y, min_x, min_z)
            if item.get("transform") and len(item["transform"]) >= 4:
                for row in range(3):
                    if item["transform"][row] and len(item["transform"][row]) >= 3:
                        vector = _denormalize_vector(item["transform"][row], floor_theta)
                        item["transform"][row][0] = vector[0]
                        item["transform"][row][1] = vector[1]
                        item["transform"][row][2] = vector[2]
                item["transform"][3][0:3] = item["center"]
                item["rotation"] = _roomplan_rotation_from_transform(item["transform"])
            if item.get("obbVertices"):
                item["obbVertices"] = [
                    _denormalize_point(vertex, floor_theta, floor_y, min_x, min_z)
                    for vertex in item["obbVertices"]
                ]
            for key in ("frontVector", "backVector", "leftVector", "rightVector", "upVector"):
                if item.get(key):
                    item[key] = _denormalize_vector(item[key], floor_theta)

    return denormalized
