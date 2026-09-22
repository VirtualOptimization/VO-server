"""Optional AI-generated layout constraints for the room optimizer."""

from __future__ import annotations

import json
import math
import os
import ssl
import urllib.error
import urllib.request
from copy import deepcopy
from typing import Any

import certifi


SUPPORTED_PROVIDER = "anthropic"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5"
DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_MAX_TOKENS = 4096
BACK_TO_WALL_TYPES = {"bed", "closet", "cabinet", "shelf", "storage"}


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def maybe_apply_ai_constraints(
    problem: dict[str, Any],
    failure_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask Claude for recovery constraints only after deterministic failure.

    The AI does not place furniture directly. It only suggests semantic constraints
    that the deterministic optimizer can score, such as chair-table pairing and
    wall/corner preferences.
    """
    if not env_bool("AI_LAYOUT_ENABLED") or failure_context is None:
        problem["ai_used"] = False
        return problem

    provider = os.getenv("AI_LAYOUT_PROVIDER", SUPPORTED_PROVIDER).strip().lower()
    if provider != SUPPORTED_PROVIDER:
        problem["ai_used"] = False
        problem["ai_error"] = f"Unsupported AI layout provider: {provider}"
        return problem

    try:
        constraints = generate_claude_constraints(problem, failure_context)
        apply_ai_constraints(problem, constraints)
        problem["ai_used"] = True
        problem["ai_error"] = None
        problem["ai_constraints"] = constraints
    except Exception as exc:  # Keep the optimizer usable even when AI is unavailable.
        problem["ai_used"] = False
        problem["ai_error"] = f"{exc.__class__.__name__}: {exc}"

    return problem


def optimizer_needs_recovery(optimized: dict[str, Any]) -> bool:
    """Return whether the first deterministic pass needs Claude recovery advice."""
    optimization = optimized.get("optimization", {})
    collision = float(optimization.get("remaining_collision_penetration_m", 0.0) or 0.0)
    validation = optimized.get("neufert_validation", {}).get("summary", {})
    return collision > 1e-3 or int(validation.get("fail", 0) or 0) > 0


def build_failure_context(optimized: dict[str, Any]) -> dict[str, Any]:
    """Keep the Claude recovery request focused on actionable failures."""
    optimization = optimized.get("optimization", {})
    validation = optimized.get("neufert_validation", {})
    return {
        "remaining_collision_penetration_m": optimization.get("remaining_collision_penetration_m", 0.0),
        "neufert_failures": [
            check
            for check in validation.get("checks", [])
            if check.get("status") == "fail"
        ],
        "qualitative_constraints": [
            check
            for check in validation.get("checks", [])
            if check.get("status") == "qualitative_only"
        ],
    }


def prepare_ai_retry_problem(problem: dict[str, Any]) -> dict[str, Any]:
    """Create a retry payload where excluded items are pinned, not deleted.

    Dropping an excluded item from movable_items would also drop it from
    every collision check, so other furniture could end up placed right on
    top of where it physically still sits. Pinning it instead keeps it as a
    real (immovable) obstacle for the rest of the retry optimization.
    """
    retry_problem = deepcopy(problem)
    excluded = set(problem.get("ai_excluded_object_ids", []))
    if excluded:
        for item in retry_problem.get("movable_items", []):
            if item.get("id") in excluded:
                item["pinned"] = True
    return retry_problem


def _placement_quality(output: dict[str, Any]) -> tuple[float, int]:
    """Lower is better: (remaining collision penetration, failed rule count)."""
    optimization = output.get("optimization", {})
    penetration = float(optimization.get("remaining_collision_penetration_m", 0.0) or 0.0)
    fail_count = int(output.get("neufert_validation", {}).get("summary", {}).get("fail", 0) or 0)
    return (penetration, fail_count)


def merge_ai_retry_output(
    first_output: dict[str, Any],
    retry_output: dict[str, Any],
    problem: dict[str, Any],
) -> dict[str, Any]:
    """Merge retry coordinates back into the complete furniture result.

    An AI-guided retry is a best-effort nudge, not a guarantee. Callers must
    run validate_neufert_rules() on retry_output before calling this, so both
    outputs can be compared on equal footing. If the retry did not actually
    improve on the first pass (equal-or-fewer collisions and rule failures),
    the first pass is kept instead of silently handing back a worse layout.
    """
    excluded = set(problem.get("ai_excluded_object_ids", []))

    if _placement_quality(retry_output) > _placement_quality(first_output):
        merged = deepcopy(first_output)
        merged["optimization"]["recovery_rejected"] = True
        merged["optimization"]["recovery_excluded_object_ids"] = sorted(excluded)
        return merged

    merged = deepcopy(first_output)
    retry_by_id = {item.get("id"): item for item in retry_output.get("movable_items", [])}
    for item in merged.get("movable_items", []):
        retry_item = retry_by_id.get(item.get("id"))
        if retry_item:
            for key in ("optimized_pos", "optimized_rotation_y_deg"):
                if key in retry_item:
                    item[key] = retry_item[key]
        elif item.get("id") in excluded:
            item["optimized_pos"] = item.get("pos")
            item["optimized_rotation_y_deg"] = item.get("rotation_y_deg", 0.0)

    merged["optimization"] = deepcopy(retry_output.get("optimization", {}))
    merged["optimization"]["recovery_rejected"] = False
    merged["optimization"]["recovery_excluded_object_ids"] = sorted(excluded)
    merged["neufert_validation"] = deepcopy(retry_output.get("neufert_validation", {}))
    return merged


class _UnusableClaudeResponseError(RuntimeError):
    """Claude answered, but the response couldn't be turned into JSON constraints."""


def _call_claude_once(prompt: str, *, api_key: str, model: str, max_tokens: int, timeout: float) -> dict[str, Any]:
    url = "https://api.anthropic.com/v1/messages"
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": "You are a spatial layout planning assistant. Return only valid JSON.",
        "messages": [{"role": "user", "content": prompt}],
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )

    try:
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Claude API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Claude API connection error: {exc.reason}") from exc

    try:
        text = _extract_candidate_text(payload)
        return _loads_json_object(text)
    except (RuntimeError, json.JSONDecodeError) as exc:
        raise _UnusableClaudeResponseError(str(exc)) from exc


