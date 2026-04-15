"""Mapping rules for RoomPlan categories and optimizer defaults."""

from __future__ import annotations

from typing import Any


CATEGORY_MAPPING = {
    "table": "desk",
    "storage": "shelf",
}


DEFAULT_FRONT_VECTOR_BY_TYPE = {
    "bed": [0, 0, 1],
    "desk": [0, -1, 0],
    "chair": [0, -1, 0],
    "closet": [0, -1, 0],
    "shelf": [0, -1, 0],
}


def map_roomplan_category(category: str) -> str:
    normalized = category.lower()
    return CATEGORY_MAPPING.get(normalized, normalized)


def build_optimizer_metadata(category: str) -> dict[str, Any]:
    front_vector = DEFAULT_FRONT_VECTOR_BY_TYPE.get(category, [0, -1, 0])
    metadata: dict[str, Any] = {
        "label": f"Scanned {category.capitalize()}",
        "front_vector": front_vector,
    }

    if category == "bed":
        metadata["tags"] = ["wall_cling_required", "corner_preferred"]
        metadata["head_side"] = "back"
    elif category in {"desk", "closet"}:
        metadata["tags"] = ["wall_cling_required"]

    return metadata
