"""Optional AI-generated layout constraints for the room optimizer."""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from typing import Any


SUPPORTED_PROVIDER = "gemini"
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_TIMEOUT_SECONDS = 15
BACK_TO_WALL_TYPES = {"bed", "closet", "cabinet", "shelf", "storage"}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def maybe_apply_ai_constraints(problem: dict[str, Any]) -> dict[str, Any]:
    """Ask an AI model for soft layout constraints and merge them into a problem.

    The AI does not place furniture directly. It only suggests semantic constraints
    that the deterministic optimizer can score, such as chair-table pairing and
    wall/corner preferences.
    """
    if not env_bool("AI_LAYOUT_ENABLED"):
        problem["ai_used"] = False
        return problem

    provider = os.getenv("AI_LAYOUT_PROVIDER", SUPPORTED_PROVIDER).strip().lower()
    if provider != SUPPORTED_PROVIDER:
        problem["ai_used"] = False
        problem["ai_error"] = f"Unsupported AI layout provider: {provider}"
        return problem

    try:
        constraints = generate_gemini_constraints(problem)
        apply_ai_constraints(problem, constraints)
        problem["ai_used"] = True
        problem["ai_error"] = None
        problem["ai_constraints"] = constraints
    except Exception as exc:  # Keep the optimizer usable even when AI is unavailable.
        problem["ai_used"] = False
        problem["ai_error"] = f"{exc.__class__.__name__}: {exc}"

    return problem


