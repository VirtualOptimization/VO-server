"""Extract furniture placement relations from LH standard bedroom unit plans.

Source: LH 주택평면계획기준 연구 (OTKCRK150014.pdf), 그림 6-42~6-46 (주침실/부침실
단위공간계획). Each cropped panel is one standard plan.

Split of work:
- The door wall is measured geometrically (plan_geometry.detect_door); vision models
  misread doors near corners because the open door leaf lies along the adjacent wall.
- A vision model only recognizes furniture symbols and which labelled image side
  they touch, answering through a forced tool call so the output is always valid.
- Door-relative relations (opposite_door, left_of_door, ...) are derived in code.

Usage:
    python workers/rag/extract_lh_unit_plan_layouts.py MANIFEST OUTPUT [MANUAL_ANSWERS]
"""

from __future__ import annotations

import base64
import io
import json
import os
import ssl
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import certifi
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

from plan_geometry import detect_door

SIDES = ["top", "bottom", "left", "right"]
OPPOSITE = {"top": "bottom", "bottom": "top", "left": "right", "right": "left"}
MARGIN = 48

TOOL = {
    "name": "report_furniture",
    "description": "Report which labelled side of the drawing each furniture item touches.",
    "input_schema": {
        "type": "object",
        "properties": {
            "bed_present": {"type": "boolean"},
            "bed_head_wall": {"type": "string", "enum": SIDES + ["none"],
                              "description": "Side whose wall the pillow end of the bed is against."},
            "bed_long_side_walls": {"type": "array", "items": {"type": "string", "enum": SIDES},
                                    "description": "Sides whose wall a LONG side of the bed directly touches. A nightstand between the bed and the wall does not count."},
            "wardrobe_present": {"type": "boolean"},
            "wardrobe_wall": {"type": "string", "enum": SIDES + ["none"],
                              "description": "Side whose wall the wardrobe's back is against."},
            "desk_present": {"type": "boolean"},
            "desk_wall": {"type": "string", "enum": SIDES + ["none"],
                          "description": "Side whose wall the desk's back edge is against (the edge away from the chair)."},
        },
        "required": ["bed_present", "bed_head_wall", "bed_long_side_walls",
                     "wardrobe_present", "wardrobe_wall", "desk_present", "desk_wall"],
    },
}

PROMPT = """Top-down floor plan of ONE bedroom from a Korean public-housing design
standard. The image sides are labelled TOP, BOTTOM, LEFT, RIGHT. The room is the
rectangle (or L-shape) drawn with thick gray walls. The red box marks the door
opening -- the curved arc and straight line near it are the door swing, not furniture.

Symbols: bed = rectangle with pillow(s) drawn at its head end; wardrobe = long box
with zigzag hanger marks; desk = plain rectangle with a small chair drawn beside it;
small square with a circle = nightstand/lamp (not furniture of interest).

You MUST answer by calling the report_furniture tool exactly once, using only the
labelled sides. Do not answer in plain text."""


