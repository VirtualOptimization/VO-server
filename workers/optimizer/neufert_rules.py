"""Load validated Neufert layout rules into the optimizer problem."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


ENTITY_ALIASES = {
    "wardrobe": "closet",
    "closet": "closet",
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


def validate_neufert_rules(problem: dict[str, Any], optimized: dict[str, Any]) -> dict[str, Any]:
    """Report rule checks against the final 2-D furniture placement.

    This is intentionally a conservative, axis-aligned check for the first
    validation pass. Qualitative rules are reported separately instead of
    being treated as failed numeric constraints.
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

    def polygon_gap(first: list[tuple[float, float]], second: list[tuple[float, float]]) -> float:
        axes: list[tuple[float, float]] = []
        for poly in (first, second):
            for index, point in enumerate(poly):
                other = poly[(index + 1) % len(poly)]
                edge = (other[0] - point[0], other[1] - point[1])
                length = math.hypot(edge[0], edge[1]) or 1.0
                axes.append((-edge[1] / length, edge[0] / length))
        gaps: list[float] = []
        for axis in axes:
            first_projection = [point[0] * axis[0] + point[1] * axis[1] for point in first]
            second_projection = [point[0] * axis[0] + point[1] * axis[1] for point in second]
            gap = max(
                min(second_projection) - max(first_projection),
                min(first_projection) - max(second_projection),
                0.0,
            )
            gaps.append(gap)
        # Two separated convex polygons are guaranteed a positive gap on at
        # least one candidate axis (the true separating axis); the other
        # axes typically still show projection overlap and report 0. The
        # real separation distance is therefore the *largest* gap found,
        # not the smallest -- mirroring _sat_gap_vector in layout.py.
        return max(gaps)

    item_corners = {id(item): corners(item) for item in items}
    checks: list[dict[str, Any]] = []
    for rule in problem.get("neufert_rules", []):
        subject = _entity(rule.get("subject"))
        matched = by_subject.get(subject, [])
        numeric = rule.get("min_distance_m")
        if numeric is None:
            numeric = rule.get("recommended_distance_m")

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
            else:
                # An ``around`` rule describes usable space around the item;
                # a wall-contact side is intentionally not treated as a
                # failed passage. This matters for beds placed lengthwise
                # against a wall.
                measured = float("inf") if relation == "around" and object_name == "room" else wall_distance(poly)
                for other in items:
                    if other is item:
                        continue
                    measured = min(measured, polygon_gap(poly, item_corners[id(other)]))
                if not math.isfinite(measured):
                    measured = item_wall_distance

            check["measured_distance_m"] = round(max(0.0, measured), 4)
            check["status"] = "pass" if measured + 1e-3 >= float(numeric) else "fail"
            checks.append(check)

    summary = {
        "total": len(checks),
        "pass": sum(c["status"] == "pass" for c in checks),
        "fail": sum(c["status"] == "fail" for c in checks),
        "qualitative_only": sum(c["status"] == "qualitative_only" for c in checks),
    }
    optimized["neufert_validation"] = {"summary": summary, "checks": checks}
    return optimized
