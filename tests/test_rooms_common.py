from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from server.api.v1.endpoints.rooms_common import (
    _attach_model_asset_urls,
    _catalog_db_model_key,
    _catalog_model_key,
    _get_catalog_model_asset_urls,
)
from server.services.transform.mappers import infer_catalog_model_key

# RoomPlanCatalog.bundle/Resources layout, mirrored 1:1 under s3://<bucket>/asset/ as .glb.
CATALOG_BUNDLE_PATHS = [
    "Chair/Default/DefaultChair.rooms.usdc",
    "Chair/DiningChair_wBack_fourLegs_noArms/DiningChair_wBack_fourLegs_noArms.rooms.usdc",
    "Chair/DiningChair_wBack_fourLegs_wArms/DiningChair_wBack_fourLegs_wArms.rooms.usdc",
    "Chair/Stool_noBack_fourLegs_noArms/Stool_noBack_fourLegs_noArms.rooms.usdc",
    "Chair/Stool_noBack_fourLegs_wArms/Stool_noBack_fourLegs_wArms.rooms.usdc",
    "Chair/Stool_noBack_starLegs_noArms/Stool_noBack_starLegs_noArms.rooms.usdc",
    "Chair/Stool_noBack_starLegs_wArms/Stool_noBack_starLegs_wArms.rooms.usdc",
    "Chair/Stool_noBack_unidentifiedLegs_noArms/Stool_noBack_unidentifiedLegs_noArms.rooms.usdc",
    "Chair/Stool_noBack_unidentifiedLegs_wArms/Stool_noBack_unidentifiedLegs_wArms.rooms.usdc",
    "Chair/Swivel_wBack_starLegs_noArms/Swivel_wBack_starLegs_noArms.rooms.usdc",
    "Chair/Swivel_wBack_starLegs_wArms/Swivel_wBack_starLegs_wArms.rooms.usdc",
    "Chair/Unidentified_noBack_fourLegs_noArms/Stool_noBack_fourLegs_noArms.rooms.usdc",
    "Chair/Unidentified_noBack_fourLegs_wArms/Stool_noBack_fourLegs_wArms.rooms.usdc",
    "Chair/Unidentified_noBack_starLegs_noArms/Stool_noBack_starLegs_noArms.rooms.usdc",
    "Chair/Unidentified_noBack_starLegs_wArms/Stool_noBack_starLegs_wArms.rooms.usdc",
    "Chair/Unidentified_noBack_unidentifiedLegs_noArms/Stool_noBack_unidentifiedLegs_noArms.rooms.usdc",
    "Chair/Unidentified_noBack_unidentifiedLegs_wArms/Stool_noBack_unidentifiedLegs_wArms.rooms.usdc",
    "Chair/Unidentified_wBack_fourLegs_noArms/Unidentified_wBack_fourLegs_noArms.rooms.usdc",
    "Chair/Unidentified_wBack_fourLegs_wArms/Unidentified_wBack_fourLegs_wArms.rooms.usdc",
    "Chair/Unidentified_wBack_starLegs_noArms/Swivel_wBack_starLegs_noArms.rooms.usdc",
    "Chair/Unidentified_wBack_starLegs_wArms/Swivel_wBack_starLegs_wArms.rooms.usdc",
    "Chair/Unidentified_wBack_unidentifiedLegs_noArms/Unidentified_wBack_otherLegType_noArms.rooms.usdc",
    "Chair/Unidentified_wBack_unidentifiedLegs_wArms/Unidentified_wBack_otherLegType_wArms.rooms.usdc",
    "Sofa/Default/Rectangular.rooms.usdc",
    "Sofa/LShaped/LShaped.rooms.usdc",
    "Sofa/LShapedExtension/LShapedExtension.rooms.usdc",
    "Sofa/Rectangular/Rectangular.rooms.usdc",
    "Sofa/SingleSeat/SingleSeat.rooms.usdc",
    "Sofa/Unidentified/Rectangular.rooms.usdc",
    "Storage/Shelf/shelf_vertical.rooms.usdc",
    "Table/CoffeeTable_circularElliptic/CoffeeTable_circularElliptic.rooms.usdc",
    "Table/CoffeeTable_lShaped/CoffeeTable_lshaped.rooms.usdc",
    "Table/CoffeeTable_rectangular/CoffeeTable_rectangular.rooms.usdc",
    "Table/CoffeeTable_unidentifiedShape/CoffeeTable_rectangular.rooms.usdc",
    "Table/Default/DefaultTable.rooms.usdc",
    "Table/DiningTable_circularElliptic/DiningTable_circularElliptic.rooms.usdc",
    "Table/DiningTable_rectangular/DiningTable_rectangular.rooms.usdc",
    "Table/DiningTable_unidentifiedShape/DiningTable_rectangular.rooms.usdc",
    "Table/Unidentified_circularElliptic/Unidentified_circularElliptic.rooms.usdc",
    "Table/Unidentified_lShaped/Unidentified_lShaped.rooms.usdc",
    "Table/Unidentified_rectangular/Unidentified_rectangular.rooms.usdc",
    "Table/Unidentified_unidentifiedShape/Unidentified_rectangular.rooms.usdc",
]


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

    def test_l_shaped_table_keys_match_s3_catalog_casing(self) -> None:
        # S3: asset/Table/Unidentified_lShaped/Unidentified_lShaped.rooms.glb
        self.assertEqual(
            _catalog_model_key("table", "Unidentified_lShaped.rooms.usdc"),
            "asset/Table/Unidentified_lShaped/Unidentified_lShaped.rooms.glb",
        )
        expected_key = "Table/Unidentified_lShaped/Unidentified_lShaped.rooms.usdc"
        self.assertEqual(_catalog_db_model_key("table", "Unidentified_lShaped.rooms.usdc"), expected_key)
        self.assertEqual(infer_catalog_model_key("table", "Unidentified_lShaped.rooms.usdc"), expected_key)

    def test_attaches_urls_to_saved_lowercase_l_shaped_model_key(self) -> None:
        objects = [
            {
                "identifier": "desk-1",
                "model_key": "Table/Unidentified_lShaped/Unidentified_lshaped.rooms.usdc",
            }
        ]
        model_urls = {
            "Table/Unidentified_lShaped/Unidentified_lShaped.rooms.usdc": "signed:desk.glb",
        }

        result = _attach_model_asset_urls(objects, model_urls, {})

        self.assertEqual(result[0]["glb_url"], "signed:desk.glb")

    def test_every_catalog_file_resolves_to_an_uploaded_asset(self) -> None:
        # iOS sends only the file name (lastPathComponent) and the RoomPlan category.
        s3_keys = {f"asset/{path.removesuffix('.usdc')}.glb" for path in CATALOG_BUNDLE_PATHS}
        db_keys = set(CATALOG_BUNDLE_PATHS)
        for path in CATALOG_BUNDLE_PATHS:
            category, filename = path.split("/")[0].lower(), path.split("/")[-1]
            with self.subTest(path=path):
                self.assertIn(_catalog_model_key(category, filename), s3_keys)
                db_key = _catalog_db_model_key(category, filename)
                self.assertIn(db_key, db_keys)
                self.assertEqual(infer_catalog_model_key(category, filename), db_key)
