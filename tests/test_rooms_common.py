from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from server.api.v1.endpoints.rooms_common import (
    _attach_model_asset_urls,
    _get_catalog_model_asset_urls,
)


class CatalogModelAssetUrlsTest(IsolatedAsyncioTestCase):
    async def test_returns_glb_and_only_existing_usdz_urls(self) -> None:
        room_data = {
            "objects": [
                {
                    "category": "chair",
                    "modelFileName": "DefaultChair.rooms.usdc",
                },
                {
                    "category": "table",
                    "modelFileName": "DefaultTable.rooms.usdc",
                },
            ]
        }

        with (
            patch(
                "server.api.v1.endpoints.rooms_common.get_json",
                new=AsyncMock(return_value=room_data),
            ),
            patch(
                "server.api.v1.endpoints.rooms_common.head_object",
                new=AsyncMock(side_effect=[{"ContentLength": 1}, None]),
            ) as head_mock,
            patch(
                "server.api.v1.endpoints.rooms_common.generate_presigned_url",
                side_effect=lambda key: f"signed:{key}",
            ),
        ):
            model_urls, model_usdz_urls = await _get_catalog_model_asset_urls(
                "user01/scans/11/origin"
            )

        self.assertEqual(
            model_urls,
            {
                "Chair/Default/DefaultChair.rooms.usdc": (
                    "signed:asset/Chair/Default/DefaultChair.rooms.glb"
                ),
                "Table/Default/DefaultTable.rooms.usdc": (
                    "signed:asset/Table/Default/DefaultTable.rooms.glb"
                ),
            },
        )
        self.assertEqual(
            model_usdz_urls,
            {
                "Chair/Default/DefaultChair.rooms.usdc": (
                    "signed:asset/Chair/Default/DefaultChair.rooms.usdz"
                )
            },
        )
        self.assertEqual(head_mock.await_count, 2)

    def test_attaches_urls_to_legacy_filename_model_key(self) -> None:
        objects = [
            {
                "identifier": "chair-1",
                "model_key": "DefaultChair.rooms.usdc",
                "model_id": 1,
                "center": [1.0, 0.5, 2.0],
                "rotation": [0.0, 1.570796, 0.0],
                "dimensions": [0.5, 1.0, 0.5],
            }
        ]
        model_urls = {
            "Chair/Default/DefaultChair.rooms.usdc": "signed:chair.glb",
        }
        model_usdz_urls = {
            "Chair/Default/DefaultChair.rooms.usdc": "signed:chair.usdz",
        }

        result = _attach_model_asset_urls(objects, model_urls, model_usdz_urls)

        self.assertEqual(result[0]["glb_url"], "signed:chair.glb")
        self.assertEqual(result[0]["usdz_url"], "signed:chair.usdz")
        self.assertEqual(result[0]["center"], [1.0, 0.5, 2.0])
        self.assertEqual(result[0]["rotation"], [0.0, 1.570796, 0.0])
        self.assertEqual(result[0]["dimensions"], [0.5, 1.0, 0.5])
