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


def _inverse_rotate_xz(rx: float, rz: float, angle: float) -> tuple[float, float]:
    c, s = math.cos(angle), math.sin(angle)
    return (rx * c - rz * s), (rx * s + rz * c)


def _floor_normalization_context(room_data: dict[str, Any]) -> tuple[float, float, float, float, float, float]:
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
        floor_depth_axis = 1 if len(dimensions) == 2 else 2
        axis_z = np.array([transform[floor_depth_axis][0], transform[floor_depth_axis][2]], dtype=float)
        center_xz = np.array([center[0], center[2]], dtype=float)
        for sx in (-1.0, 1.0):
            for sz in (-1.0, 1.0):
                depth = float(dimensions[1] if len(dimensions) == 2 else dimensions[2])
                point = center_xz + axis_x * float(dimensions[0]) * 0.5 * sx + axis_z * depth * 0.5 * sz
                points.append(_rotate_xz(float(point[0]), float(point[1]), floor_theta))

    arr = np.array(points, dtype=float)
    min_x, min_z = np.min(arr, axis=0)
    max_x, max_z = np.max(arr, axis=0)
    return floor_theta, floor_y, float(min_x), float(min_z), float(max_x - min_x), float(max_z - min_z)


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


def _rotate_point_180_in_room(point: list[float], room_width: float, room_depth: float) -> list[float]:
    return [_round6(room_width - float(point[0])), _round6(float(point[1])), _round6(room_depth - float(point[2]))]


def _rotate_vector_180(vector: list[float]) -> list[float]:
    return [_round6(-float(vector[0])), _round6(float(vector[1])), _round6(-float(vector[2]))]


def _rotate_object_layer_180(item: dict[str, Any], room_width: float, room_depth: float) -> None:
    if item.get("center"):
        item["center"] = _rotate_point_180_in_room(item["center"], room_width, room_depth)

    if item.get("transform") and len(item["transform"]) >= 4:
        for row in range(3):
            if item["transform"][row] and len(item["transform"][row]) >= 3:
                vector = _rotate_vector_180(item["transform"][row])
                item["transform"][row][0] = vector[0]
                item["transform"][row][1] = vector[1]
                item["transform"][row][2] = vector[2]
        item["transform"][3][0:3] = item["center"]

    if item.get("obbVertices"):
        item["obbVertices"] = [
            _rotate_point_180_in_room(vertex, room_width, room_depth)
            for vertex in item["obbVertices"]
        ]

    for key in ("frontVector", "backVector", "leftVector", "rightVector", "upVector"):
        if item.get(key):
            item[key] = _rotate_vector_180(item[key])


def _unity_rotation_from_transform(transform: list[list[float]]) -> list[float]:
    # Catalog GLB furniture faces local -Z in Unity, while RoomPlan transform row 2 is the back axis.
    yaw = math.atan2(float(transform[2][0]), float(transform[2][2]))
    return [0.0, _round6(yaw), 0.0]


def _roomplan_rotation_from_transform(transform: list[list[float]]) -> list[float]:
    yaw = math.atan2(float(transform[0][2]), float(transform[0][0]))
    return [0.0, _round6(yaw), 0.0]


def normalize_roomplan_for_unity(room_data: dict[str, Any]) -> dict[str, Any]:
    """Return a RoomPlan-like payload aligned to a Unity-friendly +X/+Z floor frame."""
    floor_theta, floor_y, min_x, min_z, room_width, room_depth = _floor_normalization_context(room_data)
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

    for item in normalized.get("objects", []):
        _rotate_object_layer_180(item, room_width, room_depth)
        if item.get("transform") and len(item["transform"]) >= 4:
            item["rotation"] = _unity_rotation_from_transform(item["transform"])

    normalized["unityNormalization"] = {
        "floorThetaRadians": _round6(floor_theta),
        "floorY": _round6(floor_y),
        "minX": _round6(min_x),
        "minZ": _round6(min_z),
        "roomWidth": _round6(room_width),
        "roomDepth": _round6(room_depth),
        "objectLayerRotationRadians": _round6(math.pi),
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
    room_width = context.get("roomWidth")
    room_depth = context.get("roomDepth")
    object_layer_rotation = float(context.get("objectLayerRotationRadians", 0.0))

    denormalized = json.loads(json.dumps(room_data))
    denormalized["coordinateSystem"] = "RoomPlan"
    denormalized.pop("unityNormalization", None)

    if room_width is not None and room_depth is not None and abs(object_layer_rotation - math.pi) < 1e-5:
        for item in denormalized.get("objects", []):
            _rotate_object_layer_180(item, float(room_width), float(room_depth))

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
