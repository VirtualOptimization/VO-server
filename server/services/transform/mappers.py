"""Mapping rules for RoomPlan categories and optimizer defaults."""

from __future__ import annotations

from typing import Any


CATEGORY_MAPPING = {
    "table": "desk",
    "storage": "shelf",
}


DEFAULT_FRONT_VECTOR_2D_BY_TYPE = {
    "bed": [1.0, 0.0],
    "desk": [0.0, -1.0],
    "chair": [0.0, 1.0],
    "closet": [0.0, -1.0],
    "shelf": [0.0, -1.0],
}


def map_roomplan_category(category: str) -> str:
    normalized = category.lower()
    return CATEGORY_MAPPING.get(normalized, normalized)


def build_optimizer_metadata(category: str) -> dict[str, Any]:
    front_vector_2d = DEFAULT_FRONT_VECTOR_2D_BY_TYPE.get(category, [0.0, -1.0])
    metadata: dict[str, Any] = {
        "label": f"Scanned {category.capitalize()}",
        "front_vector_2d": front_vector_2d,
    }

    if category == "bed":
        metadata["anchor_preferences"] = {
            "wall_cling_required": True,
            "corner_preferred": True,
            "back_to_wall": True,
        }
        metadata["head_side"] = "back"
    elif category in {"desk", "closet"}:
        metadata["anchor_preferences"] = {
            "wall_cling_required": True,
            "back_to_wall": category == "closet",
        }
    elif category == "shelf":
        metadata["anchor_preferences"] = {
            "wall_cling_required": True,
            "back_to_wall": True,
        }

    return metadata