def generate_gemini_constraints(problem: dict[str, Any]) -> dict[str, Any]:
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("GEMINI_API_KEY is required when AI_LAYOUT_ENABLED=true")

    model = os.getenv("GEMINI_LAYOUT_MODEL", DEFAULT_GEMINI_MODEL).strip() or DEFAULT_GEMINI_MODEL
    timeout = float(os.getenv("AI_LAYOUT_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))

    prompt = _build_prompt(_summarize_problem(problem))
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
        },
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Gemini API error {exc.code}: {detail}") from exc

    text = _extract_candidate_text(payload)
    return _validate_constraints(_loads_json_object(text), problem)


def apply_ai_constraints(problem: dict[str, Any], constraints: dict[str, Any]) -> None:
    items = problem.get("movable_items", [])
    items_by_id = {item.get("id"): item for item in items}

    for orientation in constraints.get("orientation_preferences", []):
        item = items_by_id.get(orientation.get("object_id"))
        front_vector = _validated_front_vector(orientation.get("front_vector_2d"))
        if not item or front_vector is None:
            continue

        item["front_vector_2d"] = front_vector
        item["back_vector_2d"] = [-front_vector[0], -front_vector[1]]
        item["orientation_source"] = "ai"

        if orientation.get("back_to_wall") and item.get("type") in BACK_TO_WALL_TYPES:
            anchor_preferences = item.setdefault("anchor_preferences", {})
            anchor_preferences["back_to_wall"] = True
            anchor_preferences.setdefault("wall_cling_required", True)
        if orientation.get("open_side"):
            item["open_side"] = orientation["open_side"]

    for role in constraints.get("furniture_roles", []):
        item = items_by_id.get(role.get("object_id"))
        if not item:
            continue
        item["semantic_role"] = role.get("role")

    for priority in constraints.get("layout_priorities", []):
        item = items_by_id.get(priority.get("object_id"))
        if not item:
            continue
        item_priorities = item.setdefault("layout_priorities", [])
        item_priorities.append(
            {
                "priority": priority.get("priority", "unknown"),
                "level": priority.get("level", "medium"),
                "reason": priority.get("reason", ""),
                "source": "ai",
            }
        )

        if priority.get("priority") == "wall_attachment" and item.get("type") in BACK_TO_WALL_TYPES:
            anchor_preferences = item.setdefault("anchor_preferences", {})
            anchor_preferences["wall_cling_required"] = True
            anchor_preferences["back_to_wall"] = True

    applied_support_counts: dict[str, int] = {}
    ai_paired_chairs: set[str] = set()

    for group in constraints.get("support_groups", []):
        if not isinstance(group, dict):
            continue
        support = items_by_id.get(group.get("support_id"))
        if not support or support.get("type") not in {"table", "desk"}:
            continue

        role = group.get("role")
        if role:
            support["semantic_role"] = role

        for chair_id in group.get("chair_ids", []):
            chair = items_by_id.get(chair_id)
            if not chair or chair.get("type") != "chair":
                continue
            if _apply_validated_pair(
                chair,
                support,
                strength=group.get("priority", "primary"),
                reason=group.get("reason", ""),
                source="ai_support_group",
                applied_support_counts=applied_support_counts,
                semantic=True,
            ):
                ai_paired_chairs.add(chair["id"])

    # Legacy response compatibility. If the model still returns chair_pairs,
    # accept them only for chairs not already handled by support_groups.
    for pair in constraints.get("chair_pairs", []):
        if not isinstance(pair, dict):
            continue
        chair = items_by_id.get(pair.get("chair_id"))
        support = items_by_id.get(pair.get("support_id"))
        if not chair or not support or chair.get("type") != "chair":
            continue
        if chair.get("id") in ai_paired_chairs:
            continue
        if _apply_validated_pair(
            chair,
            support,
            strength=pair.get("strength", "primary"),
            reason=pair.get("reason", ""),
            source="ai_validated",
            applied_support_counts=applied_support_counts,
            semantic=False,
        ):
            ai_paired_chairs.add(chair["id"])

    _assign_unpaired_chairs_to_supports(problem)

    for preference in constraints.get("wall_preferences", []):
        if not isinstance(preference, dict):
            continue
        item = items_by_id.get(preference.get("object_id"))
        if not item:
            continue

        anchor_preferences = item.setdefault("anchor_preferences", {})
        for key in ("wall_cling_required", "back_to_wall", "corner_preferred"):
            if key in preference:
                anchor_preferences[key] = bool(preference[key])


def _summarize_problem(problem: dict[str, Any]) -> dict[str, Any]:
    room = problem.get("room_metadata", {})
    fixed_elements = problem.get("fixed_elements", [])
    movable_items = problem.get("movable_items", [])
    supports = [item for item in movable_items if item.get("type") in {"table", "desk"}]

    return {
        "room": {
            "width": round(float(room.get("width", 0.0)), 3),
            "depth": round(float(room.get("depth", 0.0)), 3),
            "height": round(float(room.get("height", 0.0)), 3),
        },
        "fixed_elements": [
            {
                "id": element.get("id"),
                "type": element.get("type"),
                "pos": _round_list(element.get("pos", [])),
                "extent": _round_list(element.get("extent", [])),
                "wall": element.get("wall"),
                "span": element.get("span"),
                "clearance_depth": element.get("clearance_depth"),
            }
            for element in fixed_elements
            if element.get("type") in {"door", "window", "opening"}
        ],
        "furniture": [
            {
                "id": item.get("id"),
                "type": item.get("type"),
                "pos": _round_list(item.get("pos", [])),
                "extent": _round_list(item.get("extent", [])),
                "rotation_y_deg": round(float(item.get("rotation_y_deg", 0.0)), 3),
                "existing_pair_with": item.get("pair_with"),
                "relationship": _relationship_summary(item),
                "front_vector_2d": _round_list(item.get("front_vector_2d", [])),
                "back_vector_2d": _round_list(item.get("back_vector_2d", [])),
                "nearest_walls": _nearest_walls(item, room),
                "nearby_supports": _nearby_supports(item, supports) if item.get("type") == "chair" else [],
                "placement_rules": item.get("placement_rules", {}),
            }
            for item in movable_items
        ],
    }


def _build_prompt(summary: dict[str, Any]) -> str:
    schema = {
        "support_groups": [
            {
                "support_id": "table or desk object id",
                "chair_ids": ["chair object id"],
                "role": "dining | work | meeting | unknown",
                "priority": "primary | weak",
                "reason": "short reason",
            }
        ],
        "chair_pairs": [
            {
                "chair_id": "chair object id",
                "support_id": "table or desk object id",
                "strength": "primary | weak",
                "reason": "short reason",
            }
        ],
        "wall_preferences": [
            {
                "object_id": "furniture object id",
                "wall_cling_required": True,
                "back_to_wall": True,
                "corner_preferred": False,
                "reason": "short reason",
            }
        ],
        "orientation_preferences": [
            {
                "object_id": "furniture object id",
                "front_vector_2d": [0.0, 1.0],
                "back_to_wall": True,
                "open_side": "front | left | right | none",
                "reason": "short reason",
            }
        ],
        "furniture_roles": [
            {
                "object_id": "furniture object id",
                "role": "work_surface | dining_surface | seating | sleeping | storage | display | unknown",
                "reason": "short reason",
            }
        ],
        "layout_priorities": [
            {
                "object_id": "furniture object id",
                "priority": "wall_attachment | access_clearance | group_cohesion | orientation | unknown",
                "level": "critical | high | medium | low",
                "reason": "short reason",
            }
        ],
        "rationale": "short explanation",
    }
    return (
        "You are a furniture layout constraint planner. "
        "Return ONLY valid JSON. Do not return markdown. "
        "Do not create final coordinates. Use only object ids that exist in the input. "
        "Suggest semantic constraints for a deterministic optimizer. "
        "Prefer support_groups over chair_pairs. support_groups describe human-intended furniture clusters; "
        "the optimizer will validate exact final placement.\n\n"
        "Rules:\n"
        "- All furniture rotations must stay on 90-degree intervals: 0, 90, 180, or 270 degrees.\n"
        "- Facing a table/desk is secondary to the 90-degree rotation rule; choose the closest cardinal direction that faces the support.\n"
        "- Group chairs with tables/desks only when they are semantically part of the same activity zone.\n"
        "- Do not assign every chair to one support unless all chairs clearly belong around that same table.\n"
        "- When there are multiple tables or desks, split support_groups by spatial group, table size, and expected use.\n"
        "- A chair may belong to at most one support_group. Choose the most plausible support for each chair.\n"
        "- Balance chair groups across multiple plausible tables/desks instead of overloading a single support.\n"
        "- If a chair is uncertain, omit it from support_groups instead of forcing a bad pair.\n"
        "- A chair paired with a support should end up facing that support; mark the support group so the optimizer can rotate it.\n"
        "- Prefer storage furniture such as closet, shelf, cabinet, or bed against a wall when appropriate.\n"
        "- Wall attachment is more important than decorative centering for shelf, cabinet, closet, storage, and bed.\n"
        "- Prefer two-wall corner placement for beds, shelves, cabinets, closets, and bulky storage when it does not block access.\n"
        "- For a bed, put the head/back or one long side against a wall, and prefer a corner so two sides touch walls.\n"
        "- For a shelf/cabinet/closet, infer the front access side and put the back/closed side against a wall; prefer a corner when possible.\n"
        "- Infer each furniture front/back direction when category defaults look ambiguous.\n"
        "- For desks, tables, shelves, closets, and beds, front_vector_2d should point toward the side a person uses or accesses.\n"
        "- A desk chair should stay on the usable/front side of the desk. Do not place desk chairs in door clearance zones.\n"
        "- Door swing/clearance areas are hard constraints. Keep furniture bodies and chair pull-out zones out of doors/openings.\n"
        "- Set back_to_wall only for storage, closet, shelf, cabinet, and bed. Do not set it for ordinary tables or chairs.\n"
        "- Assign a semantic role when it helps distinguish desk/table/storage/seating usage.\n"
        "- Keep door/window access and walking paths in mind.\n\n"
        f"Output schema example:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
        f"Input room summary:\n{json.dumps(summary, ensure_ascii=False)}"
    )


def _extract_candidate_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini response has no candidates")

    parts = candidates[0].get("content", {}).get("parts", [])
    texts = [part.get("text", "") for part in parts if part.get("text")]
    text = "\n".join(texts).strip()
    if not text:
        raise RuntimeError("Gemini response has no text")
    return text


def _loads_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("AI response does not contain a JSON object")
    return json.loads(stripped[start : end + 1])


def _validate_constraints(payload: dict[str, Any], problem: dict[str, Any]) -> dict[str, Any]:
    items_by_id = {item.get("id"): item for item in problem.get("movable_items", [])}
    support_types = {"table", "desk"}
    valid_strengths = {"primary", "weak"}

    support_groups: list[dict[str, Any]] = []
    valid_group_roles = {"dining", "work", "meeting", "unknown"}
    seen_group_pairs: set[tuple[str, str]] = set()
    chair_group_candidates: dict[str, list[tuple[float, dict[str, Any], dict[str, Any]]]] = {}
    for group in payload.get("support_groups", []):
        if not isinstance(group, dict):
            continue
        support = items_by_id.get(group.get("support_id"))
        if not support or support.get("type") not in support_types:
            continue

        chair_ids: list[str] = []
        raw_chair_ids = group.get("chair_ids", [])
        if not isinstance(raw_chair_ids, list):
            raw_chair_ids = []
        for chair_id in raw_chair_ids:
            chair = items_by_id.get(chair_id)
            if not chair or chair.get("type") != "chair":
                continue
            key = (support["id"], chair["id"])
            if key in seen_group_pairs:
                continue
            seen_group_pairs.add(key)
            chair_ids.append(chair["id"])

        role = group.get("role", "unknown")
        if role not in valid_group_roles:
            role = "unknown"
        priority = group.get("priority", "primary")
        if priority not in valid_strengths:
            priority = "primary"

        group_meta = {
            "role": role,
            "priority": priority,
            "reason": str(group.get("reason", ""))[:200],
        }
        for chair_id in chair_ids:
            chair = items_by_id[chair_id]
            chair_group_candidates.setdefault(chair_id, []).append((_distance_2d(chair, support), support, group_meta))

    grouped_chairs: dict[str, list[str]] = {}
    group_meta_by_support: dict[str, dict[str, Any]] = {}
    support_counts: dict[str, int] = {}
    sorted_candidates = sorted(
        chair_group_candidates.items(),
        key=lambda item: min(candidate[0] for candidate in item[1]),
    )
    for chair_id, candidates in sorted_candidates:
        for _, support, group_meta in sorted(candidates, key=lambda candidate: candidate[0]):
            support_id = support["id"]
            if support_counts.get(support_id, 0) >= _support_pair_capacity(support):
                continue
            support_counts[support_id] = support_counts.get(support_id, 0) + 1
            grouped_chairs.setdefault(support_id, []).append(chair_id)
            group_meta_by_support.setdefault(support_id, group_meta)
            break

    for support_id, chair_ids in grouped_chairs.items():
        meta = group_meta_by_support.get(support_id, {})
        support_groups.append(
            {
                "support_id": support_id,
                "chair_ids": chair_ids,
                "role": meta.get("role", "unknown"),
                "priority": meta.get("priority", "primary"),
                "reason": meta.get("reason", ""),
            }
        )

    chair_pairs: list[dict[str, Any]] = []
    for pair in payload.get("chair_pairs", []):
        if not isinstance(pair, dict):
            continue
        chair = items_by_id.get(pair.get("chair_id"))
        support = items_by_id.get(pair.get("support_id"))
        if not chair or not support:
            continue
        if chair.get("type") != "chair" or support.get("type") not in support_types:
            continue

        strength = pair.get("strength", "primary")
        if strength not in valid_strengths:
            strength = "primary"

        chair_pairs.append(
            {
                "chair_id": chair["id"],
                "support_id": support["id"],
                "strength": strength,
                "reason": str(pair.get("reason", ""))[:200],
            }
        )

    wall_preferences: list[dict[str, Any]] = []
    for preference in payload.get("wall_preferences", []):
        if not isinstance(preference, dict):
            continue
        item = items_by_id.get(preference.get("object_id"))
        if not item:
            continue

        wall_preferences.append(
            {
                "object_id": item["id"],
                "wall_cling_required": bool(preference.get("wall_cling_required", False)),
                "back_to_wall": bool(preference.get("back_to_wall", False)),
                "corner_preferred": bool(preference.get("corner_preferred", False)),
                "reason": str(preference.get("reason", ""))[:200],
            }
        )

    orientation_preferences: list[dict[str, Any]] = []
    valid_open_sides = {"front", "left", "right", "none"}
    for orientation in payload.get("orientation_preferences", []):
        if not isinstance(orientation, dict):
            continue
        item = items_by_id.get(orientation.get("object_id"))
        front_vector = _validated_front_vector(orientation.get("front_vector_2d"))
        if not item or front_vector is None:
            continue

        open_side = orientation.get("open_side", "front")
        if open_side not in valid_open_sides:
            open_side = "front"

        orientation_preferences.append(
            {
                "object_id": item["id"],
                "front_vector_2d": front_vector,
                "back_to_wall": bool(orientation.get("back_to_wall", False))
                and item.get("type") in BACK_TO_WALL_TYPES,
                "open_side": open_side,
                "reason": str(orientation.get("reason", ""))[:200],
            }
        )

    furniture_roles: list[dict[str, Any]] = []
    valid_roles = {"work_surface", "dining_surface", "seating", "sleeping", "storage", "display", "unknown"}
    for role in payload.get("furniture_roles", []):
        if not isinstance(role, dict):
            continue
        item = items_by_id.get(role.get("object_id"))
        if not item:
            continue
        semantic_role = role.get("role", "unknown")
        if semantic_role not in valid_roles:
            semantic_role = "unknown"
        furniture_roles.append(
            {
                "object_id": item["id"],
                "role": semantic_role,
                "reason": str(role.get("reason", ""))[:200],
            }
        )

    layout_priorities: list[dict[str, Any]] = []
    valid_priorities = {"wall_attachment", "access_clearance", "group_cohesion", "orientation", "unknown"}
    valid_levels = {"critical", "high", "medium", "low"}
    for priority in payload.get("layout_priorities", []):
        if not isinstance(priority, dict):
            continue
        item = items_by_id.get(priority.get("object_id"))
        if not item:
            continue
        priority_name = priority.get("priority", "unknown")
        if priority_name not in valid_priorities:
            priority_name = "unknown"
        level = priority.get("level", "medium")
        if level not in valid_levels:
            level = "medium"
        layout_priorities.append(
            {
                "object_id": item["id"],
                "priority": priority_name,
                "level": level,
                "reason": str(priority.get("reason", ""))[:200],
            }
        )

    return {
        "support_groups": support_groups,
        "chair_pairs": chair_pairs,
        "wall_preferences": wall_preferences,
        "orientation_preferences": orientation_preferences,
        "furniture_roles": furniture_roles,
        "layout_priorities": layout_priorities,
        "rationale": str(payload.get("rationale", ""))[:500],
    }


def _round_list(values: Any) -> list[float]:
    if not isinstance(values, list):
        return []
    rounded: list[float] = []
    for value in values[:4]:
        try:
            rounded.append(round(float(value), 3))
        except (TypeError, ValueError):
            continue
    return rounded


def _relationship_summary(item: dict[str, Any]) -> dict[str, Any]:
    relationship = item.get("relationship")
    if not isinstance(relationship, dict):
        return {}

    summary: dict[str, Any] = {}
    for key in (
        "nearest_support_id",
        "nearest_support_type",
        "distance_to_nearest_support",
        "nearest_desk_id",
        "distance_to_nearest_desk",
        "strength",
        "source",
    ):
        if key in relationship:
            summary[key] = relationship[key]
    return summary


def _validated_front_vector(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) < 2:
        return None

    try:
        x = float(value[0])
        y = float(value[1])
    except (TypeError, ValueError):
        return None

    if not math.isfinite(x) or not math.isfinite(y):
        return None

    norm = math.hypot(x, y)
    if norm < 1e-8:
        return None

    return [round(x / norm, 3), round(y / norm, 3)]


def _nearest_walls(item: dict[str, Any], room: dict[str, Any]) -> list[dict[str, Any]]:
    pos = item.get("pos", [])
    if len(pos) < 2:
        return []

    try:
        x = float(pos[0])
        y = float(pos[1])
        width = float(room.get("width", 0.0))
        depth = float(room.get("depth", 0.0))
    except (TypeError, ValueError):
        return []

    if width <= 0.0 or depth <= 0.0:
        return []

    candidates = [
        {"wall": "west", "distance": x, "inward_normal_2d": [1.0, 0.0]},
        {"wall": "east", "distance": width - x, "inward_normal_2d": [-1.0, 0.0]},
        {"wall": "south", "distance": y, "inward_normal_2d": [0.0, 1.0]},
        {"wall": "north", "distance": depth - y, "inward_normal_2d": [0.0, -1.0]},
    ]
    valid = [
        {
            "wall": candidate["wall"],
            "distance": round(max(0.0, float(candidate["distance"])), 3),
            "inward_normal_2d": candidate["inward_normal_2d"],
        }
        for candidate in candidates
        if math.isfinite(float(candidate["distance"]))
    ]
    return sorted(valid, key=lambda candidate: candidate["distance"])[:2]


def _nearby_supports(chair: dict[str, Any], supports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    distances = []
    for support in supports:
        distances.append(
            {
                "id": support.get("id"),
                "type": support.get("type"),
                "distance": round(_distance_2d(chair, support), 3),
                "extent": _round_list(support.get("extent", [])),
                "relative_offset": _relative_offset(chair, support),
            }
        )
    return sorted(distances, key=lambda item: item["distance"])[:4]


def _distance_2d(first: dict[str, Any], second: dict[str, Any]) -> float:
    first_pos = first.get("pos", [])
    second_pos = second.get("pos", [])
    if len(first_pos) < 2 or len(second_pos) < 2:
        return 0.0
    return math.dist([float(first_pos[0]), float(first_pos[1])], [float(second_pos[0]), float(second_pos[1])])


def _relative_offset(item: dict[str, Any], support: dict[str, Any]) -> list[float]:
    item_pos = item.get("pos", [])
    support_pos = support.get("pos", [])
    if len(item_pos) < 2 or len(support_pos) < 2:
        return [0.0, 0.0]
    return [round(float(item_pos[0]) - float(support_pos[0]), 3), round(float(item_pos[1]) - float(support_pos[1]), 3)]


def _pair_metrics(chair: dict[str, Any], support: dict[str, Any]) -> dict[str, float]:
    chair_pos = chair.get("pos", [])
    support_pos = support.get("pos", [])
    if len(chair_pos) < 2 or len(support_pos) < 2:
        return {"distance": float("inf"), "chair_faces_support": -1.0, "support_side_alignment": -1.0}

    dx = float(support_pos[0]) - float(chair_pos[0])
    dy = float(support_pos[1]) - float(chair_pos[1])
    distance = math.hypot(dx, dy)
    if distance < 1e-8:
        direction_to_support = (0.0, 0.0)
    else:
        direction_to_support = (dx / distance, dy / distance)

    chair_front = _normalize_2d(chair.get("front_vector_2d"), (0.0, 1.0))
    support_front = _normalize_2d(support.get("front_vector_2d"), (0.0, -1.0))
    support_lateral = (-support_front[1], support_front[0])

    chair_faces_support = direction_to_support[0] * chair_front[0] + direction_to_support[1] * chair_front[1]
    support_side_alignment = max(
        abs(direction_to_support[0] * support_lateral[0] + direction_to_support[1] * support_lateral[1]),
        abs(direction_to_support[0] * support_front[0] + direction_to_support[1] * support_front[1]),
    )
    return {
        "distance": distance,
        "chair_faces_support": chair_faces_support,
        "support_side_alignment": support_side_alignment,
    }


def _apply_validated_pair(
    chair: dict[str, Any],
    support: dict[str, Any],
    *,
    strength: str,
    reason: str,
    source: str,
    applied_support_counts: dict[str, int],
    semantic: bool,
) -> bool:
    if not _is_valid_ai_pair(chair, support, semantic=semantic):
        return False

    if strength not in {"primary", "weak"}:
        strength = "primary"

    support_id = support["id"]
    preference = {
        "support_id": support_id,
        "strength": strength,
        "reason": reason,
        "distance": round(_distance_2d(chair, support), 3),
        "source": source,
    }
    if source == "ai_support_group":
        chair["ai_support_group"] = preference
    else:
        chair["ai_pair_preference"] = preference

    if applied_support_counts.get(support_id, 0) >= _support_pair_capacity(support):
        return False
    if not _should_apply_ai_pair(chair, support, semantic=semantic):
        return False

    applied_support_counts[support_id] = applied_support_counts.get(support_id, 0) + 1
    chair["pair_with"] = support_id
    relationship = chair.setdefault("relationship", {})
    relationship["nearest_support_id"] = support_id
    relationship["nearest_support_type"] = support.get("type", "table")
    relationship["nearest_desk_id"] = support_id
    relationship["strength"] = strength
    relationship["local_offset"] = _local_offset(chair, support)
    relationship["rotation_delta_deg"] = _rotation_delta_deg(chair, support)
    relationship["source"] = source
    return True


def _assign_unpaired_chairs_to_supports(problem: dict[str, Any]) -> None:
    """Keep chairs from drifting alone by assigning leftovers to balanced weak groups."""
    items = problem.get("movable_items", [])
    supports = [item for item in items if item.get("type") in {"table", "desk"}]
    if not supports:
        return

    supports_by_id = {support.get("id"): support for support in supports}
    support_counts = {support["id"]: 0 for support in supports if support.get("id")}
    for item in items:
        if item.get("type") != "chair":
            continue
        pair_id = item.get("pair_with")
        if pair_id in supports_by_id:
            support_counts[pair_id] = support_counts.get(pair_id, 0) + 1

    unpaired_chairs = [
        item
        for item in items
        if item.get("type") == "chair" and item.get("pair_with") not in supports_by_id
    ]
    if not unpaired_chairs:
        return

    for chair in sorted(unpaired_chairs, key=lambda item: min(_distance_2d(item, support) for support in supports)):
        support, distance = _choose_balanced_support(chair, supports, support_counts)
        support_id = support["id"]
        support_counts[support_id] = support_counts.get(support_id, 0) + 1

        chair["pair_with"] = support_id
        chair["auto_support_fallback"] = {
            "support_id": support_id,
            "distance": round(distance, 3),
            "source": "balanced_weak_fallback",
            "reason": "남은 의자가 혼자 떨어지지 않도록 가까운 테이블/책상에 약한 그룹으로 배정",
        }
        relationship = chair.setdefault("relationship", {})
        relationship["nearest_support_id"] = support_id
        relationship["nearest_support_type"] = support.get("type", "table")
        relationship["nearest_desk_id"] = support_id
        relationship["distance_to_nearest_support"] = round(distance, 3)
        relationship["distance_to_nearest_desk"] = round(distance, 3)
        relationship["strength"] = "weak"
        relationship["local_offset"] = _local_offset(chair, support)
        relationship["rotation_delta_deg"] = _rotation_delta_deg(chair, support)
        relationship["source"] = "balanced_weak_fallback"


def _choose_balanced_support(
    chair: dict[str, Any],
    supports: list[dict[str, Any]],
    support_counts: dict[str, int],
) -> tuple[dict[str, Any], float]:
    best: tuple[float, dict[str, Any], float] | None = None
    fallback: tuple[float, dict[str, Any], float] | None = None

    for support in supports:
        distance = _distance_2d(chair, support)
        capacity = _support_pair_capacity(support)
        count = support_counts.get(support["id"], 0)
        max_distance = 6.5 if support.get("type") == "table" else 3.5
        load_ratio = count / max(capacity, 1)
        overflow = max(0, count - capacity)
        score = distance + load_ratio * 0.85 + overflow * 2.0
        if count >= capacity:
            score += 4.0

        candidate = (score, support, distance)
        if fallback is None or distance < fallback[2]:
            fallback = candidate
        if distance > max_distance:
            continue
        if best is None or score < best[0]:
            best = candidate

    chosen = best or fallback
    if chosen is None:
        raise ValueError("No support furniture available for unpaired chair")
    return chosen[1], chosen[2]


def _is_valid_ai_pair(chair: dict[str, Any], support: dict[str, Any], *, semantic: bool = False) -> bool:
    if support.get("type") not in {"table", "desk"}:
        return False

    metrics = _pair_metrics(chair, support)
    if not math.isfinite(metrics["distance"]):
        return False

    # Semantic grouping can be useful even when the current scan has a chair
    # slightly outside the final service radius; the optimizer will pull it in.
    max_distance = 3.2 if support.get("type") == "table" else 2.2
    if metrics["distance"] > max_distance:
        return False

    current_distance = _current_pair_distance(chair)
    if current_distance is not None and metrics["distance"] > current_distance + 1.0:
        return False

    if semantic:
        return True

    # Do not accept legacy pair hints where the chair clearly faces away from the support.
    return metrics["chair_faces_support"] > -0.35


def _should_apply_ai_pair(chair: dict[str, Any], support: dict[str, Any], *, semantic: bool = False) -> bool:
    current_pair_id = chair.get("pair_with")
    if current_pair_id == support.get("id"):
        return True

    relationship = chair.get("relationship")
    current_strength = relationship.get("strength") if isinstance(relationship, dict) else None
    if not current_pair_id or current_strength in {None, "free"}:
        return True

    current_distance = _current_pair_distance(chair)
    ai_metrics = _pair_metrics(chair, support)
    if current_distance is None:
        return current_strength != "primary"

    if current_strength == "primary":
        return ai_metrics["distance"] + 0.2 < current_distance and ai_metrics["chair_faces_support"] > -0.1

    if semantic and current_strength == "weak":
        return ai_metrics["distance"] <= max(current_distance + 0.6, current_distance * 1.25)

    return ai_metrics["distance"] <= current_distance + 0.15


def _current_pair_distance(chair: dict[str, Any]) -> float | None:
    relationship = chair.get("relationship")
    if not isinstance(relationship, dict):
        return None
    value = relationship.get("distance_to_nearest_support") or relationship.get("distance_to_nearest_desk")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _support_pair_capacity(support: dict[str, Any]) -> int:
    if support.get("type") == "desk":
        return 1
    extent = support.get("extent", [])
    if not isinstance(extent, list) or len(extent) < 2:
        return 4
    try:
        width = float(extent[0])
        depth = float(extent[1])
    except (TypeError, ValueError):
        return 4
    return max(2, min(6, round((width + depth) * 1.5)))


def _normalize_2d(vector: Any, default: tuple[float, float]) -> tuple[float, float]:
    if not isinstance(vector, list) or len(vector) < 2:
        return default
    x = float(vector[0])
    y = float(vector[1])
    norm = math.hypot(x, y)
    if norm < 1e-8:
        return default
    return (x / norm, y / norm)


def _local_offset(chair: dict[str, Any], support: dict[str, Any]) -> list[float]:
    chair_pos = chair.get("pos", [])
    support_pos = support.get("pos", [])
    if len(chair_pos) < 2 or len(support_pos) < 2:
        return [0.0, 0.0]

    support_front = _normalize_2d(support.get("front_vector_2d"), (0.0, -1.0))
    support_lateral = (-support_front[1], support_front[0])
    dx = float(chair_pos[0]) - float(support_pos[0])
    dy = float(chair_pos[1]) - float(support_pos[1])
    local_front = dx * support_front[0] + dy * support_front[1]
    local_lateral = dx * support_lateral[0] + dy * support_lateral[1]
    return [round(local_front, 3), round(local_lateral, 3)]


def _rotation_delta_deg(chair: dict[str, Any], support: dict[str, Any]) -> float:
    return round(float(chair.get("rotation_y_deg", 0.0)) - float(support.get("rotation_y_deg", 0.0)), 3)
