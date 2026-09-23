"""Sourced furniture placement principles and helpers to check them.

Sources:
- LH 주택평면계획기준 연구 (OTKCRK150014.pdf) 그림 6-42~6-46: 26 standard
  bedroom unit plans, read by hand and cross-checked by a CV + vision-model
  pipeline (workers/rag/extract_lh_unit_plan_layouts.py).
- Spörrle & Stich (2010), Evolutionary Psychology 8(3): bed placement preference.

Walls are named relative to a person standing in the entrance door facing into the
room: door_wall, opposite_door, left_of_door, right_of_door.
"""

from __future__ import annotations

import math
from typing import Any

ABSOLUTE_WALLS = ("west", "east", "south", "north")
OPPOSITE = {"west": "east", "east": "west", "south": "north", "north": "south"}
# Facing into the room from the door wall, which wall is on the viewer's left.
VIEWER_LEFT = {"south": "west", "north": "east", "west": "north", "east": "south"}
NORMALS = {"west": (-1.0, 0.0), "east": (1.0, 0.0), "south": (0.0, -1.0), "north": (0.0, 1.0)}

PRINCIPLES = [
    {"id": "P0", "applies_to": "bed", "level": "must",
     "text": "The head of the bed is against a wall.",
     "source": "LH 표준 침실 평면 26개 중 26개; Neufert p.257 (baş kısmı duvara dayalı)"},
    {"id": "P1", "applies_to": "bed", "level": "must",
     "text": "Never put the head of the bed against the door wall.",
     "source": "LH 표준 침실 평면 26개 중 0개 예외; Spörrle & Stich 2010 (83% placed the bed so the door is visible when lying down)"},
    {"id": "P2", "applies_to": "bed", "level": "prefer",
     "text": "Lying in bed, the door should be in front, not behind or level with the pillow.",
     "source": "LH 26개 중 문이 앞쪽 21, 머리 옆 5, 머리 뒤 0; Spörrle & Stich 2010"},
    {"id": "P3", "applies_to": "wardrobe", "level": "prefer",
     "text": "Put the wardrobe on the door wall or a wall beside the door, not on the wall opposite the door.",
     "source": "LH 표준 침실 평면 26개 중 24개"},
    {"id": "P4", "applies_to": "desk", "level": "must",
     "text": "Never put the desk's back against the door wall.",
     "source": "LH 1인 침실(부침실) 평면 12개 중 0개 예외"},
    {"id": "P5", "applies_to": "single_bed", "level": "prefer",
     "text": "A single bed may go in a corner with one long side against a wall.",
     "source": "LH 1인 침실 평면 12개 중 6개"},
    {"id": "P6", "applies_to": "desk", "level": "prefer",
     "text": "Put the desk on the same wall as the bed head, side by side.",
     "source": "LH 1인 침실 평면 12개 중 8개"},
]

TALL_STORAGE_HEIGHT_M = 1.2
WALL_MOUNTED_MIN_BOTTOM_M = 0.5
SINGLE_BED_MAX_WIDTH_M = 1.3
# How close an item's back must be to a wall to count as "against" it. The
# optimizer keeps items a few cm off the room boundary (see CanonicalLayoutOptimizer._wall_band).
AGAINST_WALL_TOLERANCE_M = 0.25
DOOR_IN_FRONT_MIN_M = 0.6


def entrance_door(fixed_elements: list[dict[str, Any]]) -> dict[str, Any] | None:
    doors = [e for e in fixed_elements if e.get("type") == "door" and e.get("wall") in ABSOLUTE_WALLS]
    if not doors:
        return None
    return max(doors, key=lambda d: float(d["span"]["end"]) - float(d["span"]["start"]))


def relative_to_absolute(door_wall: str) -> dict[str, str]:
    left = VIEWER_LEFT[door_wall]
    return {
        "door_wall": door_wall,
        "opposite_door": OPPOSITE[door_wall],
        "left_of_door": left,
        "right_of_door": OPPOSITE[left],
    }


def door_point(door: dict[str, Any], room_width: float, room_depth: float) -> tuple[float, float]:
    mid = (float(door["span"]["start"]) + float(door["span"]["end"])) / 2.0
    return {
        "west": (0.0, mid),
        "east": (room_width, mid),
        "south": (mid, 0.0),
        "north": (mid, room_depth),
    }[door["wall"]]


def role(item: dict[str, Any], items: list[dict[str, Any]]) -> str:
    item_type = item.get("type")
    if item_type == "bed":
        return "single_bed" if min(map(float, item["extent"][:2])) <= SINGLE_BED_MAX_WIDTH_M else "bed"
    if item_type in {"shelf", "closet"}:
        # pos[2] is the item's TOP height; extent[2] is its own height. Storage
        # whose bottom is well off the floor is a wall-mounted cabinet, not a wardrobe.
        top = float(item["pos"][2])
        height = float(item["extent"][2]) if len(item["extent"]) > 2 else top
        if top - height > WALL_MOUNTED_MIN_BOTTOM_M:
            return "wall_cabinet"
        return "wardrobe" if height >= TALL_STORAGE_HEIGHT_M else "low_storage"
    if item_type in {"table", "desk"}:
        paired = any(other.get("pair_with") == item.get("id") for other in items)
        return "desk" if paired else "table"
    return item_type or "unknown"


