"""Convert iOS RoomPlan payloads into normalized scan JSON."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .mappers import build_optimizer_metadata, infer_catalog_model_key, map_roomplan_category


def _rotate_point(x: float, z: float, angle: float) -> tuple[float, float]:
    c, s = np.cos(-angle), np.sin(-angle)
    return (x * c - z * s), (x * s + z * c)


def _rotate_vector(x: float, z: float, angle: float) -> tuple[float, float]:
    c, s = np.cos(-angle), np.sin(-angle)
    return (x * c - z * s), (x * s + z * c)


def _round3(value: float) -> float:
    return round(float(value), 3)


def _extract_yaw_deg(transform: list[list[float]], floor_theta: float) -> float:
    raw_theta = math.atan2(transform[0][2], transform[0][0])
    return _round3(math.degrees(raw_theta - floor_theta))


def _normalize_vector_2d(x: float, y: float, fallback: list[float]) -> list[float]:
    norm = math.hypot(x, y)
    if norm < 1e-6:
        return fallback
    return [_round3(x / norm), _round3(y / norm)]


def _extract_usage_front_2d(obj: dict[str, Any], floor_theta: float, fallback: list[float]) -> list[float]:
    front = obj.get("frontVector")
    if front and len(front) >= 3:
        fx, fy = _rotate_vector(front[0], front[2], floor_theta)
        return _normalize_vector_2d(fx, fy, fallback)

    back = obj.get("backVector")
    if back and len(back) >= 3:
        bx, by = _rotate_vector(back[0], back[2], floor_theta)
        return _normalize_vector_2d(-bx, -by, fallback)

    transform = obj.get("transform")
    if transform:
        tx, ty = _rotate_vector(transform[2][0], transform[2][2], floor_theta)
        return _normalize_vector_2d(tx, ty, fallback)

    return fallback


def _extract_axis_2d(element: dict[str, Any], floor_theta: float) -> list[float] | None:
    transform = element.get("transform")
    if not transform or len(transform) < 1 or len(transform[0]) < 3:
        return None

    ax, ay = _rotate_vector(transform[0][0], transform[0][2], floor_theta)
    return _normalize_vector_2d(ax, ay, [1.0, 0.0])


def _classify_wall_and_span(
    center_x: float,
    center_y: float,
    span_length: float,
    room_width: float,
    room_depth: float,
) -> tuple[str, dict[str, float]]:
    distances = {
        "west": abs(center_x),
        "east": abs(room_width - center_x),
        "south": abs(center_y),
        "north": abs(room_depth - center_y),
    }
    wall = min(distances, key=distances.get)

    if wall in {"west", "east"}:
        start = max(0.0, center_y - span_length / 2.0)
        end = min(room_depth, center_y + span_length / 2.0)
    else:
        start = max(0.0, center_x - span_length / 2.0)
        end = min(room_width, center_x + span_length / 2.0)

    return wall, {"start": _round3(start), "end": _round3(end)}


def _element_span_length(dimensions: list[float]) -> float:
    if len(dimensions) >= 3:
        return max(dimensions[0], dimensions[2])
    if len(dimensions) == 2:
        return dimensions[0]
    return dimensions[0]


def _element_height(dimensions: list[float]) -> float:
    if len(dimensions) >= 2:
        return dimensions[1]
    return 0.0


def _rotated_2d_vertices(element: dict[str, Any], floor_theta: float) -> list[tuple[float, float]]:
    obb_vertices = element.get("obbVertices", [])
    if obb_vertices:
        return [_rotate_point(vertex[0], vertex[2], floor_theta) for vertex in obb_vertices]

    center = element["center"]
    rx, rz = _rotate_point(center[0], center[2], floor_theta)
    return [(rx, rz)]


def convert_roomplan_to_optimizer_payload(src: dict[str, Any]) -> dict[str, Any]:
    """Normalize RoomPlan JSON into a scan-preserving canonical schema."""
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

    rotated_pts = []
    for element in all_elements:
        rotated_pts.extend(_rotated_2d_vertices(element, floor_theta))

    rotated_pts_np = np.array(rotated_pts)
    min_x, min_z = np.min(rotated_pts_np, axis=0)
    max_x, max_z = np.max(rotated_pts_np, axis=0)

    room_width = max_x - min_x
    room_depth = max_z - min_z

    scanned_objects = []
    for index, obj in enumerate(src.get("objects", [])):
        category = map_roomplan_category(obj["category"])
        center = obj["center"]
        dimensions = obj["dimensions"]
        rx, rz = _rotate_point(center[0], center[2], floor_theta)
        transform = obj.get("transform")
        rotation_y_deg = _extract_yaw_deg(transform, floor_theta) if transform else 0.0

        item = {
            "id": obj.get("identifier", f"f_{category}_{index}"),
            "type": category,
            "extent": [_round3(dimensions[0]), _round3(dimensions[2]), _round3(dimensions[1])],
            "pos": [
                _round3(rx - min_x),
                _round3(rz - min_z),
                _round3((center[1] - floor_y) + (dimensions[1] / 2.0)),
            ],
            "rotation_y_deg": rotation_y_deg,
        }
        item.update(build_optimizer_metadata(category))
        item["front_vector_2d"] = _extract_usage_front_2d(obj, floor_theta, item["front_vector_2d"])
        if obj.get("backVector"):
            bx, by = _rotate_vector(obj["backVector"][0], obj["backVector"][2], floor_theta)
            item["back_vector_2d"] = _normalize_vector_2d(bx, by, [-item["front_vector_2d"][0], -item["front_vector_2d"][1]])
        if "modelFileName" in obj:
            item["source_model_file"] = obj["modelFileName"]
            model_key = infer_catalog_model_key(obj["category"], obj["modelFileName"])
            if model_key:
                item["model_key"] = model_key
        scanned_objects.append(item)

    fixed_elements = []
    for collection_name, element_type in (
        ("doors", "door"),
        ("windows", "window"),
        ("walls", "wall"),
    ):
        for index, element in enumerate(src.get(collection_name, [])):
            center = element["center"]
            dimensions = element["dimensions"]
            rx, rz = _rotate_point(center[0], center[2], floor_theta)
            center_x = rx - min_x
            center_y = rz - min_z
            span_length = _element_span_length(dimensions)
            wall, span = _classify_wall_and_span(
                center_x,
                center_y,
                span_length,
                room_width,
                room_depth,
            )

            fixed_item: dict[str, Any] = {
                "id": element.get("identifier", f"{element_type}_{index}"),
                "type": element_type,
                "wall": wall,
                "span": span,
            }
            if element_type == "door":
                fixed_item["clearance_depth"] = _round3(_element_span_length(dimensions))
                fixed_item["is_main_entry"] = index == 0
            elif element_type == "window":
                height = _element_height(dimensions)
                fixed_item["sill_height"] = _round3((center[1] - floor_y) - height / 2.0)
                fixed_item["height"] = _round3(height)
                fixed_item["keep_visual_open"] = True
            else:
                fixed_item["center"] = [_round3(center_x), _round3(center_y)]
                fixed_item["axis"] = _extract_axis_2d(element, floor_theta) or [1.0, 0.0]
                fixed_item["length"] = _round3(span_length)
                fixed_item["thickness"] = _round3(min(dimensions[0], dimensions[2] if len(dimensions) >= 3 else 0.1))

            fixed_elements.append(fixed_item)

    inferred_room_height = src.get("room_height")
    if inferred_room_height is None:
        wall_heights = [
            _element_height(wall.get("dimensions", []))
            for wall in src.get("walls", [])
            if wall.get("dimensions")
        ]
        inferred_room_height = max(wall_heights, default=2.4)

    return {
        "room_metadata": {
            "room_id": src.get("room_id", "roomplan_scan"),
            "dimensions": {
                "width": _round3(room_width),
                "depth": _round3(room_depth),
                "height": _round3(inferred_room_height),
            },
            "floor_axis": "xy",
        },
        "fixed_elements": fixed_elements,
        "scanned_objects": scanned_objects,
        "raw_summary": {
            "object_count": len(src.get("objects", [])),
            "door_count": len(src.get("doors", [])),
            "window_count": len(src.get("windows", [])),
            "wall_count": len(src.get("walls", [])),
        },
    }
