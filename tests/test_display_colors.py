import copy
import unittest
import importlib
import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from server.services.display_colors import ensure_display_colors, backfill_layout_colors
from server.services.transform.unity_roomplan import (
    normalize_roomplan_for_unity, normalize_roomplan_for_ios_view,
    denormalize_roomplan_from_unity,
)
from server.services.transform.roomplan_export import export_optimized_layout_to_roomplan


@contextmanager
def endpoint_module(name):
    # Color/pose tests do not need authentication or a database connection.
    stubs = {}
    deps = ModuleType("server.api.v1.deps")
    deps.get_current_user = lambda: None
    stubs[deps.__name__] = deps
    db = ModuleType("shared.db")
    db.SessionLocal = lambda: None
    stubs[db.__name__] = db
    for model in ("room", "user", "version"):
        module = ModuleType(f"shared.models.{model}")
        setattr(module, model.title(), type(model.title(), (), {}))
        stubs[module.__name__] = module
    with patch.dict(sys.modules, stubs):
        yield importlib.import_module(f"server.api.v1.endpoints.{name}")


def room():
    return {
        "floors": [{"center": [0, 0, 0], "dimensions": [4, 4],
                    "transform": [[1, 0, 0, 0], [0, 0, 1, 0],
                                  [0, 1, 0, 0], [0, 0, 0, 1]]}],
        "objects": [{"identifier": "bed-1", "category": "bed",
                     "center": [0, 0.5, 0], "dimensions": [1, 1, 2]}],
    }


class DisplayColorTests(unittest.TestCase):
    def test_palette(self):
        expected = {"bed": "#F2CCD4", "sofa": "#B8D4E6", "chair": "#B8D4E6",
                    "table": "#E0D1B8", "storage": "#C7D1C2", "television": "#8C8C94",
                    "refrigerator": "#E0E6EB", "washerdryer": "#D1E0EB", "unknown": "#CCC7BF"}
        for category, color in expected.items():
            with self.subTest(category=category):
                payload = {"objects": [{"category": category}]}
                self.assertTrue(ensure_display_colors(payload))
                self.assertEqual(payload["objects"][0]["display_color_hex"], color)
                self.assertFalse(ensure_display_colors(payload))

    def test_invalid_and_custom_colors(self):
        payload = {"objects": [{"category": {"bed": {}}, "display_color_hex": "bad"},
                               {"category": "bed", "display_color_hex": "#aabbcc"}]}
        ensure_display_colors(payload)
        self.assertEqual([o["display_color_hex"] for o in payload["objects"]],
                         ["#F2CCD4", "#AABBCC"])

    def test_optimized_and_unity_round_trip(self):
        for custom in (None, "#123456"):
            original = room()
            if custom:
                original["objects"][0]["display_color_hex"] = custom
            optimized = export_optimized_layout_to_roomplan(original, {"movable_items": [
                {"id": "bed-1", "optimized_pos": [1, 1, 1], "optimized_rotation_y_deg": 90}]})
            for payload in (original, optimized):
                unity = normalize_roomplan_for_unity(payload)
                restored = normalize_roomplan_for_ios_view(denormalize_roomplan_from_unity(unity))
                self.assertEqual(restored["objects"][0]["display_color_hex"], custom or "#F2CCD4")
                self.assertEqual(unity["objects"][0]["display_color_hex"], custom or "#F2CCD4")


class BackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_download_without_unity(self):
        with patch("server.core.s3.get_json", AsyncMock(return_value=room())), patch(
            "server.core.s3.upload_json", AsyncMock()
        ) as upload:
            await backfill_layout_colors("ios", None)
            self.assertEqual(upload.await_args.args[1]["objects"][0]["display_color_hex"], "#F2CCD4")

    async def test_actual_download_payloads_are_persisted_and_idempotent(self):
        stored = {"ios": room(), "unity": room()}
        stored["ios"]["objects"][0]["display_color_hex"] = "#123456"
        stored["unity"]["objects"][0]["display_color_hex"] = "#FFFFFF"

        async def read(key):
            return copy.deepcopy(stored[key])

        async def write(key, data):
            stored[key] = data

        with patch("server.core.s3.get_json", side_effect=read), patch(
            "server.core.s3.upload_json", side_effect=write
        ) as upload:
            await backfill_layout_colors("ios", "unity")
            self.assertEqual(stored["unity"]["objects"][0]["display_color_hex"], "#123456")
            upload.reset_mock()
            await backfill_layout_colors("ios", "unity")
            upload.assert_not_called()

    async def test_scan_saves_original_and_unity(self):
        with endpoint_module("scan") as module, patch.object(
            module, "get_json", AsyncMock(return_value=room())
        ), patch.object(module, "upload_json", AsyncMock()) as upload:
            await module._create_origin_unity_json("origin")
            self.assertEqual(upload.await_count, 2)
            for call in upload.await_args_list:
                self.assertEqual(call.args[1]["objects"][0]["display_color_hex"], "#F2CCD4")

    async def test_user_edits_from_original_and_optimized(self):
        with endpoint_module("rooms_edit") as module:
            await self._check_edits(module)

    async def _check_edits(self, module):
        for optimized in (False, True):
            source = room()
            if optimized:
                source = export_optimized_layout_to_roomplan(source, {"movable_items": []})
            unity = normalize_roomplan_for_unity(source)
            update = [{"identifier": "bed-1", "center": [1, 0.5, 1]}]
            for ios_updates, unity_updates in ((update, None), (None, update), ([], None)):
                with patch.object(module, "_load_json_from_s3_uri",
                           AsyncMock(side_effect=[copy.deepcopy(source), copy.deepcopy(unity)])):
                    parent = SimpleNamespace(s3_json_url="s3://bucket/room_data.json", json_data={})
                    ios, vr = await module.build_edited_layouts(parent, ios_updates, unity_updates)
                    self.assertEqual(ios["objects"][0]["display_color_hex"], "#F2CCD4")
                    self.assertEqual(vr["objects"][0]["display_color_hex"], "#F2CCD4")
