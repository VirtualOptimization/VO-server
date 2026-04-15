"""Convert iOS RoomPlan payloads into optimizer-friendly JSON."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .mappers import build_optimizer_metadata, map_roomplan_category


def _rotate_point(x: float, z: float, angle: float) -> tuple[float, float]:
    c, s = np.cos(-angle), np.sin(-angle)
    return (x * c - z * s), -(x * s + z * c)


def convert_roomplan_to_optimizer_payload(src: dict[str, Any]) -> dict[str, Any]:
    """Normalize RoomPlan JSON into the optimizer input schema."""
    floors = src.get("floors", [])
    if not floors:
        raise ValueError("RoomPlan payload must include at least one floor.")

    floor_transform = floors[0]["transform"]
    floor_theta = math.atan2(floor_transform[0][2], floor_transform[0][0])

    all_elements = src.get("objects", []) + src.get("doors", []) + src.get("walls", [])
    if not all_elements:
        raise ValueError("RoomPlan payload must include at least one object, door, or wall.")

    rotated_pts = []
    for element in all_elements:
        rx, rz = _rotate_point(element["center"][0], element["center"][2], floor_theta)
        rotated_pts.append([rx, rz])

    rotated_pts_np = np.array(rotated_pts)
    min_x, min_z = np.min(rotated_pts_np, axis=0)
    max_x, max_z = np.max(rotated_pts_np, axis=0)

    furnitures = []
    for index, obj in enumerate(src.get("objects", [])):
        category = map_roomplan_category(obj["category"])
        center = obj["center"]
        dimensions = obj["dimensions"]
        rx, rz = _rotate_point(center[0], center[2], floor_theta)

        rotation_y_deg = 0.0
        vertices = obj.get("obbVertices", [])
        if len(vertices) >= 2:
            raw_theta = math.degrees(
                math.atan2(vertices[1][2] - vertices[0][2], vertices[1][0] - vertices[0][0])
            )
            rotation_y_deg = -(raw_theta - math.degrees(floor_theta))

        item = {
            "id": f"f_{category}_{index}",
            "type": category,
            "extent": [
                round(dimensions[0] * 1000),
                round(dimensions[2] * 1000),
                round(dimensions[1] * 1000),
            ],
            "pos": [
                round((rx - min_x) * 1000),
                round((rz - min_z) * 1000),
                round((dimensions[1] / 2) * 1000),
            ],
            "rotation_y_deg": round(rotation_y_deg, 2),
        }
        item.update(build_optimizer_metadata(category))
        furnitures.append(item)

    fixed_elements = []
    for index, door in enumerate(src.get("doors", [])):
        center = door["center"]
        dimensions = door["dimensions"]
        rx, rz = _rotate_point(center[0], center[2], floor_theta)
        fixed_elements.append(
            {
                "id": f"main_door_{index}",
                "type": "door",
                "pos": [round((rx - min_x) * 1000), round((rz - min_z) * 1000), 0],
                "width": round(dimensions[0] * 1000),
                "is_main_entry": index == 0,
            }
        )

    return {
        "room_metadata": {
            "room_id": src.get("room_id", "roomplan_scan"),
            "dimensions": {
                "width": round((max_x - min_x) * 1000),
                "depth": round((max_z - min_z) * 1000),
                "height": 2400,
            },
        },
        "fixed_elements": fixed_elements,
        "furnitures": furnitures,
    }
