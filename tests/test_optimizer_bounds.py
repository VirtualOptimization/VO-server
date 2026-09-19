import unittest

from workers.optimizer.bounds import center_axis_bounds


class OptimizerBoundsTests(unittest.TestCase):
    def test_center_axis_bounds_keep_normal_item_inside_room(self):
        self.assertEqual(
            center_axis_bounds(4.0, 2.0),
            (1.0, 3.0),
        )

    def test_center_axis_bounds_collapse_oversized_item_to_room_center(self):
        self.assertEqual(
            center_axis_bounds(2.0, 3.0),
            (1.0, 1.0),
        )


if __name__ == "__main__":
    unittest.main()