def generate_claude_constraints(
    problem: dict[str, Any],
    failure_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is required when AI_LAYOUT_ENABLED=true")

    model = os.getenv("ASSISTANT_MODEL", DEFAULT_CLAUDE_MODEL).strip() or DEFAULT_CLAUDE_MODEL
    # This is a separate, larger budget from the chatbot's ASSISTANT_MAX_TOKENS:
    # the recovery response is a much bigger structured JSON schema (support
    # groups, chair pairs, orientation, recovery actions, ...) across every
    # piece of furniture, not a short conversational reply. Sharing the
    # chatbot's 1024-token budget caused Claude's response to be truncated
    # mid-JSON (stop_reason="max_tokens"), which always fails to parse.
    max_tokens = int(os.getenv("AI_LAYOUT_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
    timeout = float(os.getenv("AI_LAYOUT_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS)))
    prompt = _build_prompt(_summarize_problem(problem), failure_context)

    # An occasional response that isn't clean JSON (e.g. Claude straying into
    # stray code-like syntax inside a string value) is a random one-off
    # hiccup, not a systemic failure -- the same prompt can succeed on a
    # later attempt with no other change. Retry with fresh calls before
    # giving up. Network/API-level errors are not retried here since asking
    # again won't fix an auth failure or a dead connection.
    max_attempts = 3
    last_error: _UnusableClaudeResponseError | None = None
    raw_constraints: dict[str, Any] | None = None
    for _ in range(max_attempts):
        try:
            raw_constraints = _call_claude_once(prompt, api_key=api_key, model=model, max_tokens=max_tokens, timeout=timeout)
            break
        except _UnusableClaudeResponseError as exc:
            last_error = exc
    if raw_constraints is None:
        raise last_error

    return _validate_constraints(raw_constraints, problem)


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

    excluded_ids: list[str] = []
    for action in constraints.get("recovery_actions", []):
        if not isinstance(action, dict):
            continue
        object_id = action.get("object_id")
        item = items_by_id.get(object_id)
        action_name = action.get("action")
        if not item or action_name not in {"prioritize", "relax", "exclude"}:
            continue

        if action_name == "exclude":
            excluded_ids.append(item["id"])
            continue

        if action_name == "prioritize":
            item.setdefault("layout_priorities", []).append(
                {
                    "priority": action.get("priority", "access_clearance"),
                    "level": action.get("level", "high"),
                    "reason": str(action.get("reason", ""))[:200],
                    "source": "ai_recovery",
                }
            )
            continue

        # Only relax a non-hard Neufert rule and keep the relaxation bounded.
        factor = action.get("relaxation_factor", 1.0)
        try:
            factor = min(1.0, max(0.7, float(factor)))
        except (TypeError, ValueError):
            continue
        page_number = action.get("rule_page_number")
        for rule in item.get("placement_rules", {}).get("neufert_rules", []):
            if page_number is not None and rule.get("page_number") != page_number:
                continue
            if rule.get("priority") == "hard":
                continue
            relation = str(rule.get("relation", ""))
            direction = str(rule.get("direction", ""))
            if relation in {"passage", "clearance"}:
                if direction == "front":
                    item["placement_rules"]["front_clearance"] *= factor
                elif direction in {"side", "around"}:
                    item["placement_rules"]["side_clearance"] *= factor
                    if direction == "around":
                        item["placement_rules"]["front_clearance"] *= factor

    problem["ai_excluded_object_ids"] = sorted(set(excluded_ids))

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


def _neufert_guidance(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact, model-friendly view of the Neufert rules already matched to this item.

    apply_neufert_rules() attaches the raw matched rules (with page numbers, Turkish
    evidence text, confidence scores) under placement_rules.neufert_rules before this
    module ever runs. Sending that raw list as-is buries the one or two facts that
    actually matter in noise the model has no instructions to use. This distills each
    rule down to the object/relation/direction/distance the optimizer already derived
    from it, plus a short evidence snippet for traceability.
    """
    rules = item.get("placement_rules", {}).get("neufert_rules", [])
    guidance: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        entry: dict[str, Any] = {
            "object": rule.get("object"),
            "relation": rule.get("relation"),
            "direction": rule.get("direction"),
        }
        distance = rule.get("min_distance_m")
        distance_kind = "min"
        if distance is None:
            distance = rule.get("recommended_distance_m")
            distance_kind = "recommended"
        if distance is not None:
            try:
                entry["distance_m"] = round(float(distance), 3)
                entry["distance_kind"] = distance_kind
            except (TypeError, ValueError):
                pass
        evidence = rule.get("evidence")
        if isinstance(evidence, str) and evidence.strip():
            entry["evidence"] = evidence.strip()[:140]
        guidance.append(entry)
    return guidance


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
                "placement_rules": {
                    key: value
                    for key, value in item.get("placement_rules", {}).items()
                    if key != "neufert_rules"
                },
                "neufert_guidance": _neufert_guidance(item),
            }
            for item in movable_items
        ],
    }


def _build_prompt(
    summary: dict[str, Any],
    failure_context: dict[str, Any] | None = None,
) -> str:
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
        "recovery_actions": [
            {
                "action": "prioritize | relax | exclude",
                "object_id": "furniture object id",
                "priority": "wall_attachment | access_clearance | group_cohesion | orientation | unknown",
                "level": "critical | high | medium | low",
                "rule_page_number": 255,
                "relaxation_factor": 0.8,
                "reason": "short reason",
            }
        ],
        "rationale": "short explanation",
    }
    return (
        "You are a furniture layout constraint planner. "
        "Return ONLY valid JSON. Do not return markdown. "
        "Do not create final coordinates. Use only object ids that exist in the input, "
        "copied exactly character-for-character. Every JSON string value must be a plain "
        "literal string -- never a function call, method chain (e.g. .replace(...)), "
        "ternary/conditional expression, or any other code inside a string. "
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
        "- Keep door/window access and walking paths in mind.\n"
        "- This is a recovery pass after a deterministic optimization failure.\n"
        "- Use recovery_actions to identify a priority change, a bounded relaxation of a non-hard rule, "
        "or a temporary exclusion only when the room is genuinely over-constrained.\n"
        "- Never relax door, wall, collision, or hard minimum constraints.\n"
        "- Use exclude sparingly and only for a movable, non-essential object.\n"
        "- Each furniture object may include a `neufert_guidance` list: rules already matched from the "
        "Neufert architectural reference for this exact object. Treat these as authoritative over your own "
        "generic assumptions about that object -- if guidance says an object should be against a wall, "
        "parallel to another object, or oriented a certain way, reflect that in wall_preferences/"
        "orientation_preferences/layout_priorities for that object_id, and mention it in the reason field. "
        "An empty or missing `neufert_guidance` means no matched reference rule; fall back to general "
        "best practice for that case.\n\n"
        f"Output schema example:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
        f"Input room summary:\n{json.dumps(summary, ensure_ascii=False)}"
        + (
            f"\n\nFailure context from the first optimizer pass:\n"
            f"{json.dumps(failure_context, ensure_ascii=False)}"
            if failure_context is not None
            else ""
        )
    )


def _extract_candidate_text(payload: dict[str, Any]) -> str:
    content = payload.get("content") or []
    texts = [
        block.get("text", "")
        for block in content
        if block.get("type") == "text" and block.get("text")
    ]
    text = "\n".join(texts).strip()
    if not text:
        raise RuntimeError("Claude response has no text")
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

    recovery_actions: list[dict[str, Any]] = []
    valid_actions = {"prioritize", "relax", "exclude"}
    for action in payload.get("recovery_actions", []):
        if not isinstance(action, dict) or action.get("action") not in valid_actions:
            continue
        item = items_by_id.get(action.get("object_id"))
        if not item:
            continue
        action_name = action["action"]
        normalized: dict[str, Any] = {
            "action": action_name,
            "object_id": item["id"],
            "reason": str(action.get("reason", ""))[:200],
        }
        if action_name == "prioritize":
            priority_name = action.get("priority", "access_clearance")
            normalized["priority"] = priority_name if priority_name in valid_priorities else "unknown"
            level = action.get("level", "high")
            normalized["level"] = level if level in valid_levels else "medium"
        elif action_name == "relax":
            normalized["rule_page_number"] = action.get("rule_page_number")
            try:
                normalized["relaxation_factor"] = min(
                    1.0, max(0.7, float(action.get("relaxation_factor", 0.8)))
                )
            except (TypeError, ValueError):
                normalized["relaxation_factor"] = 0.8
        recovery_actions.append(normalized)

    return {
        "support_groups": support_groups,
        "chair_pairs": chair_pairs,
        "wall_preferences": wall_preferences,
        "orientation_preferences": orientation_preferences,
        "furniture_roles": furniture_roles,
        "layout_priorities": layout_priorities,
        "recovery_actions": recovery_actions,
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
