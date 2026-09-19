"""Generate a synthetic, deliberately messy RoomPlan export for optimizer stress-testing."""

from __future__ import annotations

import json
import math
from pathlib import Path

ROOM_WIDTH = 2.8
ROOM_DEPTH = 3.6


def _identity_transform(center: list[float]) -> list[list[float]]:
    return [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [*center, 1.0],
    ]


def _object(
    identifier: str,
    category: str,
    center: list[float],
    dimensions: list[float],
    theta_deg: float,
) -> dict:
    theta = math.radians(theta_deg)
    c, s = math.cos(theta), math.sin(theta)
    transform = [
        [c, 0.0, s, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [-s, 0.0, c, 0.0],
        [*center, 1.0],
    ]
    front = [s, 0.0, -c]
    back = [-s, 0.0, c]
    return {
        "identifier": identifier,
        "category": category,
        "center": center,
        "dimensions": dimensions,
        "transform": transform,
        "frontVector": front,
        "backVector": back,
    }


def _wall(identifier: str, center: list[float], span_length: float, height: float, axis: str) -> dict:
    if axis == "x":
        transform = _identity_transform(center)
    else:
        transform = [
            [0.0, 0.0, 1.0, 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [-1.0, 0.0, 0.0, 0.0],
            [*center, 1.0],
        ]
    return {
        "identifier": identifier,
        "center": center,
        "dimensions": [span_length, height],
        "transform": transform,
    }


def build() -> dict:
    floor = {
        "center": [ROOM_WIDTH / 2.0, 0.0, ROOM_DEPTH / 2.0],
        "dimensions": [ROOM_WIDTH, ROOM_DEPTH],
        "transform": _identity_transform([ROOM_WIDTH / 2.0, 0.0, ROOM_DEPTH / 2.0]),
    }

    walls = [
        _wall("wall_west", [0.0, 1.2, ROOM_DEPTH / 2.0], ROOM_DEPTH, 2.4, axis="z"),
        _wall("wall_east", [ROOM_WIDTH, 1.2, ROOM_DEPTH / 2.0], ROOM_DEPTH, 2.4, axis="z"),
        _wall("wall_south", [ROOM_WIDTH / 2.0, 1.2, 0.0], ROOM_WIDTH, 2.4, axis="x"),
        _wall("wall_north", [ROOM_WIDTH / 2.0, 1.2, ROOM_DEPTH], ROOM_WIDTH, 2.4, axis="x"),
    ]

    door = {
        "identifier": "door_main",
        "center": [0.6, 1.0, 0.0],
        "dimensions": [0.8, 2.0],
        "transform": _identity_transform([0.6, 1.0, 0.0]),
    }

    window = {
        "identifier": "window_main",
        "center": [1.7, 1.5, ROOM_DEPTH],
        "dimensions": [1.2, 1.2],
        "transform": _identity_transform([1.7, 1.5, ROOM_DEPTH]),
    }

    objects = [
        # Bed: floating at an odd 35-degree angle in the middle of the room,
        # not against any wall, encroaching on the walking path.
        _object("obj_bed", "bed", [1.6, 0.25, 2.0], [1.3, 0.5, 2.0], 35.0),
        # Desk: shoved into and overlapping the bed.
        _object("obj_desk", "desk", [2.0, 0.375, 1.8], [1.0, 0.75, 0.5], 10.0),
        # Chair: stranded in a far corner, facing the wrong way, nowhere near the desk.
        _object("obj_chair", "chair", [0.3, 0.45, 3.3], [0.45, 0.9, 0.45], 170.0),
        # Closet: parked right against the window, blocking it.
        _object("obj_closet", "closet", [1.7, 1.0, 3.0], [0.6, 2.0, 1.2], 0.0),
        # Shelf: sitting in the door's clearance zone.
        _object("obj_shelf", "shelf", [0.6, 0.9, 0.5], [0.8, 1.8, 0.35], 90.0),
    ]

    return {
        "coordinateSystem": "RoomPlan (Y-up, meters, synthetic messy-room test)",
        "doors": [door],
        "floors": [floor],
        "objectCount": len(objects),
        "objects": objects,
        "scannedAt": "2026-09-19T00:00:00Z",
        "walls": walls,
        "windows": [window],
    }


if __name__ == "__main__":
    output_path = Path(__file__).parent / "messy_room.json"
    output_path.write_text(json.dumps(build(), indent=2), encoding="utf-8")
    print(f"Wrote {output_path}")
