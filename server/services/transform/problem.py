"""Build an optimizer-ready layout problem from normalized scan data."""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np

def _copy(data: Any) -> Any:
    return json.loads(json.dumps(data))


def _distance(a: list[float], b: list[float]) -> float:
    return float(np.linalg.norm(np.array(a[:2], dtype=float) - np.array(b[:2], dtype=float)))


def _normalize_2d(vector: list[float]) -> np.ndarray:
    arr = np.array(vector[:2], dtype=float)
    norm = np.linalg.norm(arr)
    if norm < 1e-8:
        return np.array([0.0, -1.0], dtype=float)
    return arr / norm


def _classify_chair_relationship(chair: dict[str, Any], support: dict[str, Any]) -> dict[str, Any]:
    chair_pos = np.array(chair["pos"][:2], dtype=float)
    support_pos = np.array(support["pos"][:2], dtype=float)
    offset = chair_pos - support_pos
    distance = float(np.linalg.norm(offset))

    support_front = _normalize_2d(support.get("front_vector_2d", [0.0, -1.0]))
    support_lateral = np.array([-support_front[1], support_front[0]], dtype=float)
    chair_front = _normalize_2d(chair.get("front_vector_2d", [0.0, 1.0]))

    if distance < 1e-8:
        direction_to_chair = np.array([0.0, 0.0], dtype=float)
    else:
        direction_to_chair = offset / distance

    front_projection = float(np.dot(direction_to_chair, support_front))
    facing_alignment = float(np.dot(chair_front, -support_front))

    local_front = float(np.dot(offset, support_front))
    local_lateral = float(np.dot(offset, support_lateral))
    rotation_delta_deg = float(chair.get("rotation_y_deg", 0.0)) - float(support.get("rotation_y_deg", 0.0))

    if support["type"] == "table" and distance <= 1.6:
        strength = "primary"
    elif distance <= 1.2 and facing_alignment > 0.1:
        strength = "primary"
    elif distance <= 2.2:
        strength = "weak"
    else:
        strength = "free"

    return {
        "nearest_support_id": support["id"],
        "nearest_support_type": support["type"],
        "nearest_desk_id": support["id"],
        "distance_to_nearest_support": round(distance, 3),
        "distance_to_nearest_desk": round(distance, 3),
        "front_projection": round(front_projection, 3),
        "facing_alignment": round(facing_alignment, 3),
        "local_offset": [round(local_front, 3), round(local_lateral, 3)],
        "rotation_delta_deg": round(rotation_delta_deg, 3),
        "strength": strength,
    }


def _placement_rules(item_type: str) -> dict[str, Any]:
    common = {"body_collision": "critical"}
    by_type = {
        "bed": {
            **common,
            "side_clearance": 0.6,
            "foot_clearance": 0.4,
            "min_wall_contact": 1,
            "corner_preferred": True,
            "window_block_soft_penalty": True,
        },
        "desk": {
            **common,
            "front_clearance": 0.75,
            "side_clearance": 0.6,
            "back_near_wall_preferred": True,
            "window_block_soft_penalty": True,
        },
        "table": {
            **common,
            "front_clearance": 0.75,
            "side_clearance": 0.6,
            "back_near_wall_preferred": True,
            "window_block_soft_penalty": True,
            "chair_group_preferred": True,
        },
        "chair": {
            **common,
            "seat_pullout": 0.463,
            "rotation_radius_scale": 1.2,
            "rear_wall_min": 0.1,
        },
        "closet": {
            **common,
            "front_clearance": 1.0,
            "door_swing_radius": 0.9,
            "corner_preferred": True,
        },
        "shelf": {
            **common,
            "front_clearance": 0.8,
            "drawer_clearance": 0.4,
            "corner_preferred": True,
            "visual_bulk_soft_penalty": True,
        },
    }
    return by_type.get(item_type, common)


def build_layout_problem(normalized_scan: dict[str, Any]) -> dict[str, Any]:
    """Build a layout optimization problem without dropping scanned furniture."""
    scanned_objects = _copy(normalized_scan.get("scanned_objects", []))
    fixed_elements = _copy(normalized_scan.get("fixed_elements", []))
    room_metadata = _copy(normalized_scan["room_metadata"])

    chair_supports = [obj for obj in scanned_objects if obj["type"] in {"desk", "table"}]
    chairs = [obj for obj in scanned_objects if obj["type"] == "chair"]

    movable_items: list[dict[str, Any]] = []
    ignored_scan_items: list[dict[str, Any]] = []

    for obj in scanned_objects:
        item = _copy(obj)
        item["placement_rules"] = _placement_rules(item["type"])
        item["source"] = "scan"
        movable_items.append(item)

    support_positions = {support["id"]: support["pos"] for support in chair_supports}
    support_by_id = {support["id"]: support for support in chair_supports}
    for item in movable_items:
        if item["type"] != "chair" or not support_positions:
            continue
        nearest_support_id = min(
            support_positions,
            key=lambda support_id: _distance(item["pos"], support_positions[support_id]),
        )
        relationship = _classify_chair_relationship(item, support_by_id[nearest_support_id])
        item["relationship"] = relationship
        if relationship["strength"] != "free":
            item["pair_with"] = nearest_support_id

    constraints = {
        "min_walkway": 0.425,
        "seat_pullout": 0.463,
        "arm_reach": 0.565,
    }

    return {
        "room_metadata": room_metadata,
        "fixed_elements": fixed_elements,
        "movable_items": movable_items,
        "ignored_scan_items": ignored_scan_items,
        "constraints": constraints,
    }
