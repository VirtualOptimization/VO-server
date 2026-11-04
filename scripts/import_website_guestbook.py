"""Copy the website's guestbook (stored in Cloudflare R2) into the V-O DB and S3.

Reads the website's public list API, keeps entries that were not deleted, copies
each photo into s3://<bucket>/guestbook/, and inserts the rows with their
original timestamps. Already-copied entries are skipped, so it can be rerun.
It is a dry run unless --apply is given.

Example:
    .venv/bin/python scripts/import_website_guestbook.py
    .venv/bin/python scripts/import_website_guestbook.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.api.v1.endpoints.guestbook import PHOTO_PREFIX  # noqa: E402
from server.core.config import settings  # noqa: E402
from server.core.s3 import get_s3_client  # noqa: E402
from server.services.guestbook_rules import (  # noqa: E402
    MESSAGE_MAX_LENGTH,
    NICKNAME_MAX_LENGTH,
    PHOTO_MAX_BYTES,
    PHOTO_TYPES,
    InvalidInput,
    normalize_mood,
    normalize_text,
)
from shared.db import SessionLocal  # noqa: E402
from shared.models.guestbook import GuestbookEntry  # noqa: E402

USER_AGENT = "vo-guestbook-import"


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read(PHOTO_MAX_BYTES + 1)


def _photo(site: str, path: str | None) -> tuple[bytes, str, str] | None:
    if not path:
        return None
    data = _get(urljoin(site, path))
    if len(data) > PHOTO_MAX_BYTES:
        raise InvalidInput("사진이 5MB를 넘어요")
    for content_type, (extension, matches) in PHOTO_TYPES.items():
        if matches(data):
            return data, content_type, extension
    raise InvalidInput("지원하지 않는 사진 형식이에요")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", default="https://v-o.cloud", help="Website origin serving /api/guests.")
    parser.add_argument("--apply", action="store_true", help="Write to the DB and S3.")
    args = parser.parse_args()

    entries = json.loads(_get(urljoin(args.site, "/api/guests")))["entries"]
    db = SessionLocal()
    imported = skipped = failed = 0
    try:
        existing = {row.id for row in db.query(GuestbookEntry.id)}
        for entry in sorted(entries, key=lambda e: e["created_at"]):
            entry_id = entry["id"].replace("-", "")
            label = f"{entry_id[:8]} {entry.get('nickname')!r}"
            if entry.get("deleted_at") or entry_id in existing:
                skipped += 1
                print(f"  [SKIP] {label}")
                continue
            try:
                nickname = normalize_text(entry.get("nickname"), field="닉네임", max_length=NICKNAME_MAX_LENGTH)
                message = normalize_text(entry.get("message"), field="내용", max_length=MESSAGE_MAX_LENGTH)
                mood = normalize_mood(entry.get("mood"))
                photo = _photo(args.site, entry.get("photo"))
            except (InvalidInput, OSError) as exc:
                failed += 1
                print(f"  [FAIL] {label}: {exc}")
                continue

            created_at = datetime.fromisoformat(entry["created_at"].replace("Z", "+00:00"))
            photo_key = f"{PHOTO_PREFIX}/{entry_id}.{photo[2]}" if photo else None
            print(f"  [{'COPY' if args.apply else 'DRY RUN'}] {label} mood={mood} photo={photo_key}")
            if not args.apply:
                continue
            if photo:
                get_s3_client().put_object(
                    Bucket=settings.s3_bucket_name, Key=photo_key, Body=photo[0], ContentType=photo[1]
                )
            db.add(
                GuestbookEntry(
                    id=entry_id,
                    nickname=nickname,
                    message=message,
                    mood=mood,
                    photo_key=photo_key,
                    # The visitor agreed on the website when the entry was written.
                    consent_at=created_at,
                    created_at=created_at,
                )
            )
            db.commit()
            imported += 1
    finally:
        db.close()

    print(f"Done. imported={imported} skipped={skipped} failed={failed} (dry run: {not args.apply})")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
