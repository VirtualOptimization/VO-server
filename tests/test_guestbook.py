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
        self.assertEqual(set(entries[0]), {"id", "nickname", "message", "photo", "created_at"})
        self.assertEqual(entries[0]["photo"], f"https://signed/guestbook/{entries[0]['id']}.png")
        self.upload.assert_awaited_once()

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


if __name__ == "__main__":
    unittest.main()


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
        with patch.object(guestbook.settings, "guestbook_api_key", "secret"), patch.object(
            guestbook, "send_guestbook_card"
        ) as send:
            result = await guestbook.send_card(
                self._payload(to="  friend@example.com ", photo_url="https://v-o.cloud/p.jpg"), x_guestbook_key="secret"
            )
        self.assertTrue(result.sent)
        send.assert_called_once_with("friend@example.com", "연짱", "수고했어", "반가워요", "https://v-o.cloud/p.jpg")

    async def test_bad_card_input_is_rejected(self):
        with patch.object(guestbook.settings, "guestbook_api_key", "secret"), patch.object(guestbook, "send_guestbook_card") as send:
            for overrides in ({"to": "not-an-email"}, {"photo_url": "http://v-o.cloud/p.jpg"}, {"message": ""}):
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
