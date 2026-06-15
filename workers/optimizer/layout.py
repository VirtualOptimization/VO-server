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
    body_collision: float = 50_000_000.0


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

    @staticmethod
    def _sat_penetration(corners1: np.ndarray, corners2: np.ndarray) -> float:
        min_overlap = 1e9
        for corners in (corners1, corners2):
            for i in range(4):
                edge = corners[(i + 1) % 4] - corners[i]
                axis = np.array([-edge[1], edge[0]], dtype=float)
                axis /= np.linalg.norm(axis) + 1e-9
                proj1 = corners1 @ axis
                proj2 = corners2 @ axis
                overlap = min(np.max(proj1), np.max(proj2)) - max(np.min(proj1), np.min(proj2))
                if overlap <= 1e-3:
                    return 0.0
                min_overlap = min(min_overlap, overlap)
        return min_overlap

    @staticmethod
    def _sat_mtv(corners1: np.ndarray, corners2: np.ndarray) -> np.ndarray:
        min_overlap = 1e9
        best_axis: np.ndarray | None = None
        for corners in (corners1, corners2):
            for i in range(4):
                edge = corners[(i + 1) % 4] - corners[i]
                axis = np.array([-edge[1], edge[0]], dtype=float)
                axis /= np.linalg.norm(axis) + 1e-9
                proj1 = corners1 @ axis
                proj2 = corners2 @ axis
                overlap = min(np.max(proj1), np.max(proj2)) - max(np.min(proj1), np.min(proj2))
                if overlap <= 1e-3:
                    return np.array([0.0, 0.0], dtype=float)
                if overlap < min_overlap:
                    min_overlap = overlap
                    best_axis = axis

        if best_axis is None:
            return np.array([0.0, 0.0], dtype=float)

        center_delta = np.mean(corners2, axis=0) - np.mean(corners1, axis=0)
        if float(np.dot(center_delta, best_axis)) < 0.0:
            best_axis = -best_axis
        return best_axis * min_overlap

    def _world_front(self, furniture: dict[str, Any], theta: float) -> np.ndarray:
        base = self._normalize(furniture.get("front_vector_2d", [0.0, -1.0]), fallback=(0.0, -1.0))
        original_theta = math.radians(float(furniture.get("rotation_y_deg", 0.0)))
        delta = theta - original_theta
        c, s = math.cos(delta), math.sin(delta)
        rotation = np.array([[c, -s], [s, c]])
        return rotation @ base

    def _theta_for_world_front(self, furniture: dict[str, Any], desired_front: np.ndarray) -> float:
        front = self._normalize(desired_front, fallback=(0.0, -1.0))
        base = self._normalize(furniture.get("front_vector_2d", [0.0, -1.0]), fallback=(0.0, -1.0))
        original_theta = math.radians(float(furniture.get("rotation_y_deg", 0.0)))
        desired_angle = math.atan2(float(front[1]), float(front[0]))
        base_angle = math.atan2(float(base[1]), float(base[0]))
        return (original_theta + desired_angle - base_angle) % (2.0 * math.pi)

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
        if kind in {"desk", "table"}:
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

    def _wall_inward_normal(self, wall: dict[str, Any]) -> np.ndarray:
        center = np.array(wall.get("center", [self.room_width / 2.0, self.room_depth / 2.0]), dtype=float)
        axis = self._normalize(wall.get("axis", [1.0, 0.0]), fallback=(1.0, 0.0))
        normal = np.array([-axis[1], axis[0]], dtype=float)
        room_center = np.array([self.room_width / 2.0, self.room_depth / 2.0], dtype=float)
        if float(np.dot(room_center - center, normal)) < 0.0:
            normal = -normal
        return normal

    def _wall_obstacle_polygon(self, wall: dict[str, Any], *, buffer: float = 0.0) -> np.ndarray:
        if "center" in wall and "axis" in wall and "length" in wall:
            center = np.array(wall["center"], dtype=float)
            axis = self._normalize(wall["axis"], fallback=(1.0, 0.0))
            normal = np.array([-axis[1], axis[0]], dtype=float)
            length = float(wall["length"])
            thickness = max(float(wall.get("thickness", 0.1)), 0.1) + buffer * 2.0
            half_length = length / 2.0
            half_thickness = thickness / 2.0
            start = center - axis * half_length
            end = center + axis * half_length
            return np.array([
                start + normal * half_thickness,
                end + normal * half_thickness,
                end - normal * half_thickness,
                start - normal * half_thickness,
            ])

        span = wall["span"]
        start = float(span["start"])
        end = float(span["end"])
        thickness = max(float(wall.get("thickness", 0.1)), 0.1) + buffer
        if wall["wall"] == "west":
            return np.array([[0.0, start], [thickness, start], [thickness, end], [0.0, end]])
        if wall["wall"] == "east":
            return np.array([[self.room_width, start], [self.room_width - thickness, start], [self.room_width - thickness, end], [self.room_width, end]])
        if wall["wall"] == "south":
            return np.array([[start, 0.0], [end, 0.0], [end, thickness], [start, thickness]])
        return np.array([[start, self.room_depth], [end, self.room_depth], [end, self.room_depth - thickness], [start, self.room_depth - thickness]])

    def _keep_obb_inside_room(self, coords: np.ndarray, furniture: dict[str, Any]) -> np.ndarray:
        adjusted = np.array(coords, dtype=float)
        for _ in range(3):
            corners = self._obb_corners(
                adjusted[0],
                adjusted[1],
                float(furniture["extent"][0]),
                float(furniture["extent"][1]),
                adjusted[3],
            )
            left, right, bottom, top = self._distance_to_walls(corners)
            shift = np.array([0.0, 0.0], dtype=float)
            if left < 0.0:
                shift[0] += -left
            if right < 0.0:
                shift[0] -= -right
            if bottom < 0.0:
                shift[1] += -bottom
            if top < 0.0:
                shift[1] -= -top
            if np.linalg.norm(shift) < 1e-8:
                break
            adjusted[:2] += shift
        return adjusted

    def _resolve_wall_collisions(self, coords: np.ndarray) -> np.ndarray:
        adjusted = np.array(coords, dtype=float)
        walls = [fixed for fixed in self.fixed_elements if fixed.get("type") == "wall"]
        if not walls:
            return adjusted

        margin = 0.02
        for _ in range(8):
            moved = False
            for i, furniture in enumerate(self.furnitures):
                corners = self._obb_corners(
                    adjusted[i, 0],
                    adjusted[i, 1],
                    float(furniture["extent"][0]),
                    float(furniture["extent"][1]),
                    adjusted[i, 3],
                )
                for wall in walls:
                    wall_poly = self._wall_obstacle_polygon(wall, buffer=margin)
                    overlap = self._sat_penetration(corners, wall_poly)
                    if overlap <= 0.0:
                        continue

                    adjusted[i, :2] += self._wall_inward_normal(wall) * (overlap + margin)
                    adjusted[i] = self._keep_obb_inside_room(adjusted[i], furniture)
                    corners = self._obb_corners(
                        adjusted[i, 0],
                        adjusted[i, 1],
                        float(furniture["extent"][0]),
                        float(furniture["extent"][1]),
                        adjusted[i, 3],
                    )
                    moved = True
            if not moved:
                break

        return adjusted

    @staticmethod
    def _mobility_weight(furniture: dict[str, Any]) -> float:
        return {
            "chair": 1.0,
            "desk": 0.55,
            "table": 0.55,
            "shelf": 0.4,
            "closet": 0.35,
            "bed": 0.25,
        }.get(furniture.get("type"), 0.6)

    def _resolve_furniture_collisions(self, coords: np.ndarray) -> np.ndarray:
        adjusted = np.array(coords, dtype=float)
        if self.num_f < 2:
            return adjusted

        margin = 0.08
        for _ in range(80):
            moved = False
            max_penetration = 0.0

            for i in range(self.num_f):
                for j in range(i + 1, self.num_f):
                    obb_i = self._obb_corners(
                        adjusted[i, 0],
                        adjusted[i, 1],
                        float(self.furnitures[i]["extent"][0]),
                        float(self.furnitures[i]["extent"][1]),
                        adjusted[i, 3],
                    )
                    obb_j = self._obb_corners(
                        adjusted[j, 0],
                        adjusted[j, 1],
                        float(self.furnitures[j]["extent"][0]),
                        float(self.furnitures[j]["extent"][1]),
                        adjusted[j, 3],
                    )
                    mtv = self._sat_mtv(obb_i, obb_j)
                    penetration = np.linalg.norm(mtv)
                    if penetration <= 1e-8:
                        continue

                    max_penetration = max(max_penetration, float(penetration))
                    direction = mtv / penetration
                    total_mobility = self._mobility_weight(self.furnitures[i]) + self._mobility_weight(self.furnitures[j])
                    i_share = self._mobility_weight(self.furnitures[j]) / total_mobility
                    j_share = self._mobility_weight(self.furnitures[i]) / total_mobility
                    adjusted[i, :2] -= direction * (penetration + margin) * i_share
                    adjusted[j, :2] += direction * (penetration + margin) * j_share
                    adjusted[i] = self._keep_obb_inside_room(adjusted[i], self.furnitures[i])
                    adjusted[j] = self._keep_obb_inside_room(adjusted[j], self.furnitures[j])
                    moved = True

            if moved:
                adjusted = self._resolve_wall_collisions(adjusted)
                if max_penetration <= 1e-3:
                    break
            else:
                break

        return adjusted

    def _max_furniture_penetration(self, coords: np.ndarray) -> float:
        max_penetration = 0.0
        obbs = [
            self._obb_corners(c[0], c[1], float(f["extent"][0]), float(f["extent"][1]), c[3])
            for c, f in zip(coords, self.furnitures)
        ]
        for i in range(self.num_f):
            for j in range(i + 1, self.num_f):
                max_penetration = max(max_penetration, self._sat_penetration(obbs[i], obbs[j]))
        return float(max_penetration)

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
        support_type = relationship.get("nearest_support_type", "desk")
        strength_weight = {"primary": 1.0, "weak": 0.6, "free": 0.35}.get(strength, 0.35)
        if pair_id and pair_id in ids:
            desk_idx = ids[pair_id]
            desk = self.furnitures[desk_idx]
            desk_coords = all_coords[desk_idx]
            desk_front = self._world_front(desk, float(desk_coords[3]))
            desk_lateral = np.array([-desk_front[1], desk_front[0]])
            desired_center = self._chair_support_center(furniture, desk, desk_coords, desk_front, desk_lateral, relationship)

            pair_distance = np.linalg.norm(coords[:2] - desired_center)
            pair_weight = self.weights.high if strength == "primary" else self.weights.medium
            total += pair_distance * pair_weight * strength_weight

            chair_front = self._world_front(furniture, float(coords[3]))
            facing_penalty = max(1.0 - float(np.dot(chair_front, -desk_front)), 0.0)
            total += facing_penalty * self.weights.medium * 4 * strength_weight

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

        if kind in {"desk", "table", "bed", "closet", "shelf"}:
            return self._quarter_turn_penalty(theta) * self.weights.rotation_snap

        if kind == "chair":
            base = self._quarter_turn_penalty(theta) * self.weights.rotation_snap * 0.35
            pair_id = furniture.get("pair_with")
            if pair_id and pair_id in ids and strength in {"primary", "weak"}:
                desk_idx = ids[pair_id]
                desk_theta = float(all_coords[desk_idx][3])
                relationship_delta = math.radians(float(relationship.get("rotation_delta_deg", 180.0)))
                desired_options = [desk_theta + relationship_delta]
                alignment_error = min(abs(math.atan2(math.sin(theta - opt), math.cos(theta - opt))) for opt in desired_options)
                weight = 1.0 if strength == "primary" else 0.45
                base += alignment_error * self.weights.rotation_snap * weight
            return base

        return self._quarter_turn_penalty(theta) * self.weights.rotation_snap * 0.2

    def _wall_anchor_penalty(self, furniture: dict[str, Any], corners: np.ndarray) -> float:
        prefs = furniture.get("anchor_preferences", {})
        rules = furniture.get("placement_rules", {})
        back_to_wall = prefs.get("back_to_wall") or rules.get("back_near_wall_preferred")

        if not (prefs.get("wall_cling_required") or rules.get("back_near_wall_preferred") or rules.get("corner_preferred")):
            return 0.0

        wall_distances = self._distance_to_walls(corners)
        nearest_idx = int(np.argmin(wall_distances))
        nearest_dist = wall_distances[nearest_idx]

        total = nearest_dist * self.weights.wall_anchor

        if prefs.get("wall_cling_required") and nearest_dist > 0.05:
            total += self.weights.high + (nearest_dist - 0.05) * self.weights.high_med

        # Enforce back-to-wall alignment
        if back_to_wall:
            theta = float(self._snap_theta(np.arctan2(corners[1, 1] - corners[0, 1], corners[1, 0] - corners[0, 0])))
            # Actually use current optimized theta
            # but corners already reflect the current pose in _objective

            # Back vector is opposite of world front
            # We can derive it from the orientation of the OBB
            front = self._world_front(furniture, self._snap_theta(theta))  # Use snapped for alignment check
            back = -front

            # Wall normals (pointing OUT of the room to match back vector)
            # 0: West (x=0) -> [-1, 0]
            # 1: East (x=W) -> [1, 0]
            # 2: South (y=0) -> [0, -1]
            # 3: North (y=D) -> [0, 1]
            wall_normals = [
                np.array([-1.0, 0.0]),
                np.array([1.0, 0.0]),
                np.array([0.0, -1.0]),
                np.array([0.0, 1.0])
            ]

            target_normal = wall_normals[nearest_idx]
            alignment = float(np.dot(back, target_normal))

            if alignment < 0.9:  # Not facing the wall
                total += self.weights.high * (1.0 - alignment)

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
            fixed_type = fixed.get("type")
            if fixed_type == "door":
                clearance = self._door_clearance_polygon(fixed)
            elif fixed_type == "wall":
                clearance = self._wall_obstacle_polygon(fixed, buffer=0.02)
            else:
                continue
            for obb in all_obbs:
                overlap = (
                    self._sat_penetration(clearance, obb)
                    if fixed_type == "wall"
                    else self._sat_overlap(clearance, obb)
                )
                if overlap > 0:
                    total += self.weights.critical + overlap * self.weights.critical

        return total

    def _outside_room_penalty(self, corners: np.ndarray) -> float:
        left, right, bottom, top = self._distance_to_walls(corners)
        penetration = sum(max(-float(distance), 0.0) for distance in (left, right, bottom, top))
        if penetration <= 1e-3:
            return 0.0
        return self.weights.critical + penetration * self.weights.critical * 10.0

    def _scan_pose_penalty(self, furniture: dict[str, Any], coords: np.ndarray) -> float:
        original = self._coords_from_furniture(furniture)
        xy_distance = float(np.linalg.norm(coords[:2] - original[:2]))
        angle_delta = abs(math.atan2(math.sin(float(coords[3] - original[3])), math.cos(float(coords[3] - original[3]))))

        move_weight = self.weights.low * 0.15
        rotation_weight = self.weights.rotation_snap * 0.015
        if furniture["type"] == "chair" and furniture.get("relationship", {}).get("strength") == "primary":
            move_weight = self.weights.low * 0.25
            rotation_weight = self.weights.rotation_snap * 0.025

        return (xy_distance * move_weight) + (angle_delta * rotation_weight)

    def _chair_support_center(
        self,
        chair: dict[str, Any],
        support: dict[str, Any],
        support_coords: np.ndarray,
        support_front: np.ndarray,
        support_lateral: np.ndarray,
        relationship: dict[str, Any],
    ) -> np.ndarray:
        local_offset = relationship.get("local_offset")
        strength = relationship.get("strength", "weak")
        support_type = relationship.get("nearest_support_type", support.get("type", "desk"))

        support_depth_radius = float(support["extent"][1]) / 2.0
        chair_depth_radius = float(chair["extent"][1]) / 2.0
        base_gap = 0.05
        target_distance = support_depth_radius + chair_depth_radius + base_gap

        if support_type == "table":
            target_distance += 0.05

        if local_offset and len(local_offset) >= 2:
            local = np.array([float(local_offset[0]), float(local_offset[1])], dtype=float)
            if support_type == "table":
                half_width = float(support["extent"][0]) / 2.0
                half_depth = float(support["extent"][1]) / 2.0
                chair_width = float(chair["extent"][0])
                chair_depth = float(chair["extent"][1])
                front_score = abs(local[0]) / max(half_depth, 1e-6)
                side_score = abs(local[1]) / max(half_width, 1e-6)

                if front_score >= side_score:
                    sign = 1.0 if local[0] >= 0 else -1.0
                    local[0] = sign * (half_depth + chair_depth * 0.12)
                    local[1] = float(np.clip(local[1], -half_width + chair_width / 2.0, half_width - chair_width / 2.0))
                else:
                    sign = 1.0 if local[1] >= 0 else -1.0
                    local[0] = float(np.clip(local[0], -half_depth + chair_depth / 2.0, half_depth - chair_depth / 2.0))
                    local[1] = sign * (half_width + chair_width * 0.12)
                return support_coords[:2] + support_front * local[0] + support_lateral * local[1]

            if strength == "primary":
                return support_coords[:2] + support_front * local[0] + support_lateral * local[1]

            distance = float(np.linalg.norm(local))
            if distance < 1e-8:
                local = np.array([target_distance, 0.0], dtype=float)
            elif distance < target_distance:
                local = local / distance * target_distance
            elif strength != "primary" and distance > target_distance:
                pull_distance = max(target_distance, 1.1 if support_type == "table" else 0.85)
                local = local / max(distance, 1e-8) * min(distance, pull_distance)

            return support_coords[:2] + support_front * local[0] + support_lateral * local[1]

        return support_coords[:2] + support_front * target_distance

    def _chair_support_initial(self, initial: np.ndarray) -> np.ndarray:
        coords = initial.reshape(-1, 4).copy()
        ids = {f["id"]: idx for idx, f in enumerate(self.furnitures)}

        for chair_idx, chair in enumerate(self.furnitures):
            if chair["type"] != "chair":
                continue
            pair_id = chair.get("pair_with")
            relationship = chair.get("relationship", {})
            if not pair_id or pair_id not in ids or relationship.get("strength") == "primary":
                continue

            support_idx = ids[pair_id]
            support = self.furnitures[support_idx]
            support_front = self._world_front(support, float(coords[support_idx, 3]))
            support_lateral = np.array([-support_front[1], support_front[0]])
            coords[chair_idx, :2] = self._chair_support_center(
                chair,
                support,
                coords[support_idx],
                support_front,
                support_lateral,
                relationship,
            )
            rotation_delta = math.radians(float(relationship.get("rotation_delta_deg", 180.0)))
            coords[chair_idx, 3] = (coords[support_idx, 3] + rotation_delta) % (2.0 * math.pi)

        return self._project_inside_room(coords.reshape(-1))

    def _sync_paired_chairs(self, x: np.ndarray, *, primary_only: bool = False) -> np.ndarray:
        coords = x.reshape(-1, 4).copy()
        ids = {f["id"]: idx for idx, f in enumerate(self.furnitures)}

        for chair_idx, chair in enumerate(self.furnitures):
            if chair["type"] != "chair":
                continue
            pair_id = chair.get("pair_with")
            relationship = chair.get("relationship", {})
            if not pair_id or pair_id not in ids:
                continue
            if primary_only and relationship.get("strength") != "primary":
                continue

            support_idx = ids[pair_id]
            support = self.furnitures[support_idx]
            support_front = self._world_front(support, float(coords[support_idx, 3]))
            support_lateral = np.array([-support_front[1], support_front[0]])
            coords[chair_idx, :2] = self._chair_support_center(
                chair,
                support,
                coords[support_idx],
                support_front,
                support_lateral,
                relationship,
            )
            rotation_delta = math.radians(float(relationship.get("rotation_delta_deg", 180.0)))
            coords[chair_idx, 3] = (coords[support_idx, 3] + rotation_delta) % (2.0 * math.pi)

        return coords.reshape(-1)

    def _wall_anchored_initial(self, initial: np.ndarray, *, force: bool = False) -> np.ndarray:
        coords = initial.reshape(-1, 4).copy()
        ids = {f["id"]: idx for idx, f in enumerate(self.furnitures)}
        target_gap = 0.02

        def sync_primary_chairs(source: np.ndarray) -> np.ndarray:
            synced = source.copy()
            for chair_idx, chair in enumerate(self.furnitures):
                if chair["type"] != "chair":
                    continue
                pair_id = chair.get("pair_with")
                relationship = chair.get("relationship", {})
                if not pair_id or pair_id not in ids or relationship.get("strength") != "primary":
                    continue

                desk_idx = ids[pair_id]
                desk = self.furnitures[desk_idx]
                desk_front = self._world_front(desk, float(synced[desk_idx, 3]))
                desk_lateral = np.array([-desk_front[1], desk_front[0]])
                local_offset = relationship.get("local_offset")
                if local_offset and len(local_offset) >= 2:
                    synced[chair_idx, :2] = (
                        synced[desk_idx, :2]
                        + desk_front * float(local_offset[0])
                        + desk_lateral * float(local_offset[1])
                    )
                rotation_delta = math.radians(float(relationship.get("rotation_delta_deg", 180.0)))
                synced[chair_idx, 3] = (synced[desk_idx, 3] + rotation_delta) % (2.0 * math.pi)
            return synced

        for i, furniture in enumerate(self.furnitures):
            prefs = furniture.get("anchor_preferences", {})
            rules = furniture.get("placement_rules", {})
            if furniture["type"] == "chair":
                continue
            if not (prefs.get("wall_cling_required") or rules.get("back_near_wall_preferred") or rules.get("corner_preferred")):
                continue

            corners = self._obb_corners(
                coords[i, 0],
                coords[i, 1],
                float(furniture["extent"][0]),
                float(furniture["extent"][1]),
                coords[i, 3],
            )
            distances = self._distance_to_walls(corners)
            candidates = [] if force else [coords]

            for wall_idx, distance in enumerate(distances):
                shift = max(float(distance) - target_gap, 0.0)
                if shift <= 0.0:
                    continue
                candidate = coords.copy()
                if wall_idx == 0:
                    candidate[i, 0] -= shift
                elif wall_idx == 1:
                    candidate[i, 0] += shift
                elif wall_idx == 2:
                    candidate[i, 1] -= shift
                else:
                    candidate[i, 1] += shift
                candidates.append(self._sync_paired_chairs(candidate, primary_only=True).reshape(-1, 4))

            if candidates:
                coords = min(
                    candidates,
                    key=lambda candidate: self._objective(candidate.reshape(-1)),
                )

        return self._sync_paired_chairs(coords, primary_only=True)

    def _project_inside_room(self, x: np.ndarray) -> np.ndarray:
        coords = x.reshape(-1, 4).copy()
        for _ in range(4):
            changed = False
            for i, furniture in enumerate(self.furnitures):
                corners = self._obb_corners(
                    coords[i, 0],
                    coords[i, 1],
                    float(furniture["extent"][0]),
                    float(furniture["extent"][1]),
                    coords[i, 3],
                )
                left, right, bottom, top = self._distance_to_walls(corners)
                dx = max(-float(left), 0.0) - max(-float(right), 0.0)
                dy = max(-float(bottom), 0.0) - max(-float(top), 0.0)
                if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                    coords[i, 0] += dx
                    coords[i, 1] += dy
                    changed = True
            if not changed:
                break
        return coords.reshape(-1)

    def _resolve_pairwise_overlaps(self, x: np.ndarray) -> np.ndarray:
        coords = x.reshape(-1, 4).copy()
        ids = {f["id"]: idx for idx, f in enumerate(self.furnitures)}

        def paired_chair_indices(index: int) -> list[int]:
            furniture_id = self.furnitures[index]["id"]
            return [
                chair_idx
                for chair_idx, chair in enumerate(self.furnitures)
                if chair["type"] == "chair" and chair.get("pair_with") == furniture_id
            ]

        def is_paired_support(chair_index: int, support_index: int) -> bool:
            chair = self.furnitures[chair_index]
            if chair["type"] != "chair":
                return False
            return ids.get(chair.get("pair_with")) == support_index

        def is_same_support_chair_pair(first_index: int, second_index: int) -> bool:
            first = self.furnitures[first_index]
            second = self.furnitures[second_index]
            return (
                first["type"] == "chair"
                and second["type"] == "chair"
                and first.get("pair_with")
                and first.get("pair_with") == second.get("pair_with")
            )

        def apply_shift(index: int, shift: np.ndarray) -> None:
            coords[index, :2] += shift
            if self.furnitures[index]["type"] != "chair":
                for chair_idx in paired_chair_indices(index):
                    coords[chair_idx, :2] += shift

        for _ in range(12):
            moved = False
            obbs = [
                self._obb_corners(c[0], c[1], float(f["extent"][0]), float(f["extent"][1]), c[3])
                for c, f in zip(coords, self.furnitures)
            ]
            for i in range(self.num_f):
                for j in range(i + 1, self.num_f):
                    overlap = self._sat_overlap(obbs[i], obbs[j])
                    if overlap <= 1e-3:
                        continue
                    if is_paired_support(i, j) or is_paired_support(j, i):
                        continue
                    if is_same_support_chair_pair(i, j):
                        continue

                    direction = coords[j, :2] - coords[i, :2]
                    norm = float(np.linalg.norm(direction))
                    if norm < 1e-8:
                        direction = np.array([1.0, 0.0], dtype=float)
                    else:
                        direction = direction / norm

                    shift = direction * ((overlap / 2.0) + 0.015)
                    apply_shift(i, -shift)
                    apply_shift(j, shift)
                    moved = True

            if not moved:
                break
            coords = self._project_inside_room(coords.reshape(-1)).reshape(-1, 4)

        return coords.reshape(-1)

    def _resolve_same_support_chair_overlaps(self, x: np.ndarray) -> np.ndarray:
        coords = x.reshape(-1, 4).copy()
        ids = {f["id"]: idx for idx, f in enumerate(self.furnitures)}

        for _ in range(8):
            moved = False
            obbs = [
                self._obb_corners(c[0], c[1], float(f["extent"][0]), float(f["extent"][1]), c[3])
                for c, f in zip(coords, self.furnitures)
            ]
            for i in range(self.num_f):
                first = self.furnitures[i]
                if first["type"] != "chair" or not first.get("pair_with"):
                    continue
                support_idx = ids.get(first["pair_with"])
                if support_idx is None:
                    continue

                for j in range(i + 1, self.num_f):
                    second = self.furnitures[j]
                    if second["type"] != "chair" or second.get("pair_with") != first.get("pair_with"):
                        continue

                    overlap = self._sat_overlap(obbs[i], obbs[j])
                    if overlap <= 1e-3:
                        continue

                    direction = coords[j, :2] - coords[i, :2]
                    norm = float(np.linalg.norm(direction))
                    if norm < 1e-8:
                        support_center = coords[support_idx, :2]
                        first_dir = coords[i, :2] - support_center
                        if float(np.linalg.norm(first_dir)) < 1e-8:
                            direction = np.array([1.0, 0.0], dtype=float)
                        else:
                            direction = np.array([-first_dir[1], first_dir[0]], dtype=float)
                    else:
                        direction = direction / norm

                    first_is_primary = first.get("relationship", {}).get("strength") == "primary"
                    second_is_primary = second.get("relationship", {}).get("strength") == "primary"
                    if first_is_primary:
                        coords[j, :2] += direction * (overlap + 0.01)
                    elif second_is_primary:
                        coords[i, :2] -= direction * (overlap + 0.01)
                    else:
                        shift = direction * ((overlap / 2.0) + 0.01)
                        coords[i, :2] -= shift
                        coords[j, :2] += shift
                    moved = True

            if not moved:
                break
            coords = self._project_inside_room(coords.reshape(-1)).reshape(-1, 4)

        return coords.reshape(-1)

    @staticmethod
    def _spread_slots_1d(values: list[float], sizes: list[float], low: float, high: float) -> list[float]:
        if not values:
            return []
        if low > high:
            center = (low + high) / 2.0
            return [center for _ in values]

        order = sorted(range(len(values)), key=lambda idx: values[idx])
        placed = [0.0 for _ in values]
        for position, idx in enumerate(order):
            value = min(max(values[idx], low), high)
            if position > 0:
                prev_idx = order[position - 1]
                min_gap = (sizes[prev_idx] + sizes[idx]) / 2.0 + 0.06
                value = max(value, placed[prev_idx] + min_gap)
            placed[idx] = value

        overflow = placed[order[-1]] - high
        if overflow > 0.0:
            for idx in order:
                placed[idx] -= overflow

        underflow = low - placed[order[0]]
        if underflow > 0.0:
            for idx in order:
                placed[idx] += underflow

        return placed

    def _arrange_table_chairs(self, x: np.ndarray) -> np.ndarray:
        coords = x.reshape(-1, 4).copy()
        ids = {f["id"]: idx for idx, f in enumerate(self.furnitures)}
        table_groups: dict[int, list[int]] = {}

        for chair_idx, chair in enumerate(self.furnitures):
            if chair["type"] != "chair":
                continue
            support_idx = ids.get(chair.get("pair_with"))
            if support_idx is None or self.furnitures[support_idx]["type"] != "table":
                continue
            table_groups.setdefault(support_idx, []).append(chair_idx)

        for table_idx, chair_indices in table_groups.items():
            table = self.furnitures[table_idx]
            table_front = self._world_front(table, float(coords[table_idx, 3]))
            table_lateral = np.array([-table_front[1], table_front[0]])
            half_width = float(table["extent"][0]) / 2.0
            half_depth = float(table["extent"][1]) / 2.0
            side_groups: dict[tuple[str, float], list[tuple[int, float, float]]] = {}

            for chair_idx in chair_indices:
                chair = self.furnitures[chair_idx]
                local_offset = chair.get("relationship", {}).get("local_offset")
                if local_offset and len(local_offset) >= 2:
                    local = np.array([float(local_offset[0]), float(local_offset[1])], dtype=float)
                else:
                    local = np.array([0.0, -1.0], dtype=float)

                front_score = abs(local[0]) / max(half_depth, 1e-6)
                side_score = abs(local[1]) / max(half_width, 1e-6)
                if front_score >= side_score:
                    side = ("front", 1.0 if local[0] >= 0 else -1.0)
                    axis_value = float(local[1])
                    axis_size = float(chair["extent"][0])
                else:
                    side = ("side", 1.0 if local[1] >= 0 else -1.0)
                    axis_value = float(local[0])
                    axis_size = float(chair["extent"][1])
                side_groups.setdefault(side, []).append((chair_idx, axis_value, axis_size))

            for (side_name, sign), entries in side_groups.items():
                values = [entry[1] for entry in entries]
                sizes = [entry[2] for entry in entries]
                if side_name == "front":
                    low = -half_width + max(sizes) / 2.0
                    high = half_width - max(sizes) / 2.0
                else:
                    low = -half_depth + max(sizes) / 2.0
                    high = half_depth - max(sizes) / 2.0
                slots = self._spread_slots_1d(values, sizes, low, high)

                for (chair_idx, _, _), slot in zip(entries, slots, strict=True):
                    chair = self.furnitures[chair_idx]
                    if side_name == "front":
                        chair_depth = float(chair["extent"][1])
                        local = np.array([sign * (half_depth + chair_depth / 2.0 + 0.04), slot], dtype=float)
                    else:
                        chair_width = float(chair["extent"][0])
                        local = np.array([slot, sign * (half_width + chair_width / 2.0 + 0.04)], dtype=float)

                    coords[chair_idx, :2] = coords[table_idx, :2] + table_front * local[0] + table_lateral * local[1]
                    coords[chair_idx, 3] = self._theta_for_world_front(chair, coords[table_idx, :2] - coords[chair_idx, :2])

        return coords.reshape(-1)

    def _orient_paired_chairs_toward_support(self, x: np.ndarray) -> np.ndarray:
        coords = x.reshape(-1, 4).copy()
        ids = {f["id"]: idx for idx, f in enumerate(self.furnitures)}
        for chair_idx, chair in enumerate(self.furnitures):
            if chair["type"] != "chair":
                continue
            support_idx = ids.get(chair.get("pair_with"))
            if support_idx is None:
                continue
            coords[chair_idx, 3] = self._theta_for_world_front(chair, coords[support_idx, :2] - coords[chair_idx, :2])
        return coords.reshape(-1)

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

            total += self._outside_room_penalty(corners)

            for j in range(i + 1, self.num_f):
                other = self.furnitures[j]
                is_support_pair = (
                    furniture["type"] == "chair"
                    and ids.get(furniture.get("pair_with")) == j
                ) or (
                    other["type"] == "chair"
                    and ids.get(other.get("pair_with")) == i
                )
                if is_support_pair:
                    continue

                penetration = self._sat_penetration(corners, obbs[j])
                if penetration > 0:
                    total += self.weights.body_collision
                    total += ((penetration + 0.02) ** 2) * self.weights.body_collision * 20.0

            if furniture["type"] == "bed":
                total += self._bed_penalty(furniture, coords[i], corners, obbs, i)
            elif furniture["type"] in {"desk", "table"}:
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
            total += self._scan_pose_penalty(furniture, coords[i])

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
        use_global: bool = True,
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

        original_initial = np.array(initial, dtype=float)
        wall_initial = self._wall_anchored_initial(original_initial)
        support_initial = self._chair_support_initial(wall_initial)

        if use_global:
            result_global = differential_evolution(
                self._objective,
                bounds,
                seed=42,
                init="sobol",
                polish=False,
                maxiter=global_maxiter,
                popsize=global_popsize,
            )
            candidates = [original_initial, wall_initial, support_initial]
            if np.isfinite(result_global.fun):
                candidates.append(result_global.x)
            initial_guess = min(candidates, key=self._objective)
        else:
            initial_guess = min((original_initial, wall_initial, support_initial), key=self._objective)

        result_local = minimize(
            self._objective,
            initial_guess,
            method="SLSQP",
            bounds=bounds,
            tol=1e-4,
            options={"maxiter": local_maxiter},
        )

        optimized_x = self._wall_anchored_initial(result_local.x, force=True)
        optimized_x = self._sync_paired_chairs(optimized_x)
        optimized_x = self._project_inside_room(optimized_x)
        for _ in range(4):
            optimized_x = self._resolve_pairwise_overlaps(optimized_x)
            optimized_x = self._project_inside_room(optimized_x)
        optimized_x = self._arrange_table_chairs(optimized_x)
        optimized_x = self._project_inside_room(optimized_x)
        optimized_x = self._resolve_same_support_chair_overlaps(optimized_x)
        optimized_x = self._project_inside_room(optimized_x)
        optimized_x = self._orient_paired_chairs_toward_support(optimized_x)
        optimized = optimized_x.reshape(-1, 4)
        for i, furniture in enumerate(self.furnitures):
            optimized[i, 3] = self._snap_theta(float(optimized[i, 3]))
        for _ in range(6):
            optimized = self._resolve_wall_collisions(optimized)
            optimized = self._resolve_furniture_collisions(optimized)
            optimized = self._resolve_wall_collisions(optimized)
            if self._max_furniture_penetration(optimized) <= 1e-3:
                break

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
            "remaining_collision_penetration_m": round(self._max_furniture_penetration(optimized), 4),
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
