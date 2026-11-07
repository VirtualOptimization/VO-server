import copy

import numpy as np

from workers.optimizer.layout import CanonicalLayoutOptimizer


def _wall(name, center, axis, length):
    return {"type": "wall", "wall": name, "span": {"start": 0.0, "end": length}, "center": center, "axis": axis, "length": length, "thickness": 0.1}


def _room(items) -> dict:
    width, depth = 4.0, 3.0
    return {
        "room_metadata": {"dimensions": {"width": width, "depth": depth, "height": 2.4}},
        "fixed_elements": [
            _wall("south", [width / 2, 0.0], [1.0, 0.0], width),
            _wall("north", [width / 2, depth], [-1.0, 0.0], width),
            _wall("west", [0.0, depth / 2], [0.0, -1.0], depth),
            _wall("east", [width, depth / 2], [0.0, 1.0], depth),
        ],
        "movable_items": items,
    }


def _box(item_id, pos, extent=(1.0, 1.0), item_type="shelf"):
    return {"id": item_id, "type": item_type, "extent": [*extent, 0.8], "pos": [*pos, 0.4], "rotation_y_deg": 0.0,
            "front_vector_2d": [0.0, 1.0]}


def _scanned(optimizer):
    return np.array([optimizer._coords_from_furniture(f) for f in optimizer.furnitures])


def test_largest_open_rect_prefers_one_block_over_split_floor():
    # Same furniture, same free area: pushed into a corner vs. in the middle.
    corner = CanonicalLayoutOptimizer(_room([_box("a", (0.5, 0.5))]))
    middle = CanonicalLayoutOptimizer(_room([_box("a", (2.0, 1.5))]))
    assert corner._largest_open_rect(_scanned(corner)) > middle._largest_open_rect(_scanned(middle)) + 2.0


def test_overlap_between_pieces_left_where_scanned_is_not_a_collision():
    room = _room([_box("a", (1.0, 1.0)), _box("b", (1.6, 1.0))])  # scanned 0.4 m into each other
    optimizer = CanonicalLayoutOptimizer(copy.deepcopy(room))
    coords = _scanned(optimizer)
    assert optimizer._collision_penetration(coords) == 0.0

    coords[1, 1] += 0.3  # move one of them: the overlap is now the layout's fault
    assert optimizer._collision_penetration(coords) > 0.1


def test_moved_count_counts_only_noticeable_moves():
    optimizer = CanonicalLayoutOptimizer(_room([_box("a", (1.0, 1.0)), _box("b", (3.0, 2.0))]))
    coords = _scanned(optimizer)
    coords[0, 0] += 0.05
    assert optimizer._moved_count(coords) == 0
    coords[1, 0] -= 0.5
    assert optimizer._moved_count(coords) == 1


def test_two_chairs_at_the_same_table_still_collide():
    table = _box("t", (2.0, 1.5), (1.2, 0.8), "table")
    chairs = [
        {**_box(f"c{k}", (1.8 + 0.3 * k, 0.8), (0.5, 0.5), "chair"), "pair_with": "t"}
        for k in range(2)
    ]
    optimizer = CanonicalLayoutOptimizer(_room([table, *chairs]))
    coords = _scanned(optimizer)
    coords[1:, 0] += 0.5  # moved, so the scan exemption does not apply
    assert optimizer._collision_penetration(coords) > 0.1


def test_shelf_standing_in_the_room_counts_as_off_wall():
    shelf = {**_box("s", (2.0, 1.5), (0.8, 0.4)), "front_vector_2d": [0.0, -1.0],
             "anchor_preferences": {"back_to_wall": True}}
    optimizer = CanonicalLayoutOptimizer(_room([shelf]))
    coords = _scanned(optimizer)
    assert optimizer._off_wall_count(coords) == 1
    coords[0, 1] = 3.0 - 0.2  # back (north side) flush with the north wall
    assert optimizer._off_wall_count(coords) == 0


def test_piece_just_past_the_door_swing_blocks_the_entry():
    room = _room([_box("s", (0.9, 1.3), (0.5, 0.5))])
    room["fixed_elements"].append({"id": "door", "type": "door", "wall": "west", "span": {"start": 1.0, "end": 1.8}, "clearance_depth": 0.8})
    optimizer = CanonicalLayoutOptimizer(room)
    coords = _scanned(optimizer)
    assert optimizer._entry_blocked_count(coords) == 1  # 0.65-1.15 m from the wall: past the 0.8 m swing
    coords[0, 0] = 2.5
    assert optimizer._entry_blocked_count(coords) == 0


def test_planned_relation_to_the_bed_head_counts_until_kept():
    bed = {**_box("bed", (0.6, 1.9), (1.1, 2.0), "bed"), "front_vector_2d": [0.0, -1.0]}
    room = _room([bed, _box("n", (3.0, 0.5), (0.5, 0.5), "table")])
    room["ai_plan_relations"] = [{"object_id": "n", "near_object_id": "bed", "zone": "bed_head"}]
    optimizer = CanonicalLayoutOptimizer(room)
    coords = _scanned(optimizer)
    assert optimizer._intent_unmet_count(coords) == 1
    coords[1, :2] = [1.45, 2.6]  # beside the bed, at its head end
    assert optimizer._intent_unmet_count(coords) == 0
    coords[1, :2] = [1.45, 1.2]  # beside the bed, at its foot end
    assert optimizer._intent_unmet_count(coords) == 1


def test_tall_piece_in_front_of_a_window_blocks_it_but_a_low_one_does_not():
    room = _room([{**_box("tall", (1.0, 2.7), (0.8, 0.4)), "extent": [0.8, 0.4, 1.2], "pos": [1.0, 2.7, 1.2]},
                  {**_box("low", (3.0, 2.7), (0.8, 0.4)), "extent": [0.8, 0.4, 0.7], "pos": [3.0, 2.7, 0.7]}])
    room["fixed_elements"].append({"id": "w", "type": "window", "wall": "north", "span": {"start": 0.5, "end": 3.5}, "sill_height": 0.9})
    optimizer = CanonicalLayoutOptimizer(room)
    coords = _scanned(optimizer)
    assert optimizer._window_blocked_count(coords) == 1
    coords[0, 1] = 1.0
    assert optimizer._window_blocked_count(coords) == 0


def test_planned_relation_does_not_change_the_rank():
    bed = {**_box("bed", (0.6, 1.9), (1.1, 2.0), "bed"), "front_vector_2d": [0.0, -1.0]}
    room = _room([bed, _box("n", (3.0, 0.5), (0.5, 0.5), "table")])
    plain = CanonicalLayoutOptimizer(copy.deepcopy(room))
    room["ai_plan_relations"] = [{"object_id": "n", "near_object_id": "bed", "zone": "bed_head"}]
    planned = CanonicalLayoutOptimizer(room)
    assert plain._layout_rank(_scanned(plain)) == planned._layout_rank(_scanned(planned))
