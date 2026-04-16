"""Canonical-layout optimizer with ergonomic penalty hierarchy."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy.optimize import differential_evolution, minimize


@dataclass(frozen=True)
class Anthropometrics:
    """Target user body dimensions in meters."""

    shoulder_width: float = 0.425
    sitting_popliteal: float = 0.463
    arm_reach: float = 0.565


@dataclass(frozen=True)
class PenaltyWeights:
    """Penalty hierarchy aligned with the project rubric."""

    critical: float = 1_000_000.0
    high: float = 100_000.0
    high_med: float = 50_000.0
    medium: float = 5_000.0
    low: float = 500.0
    reward: float = 2_000.0
    rotation_snap: float = 25_000.0
    wall_anchor: float = 15_000.0


class CanonicalLayoutOptimizer:
    """Optimize furniture placement from canonical room JSON."""

    def __init__(
        self,
        payload: dict[str, Any],
        anthropometrics: Anthropometrics | None = None,
        weights: PenaltyWeights | None = None,
    ):
        self.payload = payload
        dims = payload["room_metadata"]["dimensions"]
        self.room_width = float(dims["width"])
        self.room_depth = float(dims["depth"])
        self.room_height = float(dims["height"])
        self.furnitures = payload["movable_items"]
        self.fixed_elements = payload.get("fixed_elements", [])
        self.num_f = len(self.furnitures)
        self.body = anthropometrics or Anthropometrics()
        self.weights = weights or PenaltyWeights()

    @classmethod
    def from_path(cls, path: str | Path) -> "CanonicalLayoutOptimizer":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    @staticmethod
    def _obb_corners(cx: float, cy: float, width: float, depth: float, theta: float) -> np.ndarray:
        c, s = math.cos(theta), math.sin(theta)
        rotation = np.array([[c, -s], [s, c]])
        hw, hd = width / 2.0, depth / 2.0
        corners = np.array([[-hw, -hd], [hw, -hd], [hw, hd], [-hw, hd]])
        return corners @ rotation.T + [cx, cy]

    @staticmethod
    def _coords_from_furniture(furniture: dict[str, Any], optimized: bool = False) -> np.ndarray:
        pos_key = "optimized_pos" if optimized and "optimized_pos" in furniture else "pos"
        theta_key = "optimized_rotation_y_deg" if optimized and "optimized_rotation_y_deg" in furniture else "rotation_y_deg"
        pos = furniture[pos_key]
        theta = math.radians(float(furniture.get(theta_key, 0.0)))
        return np.array([float(pos[0]), float(pos[1]), float(pos[2]), theta], dtype=float)

    @staticmethod
    def _normalize(vector: list[float] | np.ndarray, fallback: tuple[float, float]) -> np.ndarray:
        arr = np.array(vector, dtype=float)
        norm = np.linalg.norm(arr)
        if norm < 1e-8:
            return np.array(fallback, dtype=float)
        return arr / norm

    @staticmethod
    def _sat_overlap(corners1: np.ndarray, corners2: np.ndarray) -> float:
        max_overlap = -1e9
        for corners in (corners1, corners2):
            for i in range(4):
                edge = corners[(i + 1) % 4] - corners[i]
                axis = np.array([-edge[1], edge[0]], dtype=float)
                axis /= np.linalg.norm(axis) + 1e-9
                proj1 = corners1 @ axis
                proj2 = corners2 @ axis
                overlap = min(np.max(proj1), np.max(proj2)) - max(np.min(proj1), np.min(proj2))
                if np.max(proj1) < np.min(proj2) - 1e-3 or np.max(proj2) < np.min(proj1) - 1e-3:
                    return 0.0
                max_overlap = max(max_overlap, overlap)
        return max_overlap

    def _world_front(self, furniture: dict[str, Any], theta: float) -> np.ndarray:
        base = self._normalize(furniture.get("front_vector_2d", [0.0, -1.0]), fallback=(0.0, -1.0))
        c, s = math.cos(theta), math.sin(theta)
        rotation = np.array([[c, -s], [s, c]])
        return rotation @ base

    def _distance_to_walls(self, corners: np.ndarray) -> tuple[float, float, float, float]:
        min_x, max_x = np.min(corners[:, 0]), np.max(corners[:, 0])
        min_y, max_y = np.min(corners[:, 1]), np.max(corners[:, 1])
        return min_x, self.room_width - max_x, min_y, self.room_depth - max_y

    @staticmethod
    def _quarter_turn_penalty(theta: float) -> float:
        """0 when theta is at 0/90/180/270 deg, max near 45 deg offsets."""
        return math.sin(2.0 * theta) ** 2

    @staticmethod
    def _snap_theta(theta: float) -> float:
        quarter = math.pi / 2.0
        return round(theta / quarter) * quarter

    def _front_clearance_depth(self, furniture: dict[str, Any]) -> float:
        kind = furniture["type"]
        if kind == "bed":
            return 0.4
        if kind == "desk":
            return 0.75
        if kind == "chair":
            return self.body.sitting_popliteal
        if kind == "closet":
            return 1.0
        if kind == "shelf":
            return 0.8
        return 0.6

    def _activity_area(
        self,
        furniture: dict[str, Any],
        coords: np.ndarray,
        *,
        width_override: float | None = None,
        depth_override: float | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        width = float(width_override if width_override is not None else furniture["extent"][0])
        depth = float(furniture["extent"][1])
        theta = float(coords[3])
        front = self._world_front(furniture, theta)
        activity_depth = float(depth_override if depth_override is not None else self._front_clearance_depth(furniture))
        offset = depth / 2.0 + activity_depth / 2.0
        center = coords[:2] + front * offset
        return self._obb_corners(center[0], center[1], width, activity_depth, theta), front

    def _door_clearance_polygon(self, door: dict[str, Any]) -> np.ndarray:
        wall = door["wall"]
        span = door["span"]
        start = float(span["start"])
        end = float(span["end"])
        depth = float(door.get("clearance_depth", 0.9))

        if wall == "west":
            return np.array([[0.0, start], [depth, start], [depth, end], [0.0, end]])
        if wall == "east":
            return np.array([[self.room_width, start], [self.room_width - depth, start], [self.room_width - depth, end], [self.room_width, end]])
        if wall == "south":
            return np.array([[start, 0.0], [end, 0.0], [end, depth], [start, depth]])
        return np.array([[start, self.room_depth], [end, self.room_depth], [end, self.room_depth - depth], [start, self.room_depth - depth]])

    def _wall_overlap_length(self, furniture_corners: np.ndarray, fixed: dict[str, Any]) -> float:
        wall = fixed["wall"]
        span = fixed["span"]
        start = float(span["start"])
        end = float(span["end"])
        left, right, bottom, top = self._distance_to_walls(furniture_corners)

        if wall == "west" and left < 0.12:
            furniture_start = np.min(furniture_corners[:, 1])
            furniture_end = np.max(furniture_corners[:, 1])
        elif wall == "east" and right < 0.12:
            furniture_start = np.min(furniture_corners[:, 1])
            furniture_end = np.max(furniture_corners[:, 1])
        elif wall == "south" and bottom < 0.12:
            furniture_start = np.min(furniture_corners[:, 0])
            furniture_end = np.max(furniture_corners[:, 0])
        elif wall == "north" and top < 0.12:
            furniture_start = np.min(furniture_corners[:, 0])
            furniture_end = np.max(furniture_corners[:, 0])
        else:
            return 0.0

        return max(0.0, min(furniture_end, end) - max(furniture_start, start))

    def _pairwise_clearance_penalty(self, a: np.ndarray, b: np.ndarray, required_gap: float, weight: float) -> float:
        overlap = self._sat_overlap(a, b)
        if overlap > 0:
            return self.weights.critical + overlap * weight
        expanded_required = required_gap / 2.0
        center_a = np.mean(a, axis=0)
        center_b = np.mean(b, axis=0)
        direction = center_b - center_a
        dist = np.linalg.norm(direction)
        if dist < 1e-8:
            return weight * required_gap
        unit = direction / dist
        proj_a = a @ unit
        proj_b = b @ unit
        free_gap = max(np.min(proj_b) - np.max(proj_a), np.min(proj_a) - np.max(proj_b), 0.0)
        return max(required_gap - free_gap, 0.0) * weight

    def _bed_penalty(self, furniture: dict[str, Any], coords: np.ndarray, corners: np.ndarray, all_obbs: list[np.ndarray], index: int) -> float:
        total = 0.0
        width, depth = map(float, furniture["extent"][:2])
        theta = float(coords[3])
        front = self._world_front(furniture, theta)
        lateral = np.array([-front[1], front[0]])

        side_clearance = 0.6
        foot_clearance = 0.4
        left_center = coords[:2] + lateral * (width / 2.0 + side_clearance / 2.0)
        right_center = coords[:2] - lateral * (width / 2.0 + side_clearance / 2.0)
        left_zone = self._obb_corners(left_center[0], left_center[1], side_clearance, depth, theta)
        right_zone = self._obb_corners(right_center[0], right_center[1], side_clearance, depth, theta)
        foot_center = coords[:2] + front * (depth / 2.0 + foot_clearance / 2.0)
        foot_zone = self._obb_corners(foot_center[0], foot_center[1], width, foot_clearance, theta)

        for zone in (left_zone, right_zone):
            if np.any(zone[:, 0] < 0) or np.any(zone[:, 0] > self.room_width) or np.any(zone[:, 1] < 0) or np.any(zone[:, 1] > self.room_depth):
                total += self.weights.high
            for j, other in enumerate(all_obbs):
                if j == index:
                    continue
                if self._sat_overlap(zone, other) > 0:
                    total += self.weights.high

        if np.any(foot_zone[:, 0] < 0) or np.any(foot_zone[:, 0] > self.room_width) or np.any(foot_zone[:, 1] < 0) or np.any(foot_zone[:, 1] > self.room_depth):
            total += self.weights.medium
        for j, other in enumerate(all_obbs):
            if j == index:
                continue
            if self._sat_overlap(foot_zone, other) > 0:
                total += self.weights.medium

        wall_distances = self._distance_to_walls(corners)
        nearest_wall = min(wall_distances)
        if nearest_wall > 0.05:
            total += self.weights.high + (nearest_wall - 0.05) * self.weights.high

        if sum(distance < 0.1 for distance in wall_distances) >= 2:
            total -= self.weights.reward * 4
        elif nearest_wall < 0.05:
            total -= self.weights.reward * 2

        return total

    def _desk_penalty(self, furniture: dict[str, Any], coords: np.ndarray, corners: np.ndarray, all_obbs: list[np.ndarray], index: int) -> float:
        total = 0.0
        front_zone, _ = self._activity_area(furniture, coords, depth_override=0.75)
        side_zone, _ = self._activity_area(furniture, coords, width_override=max(float(furniture["extent"][0]), self.body.shoulder_width), depth_override=0.6)

        if np.any(front_zone[:, 0] < 0) or np.any(front_zone[:, 0] > self.room_width) or np.any(front_zone[:, 1] < 0) or np.any(front_zone[:, 1] > self.room_depth):
            total += self.weights.high_med
        for j, other in enumerate(all_obbs):
            if j == index:
                continue
            if self._sat_overlap(front_zone, other) > 0:
                total += self.weights.high_med
            if self._sat_overlap(side_zone, other) > 0:
                total += self.weights.low

        back_wall_distance = min(self._distance_to_walls(corners))
        if back_wall_distance > 0.1:
            total += (back_wall_distance - 0.1) * self.weights.low

        return total

    def _chair_penalty(self, furniture: dict[str, Any], coords: np.ndarray, corners: np.ndarray, ids: dict[str, int], all_coords: np.ndarray) -> float:
        total = 0.0
        pair_id = furniture.get("pair_with")
        relationship = furniture.get("relationship", {})
        strength = relationship.get("strength", "weak")
        strength_weight = {"primary": 1.0, "weak": 0.35, "free": 0.1}.get(strength, 0.35)
        if pair_id and pair_id in ids:
            desk_idx = ids[pair_id]
            desk = self.furnitures[desk_idx]
            desk_coords = all_coords[desk_idx]
            desk_front = self._world_front(desk, float(desk_coords[3]))
            desk_lateral = np.array([-desk_front[1], desk_front[0]])
            desired_center = desk_coords[:2] + desk_front * (float(desk["extent"][1]) / 2.0 + self.body.sitting_popliteal / 2.0)
            axis_error = abs(np.dot((coords[:2] - desk_coords[:2]), desk_lateral))
            total += axis_error * self.weights.low * 10 * strength_weight
            total += np.linalg.norm(coords[:2] - desired_center) * self.weights.low * 6 * strength_weight

            chair_front = self._world_front(furniture, float(coords[3]))
            facing_penalty = max(1.0 - float(np.dot(chair_front, -desk_front)), 0.0)
            total += facing_penalty * self.weights.low * 20 * strength_weight

        rear_wall_distance = min(self._distance_to_walls(corners))
        if rear_wall_distance < 0.1:
            total += (0.1 - rear_wall_distance) * self.weights.low * 10

        radius = max(float(furniture["extent"][0]), float(furniture["extent"][1])) * 0.6
        center = coords[:2]
        wall_penalty = max(radius - center[0], 0) + max(radius - center[1], 0)
        wall_penalty += max(center[0] + radius - self.room_width, 0) + max(center[1] + radius - self.room_depth, 0)
        total += wall_penalty * self.weights.low * 4
        return total

    def _rotation_penalty(self, furniture: dict[str, Any], coords: np.ndarray, ids: dict[str, int], all_coords: np.ndarray) -> float:
        theta = float(coords[3])
        kind = furniture["type"]
        relationship = furniture.get("relationship", {})
        strength = relationship.get("strength", "weak")

        if kind in {"desk", "bed", "closet", "shelf"}:
            return self._quarter_turn_penalty(theta) * self.weights.rotation_snap

        if kind == "chair":
            base = self._quarter_turn_penalty(theta) * self.weights.rotation_snap * 0.35
            pair_id = furniture.get("pair_with")
            if pair_id and pair_id in ids and strength in {"primary", "weak"}:
                desk_idx = ids[pair_id]
                desk_theta = float(all_coords[desk_idx][3])
                desired_options = [
                    desk_theta,
                    desk_theta + math.pi,
                    desk_theta + math.pi / 2.0,
                    desk_theta - math.pi / 2.0,
                ]
                alignment_error = min(abs(math.atan2(math.sin(theta - opt), math.cos(theta - opt))) for opt in desired_options)
                weight = 1.0 if strength == "primary" else 0.45
                base += alignment_error * self.weights.rotation_snap * weight
            return base

        return self._quarter_turn_penalty(theta) * self.weights.rotation_snap * 0.2

    def _wall_anchor_penalty(self, furniture: dict[str, Any], corners: np.ndarray) -> float:
        prefs = furniture.get("anchor_preferences", {})
        rules = furniture.get("placement_rules", {})
        if not (prefs.get("wall_cling_required") or rules.get("back_near_wall_preferred") or rules.get("corner_preferred")):
            return 0.0

        wall_distances = self._distance_to_walls(corners)
        nearest = min(wall_distances)
        total = nearest * self.weights.wall_anchor

        if prefs.get("corner_preferred") or rules.get("corner_preferred"):
            corners_metric = min(
                wall_distances[0] + wall_distances[2],
                wall_distances[0] + wall_distances[3],
                wall_distances[1] + wall_distances[2],
                wall_distances[1] + wall_distances[3],
            )
            total += corners_metric * self.weights.wall_anchor * 0.5

        return total

    def _closet_penalty(self, furniture: dict[str, Any], coords: np.ndarray, corners: np.ndarray, all_obbs: list[np.ndarray], index: int) -> float:
        total = 0.0
        front_zone, front = self._activity_area(furniture, coords, depth_override=1.0)

        if np.any(front_zone[:, 0] < 0) or np.any(front_zone[:, 0] > self.room_width) or np.any(front_zone[:, 1] < 0) or np.any(front_zone[:, 1] > self.room_depth):
            total += self.weights.high_med
        for j, other in enumerate(all_obbs):
            if j == index:
                continue
            if self._sat_overlap(front_zone, other) > 0:
                total += self.weights.high_med

        hinge_radius = max(float(furniture["extent"][0]), float(furniture["extent"][1]))
        swing_center = coords[:2] + front * (float(furniture["extent"][1]) / 2.0)
        sweep = self._obb_corners(swing_center[0], swing_center[1], hinge_radius, hinge_radius, float(coords[3]))
        for j, other in enumerate(all_obbs):
            if j == index:
                continue
            if self._sat_overlap(sweep, other) > 0:
                total += self.weights.critical

        wall_distances = self._distance_to_walls(corners)
        if min(wall_distances) < 0.05 and sum(distance < 0.12 for distance in wall_distances) >= 2:
            total -= self.weights.reward * 3
        return total

    def _shelf_penalty(self, furniture: dict[str, Any], coords: np.ndarray, corners: np.ndarray, all_obbs: list[np.ndarray], index: int) -> float:
        total = 0.0
        drawer_zone, _ = self._activity_area(furniture, coords, depth_override=0.4)
        front_zone, _ = self._activity_area(furniture, coords, depth_override=0.8)

        for zone, weight in ((drawer_zone, self.weights.high), (front_zone, self.weights.high_med)):
            if np.any(zone[:, 0] < 0) or np.any(zone[:, 0] > self.room_width) or np.any(zone[:, 1] < 0) or np.any(zone[:, 1] > self.room_depth):
                total += weight
            for j, other in enumerate(all_obbs):
                if j == index:
                    continue
                if self._sat_overlap(zone, other) > 0:
                    total += weight

        wall_distances = self._distance_to_walls(corners)
        if min(wall_distances) < 0.05 and sum(distance < 0.12 for distance in wall_distances) >= 2:
            total -= self.weights.reward * 2
        return total

    def _window_penalty(self, furniture: dict[str, Any], corners: np.ndarray) -> float:
        total = 0.0
        height = float(furniture["extent"][2])
        for fixed in self.fixed_elements:
            if fixed.get("type") != "window":
                continue
            if height <= float(fixed.get("sill_height", 0.0)):
                continue
            overlap = self._wall_overlap_length(corners, fixed)
            if overlap > 0:
                total += overlap * self.weights.medium
        return total

    def _fixed_element_penalty(self, all_obbs: list[np.ndarray], all_coords: np.ndarray) -> float:
        total = 0.0
        for fixed in self.fixed_elements:
            if fixed.get("type") != "door":
                continue
            clearance = self._door_clearance_polygon(fixed)
            for obb in all_obbs:
                if self._sat_overlap(clearance, obb) > 0:
                    total += self.weights.critical

        return total

    def _objective(self, x: np.ndarray) -> float:
        coords = x.reshape(-1, 4)
        total = 0.0
        room_center = np.array([self.room_width / 2.0, self.room_depth / 2.0])
        obbs = [
            self._obb_corners(c[0], c[1], float(f["extent"][0]), float(f["extent"][1]), c[3])
            for c, f in zip(coords, self.furnitures)
        ]
        ids = {f["id"]: idx for idx, f in enumerate(self.furnitures)}

        for i, furniture in enumerate(self.furnitures):
            corners = obbs[i]
            left, right, bottom, top = self._distance_to_walls(corners)

            if min(left, right, bottom, top) < -1e-3:
                total += self.weights.critical

            for j in range(i + 1, self.num_f):
                overlap = self._sat_overlap(corners, obbs[j])
                if overlap > 0:
                    total += self.weights.critical + overlap * self.weights.critical

            if furniture["type"] == "bed":
                total += self._bed_penalty(furniture, coords[i], corners, obbs, i)
            elif furniture["type"] == "desk":
                total += self._desk_penalty(furniture, coords[i], corners, obbs, i)
            elif furniture["type"] == "chair":
                total += self._chair_penalty(furniture, coords[i], corners, ids, coords)
            elif furniture["type"] == "closet":
                total += self._closet_penalty(furniture, coords[i], corners, obbs, i)
            elif furniture["type"] == "shelf":
                total += self._shelf_penalty(furniture, coords[i], corners, obbs, i)

            total += self._window_penalty(furniture, corners)
            total += self._rotation_penalty(furniture, coords[i], ids, coords)
            total += self._wall_anchor_penalty(furniture, corners)

            center_distance = np.linalg.norm(coords[i, :2] - room_center)
            total += self.weights.low * 2 / (center_distance + 0.2)

        total += self._fixed_element_penalty(obbs, coords)
        return float(total)

    def optimize(
        self,
        *,
        global_maxiter: int = 40,
        global_popsize: int = 10,
        local_maxiter: int = 200,
    ) -> dict[str, Any]:
        if not self.furnitures:
            return self.payload

        bounds: list[tuple[float, float]] = []
        initial = []
        for furniture in self.furnitures:
            half_w = float(furniture["extent"][0]) / 2.0
            half_d = float(furniture["extent"][1]) / 2.0
            center_z = float(furniture["pos"][2])
            bounds.extend(
                [
                    (half_w, self.room_width - half_w),
                    (half_d, self.room_depth - half_d),
                    (center_z, center_z),
                    (0.0, 2.0 * math.pi),
                ]
            )
            initial.extend(
                [
                    float(furniture["pos"][0]),
                    float(furniture["pos"][1]),
                    center_z,
                    math.radians(float(furniture.get("rotation_y_deg", 0.0))) % (2.0 * math.pi),
                ]
            )

        result_global = differential_evolution(
            self._objective,
            bounds,
            seed=42,
            init="sobol",
            polish=False,
            maxiter=global_maxiter,
            popsize=global_popsize,
        )
        result_local = minimize(
            self._objective,
            result_global.x if np.isfinite(result_global.fun) else np.array(initial, dtype=float),
            method="SLSQP",
            bounds=bounds,
            tol=1e-4,
            options={"maxiter": local_maxiter},
        )

        optimized = result_local.x.reshape(-1, 4)
        for i, furniture in enumerate(self.furnitures):
            optimized[i, 3] = self._snap_theta(float(optimized[i, 3]))
        output = json.loads(json.dumps(self.payload))
        for i, furniture in enumerate(output["movable_items"]):
            theta_deg = round(math.degrees(optimized[i, 3]) % 360.0, 3)
            furniture["optimized_pos"] = [
                round(float(optimized[i, 0]), 3),
                round(float(optimized[i, 1]), 3),
                round(float(optimized[i, 2]), 3),
            ]
            furniture["optimized_rotation_y_deg"] = theta_deg

        output["optimization"] = {
            "objective_value": round(float(result_local.fun), 3),
            "solver": "differential_evolution + SLSQP",
            "anthropometrics_m": {
                "shoulder_width": self.body.shoulder_width,
                "sitting_popliteal": self.body.sitting_popliteal,
                "arm_reach": self.body.arm_reach,
            },
        }
        return output

    def save_comparison_svg(self, payload: dict[str, Any], output_path: str | Path) -> None:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        panel_width = 520
        panel_height = 520
        margin = 40
        gap = 40
        total_width = panel_width * 2 + gap + margin * 2
        total_height = panel_height + margin * 2

        colors = {
            "desk": "#60a5fa",
            "chair": "#34d399",
            "bed": "#fbbf24",
            "closet": "#f87171",
            "shelf": "#a78bfa",
        }

        def project(x: float, y: float, x_offset: float) -> tuple[float, float]:
            px = x_offset + (x / self.room_width) * panel_width
            py = margin + panel_height - (y / self.room_depth) * panel_height
            return px, py

        def polygon_points(corners: np.ndarray, x_offset: float) -> str:
            return " ".join(
                f"{px:.1f},{py:.1f}" for px, py in (project(float(x), float(y), x_offset) for x, y in corners)
            )

        def fixed_svg(fixed: dict[str, Any], x_offset: float) -> str:
            wall = fixed.get("wall")
            span = fixed.get("span", {})
            start = float(span.get("start", 0.0))
            end = float(span.get("end", 0.0))
            color = {"door": "#d97706", "window": "#2563eb", "wall": "#6b7280"}.get(fixed.get("type"), "#6b7280")

            if wall == "south":
                x1, y1 = project(start, 0.0, x_offset)
                x2, y2 = project(end, 0.0, x_offset)
            elif wall == "north":
                x1, y1 = project(start, self.room_depth, x_offset)
                x2, y2 = project(end, self.room_depth, x_offset)
            elif wall == "west":
                x1, y1 = project(0.0, start, x_offset)
                x2, y2 = project(0.0, end, x_offset)
            else:
                x1, y1 = project(self.room_width, start, x_offset)
                x2, y2 = project(self.room_width, end, x_offset)
            return f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color}" stroke-width="5" />'

        panels: list[str] = []
        for idx, (title, optimized) in enumerate((("Original", False), ("Optimized", True))):
            x_offset = margin + idx * (panel_width + gap)
            items = [
                f'<rect x="{x_offset}" y="{margin}" width="{panel_width}" height="{panel_height}" fill="#ffffff" stroke="#111827" stroke-width="2" />',
                f'<text x="{x_offset}" y="{margin - 12}" font-size="18" font-family="Arial, sans-serif" fill="#111827">{title}</text>',
            ]
            items.extend(fixed_svg(fixed, x_offset) for fixed in payload.get("fixed_elements", []))

            for furniture in payload["movable_items"]:
                coords = self._coords_from_furniture(furniture, optimized=optimized)
                corners = self._obb_corners(
                    coords[0],
                    coords[1],
                    float(furniture["extent"][0]),
                    float(furniture["extent"][1]),
                    coords[3],
                )
                fill = colors.get(furniture["type"], "#cbd5e1")
                items.append(
                    f'<polygon points="{polygon_points(corners, x_offset)}" fill="{fill}" fill-opacity="0.35" stroke="#111827" stroke-width="1.5" />'
                )

                activity_corners, front = self._activity_area(furniture, coords)
                items.append(
                    f'<polygon points="{polygon_points(activity_corners, x_offset)}" fill="none" stroke="#ef4444" stroke-dasharray="6 4" stroke-width="1.5" />'
                )

                cx, cy = project(float(coords[0]), float(coords[1]), x_offset)
                fx, fy = project(float(coords[0] + front[0] * 0.35), float(coords[1] + front[1] * 0.35), x_offset)
                items.append(
                    f'<line x1="{cx:.1f}" y1="{cy:.1f}" x2="{fx:.1f}" y2="{fy:.1f}" stroke="#ef4444" stroke-width="2" />'
                )
                items.append(
                    f'<text x="{cx:.1f}" y="{cy:.1f}" text-anchor="middle" dominant-baseline="middle" font-size="9" font-family="Arial, sans-serif" fill="#111827">{furniture["type"]}</text>'
                )

            panels.append("\n".join(items))

        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{total_width}" height="{total_height}" viewBox="0 0 {total_width} {total_height}">
<rect width="100%" height="100%" fill="#f8fafc" />
{''.join(panels)}
</svg>
"""
        output_path.write_text(svg, encoding="utf-8")
