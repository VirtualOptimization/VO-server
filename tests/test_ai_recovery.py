import pytest

from workers.optimizer.ai_constraints import merge_ai_retry_output, prepare_ai_retry_problem


def _item(item_id, pos, rotation, optimized_pos=None, optimized_rotation=None):
    item = {
        "id": item_id,
        "type": "table",
        "extent": [0.58, 0.67, 0.76],
        "pos": pos,
        "rotation_y_deg": rotation,
        "front_vector_2d": [1.0, 0.0],
        "back_vector_2d": [-1.0, 0.0],
    }
    if optimized_pos is not None:
        item["optimized_pos"] = optimized_pos
        item["optimized_rotation_y_deg"] = optimized_rotation
    return item


def _output(items, *, fails, usable, penetration=0.0):
    return {
        "movable_items": items,
        "optimization": {
            "remaining_collision_penetration_m": penetration,
            "floor_efficiency": {"usable_open_area_m2": usable},
        },
        "neufert_validation": {"summary": {"fail": fails}},
    }


def test_excluded_item_is_pinned_at_its_first_pass_pose():
    problem = {
        "movable_items": [_item("side-table", [2.39, 2.16, 0.76], 90.0)],
        "ai_excluded_object_ids": ["side-table"],
    }
    first = _output([_item("side-table", [2.39, 2.16, 0.76], 90.0, [1.76, 2.87, 0.76], 0.0)], fails=33, usable=5.36)

    retry_problem = prepare_ai_retry_problem(problem, first)

    pinned = retry_problem["movable_items"][0]
    assert pinned["pinned"] is True
    assert pinned["pos"] == [1.76, 2.87, 0.76]
    assert pinned["rotation_y_deg"] == 0.0
    # Turned back by 90 degrees along with the item.
    assert pinned["front_vector_2d"] == pytest.approx([0.0, -1.0], abs=1e-9)
    assert pinned["back_vector_2d"] == pytest.approx([0.0, 1.0], abs=1e-9)
    # The caller's problem is left untouched.
    assert problem["movable_items"][0]["pos"] == [2.39, 2.16, 0.76]


def test_excluded_item_stays_at_scan_pose_without_a_first_pass():
    problem = {
        "movable_items": [_item("side-table", [2.39, 2.16, 0.76], 90.0)],
        "ai_excluded_object_ids": ["side-table"],
    }

    pinned = prepare_ai_retry_problem(problem)["movable_items"][0]

    assert pinned["pinned"] is True
    assert pinned["pos"] == [2.39, 2.16, 0.76]
    assert pinned["rotation_y_deg"] == 90.0


def test_retry_with_same_failures_but_less_open_floor_is_rejected():
    first = _output([_item("a", [1.0, 1.0, 0.4], 0.0, [0.5, 0.5, 0.4], 0.0)], fails=33, usable=5.36)
    retry = _output([_item("a", [1.0, 1.0, 0.4], 0.0, [2.0, 2.0, 0.4], 0.0)], fails=33, usable=4.53)

    merged = merge_ai_retry_output(first, retry, {"ai_excluded_object_ids": []})

    assert merged["optimization"]["recovery_rejected"] is True
    assert merged["movable_items"][0]["optimized_pos"] == [0.5, 0.5, 0.4]


def test_retry_with_fewer_failures_is_accepted():
    first = _output([_item("a", [1.0, 1.0, 0.4], 0.0, [0.5, 0.5, 0.4], 0.0)], fails=33, usable=5.36)
    retry = _output([_item("a", [1.0, 1.0, 0.4], 0.0, [2.0, 2.0, 0.4], 0.0)], fails=20, usable=4.53)

    merged = merge_ai_retry_output(first, retry, {"ai_excluded_object_ids": []})

    assert merged["optimization"]["recovery_rejected"] is False
    assert merged["movable_items"][0]["optimized_pos"] == [2.0, 2.0, 0.4]
