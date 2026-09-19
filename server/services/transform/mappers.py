"""Mapping rules for RoomPlan categories and optimizer defaults."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any


CATEGORY_MAPPING = {
    "storage": "shelf",
}

CATALOG_CATEGORY_DIRS = {
    "bed": "Bed",
    "chair": "Chair",
    "sofa": "Sofa",
    "storage": "Storage",
    "table": "Table",
}

DEFAULT_MODEL_VARIANT_BY_FILENAME = {
    "DefaultChair": "Default",
    "DefaultTable": "Default",
    "shelf_vertical": "Shelf",
    # RoomPlan의 modelFileName(otherLegType)과 실제 카탈로그 폴더명(unidentifiedLegs)
    # 표기가 달라 폴더명 추측이 어긋난다.
    "Unidentified_wBack_otherLegType_wArms": "Unidentified_wBack_unidentifiedLegs_wArms",
}


DEFAULT_FRONT_VECTOR_2D_BY_TYPE = {
    "bed": [1.0, 0.0],
    "table": [0.0, -1.0],
    "desk": [0.0, -1.0],
    "chair": [0.0, 1.0],
    "closet": [0.0, -1.0],
    "shelf": [0.0, -1.0],
    "television": [0.0, -1.0],
}


def map_roomplan_category(category: str) -> str:
    normalized = category.lower()
    return CATEGORY_MAPPING.get(normalized, normalized)


def infer_catalog_model_key(category: str, model_file_name: str | None) -> str | None:
    """Infer the furniture_models.model_key used by the preconverted GLB catalog."""
    if not model_file_name:
        return None

    category_dir = CATALOG_CATEGORY_DIRS.get(category.lower(), category.capitalize())
    path = PurePosixPath(model_file_name.replace("\\", "/"))
    filename = path.name
    base_name = filename.removesuffix(".rooms.usdc").removesuffix(".usdc")
    variant = DEFAULT_MODEL_VARIANT_BY_FILENAME.get(
        base_name,
        path.parent.name if path.parent.name else base_name,
    )

    if not base_name or not variant:
        return None

    # S3 catalog GLB names use this lowercase variant, and DB keys mirror it
    # with the .rooms.usdc source extension.
    model_name = base_name.replace("_lShaped", "_lshaped")
    return f"{category_dir}/{variant}/{model_name}.rooms.usdc"


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
    elif category in {"desk", "closet", "television"}:
        metadata["anchor_preferences"] = {
            "wall_cling_required": True,
            "back_to_wall": category in {"closet", "television"},
        }
    elif category == "shelf":
        metadata["anchor_preferences"] = {
            "wall_cling_required": True,
            "back_to_wall": True,
        }

    return metadata
