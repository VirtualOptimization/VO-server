"""Layout score shown to users (100 points) for the exhibition ranking.

The score reuses the optimizer's own checks so a user's layout and the AI
layout are judged by the same rules. Every part gives partial credit, so small
moves change the score and ties are rare.

    Disqualified  furniture overlapping, or pushed out of the room
    Safety      30  door clearance kept free (15)
                    + room to use each piece: bed side, desk front, storage front (15)
    Principles  30  LH standard bedroom plan principles (see lh_principles.py)
    Space       40  usable open floor (LH passage width) relative to the AI layout

Scores are reported to one decimal place.

Clearance distances are from LH 주택평면계획기준 연구, 그림 6-39 (bedroom
movement dimensions) and the bedroom design guideline on p.188.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from workers.optimizer import lh_principles as lh
from workers.optimizer.layout import CanonicalLayoutOptimizer

MAX_DOOR = 15
MAX_USE = 15
MAX_SAFETY = MAX_DOOR + MAX_USE
MAX_PRINCIPLES = 30
MAX_SPACE = 40
MAX_TOTAL = MAX_SAFETY + MAX_PRINCIPLES + MAX_SPACE

# Overlap or protrusion users can leave by dragging imprecisely, and that a scan
# itself shows (a chair tucked under its desk is allowed separately).
DISQUALIFY_TOLERANCE_M = 0.03

# role -> (which side needs room, distance in metres, Korean label)
USE_CLEARANCE: dict[str, tuple[str, float, str]] = {
    "bed": ("long_side", 0.65, "침대 옆"),            # LH 그림 6-39 침대 정리하기
    "single_bed": ("long_side", 0.65, "침대 옆"),
    "desk": ("front", 0.75, "책상 앞"),               # LH 그림 6-39 책상에 앉기
    "table": ("front", 0.75, "테이블 앞"),
    "wardrobe": ("front", 0.9, "옷장 앞"),            # LH 그림 6-39 옷장 열기
    "low_storage": ("front", 0.7, "수납장 앞"),       # LH 그림 6-39 책장에 책 넣고 빼기
}

DOOR_VIEW_CREDIT = {"in_front": 1.0, "beside": 0.5, "behind": 0.0}
# Usable area is measured on a 10 cm grid, so differences below a few cells are noise.
BEAT_AI_MARGIN_M2 = 0.05
PROBE_STEP_M = 0.05


def _coords(optimizer: CanonicalLayoutOptimizer) -> np.ndarray:
    return np.array([optimizer._coords_from_furniture(f) for f in optimizer.furnitures], dtype=float)


def _footprints(optimizer: CanonicalLayoutOptimizer, coords: np.ndarray) -> list[np.ndarray]:
    return [
        optimizer._obb_corners(c[0], c[1], float(f["extent"][0]), float(f["extent"][1]), c[3])
        for c, f in zip(coords, optimizer.furnitures)
    ]


def _protrusion(optimizer: CanonicalLayoutOptimizer, footprints: list[np.ndarray]) -> float:
    worst = 0.0
    for corners in footprints:
        worst = max(
            worst,
            float(-corners[:, 0].min()),
            float(corners[:, 0].max() - optimizer.room_width),
            float(-corners[:, 1].min()),
            float(corners[:, 1].max() - optimizer.room_depth),
        )
    return worst


def _inside(points: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """Which points fall inside a (possibly rotated) rectangle."""
    origin = corners[0]
    u = corners[1] - origin
    v = corners[3] - origin
    rel = points - origin
    a = rel @ u / max(float(u @ u), 1e-12)
    b = rel @ v / max(float(v @ v), 1e-12)
    return (a >= 0.0) & (a <= 1.0) & (b >= 0.0) & (b <= 1.0)


def _door_credit(optimizer: CanonicalLayoutOptimizer, footprints: list[np.ndarray]) -> tuple[float, list[str]]:
    doors = [e for e in optimizer.fixed_elements if e.get("type") == "door" and e.get("wall") in lh.ABSOLUTE_WALLS]
    if not doors:
        return 1.0, []
    floor_items = [fp for i, fp in enumerate(footprints) if not optimizer._is_wall_mounted(i)]
    credits = []
    for door in doors:
        zone = optimizer._door_clearance_polygon(door)
        xs = np.arange(zone[:, 0].min() + 0.025, zone[:, 0].max(), 0.05)
        ys = np.arange(zone[:, 1].min() + 0.025, zone[:, 1].max(), 0.05)
        if len(xs) == 0 or len(ys) == 0:
            continue
        gx, gy = np.meshgrid(xs, ys)
        points = np.column_stack([gx.ravel(), gy.ravel()])
        covered = np.zeros(len(points), dtype=bool)
        for fp in floor_items:
            covered |= _inside(points, fp)
        credits.append(1.0 - float(covered.mean()))
    if not credits:
        return 1.0, []
    credit = sum(credits) / len(credits)
    return credit, ([] if credit >= 0.999 else ["문 앞이 가구로 막혀 있어요"])


def _free_depth(
    optimizer: CanonicalLayoutOptimizer,
    index: int,
    coords: np.ndarray,
    footprints: list[np.ndarray],
    direction: np.ndarray,
    needed: float,
) -> float:
    """How far (up to `needed`) the strip in front of the face toward `direction` stays free."""
    ids = {f["id"]: i for i, f in enumerate(optimizer.furnitures)}
    center = coords[index, :2]
    lateral = np.array([-direction[1], direction[0]])
    offsets = footprints[index] - center
    reach = float((offsets @ direction).max())
    width = 2.0 * float(np.abs(offsets @ lateral).max())
    free = 0.0
    step = PROBE_STEP_M
    while free + step <= needed + 1e-9:
        near = center + direction * (reach + free)
        far = center + direction * (reach + free + step)
        strip = np.array([
            near - lateral * width / 2.0,
            near + lateral * width / 2.0,
            far + lateral * width / 2.0,
            far - lateral * width / 2.0,
        ])
        if (
            strip[:, 0].min() < -1e-6 or strip[:, 0].max() > optimizer.room_width + 1e-6
            or strip[:, 1].min() < -1e-6 or strip[:, 1].max() > optimizer.room_depth + 1e-6
        ):
            break
        blocked = False
        for j, other in enumerate(footprints):
            if j == index or optimizer._is_wall_mounted(j) or optimizer._vertically_apart(index, j):
                continue
            if optimizer._is_paired_relationship(index, j, ids):
                continue  # a chair in front of its own desk is how the desk is used
            if CanonicalLayoutOptimizer._sat_penetration(strip, other) > 1e-3:
                blocked = True
                break
        if blocked:
            break
        free += step
    return min(free, needed)


def _use_credit(
    optimizer: CanonicalLayoutOptimizer, coords: np.ndarray, footprints: list[np.ndarray], roles: list[str]
) -> tuple[float, list[str]]:
    credits = []
    hints = []
    for i, item in enumerate(optimizer.furnitures):
        rule = USE_CLEARANCE.get(roles[i])
        if rule is None or optimizer._is_wall_mounted(i):
            continue
        side, needed, label = rule
        theta = float(coords[i, 3])
        front = optimizer._world_front(item, theta)
        lateral = np.array([-front[1], front[0]])
        if side == "front":
            got = _free_depth(optimizer, i, coords, footprints, front, needed)
        else:
            # One free long side is enough to get in and make the bed.
            got = max(
                _free_depth(optimizer, i, coords, footprints, lateral, needed),
                _free_depth(optimizer, i, coords, footprints, -lateral, needed),
            )
        ratio = got / needed
        credits.append(ratio)
        if ratio < 0.999:
            hints.append(f"{label} 공간이 {round(got * 100)}cm예요 (권장 {round(needed * 100)}cm)")
    if not credits:
        return 1.0, []
    return sum(credits) / len(credits), hints


def _principle_credit(optimizer: CanonicalLayoutOptimizer, coords: np.ndarray) -> tuple[float, list[str]]:
    context = optimizer._lh_context()
    if context is None:
        return 1.0, []
    walls, door, roles = context
    width, depth = optimizer.room_width, optimizer.room_depth
    checks: list[float] = []
    hints: list[str] = []

    def back(i: int) -> str | None:
        return lh.back_wall(optimizer.furnitures[i], coords[i, 0], coords[i, 1], coords[i, 3], width, depth)

    for i, item_role in enumerate(roles):
        if item_role in {"bed", "single_bed"}:
            head = back(i)
            checks.append(1.0 if head is not None else 0.0)
            if head is None:
                hints.append("침대 머리를 벽에 붙여보세요")
            checks.append(0.0 if head == walls["door_wall"] else 1.0)
            if head == walls["door_wall"]:
                hints.append("침대 머리가 문 있는 벽에 있어요")
            view = lh.door_view_from_bed(optimizer.furnitures[i], coords[i, 0], coords[i, 1], coords[i, 3], door, width, depth)
            checks.append(DOOR_VIEW_CREDIT[view])
            if view != "in_front":
                hints.append("누웠을 때 문이 앞에 보이면 더 좋아요")
        elif item_role == "wardrobe":
            on_opposite = back(i) == walls["opposite_door"]
            checks.append(0.0 if on_opposite else 1.0)
            if on_opposite:
                hints.append("옷장은 문 쪽 벽에 두면 좋아요")
        elif item_role == "desk":
            on_door_wall = back(i) == walls["door_wall"]
            checks.append(0.0 if on_door_wall else 1.0)
            if on_door_wall:
                hints.append("책상이 문 있는 벽을 등지고 있어요")
    if not checks:
        return 1.0, []
    return sum(checks) / len(checks), hints


def score_layout(problem: dict[str, Any], reference_usable_m2: float | None = None) -> dict[str, Any]:
    """Score the furniture poses in `problem` (each item's `pos` / `rotation_y_deg`).

    `reference_usable_m2` is the AI layout's usable open area for the same room;
    the space part is measured against it. Without it, half the floor area is
    used as the reference.
    """
    optimizer = CanonicalLayoutOptimizer(problem)
    coords = _coords(optimizer)
    footprints = _footprints(optimizer, coords)
    usable = float(optimizer._usable_open_area(coords))
    result: dict[str, Any] = {
        "max_score": MAX_TOTAL,
        "usable_area_m2": round(usable, 2),
        "room_area_m2": round(optimizer.room_width * optimizer.room_depth, 2),
    }

    overlap = optimizer._collision_penetration(coords)
    protrusion = _protrusion(optimizer, footprints)
    if overlap > DISQUALIFY_TOLERANCE_M or protrusion > DISQUALIFY_TOLERANCE_M:
        reasons = []
        if overlap > DISQUALIFY_TOLERANCE_M:
            reasons.append("가구끼리 겹쳐 있어요")
        if protrusion > DISQUALIFY_TOLERANCE_M:
            reasons.append("가구가 방 밖으로 나가 있어요")
        return {**result, "disqualified": True, "total_score": 0.0, "breakdown": None, "beat_ai": False, "hints": reasons}

    roles = [lh.role(f, optimizer.furnitures) for f in optimizer.furnitures]
    door_credit, door_hints = _door_credit(optimizer, footprints)
    use_credit, use_hints = _use_credit(optimizer, coords, footprints, roles)
    principle_credit, principle_hints = _principle_credit(optimizer, coords)

    reference = reference_usable_m2 if reference_usable_m2 and reference_usable_m2 > 0 else 0.5 * result["room_area_m2"]
    space_ratio = usable / reference if reference > 0 else 0.0

    door = round(MAX_DOOR * door_credit, 1)
    use = round(MAX_USE * use_credit, 1)
    principles = round(MAX_PRINCIPLES * principle_credit, 1)
    space = round(MAX_SPACE * min(space_ratio, 1.0), 1)
    hints = door_hints + use_hints + principle_hints
    if space_ratio < 0.99:
        hints.append(f"빈 공간이 AI 배치의 {round(space_ratio * 100)}%예요")
    return {
        **result,
        "disqualified": False,
        "total_score": round(door + use + principles + space, 1),
        "breakdown": {
            "safety": round(door + use, 1),
            "door_clearance": door,
            "furniture_use": use,
            "principles": principles,
            "space": space,
        },
        "beat_ai": bool(reference_usable_m2) and usable > reference_usable_m2 + BEAT_AI_MARGIN_M2,
        "hints": hints,
    }
