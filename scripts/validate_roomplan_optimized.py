"""Validate RoomPlan-like optimized JSON for furniture overlap and room bounds."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_path", help="room_data.roomplan_optimized.json path")
    parser.add_argument("--epsilon", type=float, default=1e-3)
    return parser.parse_args()


def rotate_point(x: float, z: float, angle: float) -> tuple[float, float]:
    c, s = math.cos(-angle), math.sin(-angle)
    return (x * c - z * s), -(x * s + z * c)


def floor_context(data: dict[str, Any]) -> tuple[float, float, float, float]:
    floors = data.get("floors", [])
    if not floors:
        raise ValueError("JSON has no floors.")

    floor_transform = floors[0]["transform"]
    floor_theta = math.atan2(floor_transform[0][2], floor_transform[0][0])

    points: list[tuple[float, float]] = []
    for floor in floors:
        center = floor["center"]
        dimensions = floor["dimensions"]
        transform = floor["transform"]
        right = np.array([transform[0][0], transform[0][2]], dtype=float)
        depth = np.array([transform[1][0], transform[1][2]], dtype=float)
        center_xz = np.array([center[0], center[2]], dtype=float)
        for sx in (-1.0, 1.0):
            for sz in (-1.0, 1.0):
                point = center_xz + right * dimensions[0] * 0.5 * sx + depth * dimensions[1] * 0.5 * sz
                points.append(rotate_point(float(point[0]), float(point[1]), floor_theta))

    arr = np.array(points, dtype=float)
    min_x, min_z = arr.min(axis=0)
    max_x, max_z = arr.max(axis=0)
    return floor_theta, float(min_x), float(min_z), float(max_x - min_x), float(max_z - min_z)


def object_corners_2d(obj: dict[str, Any], floor_theta: float, min_x: float, min_z: float) -> np.ndarray:
    vertices = obj.get("obbVertices") or []
    if vertices:
        points = [rotate_point(float(vertex[0]), float(vertex[2]), floor_theta) for vertex in vertices]
        unique = []
        seen = set()
        for x, z in points:
            key = (round(x, 6), round(z, 6))
            if key not in seen:
                seen.add(key)
                unique.append([x - min_x, z - min_z])
        if len(unique) >= 4:
            arr = np.array(unique, dtype=float)
            center = arr.mean(axis=0)
            angles = np.arctan2(arr[:, 1] - center[1], arr[:, 0] - center[0])
            return arr[np.argsort(angles)]

    center = obj["center"]
    dimensions = obj["dimensions"]
    transform = obj.get("transform", [])
    theta = math.atan2(transform[0][2], transform[0][0]) if transform else 0.0
    rx, rz = rotate_point(float(center[0]), float(center[2]), floor_theta)
    cx, cz = rx - min_x, rz - min_z
    width, depth = float(dimensions[0]), float(dimensions[2])
    c, s = math.cos(theta - floor_theta), math.sin(theta - floor_theta)
    rotation = np.array([[c, -s], [s, c]], dtype=float)
    base = np.array(
        [[-width / 2.0, -depth / 2.0], [width / 2.0, -depth / 2.0], [width / 2.0, depth / 2.0], [-width / 2.0, depth / 2.0]],
        dtype=float,
    )
    return base @ rotation.T + [cx, cz]


def wall_corners_2d(wall: dict[str, Any], floor_theta: float, min_x: float, min_z: float, thickness: float = 0.08) -> np.ndarray:
    center = wall["center"]
    dimensions = wall["dimensions"]
    transform = wall["transform"]
    length = float(dimensions[0])
    wall_thickness = thickness
    center_xz = np.array([float(center[0]), float(center[2])], dtype=float)
    axis_x = np.array([float(transform[0][0]), float(transform[0][2])], dtype=float)
    axis_z = np.array([float(transform[2][0]), float(transform[2][2])], dtype=float)
    if np.linalg.norm(axis_x) < 1e-8:
        axis_x = np.array([1.0, 0.0])
    if np.linalg.norm(axis_z) < 1e-8:
        axis_z = np.array([0.0, 1.0])
    axis_x /= np.linalg.norm(axis_x)
    axis_z /= np.linalg.norm(axis_z)

    points = []
    for sx in (-1.0, 1.0):
        for sz in (-1.0, 1.0):
            point = center_xz + axis_x * length * 0.5 * sx + axis_z * wall_thickness * 0.5 * sz
            rx, rz = rotate_point(float(point[0]), float(point[1]), floor_theta)
            points.append([rx - min_x, rz - min_z])
    arr = np.array(points, dtype=float)
    center_2d = arr.mean(axis=0)
    angles = np.arctan2(arr[:, 1] - center_2d[1], arr[:, 0] - center_2d[0])
    return arr[np.argsort(angles)]


def sat_overlap(a: np.ndarray, b: np.ndarray, epsilon: float) -> float:
    min_overlap = float("inf")
    for corners in (a, b):
        for i in range(len(corners)):
            edge = corners[(i + 1) % len(corners)] - corners[i]
            axis = np.array([-edge[1], edge[0]], dtype=float)
            norm = np.linalg.norm(axis)
            if norm < 1e-8:
                continue
            axis /= norm
            pa = a @ axis
            pb = b @ axis
            overlap = min(pa.max(), pb.max()) - max(pa.min(), pb.min())
            if overlap <= epsilon:
                return 0.0
            min_overlap = min(min_overlap, overlap)
    return float(min_overlap)


def main() -> int:
    args = parse_args()
    data = json.loads(Path(args.json_path).read_text(encoding="utf-8"))
    floor_theta, min_x, min_z, room_width, room_depth = floor_context(data)
    objects = data.get("objects", [])
    corners = [object_corners_2d(obj, floor_theta, min_x, min_z) for obj in objects]
    walls = data.get("walls", [])
    wall_corners = [wall_corners_2d(wall, floor_theta, min_x, min_z) for wall in walls]

    print(f"room_width={room_width:.3f}, room_depth={room_depth:.3f}, objects={len(objects)}")

    issues = 0
    for index, (obj, poly) in enumerate(zip(objects, corners)):
        min_pt = poly.min(axis=0)
        max_pt = poly.max(axis=0)
        out_left = max(0.0, -float(min_pt[0]))
        out_bottom = max(0.0, -float(min_pt[1]))
        out_right = max(0.0, float(max_pt[0]) - room_width)
        out_top = max(0.0, float(max_pt[1]) - room_depth)
        if max(out_left, out_bottom, out_right, out_top) > args.epsilon:
            issues += 1
            print(
                f"[OUT] #{index} {obj.get('category')} {obj.get('model_key') or obj.get('modelFileName')} "
                f"left={out_left:.3f} bottom={out_bottom:.3f} right={out_right:.3f} top={out_top:.3f}"
            )

    for i in range(len(objects)):
        for j in range(i + 1, len(objects)):
            overlap = sat_overlap(corners[i], corners[j], args.epsilon)
            if overlap > args.epsilon:
                issues += 1
                a = objects[i].get("model_key") or objects[i].get("modelFileName")
                b = objects[j].get("model_key") or objects[j].get("modelFileName")
                print(f"[OVERLAP] #{i} {a} <-> #{j} {b}: overlap={overlap:.3f}")

    for i, (obj, poly) in enumerate(zip(objects, corners)):
        for j, wall_poly in enumerate(wall_corners):
            overlap = sat_overlap(poly, wall_poly, args.epsilon)
            if overlap > args.epsilon:
                issues += 1
                name = obj.get("model_key") or obj.get("modelFileName")
                print(f"[WALL] #{i} {name} intersects wall #{j}: overlap={overlap:.3f}")

    print(f"issues={issues}")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
