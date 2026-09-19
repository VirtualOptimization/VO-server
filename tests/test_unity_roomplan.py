import unittest

from server.services.transform.unity_roomplan import (
    denormalize_roomplan_from_unity,
    normalize_roomplan_for_unity,
)


class UnityRoomPlanNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "floors": [
                {
                    "center": [0.010587387, -1.0054976, -1.461147],
                    "dimensions": [2.9013572, 4.7742095],
                    "transform": [
                        [-0.41679728, 0.0, -0.9089995, 0.0],
                        [-0.90899956, 0.0, 0.41679725, 0.0],
                        [0.0, 0.99999994, 0.0, 0.0],
                        [0.010587387, -1.0054976, -1.461147, 1.0],
                    ],
                }
            ],
            "walls": [],
            "doors": [],
            "windows": [],
            "objects": [
                {
                    "identifier": "chair-1",
                    "center": [-0.20389529, -0.6426087, -2.2487192],
                    "dimensions": [0.5651855, 0.7257776, 0.69140625],
                    "transform": [
                        [0.93349504, 0.0, 0.35859087, 0.0],
                        [0.0, 1.0, 0.0, 0.0],
                        [-0.3585909, 0.0, 0.93349504, 0.0],
                        [-0.20389529, -0.6426087, -2.2487192, 1.0],
                    ],
                }
            ],
        }

    def test_floor_uses_both_roomplan_surface_axes(self) -> None:
        normalized = normalize_roomplan_for_unity(self.payload)
        floor = normalized["floors"][0]

        self.assertAlmostEqual(floor["center"][0], 2.9013572 / 2.0, places=5)
        self.assertAlmostEqual(floor["center"][1], 0.0, places=5)
        self.assertAlmostEqual(floor["center"][2], 4.7742095 / 2.0, places=5)

    def test_unity_coordinates_round_trip_to_roomplan(self) -> None:
        normalized = normalize_roomplan_for_unity(self.payload)
        restored = denormalize_roomplan_from_unity(normalized)

        for collection in ("floors", "objects"):
            for actual, expected in zip(restored[collection], self.payload[collection]):
                for actual_value, expected_value in zip(actual["center"], expected["center"]):
                    self.assertAlmostEqual(actual_value, expected_value, places=5)


if __name__ == "__main__":
    unittest.main()
