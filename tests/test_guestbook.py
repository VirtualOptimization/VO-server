import base64
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from server.api.v1.endpoints import guestbook
from server.schemas.guestbook import GuestbookCreateRequest
from server.services.guestbook_rules import InvalidInput, decode_photo, normalize_text
from shared.models.guestbook import GuestbookEntry

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
PNG_DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


class GuestbookRulesTest(unittest.TestCase):
    def test_text_is_trimmed_and_length_checked(self):
        self.assertEqual(normalize_text("  안녕  ", field="닉네임", max_length=20), "안녕")
        self.assertEqual(normalize_text("가" * 20, field="닉네임", max_length=20), "가" * 20)
        for value in (None, "", "   ", "가" * 21):
            with self.subTest(value=value), self.assertRaises(InvalidInput):
                normalize_text(value, field="닉네임", max_length=20)

    def test_photo_is_optional(self):
        self.assertIsNone(decode_photo(None))
        self.assertIsNone(decode_photo(""))

    def test_photo_data_url_is_decoded(self):
        data, content_type, extension = decode_photo(PNG_DATA_URL)
        self.assertEqual((data, content_type, extension), (PNG, "image/png", "png"))

    def test_bad_photos_are_rejected(self):
        not_png = "data:image/png;base64," + base64.b64encode(b"hello world").decode()
        for photo in (
            "https://example.com/a.png",
            "data:text/plain;base64,aGVsbG8=",
            "data:image/png;base64,@@@",
            not_png,
        ):
            with self.subTest(photo=photo[:30]), self.assertRaises(InvalidInput):
                decode_photo(photo)


class GuestbookApiTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        GuestbookEntry.__table__.create(engine)
        self.upload = AsyncMock(return_value="s3://bucket/key")
        self.patches = [
            patch.object(guestbook, "SessionLocal", sessionmaker(bind=engine)),
            patch.object(guestbook, "upload_bytes", self.upload),
            patch.object(guestbook, "generate_presigned_url", side_effect=lambda key, expires: f"https://signed/{key}"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    async def _post(self, **overrides):
        body = {
            "nickname": "닉네임",
            "message": "방명록 내용",
            "photo": None,
            "consent": True,
            "created_at": "2000-01-01T00:00:00.000Z",
        }
        body.update(overrides)
        entry = await guestbook.create_guest(GuestbookCreateRequest.model_validate(body))
        return entry.model_dump(mode="json")

    async def _list(self):
        return (await guestbook.list_guests(limit=100)).model_dump(mode="json")["entries"]

    async def test_create_records_server_time_and_lists_newest_first(self):
        created = await self._post(nickname="첫번째")
        self.assertEqual(created["nickname"], "첫번째")
        self.assertIsNone(created["photo"])
        created_at = datetime.fromisoformat(created["created_at"].replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        self.assertLess(abs(datetime.now(timezone.utc) - created_at), timedelta(minutes=1))

        await self._post(nickname="두번째", photo=PNG_DATA_URL)
        entries = await self._list()
        self.assertEqual([e["nickname"] for e in entries], ["두번째", "첫번째"])
        self.assertEqual(set(entries[0]), {"id", "nickname", "message", "mood", "photo", "created_at"})
        self.assertEqual(entries[0]["photo"], f"https://signed/guestbook/{entries[0]['id']}.png")
        self.upload.assert_awaited_once()

    async def test_mood_is_optional_and_stored(self):
        self.assertIsNone((await self._post())["mood"])
        self.assertEqual((await self._post(mood="  반가워요 "))["mood"], "반가워요")
        with self.assertRaises(HTTPException) as caught:
            await self._post(mood="가" * 21)
        self.assertEqual(caught.exception.status_code, 400)

    async def test_invalid_input_returns_400_with_message(self):
        for overrides in (
            {"consent": False},
            {"consent": None},
            {"nickname": "가" * 21},
            {"message": ""},
            {"message": "가" * 201},
            {"photo": "data:text/plain;base64,aGVsbG8="},
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaises(HTTPException) as caught:
                    await self._post(**overrides)
                self.assertEqual(caught.exception.status_code, 400)
                self.assertIsInstance(caught.exception.detail, str)
        self.assertEqual(await self._list(), [])
        self.upload.assert_not_awaited()


class GuestbookCardTest(unittest.IsolatedAsyncioTestCase):
    def _payload(self, **overrides):
        from server.schemas.guestbook import GuestbookCardRequest

        values = {"to": "friend@example.com", "nickname": "연짱", "message": "수고했어", "mood": "반가워요"}
        values.update(overrides)
        return GuestbookCardRequest(**values)

    async def test_card_needs_the_configured_key(self):
        with patch.object(guestbook.settings, "guestbook_api_key", ""):
            with self.assertRaises(HTTPException) as ctx:
                await guestbook.send_card(self._payload(), x_guestbook_key="anything")
            self.assertEqual(ctx.exception.status_code, 503)
        with patch.object(guestbook.settings, "guestbook_api_key", "secret"):
            for key in (None, "wrong"):
                with self.subTest(key=key), self.assertRaises(HTTPException) as ctx:
                    await guestbook.send_card(self._payload(), x_guestbook_key=key)
                self.assertEqual(ctx.exception.status_code, 401)

    async def test_card_is_sent_with_cleaned_fields(self):
        files = [object()]
        with patch.object(guestbook.settings, "guestbook_api_key", "secret"), patch.object(
            guestbook, "send_guestbook_card"
        ) as send, patch.object(guestbook, "_card_files", AsyncMock(return_value=files)) as card_files:
            result = await guestbook.send_card(
                self._payload(to="  friend@example.com ", photo_url="https://v-o.cloud/p.jpg"), x_guestbook_key="secret"
            )
        self.assertTrue(result.sent)
        card_files.assert_awaited_once_with("연짱", "수고했어", "반가워요", "https://v-o.cloud/p.jpg", None)
        send.assert_called_once_with("friend@example.com", "연짱", "수고했어", "반가워요", "https://v-o.cloud/p.jpg", files)

    async def test_bad_card_input_is_rejected(self):
        with patch.object(guestbook.settings, "guestbook_api_key", "secret"), patch.object(guestbook, "send_guestbook_card") as send:
            for overrides in (
                {"to": "not-an-email"},
                {"photo_url": "http://v-o.cloud/p.jpg"},
                {"message": ""},
                {"card_image": "data:image/png;base64,bm90LWEtcG5n"},
            ):
                with self.subTest(overrides=overrides), self.assertRaises(HTTPException) as ctx:
                    await guestbook.send_card(self._payload(**overrides), x_guestbook_key="secret")
                self.assertEqual(ctx.exception.status_code, 400)
        send.assert_not_called()

    def test_card_email_escapes_visitor_text(self):
        from server.services import email_service

        with patch.object(email_service, "is_smtp_configured", return_value=True), patch.object(email_service, "_send") as smtp:
            email_service.send_guestbook_card("a@example.com", "<b>해커</b>\n", "<script>x</script>", None, None)
        message = smtp.call_args.args[0]
        html = message.get_body(("html",)).get_content()
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("\n", message["Subject"])


class GuestbookCardFilesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.upload = AsyncMock(return_value="s3://bucket/key")
        self.patches = [
            patch.object(guestbook, "upload_bytes", self.upload),
            patch.object(guestbook, "render_card", return_value=b"card-png"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    async def test_card_and_photo_get_save_links(self):
        photo = (b"\xff\xd8\xff-jpeg", "image/jpeg", "jpg")
        with patch.object(guestbook.settings, "public_base_url", "https://api.v-o.test/"), patch.object(
            guestbook, "_fetch_photo", AsyncMock(return_value=photo)
        ):
            files = await guestbook._card_files("연짱", "수고했어", "반가워요", "https://v-o.cloud/p.jpg")
        self.assertEqual([f.label for f in files], ["카드 저장하기", "사진 저장하기"])
        self.assertEqual([f.filename for f in files], ["V-O 방명록 카드.png", "V-O 방명록 사진.jpg"])
        self.assertEqual(files[1].data, photo[0])
        for file in files:
            self.assertRegex(file.link, r"^https://api\.v-o\.test/api/guests/cards/[0-9a-f]{32}/(card\.png|photo\.jpg)$")
        self.assertEqual(self.upload.await_count, 2)

    async def test_website_capture_is_used_as_the_card(self):
        capture = (PNG, "image/png", "png")
        with patch.object(guestbook.settings, "public_base_url", "https://api.v-o.test"), patch.object(
            guestbook, "_fetch_photo", AsyncMock(return_value=None)
        ), patch.object(guestbook, "render_card") as render:
            files = await guestbook._card_files("연짱", "수고했어", None, "https://v-o.cloud/p.jpg", capture)
        render.assert_not_called()
        # The photo could not be fetched, but the website's capture still has it.
        self.assertEqual([(f.label, f.data) for f in files], [("카드 저장하기", PNG)])
        self.assertTrue(files[0].link.endswith("/card.png"))

    async def test_card_without_photo_has_only_the_card(self):
        with patch.object(guestbook.settings, "public_base_url", "https://api.v-o.test"):
            files = await guestbook._card_files("연짱", "수고했어", None, None)
        self.assertEqual([f.label for f in files], ["카드 저장하기"])

    async def test_without_public_url_files_are_only_attached(self):
        with patch.object(guestbook.settings, "public_base_url", ""):
            files = await guestbook._card_files("연짱", "수고했어", None, None)
        self.assertIsNone(files[0].link)
        self.upload.assert_not_awaited()

    async def test_unreachable_photo_skips_the_files(self):
        with patch.object(guestbook, "_fetch_photo", AsyncMock(return_value=None)):
            files = await guestbook._card_files("연짱", "수고했어", None, "https://v-o.cloud/p.jpg")
        self.assertEqual(files, [])

    async def test_download_redirects_to_the_stored_file(self):
        card_id = "a" * 32
        with patch.object(guestbook, "object_exists", AsyncMock(return_value=True)), patch.object(
            guestbook, "generate_presigned_url", return_value="https://signed/card"
        ) as sign:
            response = await guestbook.download_card_file(card_id, "card.png")
        self.assertEqual(response.headers["location"], "https://signed/card")
        sign.assert_called_once_with(
            f"guestbook-cards/{card_id}/card.png", guestbook.CARD_URL_EXPIRES_SECONDS, download_name="V-O 방명록 카드.png"
        )

    async def test_download_rejects_other_paths(self):
        for card_id, name in (("a" * 32, "../x.png"), ("a" * 32, "photo.exe"), ("not-an-id", "card.png")):
            with self.subTest(card_id=card_id, name=name), self.assertRaises(HTTPException) as ctx:
                await guestbook.download_card_file(card_id, name)
            self.assertEqual(ctx.exception.status_code, 404)


class GuestbookCardEmailTest(unittest.TestCase):
    def test_email_has_save_buttons_and_attachments(self):
        from server.services import email_service

        files = [
            email_service.CardFile("V-O 방명록 카드.png", PNG, "image/png", "카드 저장하기", "https://api/cards/1/card.png"),
            email_service.CardFile("V-O 방명록 사진.jpg", b"jpg", "image/jpeg", "사진 저장하기", None),
        ]
        with patch.object(email_service, "is_smtp_configured", return_value=True), patch.object(email_service, "_send") as smtp:
            email_service.send_guestbook_card("a@example.com", "연짱", "수고했어", None, None, files)
        message = smtp.call_args.args[0]
        html = message.get_body(("html",)).get_content()
        self.assertIn('href="https://api/cards/1/card.png"', html)
        self.assertIn("카드 저장하기", html)
        self.assertNotIn("사진 저장하기", html)  # no link, so attached only
        attachments = [(a.get_filename(), a.get_content_type()) for a in message.iter_attachments()]
        self.assertEqual(attachments, [("V-O 방명록 카드.png", "image/png"), ("V-O 방명록 사진.jpg", "image/jpeg")])


class GuestbookCardImageTest(unittest.TestCase):
    def test_card_is_drawn_with_and_without_photo(self):
        import io
        from datetime import date

        from PIL import Image

        from server.services.guestbook_card_image import render_card

        buffer = io.BytesIO()
        Image.new("RGB", (800, 600), "#88aacc").save(buffer, format="JPEG")
        for photo in (buffer.getvalue(), None):
            with self.subTest(photo=bool(photo)):
                png = render_card("연짱", "수고했어\n" + "아주 긴 글이에요 " * 12, "반가워요", photo, date(2026, 11, 4))
                image = Image.open(io.BytesIO(png))
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.width, (340 + 2 * 24) * 2)


if __name__ == "__main__":
    unittest.main()
