"""Load validated Neufert layout rules into the optimizer problem."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


ENTITY_ALIASES = {
    "wardrobe": "closet",
    "closet": "closet",
    "storage": "shelf",
    "shelf": "shelf",
    "desk": "table",
    "dining table": "table",
    "bedside table": "bedside_table",
    "banyo küveti": "bathtub",
}


def _entity(value: Any) -> str:
    key = str(value or "").strip().lower()
    return ENTITY_ALIASES.get(key, key)


def load_neufert_rules(path: str | Path | None) -> list[dict[str, Any]]:
    """Read JSONL rules, keeping only well-formed parsed rules."""
    if not path:
        return []
    source = Path(path).expanduser()
    if not source.exists():
        raise FileNotFoundError(f"Neufert rule file does not exist: {source}")

    rules: list[dict[str, Any]] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        rule = row.get("parsed_rule") or {}
        if not rule.get("subject") or not rule.get("relation"):
            continue
        rules.append({"page_number": row.get("page_number"), **rule})
    return rules


SMALL_ROOM_AREA_M2 = 12.0
SMALL_ROOM_RELAXATION_FLOOR = 0.7


def _small_room_scale(room_area: float) -> float:
    """Scale factor for Neufert "ideal" (recommended-only) distances in a tight room.

    Neufert's explicit min_distance_m is a real ergonomic/access floor and is never
    shrunk. A recommended_distance_m with no accompanying min is only the *ideal*
    spacing, so in a small single-occupant room -- where floor area is the scarce
    resource -- it can reasonably shrink toward SMALL_ROOM_RELAXATION_FLOOR instead
    of being enforced as if it were a hard minimum.
    """
    if room_area <= 0 or room_area >= SMALL_ROOM_AREA_M2:
        return 1.0
    return SMALL_ROOM_RELAXATION_FLOOR + (1.0 - SMALL_ROOM_RELAXATION_FLOOR) * (room_area / SMALL_ROOM_AREA_M2)


def apply_neufert_rules(problem: dict[str, Any], path: str | Path | None) -> dict[str, Any]:
    """Attach Neufert rules and promote numeric rules to optimizer clearances."""
    rules = load_neufert_rules(path)
    if not rules:
        return problem

    items = problem.get("movable_items", [])
    by_type: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_type.setdefault(_entity(item.get("type")), []).append(item)
    attached = 0

    room_dims = problem.get("room_metadata", {}).get("dimensions", {})
    room_area = float(room_dims.get("width", 0.0)) * float(room_dims.get("depth", 0.0))
    relaxation_scale = _small_room_scale(room_area)
    problem["room_area_m2"] = round(room_area, 3)
    problem["small_room_relaxation_scale"] = round(relaxation_scale, 3)

    for item in items:
        item.setdefault("placement_rules", {})["neufert_rules"] = []

    for rule in rules:
        subject = _entity(rule.get("subject"))
        matched_items = by_type.get(subject, [])
        if not matched_items:
            continue

        attached += len(matched_items)
        for item in matched_items:
            applied_rule = dict(rule)
            item["placement_rules"]["neufert_rules"].append(applied_rule)

            min_value = rule.get("min_distance_m")
            if min_value is not None and float(min_value) > 0:
                value = float(min_value)
                applied_rule["applied_value_m"] = round(value, 4)
                applied_rule["applied_from"] = "min_distance_m"
            else:
                recommended_value = rule.get("recommended_distance_m")
                if recommended_value is None or float(recommended_value) <= 0:
                    applied_rule["applied_value_m"] = None
                    applied_rule["applied_from"] = "qualitative_only"
                    continue
                value = float(recommended_value) * relaxation_scale
                applied_rule["applied_value_m"] = round(value, 4)
                applied_rule["applied_from"] = "recommended_distance_m"
                applied_rule["relaxation_scale_applied"] = round(relaxation_scale, 3)

            if rule.get("validation_only"):
                # This rule is for neufert_validation / AI-recovery triggering
                # only. Promoting it into front_clearance/side_clearance would
                # let it compete with hard furniture-collision avoidance in
                # the SLSQP objective -- measured on real scans to sometimes
                # worsen actual overlap (Tier 0) in already-crowded rooms.
                continue

            relation = str(rule.get("relation", ""))
            direction = str(rule.get("direction", ""))
            numeric = float(value)
            rules_for_item = item["placement_rules"]

            if relation in {"passage", "clearance"}:
                if direction == "front":
                    rules_for_item["front_clearance"] = max(
                        float(rules_for_item.get("front_clearance", 0.0)), numeric
                    )
                elif direction in {"side", "around"}:
                    rules_for_item["side_clearance"] = max(
                        float(rules_for_item.get("side_clearance", 0.0)), numeric
                    )
                    if direction == "around":
                        rules_for_item["front_clearance"] = max(
                            float(rules_for_item.get("front_clearance", 0.0)), numeric
                        )

            if relation in {"to_wall", "against"} or direction == "against":
                item.setdefault("anchor_preferences", {})["back_to_wall"] = True

    problem["neufert_rules"] = rules
    problem["neufert_rules_attached"] = attached
    return problem


CLEARANCE_RELATIONS = {"around", "clearance", "passage"}
# A piece whose back is this close to a wall is wall-standing furniture.
BACKED_AGAINST_WALL_M = 0.15
# Another piece counts as in front of / beside a piece only when they face each
# other over at least this width; a corner that merely lines up is not in the way.
MIN_FACING_OVERLAP_M = 0.05
# A piece is unusable when the room on the side it is used from is under this
# share of the rule's requirement (a shelf you cannot stand in front of).
UNUSABLE_SHARE = 0.5
USE_DIRECTIONS = {"front", "around"}


def _rule_requirement(rule: dict[str, Any], relaxation_scale: float) -> float | None:
    minimum = rule.get("min_distance_m")
    if minimum is not None:
        return float(minimum)
    recommended = rule.get("recommended_distance_m")
    if recommended is None:
        return None
    return float(recommended) * relaxation_scale


def dedupe_validation_rules(rules: list[dict[str, Any]], relaxation_scale: float = 1.0) -> list[dict[str, Any]]:
    """Keep one clearance rule per subject, object and direction.

    The handbook states the same clearance on several pages with slightly
    different figures (shelf front 0.9 and 1.0 m, bed beside a shelf 0.75, 0.9
    and 1.0 m). Checking each copy counts one tight spot several times, so only
    the smallest figure -- the handbook's actual minimum -- is kept.
    """
    kept: dict[tuple[str, ...], dict[str, Any]] = {}
    order: list[tuple[str, ...]] = []
    for index, rule in enumerate(rules):
        relation = str(rule.get("relation", ""))
        required = _rule_requirement(rule, relaxation_scale)
        if relation not in CLEARANCE_RELATIONS or required is None:
            key: tuple[str, ...] = ("unique", str(index))
        else:
            key = (_entity(rule.get("subject")), _entity(rule.get("object")), str(rule.get("direction", "around")))
        if key not in kept:
            order.append(key)
            kept[key] = rule
            continue
        current = _rule_requirement(kept[key], relaxation_scale)
        merged = rule if required < float(current) else kept[key]
        pages = sorted({*kept[key].get("merged_pages", [kept[key].get("page_number")]), rule.get("page_number")} - {None})
        kept[key] = {**merged, "merged_pages": pages}
    return [kept[key] for key in order]


def validate_neufert_rules(problem: dict[str, Any], optimized: dict[str, Any]) -> dict[str, Any]:
    """Report directional rule checks against the final 2-D placement.

    Each failed check also records how far short it falls, and the summary sums
    those shortfalls: a furnished room can rarely meet every clearance, so the
    useful question is how much is missing, not how many checks miss.
    """
    room = problem.get("room_metadata", {}).get("dimensions", {})
    room_width = float(room.get("width", 0.0))
    room_depth = float(room.get("depth", 0.0))
    items = optimized.get("movable_items", [])
    by_subject: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        by_subject.setdefault(_entity(item.get("type")), []).append(item)

    def corners(item: dict[str, Any]) -> list[tuple[float, float]]:
        pos = item.get("optimized_pos") or item.get("pos") or [0.0, 0.0]
        extent = item.get("extent") or [0.0, 0.0]
        theta = math.radians(float(item.get("optimized_rotation_y_deg", item.get("rotation_y_deg", 0.0))))
        c, s = math.cos(theta), math.sin(theta)
        half_w, half_d = float(extent[0]) / 2.0, float(extent[1]) / 2.0
        local = [(-half_w, -half_d), (half_w, -half_d), (half_w, half_d), (-half_w, half_d)]
        return [(float(pos[0]) + x * c - y * s, float(pos[1]) + x * s + y * c) for x, y in local]

    def wall_distance(poly: list[tuple[float, float]]) -> float:
        xs = [point[0] for point in poly]
        ys = [point[1] for point in poly]
        return min(min(xs), room_width - max(xs), min(ys), room_depth - max(ys))

    def normalize(vector: tuple[float, float], fallback: tuple[float, float]) -> tuple[float, float]:
        length = math.hypot(vector[0], vector[1])
        if length <= 1e-9:
            return fallback
        return vector[0] / length, vector[1] / length

    def projection(poly: list[tuple[float, float]], axis: tuple[float, float]) -> tuple[float, float]:
        values = [point[0] * axis[0] + point[1] * axis[1] for point in poly]
        return min(values), max(values)

    def polygon_axes(poly: list[tuple[float, float]]) -> list[tuple[float, float]]:
        axes: list[tuple[float, float]] = []
        for index, point in enumerate(poly):
            other = poly[(index + 1) % len(poly)]
            edge = (other[0] - point[0], other[1] - point[1])
            axes.append(normalize((-edge[1], edge[0]), (1.0, 0.0)))
        return axes

    def polygons_overlap(first: list[tuple[float, float]], second: list[tuple[float, float]]) -> bool:
        for axis in polygon_axes(first) + polygon_axes(second):
            first_min, first_max = projection(first, axis)
            second_min, second_max = projection(second, axis)
            if min(first_max, second_max) - max(first_min, second_min) <= 1e-6:
                return False
        return True

    def point_segment_distance(
        point: tuple[float, float],
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> float:
        edge = (end[0] - start[0], end[1] - start[1])
        length_sq = edge[0] ** 2 + edge[1] ** 2
        if length_sq <= 1e-12:
            return math.hypot(point[0] - start[0], point[1] - start[1])
        offset = (point[0] - start[0], point[1] - start[1])
        ratio = max(0.0, min(1.0, (offset[0] * edge[0] + offset[1] * edge[1]) / length_sq))
        nearest = (start[0] + edge[0] * ratio, start[1] + edge[1] * ratio)
        return math.hypot(point[0] - nearest[0], point[1] - nearest[1])

    def polygon_gap(first: list[tuple[float, float]], second: list[tuple[float, float]]) -> float:
        if polygons_overlap(first, second):
            return 0.0
        distances: list[float] = []
        for source, target in ((first, second), (second, first)):
            for point in source:
                for index, start in enumerate(target):
                    distances.append(point_segment_distance(point, start, target[(index + 1) % len(target)]))
        return min(distances)

    def world_front(item: dict[str, Any]) -> tuple[float, float]:
        base_raw = item.get("front_vector_2d") or [0.0, -1.0]
        base = normalize((float(base_raw[0]), float(base_raw[1])), (0.0, -1.0))
        original = math.radians(float(item.get("rotation_y_deg", 0.0)))
        optimized_theta = math.radians(
            float(item.get("optimized_rotation_y_deg", item.get("rotation_y_deg", 0.0)))
        )
        delta = optimized_theta - original
        c, s = math.cos(delta), math.sin(delta)
        return normalize((base[0] * c - base[1] * s, base[0] * s + base[1] * c), (0.0, -1.0))

    def directional_polygon_gap(
        subject_poly: list[tuple[float, float]],
        target_poly: list[tuple[float, float]],
        direction: tuple[float, float],
    ) -> float | None:
        forward = normalize(direction, (0.0, -1.0))
        lateral = (-forward[1], forward[0])
        subject_cross = projection(subject_poly, lateral)
        target_cross = projection(target_poly, lateral)
        cross_overlap = min(subject_cross[1], target_cross[1]) - max(subject_cross[0], target_cross[0])
        if cross_overlap <= MIN_FACING_OVERLAP_M:
            return None

        subject_forward = projection(subject_poly, forward)
        target_forward = projection(target_poly, forward)
        subject_center = sum(subject_forward) / 2.0
        target_center = sum(target_forward) / 2.0
        if target_center <= subject_center + 1e-6:
            return None
        return max(target_forward[0] - subject_forward[1], 0.0)

    def room_gap_in_direction(
        poly: list[tuple[float, float]],
        direction: tuple[float, float],
    ) -> float:
        dx, dy = normalize(direction, (0.0, -1.0))
        distances: list[float] = []
        for x, y in poly:
            candidates: list[float] = []
            if dx > 1e-9:
                candidates.append((room_width - x) / dx)
            elif dx < -1e-9:
                candidates.append((0.0 - x) / dx)
            if dy > 1e-9:
                candidates.append((room_depth - y) / dy)
            elif dy < -1e-9:
                candidates.append((0.0 - y) / dy)
            positive = [value for value in candidates if value >= 0.0]
            if positive:
                distances.append(min(positive))
        return max(0.0, min(distances, default=0.0))

    def is_functional_pair(first: dict[str, Any], second: dict[str, Any]) -> bool:
        first_id = first.get("id")
        second_id = second.get("id")
        return first.get("pair_with") == second_id or second.get("pair_with") == first_id

    def relevant_targets(item: dict[str, Any], object_name: str) -> list[dict[str, Any]]:
        if object_name in {"room", "wall"}:
            # Chairs at a table are how the table is used, not obstacles to it.
            seats_ignored = _entity(item.get("type")) in {"table", "desk"}
            return [
                other
                for other in items
                if other is not item
                and not is_functional_pair(item, other)
                and not (seats_ignored and _entity(other.get("type")) == "chair")
            ]
        return [
            other
            for other in by_subject.get(object_name, [])
            if other is not item and not is_functional_pair(item, other)
        ]

    def open_side_gap(item: dict[str, Any], targets: list[dict[str, Any]], *, walls_only: bool = False) -> float:
        """Free distance on the item's most open side.

        An `around` rule asks for room to use the piece, and that room only has to
        exist on one side: a bed against a wall, or a chair pushed in at a table,
        is still usable. Measuring the nearest obstacle on any side instead failed
        nearly every piece that touched anything.
        """
        poly = item_corners[id(item)]
        front = world_front(item)
        lateral = (-front[1], front[0])
        best = 0.0
        for axis in (front, (-front[0], -front[1]), lateral, (-lateral[0], -lateral[1])):
            free = room_gap_in_direction(poly, axis)
            if not walls_only:
                for other in targets:
                    gap = directional_polygon_gap(poly, item_corners[id(other)], axis)
                    if gap is not None:
                        free = min(free, gap)
            best = max(best, free)
        return best

    def directional_gaps(
        item: dict[str, Any],
        targets: list[dict[str, Any]],
        direction: str,
    ) -> list[float]:
        poly = item_corners[id(item)]
        front = world_front(item)
        lateral = (-front[1], front[0])
        if direction == "front":
            directions = [front]
        elif direction == "side":
            directions = [lateral, (-lateral[0], -lateral[1])]
        else:
            return [polygon_gap(poly, item_corners[id(other)]) for other in targets]

        gaps: list[float] = []
        for axis in directions:
            for other in targets:
                gap = directional_polygon_gap(poly, item_corners[id(other)], axis)
                if gap is not None:
                    gaps.append(gap)
        return gaps

    relaxation_scale = float(problem.get("small_room_relaxation_scale", 1.0))
    item_corners = {id(item): corners(item) for item in items}
    checks: list[dict[str, Any]] = []
    for rule in dedupe_validation_rules(problem.get("neufert_rules", []), relaxation_scale):
        subject = _entity(rule.get("subject"))
        matched = by_subject.get(subject, [])
        numeric = _rule_requirement(rule, relaxation_scale)

        for item in matched:
            check = {
                "page_number": rule.get("page_number"),
                "subject": rule.get("subject"),
                "object": rule.get("object"),
                "relation": rule.get("relation"),
                "direction": rule.get("direction"),
                "item_id": item.get("id"),
                "required_distance_m": numeric,
                "evidence": rule.get("evidence"),
            }
            if rule.get("merged_pages"):
                check["merged_pages"] = rule["merged_pages"]
            if numeric is None:
                check["status"] = "qualitative_only"
                checks.append(check)
                continue

            poly = item_corners[id(item)]
            item_wall_distance = wall_distance(poly)
            relation = str(rule.get("relation", ""))
            object_name = _entity(rule.get("object"))
            if relation in {"to_wall", "against"}:
                measured = item_wall_distance
            elif object_name == "wall":
                direction = str(rule.get("direction", "around"))
                front = world_front(item)
                lateral = (-front[1], front[0])
                if direction == "side" and room_gap_in_direction(poly, (-front[0], -front[1])) <= BACKED_AGAINST_WALL_M:
                    # A shelf or desk standing with its back to a wall is meant to
                    # sit along it; a gap to the next wall beside it is not needed.
                    check["measured_distance_m"] = None
                    check["status"] = "not_applicable"
                    check["reason"] = "backed_against_wall"
                    checks.append(check)
                    continue
                if direction == "front":
                    measured = room_gap_in_direction(poly, front)
                elif direction == "side":
                    measured = min(
                        room_gap_in_direction(poly, lateral),
                        room_gap_in_direction(poly, (-lateral[0], -lateral[1])),
                    )
                else:
                    measured = open_side_gap(item, [], walls_only=True)
            elif str(rule.get("direction", "around")) == "around" and object_name == "room":
                measured = open_side_gap(item, relevant_targets(item, object_name))
            else:
                direction = str(rule.get("direction", "around"))
                targets = relevant_targets(item, object_name)
                gaps = directional_gaps(item, targets, direction)

                if object_name == "room" and direction == "front":
                    gaps.append(room_gap_in_direction(poly, world_front(item)))
                elif object_name == "room" and direction == "side":
                    front = world_front(item)
                    lateral = (-front[1], front[0])
                    gaps.extend(
                        [
                            room_gap_in_direction(poly, lateral),
                            room_gap_in_direction(poly, (-lateral[0], -lateral[1])),
                        ]
                    )

                if not gaps:
                    if object_name == "room":
                        check["measured_distance_m"] = None
                        check["status"] = "pass"
                        check["reason"] = "no_obstacle_in_direction"
                    else:
                        check["measured_distance_m"] = None
                        check["status"] = "not_applicable"
                        check["reason"] = "no_matching_target_in_direction"
                    checks.append(check)
                    continue
                measured = min(gaps)

            measured = max(0.0, measured)
            check["measured_distance_m"] = round(measured, 4)
            passed = measured + 1e-3 >= float(numeric)
            check["status"] = "pass" if passed else "fail"
            if not passed:
                check["shortfall_m"] = round(float(numeric) - measured, 4)
            checks.append(check)

    summary = {
        "total": len(checks),
        "pass": sum(c["status"] == "pass" for c in checks),
        "fail": sum(c["status"] == "fail" for c in checks),
        "qualitative_only": sum(c["status"] == "qualitative_only" for c in checks),
        "not_applicable": sum(c["status"] == "not_applicable" for c in checks),
        "shortfall_m": round(sum(c.get("shortfall_m", 0.0) for c in checks), 3),
        "unusable_items": sorted(
            {
                str(c["item_id"])
                for c in checks
                if c["status"] == "fail"
                and _entity(c.get("object")) == "room"
                and str(c.get("direction")) in USE_DIRECTIONS
                and float(c.get("measured_distance_m") or 0.0) < UNUSABLE_SHARE * float(c["required_distance_m"])
            }
        ),
    }
    optimized["neufert_validation"] = {"summary": summary, "checks": checks}
    return optimized
