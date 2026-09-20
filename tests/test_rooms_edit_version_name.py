import sys
from types import ModuleType, SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from fastapi import HTTPException

# 엔드포인트 단위 테스트에는 JWT 인증 구현이 필요하지 않다. 오래된 로컬 테스트
# 가상환경에도 동작하도록 인증 의존성만 가볍게 대체한다.
deps_stub = ModuleType("server.api.v1.deps")
deps_stub.get_current_user = lambda: None


class _Column:
    def __eq__(self, other):
        return self


class Room:
    id = _Column()
    user_id = _Column()


class Version:
    id = _Column()
    room_id = _Column()


class User:
    id = _Column()


db_stub = ModuleType("shared.db")
db_stub.SessionLocal = lambda: None
room_stub = ModuleType("shared.models.room")
room_stub.Room = Room
user_stub = ModuleType("shared.models.user")
user_stub.User = User
version_stub = ModuleType("shared.models.version")
version_stub.Version = Version

with patch.dict(
    sys.modules,
    {
        "server.api.v1.deps": deps_stub,
        "shared.db": db_stub,
        "shared.models.room": room_stub,
        "shared.models.user": user_stub,
        "shared.models.version": version_stub,
    },
):
    from server.api.v1.endpoints.rooms_edit import rename_user_version

from server.schemas.room_view import UserEditedVersionRenameRequest


class _Query:
    def __init__(self, model, room, version):
        self.model = model
        self.room = room
        self.version = version

    def filter(self, *args):
        return self

    def first(self):
        return self.room if self.model is Room else self.version


class _DB:
    def __init__(self, room, version):
        self.room = room
        self.version = version
        self.commit = Mock()
        self.refresh = Mock()
        self.rollback = Mock()
        self.close = Mock()

    def query(self, model):
        return _Query(model, self.room, self.version)


class RenameUserVersionTest(TestCase):
    def test_renames_owned_user_edited_version(self) -> None:
        room = SimpleNamespace(id=47)
        version = SimpleNamespace(id=100, version_type="USER_EDITED", version_name="이전 이름")
        db = _DB(room, version)

        with patch("server.api.v1.endpoints.rooms_edit.SessionLocal", return_value=db):
            result = rename_user_version(
                room_id=47,
                version_id=100,
                payload=UserEditedVersionRenameRequest(version_name="  창가 배치  "),
                current_user=SimpleNamespace(id=1),
            )

        self.assertEqual(version.version_name, "창가 배치")
        self.assertEqual(result.version_name, "창가 배치")
        db.commit.assert_called_once_with()
        db.refresh.assert_called_once_with(version)
        db.close.assert_called_once_with()

    def test_rejects_renaming_original_version(self) -> None:
        room = SimpleNamespace(id=47)
        version = SimpleNamespace(id=1, version_type="ORIGINAL", version_name="원본")
        db = _DB(room, version)

        with (
            patch("server.api.v1.endpoints.rooms_edit.SessionLocal", return_value=db),
            self.assertRaises(HTTPException) as raised,
        ):
            rename_user_version(
                room_id=47,
                version_id=1,
                payload=UserEditedVersionRenameRequest(version_name="바꿀 이름"),
                current_user=SimpleNamespace(id=1),
            )

        self.assertEqual(raised.exception.status_code, 403)
        db.commit.assert_not_called()
        db.rollback.assert_called_once_with()

    def test_rejects_whitespace_only_name(self) -> None:
        room = SimpleNamespace(id=47)
        version = SimpleNamespace(id=100, version_type="USER_EDITED", version_name="기존")
        db = _DB(room, version)

        with (
            patch("server.api.v1.endpoints.rooms_edit.SessionLocal", return_value=db),
            self.assertRaises(HTTPException) as raised,
        ):
            rename_user_version(
                room_id=47,
                version_id=100,
                payload=UserEditedVersionRenameRequest(version_name="   "),
                current_user=SimpleNamespace(id=1),
            )

        self.assertEqual(raised.exception.status_code, 400)
        db.commit.assert_not_called()
        db.rollback.assert_called_once_with()
