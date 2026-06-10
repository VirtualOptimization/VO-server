"""Mapping rules for RoomPlan categories and optimizer defaults."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any


CATEGORY_MAPPING = {
    "table": "desk",
    "storage": "shelf",
}

DEFAULT_MODEL_VARIANT_BY_FILENAME = {
    "DefaultChair": "Default",
    "DefaultTable": "Default",
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


def infer_catalog_model_key(category: str, model_file_name: str | None) -> str | None:
    """Infer the furniture_models.model_key used by the preconverted GLB catalog."""
    if not model_file_name:
        return None

    normalized_category = map_roomplan_category(category)
    path = PurePosixPath(model_file_name.replace("\\", "/"))
    parts = [part for part in path.parts if part not in {"", ".", "Resources", "Models"}]

    if len(parts) >= 3:
        variant = parts[-2]
    else:
        variant = path.name
        if variant.endswith(".usdc"):
            variant = variant[:-5]
        if variant.endswith(".rooms"):
            variant = variant[:-6]
        variant = DEFAULT_MODEL_VARIANT_BY_FILENAME.get(variant, variant)

    if not variant:
        return None
    return f"{normalized_category}:{variant}"


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
