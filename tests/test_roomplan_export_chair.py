import math

from server.services.transform.roomplan import convert_roomplan_to_optimizer_payload
from server.services.transform.roomplan_export import export_optimized_layout_to_roomplan


def _box(cx, cz, w, h, d, theta):
    c, s = math.cos(theta), math.sin(theta)
    return {
        "center": [cx, h / 2, cz],
        "dimensions": [w, h, d],
        "transform": [[c, 0, s, 0], [0, 1, 0, 0], [-s, 0, c, 0], [cx, h / 2, cz, 1]],
        "obbVertices": [[cx + dx, h / 2, cz + dz] for dx in (-w / 2, w / 2) for dz in (-d / 2, d / 2)],
        "frontVector": [s, 0, -c],
        "backVector": [-s, 0, c],
    }


def test_unmoved_chair_keeps_facing_the_table_after_export():
    floor = {
        "center": [2, 0, 2],
        "dimensions": [4, 4, 0],
        "transform": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [2, 0, 2, 1]],
        "obbVertices": [[0, 0, 0], [4, 0, 0], [0, 0, 4], [4, 0, 4]],
    }
    table = {**_box(2, 2, 1.2, 0.75, 0.8, 0.0), "category": "table", "identifier": "t"}
    # Chair south of the table; its +Z (the side it is used from) points at the table.
    chair = {**_box(2, 3, 0.5, 0.9, 0.5, math.pi), "category": "chair", "identifier": "c"}
    raw = {"floors": [floor], "walls": [], "doors": [], "windows": [], "objects": [table, chair]}

    items = convert_roomplan_to_optimizer_payload(raw)["scanned_objects"]
    exported = export_optimized_layout_to_roomplan(raw, {"movable_items": items})

    out_chair = next(o for o in exported["objects"] if o["identifier"] == "c")
    assert out_chair["transform"][2][2] < 0