def world_back(item: dict[str, Any], theta_rad: float) -> tuple[float, float]:
    """World back vector (bed head end, desk edge away from the chair, storage closed side)."""
    base = item.get("front_vector_2d") or [0.0, -1.0]
    delta = theta_rad - math.radians(float(item.get("rotation_y_deg", 0.0)))
    c, s = math.cos(delta), math.sin(delta)
    return (-(base[0] * c - base[1] * s), -(base[0] * s + base[1] * c))


def corners(item: dict[str, Any], x: float, y: float, theta_rad: float) -> list[tuple[float, float]]:
    c, s = math.cos(theta_rad), math.sin(theta_rad)
    hw, hd = float(item["extent"][0]) / 2, float(item["extent"][1]) / 2
    return [(x + px * c - py * s, y + px * s + py * c) for px, py in ((-hw, -hd), (hw, -hd), (hw, hd), (-hw, hd))]


def wall_gaps(points: list[tuple[float, float]], room_width: float, room_depth: float) -> dict[str, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return {"west": min(xs), "east": room_width - max(xs), "south": min(ys), "north": room_depth - max(ys)}


def back_wall(item: dict[str, Any], x: float, y: float, theta_rad: float, room_width: float, room_depth: float) -> str | None:
    """The wall the item's back is against, or None if its back is not at a wall."""
    back = world_back(item, theta_rad)
    wall = max(ABSOLUTE_WALLS, key=lambda w: back[0] * NORMALS[w][0] + back[1] * NORMALS[w][1])
    gap = wall_gaps(corners(item, x, y, theta_rad), room_width, room_depth)[wall]
    return wall if gap <= AGAINST_WALL_TOLERANCE_M else None


def door_view_from_bed(bed: dict[str, Any], x: float, y: float, theta_rad: float,
                       door: dict[str, Any], room_width: float, room_depth: float) -> str:
    """Where the door is for someone lying in the bed: behind, beside, or in_front."""
    back = world_back(bed, theta_rad)
    half_length = max(map(float, bed["extent"][:2])) / 2
    head = (x + back[0] * half_length, y + back[1] * half_length)
    target = door_point(door, room_width, room_depth)
    ahead = -((target[0] - head[0]) * back[0] + (target[1] - head[1]) * back[1])
    if ahead < -0.1:
        return "behind"
    return "beside" if ahead < DOOR_IN_FRONT_MIN_M else "in_front"


def principle_compliance(problem: dict[str, Any], optimized: dict[str, Any]) -> dict[str, Any]:
    """Score a final layout against the sourced principles."""
    room = problem["room_metadata"]["dimensions"]
    width, depth = float(room["width"]), float(room["depth"])
    door = entrance_door(problem.get("fixed_elements", []))
    to_relative = {absolute: rel for rel, absolute in relative_to_absolute(door["wall"]).items()}
    items = optimized.get("movable_items", [])

    def pose(item: dict[str, Any]) -> tuple[float, float, float]:
        pos = item.get("optimized_pos") or item["pos"]
        return float(pos[0]), float(pos[1]), math.radians(float(item.get("optimized_rotation_y_deg", item.get("rotation_y_deg", 0.0))))

    def rel_back(item: dict[str, Any]) -> str | None:
        wall = back_wall(item, *pose(item), width, depth)
        return to_relative.get(wall) if wall else None

    roles = {item["id"]: role(item, items) for item in items}
    report: dict[str, Any] = {}
    beds = [i for i in items if roles[i["id"]] in {"bed", "single_bed"}]
    if beds:
        bed = beds[0]
        report["bed_head_wall"] = rel_back(bed)
        report["P0_bed_head_against_wall"] = report["bed_head_wall"] is not None
        report["P1_bed_head_not_on_door_wall"] = report["bed_head_wall"] != "door_wall"
        view = door_view_from_bed(bed, *pose(bed), door, width, depth)
        report["door_view_from_bed"] = view
        report["P2_door_in_front"] = view == "in_front"
        if roles[bed["id"]] == "single_bed":
            head = back_wall(bed, *pose(bed), width, depth)
            gaps = wall_gaps(corners(bed, *pose(bed)), width, depth)
            sides = [w for w in ABSOLUTE_WALLS if w not in {head, OPPOSITE.get(head)} and gaps[w] <= AGAINST_WALL_TOLERANCE_M]
            report["P5_single_bed_in_corner"] = bool(head and sides)
    wardrobes = [i for i in items if roles[i["id"]] == "wardrobe"]
    if wardrobes:
        walls = [rel_back(w) for w in wardrobes]
        report["P3_wardrobe_near_door"] = sum(w not in {None, "opposite_door"} for w in walls) / len(walls)
    desks = [i for i in items if roles[i["id"]] == "desk"]
    if desks:
        walls = [rel_back(d) for d in desks]
        report["P4_desk_not_on_door_wall"] = all(w != "door_wall" for w in walls)
        if beds and report.get("bed_head_wall"):
            report["P6_desk_beside_bed_head"] = any(w == report["bed_head_wall"] for w in walls)
    checks = [v for k, v in report.items() if k.startswith("P") and v is not None]
    report["score"] = round(sum(float(v) for v in checks) / len(checks), 3) if checks else None
    return report
