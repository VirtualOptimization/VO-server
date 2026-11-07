from workers.optimizer.neufert_rules import validate_neufert_rules


def _item(item_id, item_type, extent, pos, front, **extra):
    return {
        "id": item_id,
        "type": item_type,
        "extent": [extent[0], extent[1], 0.8],
        "pos": [pos[0], pos[1], 0.4],
        "optimized_pos": [pos[0], pos[1], 0.4],
        "rotation_y_deg": 0.0,
        "optimized_rotation_y_deg": 0.0,
        "front_vector_2d": front,
        **extra,
    }


def _rule(subject, relation, direction, distance, obj="room"):
    return {"page_number": 1, "subject": subject, "object": obj, "relation": relation, "direction": direction,
            "min_distance_m": distance, "priority": "hard"}


def _check(rules, items):
    problem = {"room_metadata": {"dimensions": {"width": 4.0, "depth": 4.0}}, "neufert_rules": rules}
    return validate_neufert_rules(problem, {"movable_items": items})["neufert_validation"]["checks"]


def test_around_rule_needs_only_one_open_side():
    # Bed in a corner: two sides against walls, the room side wide open.
    bed = _item("bed", "bed", (1.0, 2.0), (0.5, 1.0), [1.0, 0.0])
    (check,) = _check([_rule("bed", "around", "around", 0.6)], [bed])
    assert check["status"] == "pass"


def test_around_rule_fails_when_boxed_in():
    shelf = _item("shelf", "shelf", (0.8, 0.4), (2.0, 0.2), [0.0, 1.0])
    blockers = [
        _item("t1", "table", (0.8, 0.5), (2.0, 0.65), [0.0, 1.0]),
        _item("t2", "table", (0.5, 0.8), (1.35, 0.4), [1.0, 0.0]),
        _item("t3", "table", (0.5, 0.8), (2.65, 0.4), [-1.0, 0.0]),
    ]
    checks = _check([_rule("shelf", "clearance", "around", 0.8)], [shelf, *blockers])
    assert checks[0]["status"] == "fail"


def test_table_front_ignores_chairs():
    table = _item("table", "table", (1.2, 0.6), (2.0, 3.7), [0.0, -1.0])
    chair = _item("chair", "chair", (0.5, 0.5), (2.0, 3.1), [0.0, 1.0])
    (check,) = _check([_rule("table", "clearance", "front", 0.75)], [table, chair])
    assert check["status"] == "pass"


def test_front_rule_fails_when_something_blocks_the_front():
    shelf = _item("shelf", "shelf", (0.8, 0.4), (2.0, 0.2), [0.0, 1.0])
    bed = _item("bed", "bed", (2.0, 1.0), (2.0, 0.95), [0.0, 1.0])
    checks = _check([_rule("shelf", "clearance", "front", 0.9)], [shelf, bed])
    assert checks[0]["status"] == "fail"
    assert checks[0]["measured_distance_m"] < 0.1


def _summary(rules, items):
    problem = {"room_metadata": {"dimensions": {"width": 4.0, "depth": 4.0}}, "neufert_rules": rules}
    return validate_neufert_rules(problem, {"movable_items": items})["neufert_validation"]


def test_duplicate_rules_are_checked_once_at_the_smallest_figure():
    shelf = _item("shelf", "shelf", (0.8, 0.4), (2.0, 0.2), [0.0, 1.0])
    bed = _item("bed", "bed", (2.0, 1.0), (2.0, 1.35), [0.0, 1.0])
    rules = [_rule("shelf", "clearance", "front", 1.0), _rule("shelf", "clearance", "front", 0.9)]
    report = _summary(rules, [shelf, bed])
    (check,) = report["checks"]
    assert check["required_distance_m"] == 0.9
    assert check["status"] == "fail"
    assert report["summary"]["shortfall_m"] == 0.45  # 0.45 m gap to the bed


def test_side_to_wall_rule_skips_pieces_backed_against_a_wall():
    shelf = _item("shelf", "shelf", (0.8, 0.4), (0.4, 0.2), [0.0, 1.0])
    (check,) = _check([_rule("shelf", "clearance", "side", 0.7, obj="wall")], [shelf])
    assert check["status"] == "not_applicable"


def test_passing_layout_has_no_shortfall():
    bed = _item("bed", "bed", (1.0, 2.0), (0.5, 1.0), [1.0, 0.0])
    report = _summary([_rule("bed", "around", "around", 0.6)], [bed])
    assert report["summary"]["shortfall_m"] == 0.0


def test_blocked_shelf_counts_as_unusable():
    shelf = _item("shelf", "shelf", (0.8, 0.4), (2.0, 0.2), [0.0, 1.0])
    desk = _item("desk", "table", (1.2, 0.6), (2.0, 0.7), [0.0, -1.0])
    report = _summary([_rule("shelf", "clearance", "front", 0.9)], [shelf, desk])
    assert report["summary"]["unusable_items"] == ["shelf"]


def test_corner_that_only_lines_up_is_not_in_front():
    shelf = _item("shelf", "shelf", (0.8, 0.4), (1.0, 0.2), [0.0, 1.0])
    # Starts exactly where the shelf ends sideways, 0.1 m in front of it.
    other = _item("bed", "bed", (1.0, 1.0), (1.9 + 0.001, 0.9), [0.0, 1.0])
    (check,) = _check([_rule("shelf", "clearance", "front", 0.9)], [shelf, other])
    assert check["status"] == "pass"


def test_nightstand_is_not_a_table_to_keep_clear_of_the_bed():
    bed = _item("bed", "bed", (1.0, 2.0), (1.0, 2.0), [0.0, -1.0])
    nightstand = _item("n", "table", (0.5, 0.5), (1.75, 2.75), [0.0, -1.0])
    desk = _item("d", "table", (1.4, 0.6), (2.4, 2.0), [0.0, -1.0])
    rule = _rule("bed", "clearance", "side", 1.1, obj="table")
    assert _check([rule], [bed, nightstand])[0]["status"] == "not_applicable"
    assert _check([rule], [bed, desk])[0]["status"] == "fail"