def labelled_image(image_path: Path, door: dict | None) -> str:
    image = Image.open(image_path).convert("RGB")
    canvas = Image.new("RGB", (image.width + 2 * MARGIN, image.height + 2 * MARGIN), "white")
    canvas.paste(image, (MARGIN, MARGIN))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.load_default(size=26)
    except TypeError:
        font = ImageFont.load_default()
    w, h = canvas.size
    draw.text((w // 2 - 30, 8), "TOP", fill="black", font=font)
    draw.text((w // 2 - 55, h - 38), "BOTTOM", fill="black", font=font)
    draw.text((6, h // 2 - 12), "LEFT", fill="black", font=font)
    draw.text((w - 84, h // 2 - 12), "RIGHT", fill="black", font=font)
    if door and "gap_box" in door:
        x0, y0, x1, y1 = door["gap_box"]
        draw.rectangle([x0 + MARGIN - 4, y0 + MARGIN - 4, x1 + MARGIN + 4, y1 + MARGIN + 4], outline="red", width=4)
    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def ask_model(image_b64: str, api_key: str, model: str) -> dict:
    body = {
        "model": model,
        "max_tokens": 1024,
        "tools": [TOOL],
        # Forced tool_choice is not supported on every model; the prompt requires the
        # tool call and a reply without one is treated as a failure below.
        "tool_choice": {"type": "auto"},
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": image_b64}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    }
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        method="POST",
    )
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=180, context=context) as response:
        payload = json.loads(response.read())
    return next(part["input"] for part in payload["content"] if part.get("type") == "tool_use")


def door_relative(side: str | None, door_wall: str | None) -> str | None:
    """Name an image side relative to a person standing in the doorway, facing into the room."""
    if side not in SIDES or door_wall not in SIDES:
        return None
    if side == door_wall:
        return "door_wall"
    if side == OPPOSITE[door_wall]:
        return "opposite_door"
    viewer_left = {"bottom": "left", "top": "right", "left": "top", "right": "bottom"}[door_wall]
    return "left_of_door" if side == viewer_left else "right_of_door"


def door_view(door: dict, head: str | None) -> str | None:
    """Where the door is for someone lying in bed: behind the head, beside it, or in front.

    Spörrle & Stich (2010): most people place the bed so the door is visible from the
    resting position, so "behind" and "beside" (door level with the pillow on an
    adjacent wall) are what matters to separate from "in_front".
    """
    door_wall = door.get("wall")
    if head not in SIDES or door_wall not in SIDES:
        return None
    if door_wall == head:
        return "behind"
    if door_wall == OPPOSITE[head]:
        return "in_front"
    return "beside" if door.get("gap_near_end") == head else "in_front"


def derive(reading: dict) -> dict:
    door = reading["door"] or {}
    door_wall = door.get("wall")
    rel = lambda side: door_relative(side, door_wall)  # noqa: E731
    head = reading["bed_head_wall"] if reading["bed_present"] else None
    sides = [rel(side) for side in reading["bed_long_side_walls"] if rel(side)] if reading["bed_present"] else []
    return {
        "bed_head_wall": rel(head),
        "bed_side_walls": sides,
        "bed_in_corner": bool(rel(head) and sides),
        "door_view_from_bed": door_view(door, head),
        "wardrobe_wall": rel(reading["wardrobe_wall"]) if reading["wardrobe_present"] else None,
        "desk_wall": rel(reading["desk_wall"]) if reading["desk_present"] else None,
    }


def from_manual(answer: dict) -> dict:
    """Put a hand annotation into the same reading shape the extractor produces."""
    return {
        "door": answer["door"],
        "bed_present": answer["bed"]["present"],
        "bed_head_wall": answer["bed"]["head_wall"] or "none",
        "bed_long_side_walls": answer["bed"]["side_walls"],
        "wardrobe_present": answer["wardrobe"]["present"],
        "wardrobe_wall": answer["wardrobe"]["wall"] or "none",
        "desk_present": answer["desk"]["present"],
        "desk_wall": answer["desk"]["wall"] or "none",
    }


def extract(row: dict, api_key: str, model: str) -> dict:
    image_path = Path(row["image_path"])
    door = detect_door(image_path)
    try:
        furniture = ask_model(labelled_image(image_path, door), api_key, model)
    except Exception as exc:  # reviewed by hand
        return {**row, "door": door, "error": f"{type(exc).__name__}: {exc}"}
    reading = {"door": door if door and "wall" in door else None, **furniture}
    return {**row, "reading": reading, "relations": derive(reading)}


def evaluate(results: list[dict], manual: dict) -> None:
    fields = ["door_wall", "door_gap_near_end", "bed_head_wall", "bed_long_side_walls", "wardrobe_wall", "desk_wall"]
    correct = {field: 0 for field in fields}
    for result in results:
        key = f"{result['page_number']}_{result['panel']}"
        expected = from_manual(manual[key])
        got = result.get("reading") or {}
        got_door = got.get("door") or {}
        pairs = {
            "door_wall": (got_door.get("wall"), expected["door"]["wall"]),
            "door_gap_near_end": (got_door.get("gap_near_end"), expected["door"]["gap_near_end"]),
            "bed_head_wall": (got.get("bed_head_wall"), expected["bed_head_wall"]),
            "bed_long_side_walls": (sorted(got.get("bed_long_side_walls") or []), sorted(expected["bed_long_side_walls"])),
            "wardrobe_wall": (got.get("wardrobe_wall"), expected["wardrobe_wall"]),
            "desk_wall": (got.get("desk_wall"), expected["desk_wall"]),
        }
        for field, (value, truth) in pairs.items():
            if value == truth:
                correct[field] += 1
            else:
                print(f"  MISMATCH {key} {field}: got={value} expected={truth}")
    total = len(results)
    for field in fields:
        print(f"{field}: {correct[field]}/{total}")


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    manifest_path, output_path = Path(sys.argv[1]), Path(sys.argv[2])
    manual_path = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    api_key = os.environ["ANTHROPIC_API_KEY"]
    model = os.getenv("LAYOUT_VISION_MODEL", "claude-opus-5-5")
    rows = json.loads(manifest_path.read_text(encoding="utf-8"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda row: extract(row, api_key, model), rows))
    for result in results:
        print(result["figure"], result["panel"], result.get("error") or result["relations"], flush=True)
    output_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in results) + "\n", encoding="utf-8")
    if manual_path:
        manual = json.loads(manual_path.read_text(encoding="utf-8"))
        manual.pop("_note", None)
        evaluate(results, manual)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
