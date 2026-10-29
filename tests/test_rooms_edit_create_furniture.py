import copy
import sys
from types import ModuleType, SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

# Same lightweight stubs as test_rooms_edit_version_name.py: these tests only
# exercise layout editing, not auth or the database.
deps_stub = ModuleType("server.api.v1.deps")
deps_stub.get_current_user = lambda: None
db_stub = ModuleType("shared.db")
db_stub.SessionLocal = lambda: None
stubs = {"server.api.v1.deps": deps_stub, "shared.db": db_stub}
for name, cls in (("room", "Room"), ("user", "User"), ("version", "Version")):
    module = ModuleType(f"shared.models.{name}")
    setattr(module, cls, type(cls, (), {}))
    stubs[f"shared.models.{name}"] = module

with patch.dict(sys.modules, stubs):
    from server.api.v1.endpoints import rooms_edit


# Floor and one chair in the RoomPlan frame (from test_unity_roomplan.py).
ROOMPLAN = {
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
            "category": "chair",
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

NEW_SHELF = {
    "identifier": "lidar_ABC",
    "operation": "create",
    "category": "custom",
    "display_name": "  내 책장  ",
    "center": [0.4, -0.4, -1.2],
    "dimensions": [0.8, 1.2, 0.4],
    "rotation": [0.0, 0.0, 0.0],
    "color_hex": "#b8875a",
}


def _by_id(layout, identifier):
    return next(o for o in layout["objects"] if o.get("identifier") == identifier)


class PatchObjectsCreateTest(TestCase):
    def _patch(self, updates):
        return rooms_edit._patch_objects(copy.deepcopy(ROOMPLAN), updates, "iOS", "roomplan")

    def test_create_adds_a_complete_object(self):
        layout = self._patch([NEW_SHELF])
        shelf = _by_id(layout, "lidar_ABC")
        self.assertEqual(len(layout["objects"]), 2)
        self.assertEqual(shelf["display_name"], "내 책장")
        self.assertEqual(shelf["category"], "custom")
        self.assertEqual(shelf["display_color_hex"], "#B8875A")
        self.assertEqual(shelf["center"], [0.4, -0.4, -1.2])
        self.assertEqual(shelf["transform"][3][0:3], [0.4, -0.4, -1.2])
        self.assertEqual(len(shelf["obbVertices"]), 8)
        self.assertNotIn("operation", shelf)
        # Height of the box is the requested 1.2 m.
        ys = [v[1] for v in shelf["obbVertices"]]
        self.assertAlmostEqual(max(ys) - min(ys), 1.2, places=5)

    def test_existing_moves_still_work_and_keep_their_shape(self):
        layout = self._patch([{"identifier": "chair-1", "center": [0.0, 0.0, -2.0]}])
        chair = _by_id(layout, "chair-1")
        self.assertEqual(chair["center"][0], 0.0)
        self.assertNotIn("display_name", chair)

    def test_display_name_can_be_renamed_on_an_existing_object(self):
        layout = self._patch([{"identifier": "chair-1", "display_name": " 의자 "}])
        self.assertEqual(_by_id(layout, "chair-1")["display_name"], "의자")

    def test_invalid_create_requests_are_400(self):
        cases = [
            {**NEW_SHELF, "identifier": "chair-1"},  # already exists
            {**NEW_SHELF, "display_name": "   "},
            {**NEW_SHELF, "display_name": "가" * 31},
            {**NEW_SHELF, "dimensions": [0.8, 0.0, 0.4]},
            {**NEW_SHELF, "center": [1.0, 2.0]},
            {**NEW_SHELF, "operation": "delete"},
        ]
        for update in cases:
            with self.subTest(update=update), self.assertRaises(HTTPException) as caught:
                self._patch([update])
            self.assertEqual(caught.exception.status_code, 400)

    def test_thirty_character_name_is_allowed(self):
        layout = self._patch([{**NEW_SHELF, "display_name": "가" * 30}])
        self.assertEqual(_by_id(layout, "lidar_ABC")["display_name"], "가" * 30)


class BuildEditedLayoutsCreateTest(IsolatedAsyncioTestCase):
    async def test_name_survives_ios_create_then_unity_move(self):
        parent = SimpleNamespace(s3_json_url="s3://bucket/u/scans/1/origin/room_data.json", json_data={})
        with patch.object(rooms_edit, "_load_json_from_s3_uri", AsyncMock(return_value=copy.deepcopy(ROOMPLAN))):
            ios_layout, unity_layout = await rooms_edit.build_edited_layouts(parent, [NEW_SHELF], [])

        self.assertEqual(_by_id(ios_layout, "lidar_ABC")["display_name"], "내 책장")
        unity_shelf = _by_id(unity_layout, "lidar_ABC")
        self.assertEqual(unity_shelf["display_name"], "내 책장")
        self.assertEqual(unity_shelf["display_color_hex"], "#B8875A")

        # A later Unity edit starts from the saved layouts: move the shelf there.
        child = SimpleNamespace(
            s3_json_url="s3://bucket/u/scans/1/user_edits/v2/room_data.json",
            json_data={"unity_layout_json": "s3://bucket/u/scans/1/user_edits/v2/room_data.unity.json"},
        )

        async def load(uri, label):
            return copy.deepcopy(unity_layout if uri.endswith(".unity.json") else ios_layout)

        moved_center = [unity_shelf["center"][0] + 0.3, unity_shelf["center"][1], unity_shelf["center"][2]]
        with patch.object(rooms_edit, "_load_json_from_s3_uri", AsyncMock(side_effect=load)):
            ios_again, unity_again = await rooms_edit.build_edited_layouts(
                child, None, [{"identifier": "lidar_ABC", "center": moved_center}]
            )

        self.assertEqual(_by_id(unity_again, "lidar_ABC")["display_name"], "내 책장")
        self.assertEqual(_by_id(ios_again, "lidar_ABC")["display_name"], "내 책장")
        self.assertEqual(_by_id(ios_again, "lidar_ABC")["display_color_hex"], "#B8875A")
