"""Shared OBB/placeholder colors; never changes catalog model materials."""
import re

DEFAULT_COLOR = "#CCC7BF"
CATEGORY_COLORS = {
    "bed": "#F2CCD4", "sofa": "#B8D4E6", "chair": "#B8D4E6",
    "table": "#E0D1B8", "storage": "#C7D1C2", "television": "#8C8C94",
    "refrigerator": "#E0E6EB", "washerdryer": "#D1E0EB",
}


def ensure_display_colors(layout: dict, reference: dict | None = None) -> bool:
    """Enrich in place, optionally using the RoomPlan colors as authority."""
    colors = {
        obj["identifier"]: obj["display_color_hex"]
        for obj in (reference or {}).get("objects", [])
        if isinstance(obj, dict) and obj.get("identifier") and obj.get("display_color_hex")
    }
    changed = False
    for obj in layout.get("objects", []):
        if not isinstance(obj, dict):
            continue
        color = colors.get(obj.get("identifier"), obj.get("display_color_hex"))
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            category = obj.get("category", "")
            if isinstance(category, dict):
                category = next(iter(category), "")
            color = CATEGORY_COLORS.get(str(category).strip().lower(), DEFAULT_COLOR)
        color = color.upper()
        if obj.get("display_color_hex") != color:
            obj["display_color_hex"] = color
            changed = True
    return changed


async def backfill_layout_colors(ios_key: str | None, unity_key: str | None) -> None:
    """Persist before issuing presigned URLs, which bypass server serialization."""
    from server.core.s3 import get_json, upload_json

    reference = None
    for key in (ios_key, unity_key):
        if not key:
            continue
        layout = await get_json(key)
        if ensure_display_colors(layout, reference):
            await upload_json(key, layout)
        if reference is None:
            reference = layout
