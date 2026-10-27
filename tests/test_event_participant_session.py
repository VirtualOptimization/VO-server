import unittest
from unittest.mock import patch

from fastapi import HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from server.api.v1.endpoints import events
from server.schemas.event import ParticipantCreateRequest
from shared.models.event import Event, EventAttempt, EventParticipant


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(element, compiler, **kw):
    return "JSON"


class ParticipantSessionTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        with engine.begin() as conn:
            # Only the challenge tables; foreign keys to rooms/versions are not enforced on SQLite.
            for table in (Event.__table__, EventParticipant.__table__, EventAttempt.__table__):
                table.create(conn)
        self.Session = sessionmaker(bind=engine)
        with self.Session() as db:
            db.add(Event(id=1, room_id=1, start_version_id=1, target_version_id=2, name="전시", max_attempts=1))
            db.commit()
        self.patch = patch.object(events, "SessionLocal", self.Session)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    async def _register(self, name="예원", phone="010-1234-5678"):
        response = Response()
        response.status_code = None  # as FastAPI injects it: unset means the route's 201
        result = await events.register_participant(
            1, ParticipantCreateRequest(name=name, phone=phone, privacy_consent=True), response
        )
        return result, response.status_code

    @staticmethod
    def _auth(token):
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)

    async def test_same_phone_and_name_re_enters_the_same_entry(self):
        first, first_status = await self._register()
        again, again_status = await self._register(phone="01012345678")
        self.assertIsNone(first_status)  # route default (201)
        self.assertFalse(first.is_returning)
        self.assertEqual(again_status, 200)
        self.assertTrue(again.is_returning)
        self.assertEqual(again.participant_id, first.participant_id)
        self.assertNotEqual(again.participant_token, first.participant_token)
        # The new token works; the old one was replaced.
        self.assertEqual(events._participant(1, self._auth(again.participant_token)), first.participant_id)
        with self.assertRaises(HTTPException) as caught:
            events._participant(1, self._auth(first.participant_token))
        self.assertEqual(caught.exception.status_code, 401)

    async def test_same_phone_with_another_name_is_refused(self):
        await self._register()
        with self.assertRaises(HTTPException) as caught:
            await self._register(name="다른사람")
        self.assertEqual(caught.exception.status_code, 409)

    async def test_leaving_invalidates_the_token(self):
        entry, _ = await self._register()
        participant_id = events._participant(1, self._auth(entry.participant_token))
        response = await events.leave_participant_session(1, participant_id)
        self.assertEqual(response.status_code, 204)
        with self.assertRaises(HTTPException):
            events._participant(1, self._auth(entry.participant_token))

    async def test_my_layout_without_a_submission_is_404(self):
        entry, _ = await self._register()
        with self.assertRaises(HTTPException) as caught:
            await events.get_my_layout(1, entry.participant_id)
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
