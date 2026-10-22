import copy

from workers.optimizer.scoring import MAX_TOTAL, score_layout


def _wall(name, center, axis, length):
    return {"type": "wall", "wall": name, "span": {"start": 0.0, "end": length}, "center": center, "axis": axis, "length": length, "thickness": 0.1}


def _room() -> dict:
    """3.6 x 3.2 m room, door on the west wall near the north corner."""
    width, depth = 3.6, 3.2
    return {
        "room_metadata": {"dimensions": {"width": width, "depth": depth, "height": 2.4}},
        "fixed_elements": [
            {"id": "door", "type": "door", "wall": "west", "span": {"start": 2.3, "end": 3.1}, "clearance_depth": 0.8},
            _wall("south", [width / 2, 0.0], [1.0, 0.0], width),
            _wall("north", [width / 2, depth], [-1.0, 0.0], width),
            _wall("west", [0.0, depth / 2], [0.0, -1.0], depth),
            _wall("east", [width, depth / 2], [0.0, 1.0], depth),
        ],
        "movable_items": [
            # Bed in the south-east corner, head against the east wall, foot toward the room.
            {"id": "bed", "type": "bed", "extent": [1.1, 2.0, 0.5], "pos": [2.6, 0.55, 0.5], "rotation_y_deg": 90.0,
             "front_vector_2d": [-1.0, 0.0]},
            # Desk against the north wall (the wall beside the door), chair in front of it.
            {"id": "desk", "type": "table", "extent": [1.2, 0.6, 0.75], "pos": [1.8, 2.9, 0.75], "rotation_y_deg": 0.0,
             "front_vector_2d": [0.0, -1.0]},
            {"id": "chair", "type": "chair", "extent": [0.5, 0.5, 0.9], "pos": [1.8, 2.45, 0.9], "rotation_y_deg": 180.0,
             "front_vector_2d": [0.0, -1.0], "pair_with": "desk"},
        ],
    }


def test_clean_layout_scores_within_range_with_full_space_credit():
    result = score_layout(_room(), reference_usable_m2=1.0)
    assert not result["disqualified"]
    assert 0 < result["total_score"] <= MAX_TOTAL
    assert result["breakdown"]["space"] == 40
    assert result["breakdown"]["door_clearance"] == 15
    assert result["beat_ai"] is True


def test_overlapping_furniture_receives_ordinary_score_and_feedback():
    problem = _room()
    problem["movable_items"][1]["pos"] = [2.6, 0.6, 0.75]  # desk dropped onto the bed
    result = score_layout(problem, reference_usable_m2=3.0)
    assert not result["disqualified"]
    assert 0 < result["total_score"] <= MAX_TOTAL
    assert result["total_score"] == round(sum(result["breakdown"][k] for k in ("safety", "principles", "space")), 1)
    assert any("겹쳐" in hint for hint in result["hints"])


def test_furniture_outside_the_room_receives_ordinary_score_and_feedback():
    problem = _room()
    problem["movable_items"][1]["pos"] = [1.8, 3.3, 0.75]
    result = score_layout(problem, reference_usable_m2=3.0)
    assert not result["disqualified"]
    assert 0 < result["total_score"] <= MAX_TOTAL
    assert result["breakdown"] is not None
    assert any("방 밖" in hint for hint in result["hints"])


def test_blocking_the_door_costs_door_points():
    clean = score_layout(_room(), reference_usable_m2=3.0)
    problem = _room()
    # Move the desk (and its chair) into the door clearance on the west wall.
    problem["movable_items"][1].update(pos=[0.35, 2.6, 0.75], rotation_y_deg=90.0)
    problem["movable_items"][2].update(pos=[0.85, 2.6, 0.9], rotation_y_deg=270.0)
    blocked = score_layout(problem, reference_usable_m2=3.0)
    assert not blocked["disqualified"]
    assert blocked["breakdown"]["door_clearance"] < clean["breakdown"]["door_clearance"]


def test_space_part_is_relative_to_the_ai_reference():
    room = _room()
    full = score_layout(copy.deepcopy(room))
    usable = full["usable_area_m2"]
    half = score_layout(copy.deepcopy(room), reference_usable_m2=usable * 2)
    assert half["breakdown"]["space"] == 20
    assert half["beat_ai"] is False
