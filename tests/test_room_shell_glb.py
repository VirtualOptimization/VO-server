import unittest

import numpy as np

from workers.converter.build_room_shell_glb import build_room_shell_mesh

WALL_HEIGHT = 2.4
THICKNESS = 0.08


def _wall(start, end, normal_sign=1.0):
    start = np.array([start[0], 0.0, start[1]])
    end = np.array([end[0], 0.0, end[1]])
    length = float(np.linalg.norm(end - start))
    axis_x = (end - start) / length
    axis_z = np.cross(axis_x, [0.0, 1.0, 0.0]) * normal_sign
    center = (start + end) / 2.0 + [0.0, WALL_HEIGHT / 2.0, 0.0]
    return {
        "center": center.tolist(),
        "dimensions": [length, WALL_HEIGHT, 0.0],
        "transform": [
            [*axis_x.tolist(), 0.0],
            [0.0, 1.0, 0.0, 0.0],
            [*axis_z.tolist(), 0.0],
            [*center.tolist(), 1.0],
        ],
    }


def _room(corners, normal_signs=None, doors=()):
    count = len(corners)
    normal_signs = normal_signs or [1.0] * count
    xs = [c[0] for c in corners]
    zs = [c[1] for c in corners]
    width, depth = max(xs) - min(xs), max(zs) - min(zs)
    return {
        "floors": [
            {
                "center": [width / 2.0, 0.0, depth / 2.0],
                "dimensions": [width, depth],
                "transform": [
                    [1.0, 0.0, 0.0, 0.0],
                    [0.0, 0.0, 1.0, 0.0],
                    [0.0, 1.0, 0.0, 0.0],
                    [width / 2.0, 0.0, depth / 2.0, 1.0],
                ],
            }
        ],
        "walls": [
            _wall(corners[i], corners[(i + 1) % count], normal_signs[i]) for i in range(count)
        ],
        "doors": list(doors),
        "windows": [],
    }


def _inside_polygon(x, z, corners, margin=1e-4):
    inside = False
    count = len(corners)
    for i in range(count):
        (x0, z0), (x1, z1) = corners[i], corners[(i + 1) % count]
        if (z0 > z) != (z1 > z):
            if x < x0 + (z - z0) * (x1 - x0) / (z1 - z0):
                inside = not inside
    if not inside:
        return False
    for i in range(count):
        a = np.array(corners[i], dtype=float)
        b = np.array(corners[(i + 1) % count], dtype=float)
        t = np.clip(np.dot([x, z] - a, b - a) / np.dot(b - a, b - a), 0.0, 1.0)
        if np.linalg.norm([x, z] - (a + t * (b - a))) < margin:
            return False
    return True


def _wall_vertices(mesh):
    vertices, _ = mesh
    return vertices[vertices[:, 1] > 1e-4]


class RoomShellWallThicknessTests(unittest.TestCase):
    RECT = [(0.0, 0.0), (4.0, 0.0), (4.0, 3.0), (0.0, 3.0)]
    L_SHAPE = [(0.0, 0.0), (4.0, 0.0), (4.0, 2.0), (2.0, 2.0), (2.0, 3.0), (0.0, 3.0)]

    def _assert_no_wall_inside(self, corners, mesh):
        for x, _, z in _wall_vertices(mesh):
            self.assertFalse(_inside_polygon(float(x), float(z), corners), f"wall vertex inside room: {x}, {z}")

    def test_wall_inner_face_stays_on_roomplan_plane(self):
        for signs in ([1.0] * 4, [-1.0] * 4, [1.0, -1.0, 1.0, -1.0]):
            with self.subTest(normal_signs=signs):
                mesh = build_room_shell_mesh(_room(self.RECT, signs), wall_thickness=THICKNESS)
                self._assert_no_wall_inside(self.RECT, mesh)
                walls = _wall_vertices(mesh)
                # Inner faces sit exactly on the RoomPlan planes.
                for value in (0.0, 4.0):
                    self.assertTrue(np.any(np.isclose(walls[:, 0], value, atol=1e-5)))
                # Thickness goes outward only, and outer corners are filled.
                np.testing.assert_allclose(walls[:, 0].min(), -THICKNESS, atol=1e-5)
                np.testing.assert_allclose(walls[:, 0].max(), 4.0 + THICKNESS, atol=1e-5)
                np.testing.assert_allclose(walls[:, 2].min(), -THICKNESS, atol=1e-5)
                np.testing.assert_allclose(walls[:, 2].max(), 3.0 + THICKNESS, atol=1e-5)

    def test_l_shaped_room_concave_corner_does_not_intrude(self):
        signs = [1.0, -1.0, 1.0, -1.0, 1.0, -1.0]
        mesh = build_room_shell_mesh(_room(self.L_SHAPE, signs), wall_thickness=THICKNESS)
        self._assert_no_wall_inside(self.L_SHAPE, mesh)

    def test_wall_with_door_opening_stays_outside(self):
        door_center = [2.0, 1.0, 0.0]
        door = {
            "center": door_center,
            "dimensions": [0.9, 2.0, 0.0],
            "transform": [
                [1.0, 0.0, 0.0, 0.0],
                [0.0, 1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0, 0.0],
                [*door_center, 1.0],
            ],
        }
        mesh = build_room_shell_mesh(_room(self.RECT, doors=[door]), wall_thickness=THICKNESS)
        self._assert_no_wall_inside(self.RECT, mesh)
        walls = _wall_vertices(mesh)
        # Door gap is still open on the south wall.
        south = walls[walls[:, 2] < 0.0]
        in_gap = (south[:, 0] > 1.56) & (south[:, 0] < 2.44) & (south[:, 1] < 1.99)
        self.assertFalse(np.any(in_gap))


if __name__ == "__main__":
    unittest.main()
