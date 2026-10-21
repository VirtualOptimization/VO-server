# Exhibition Challenge integration

The active challenge is the newest event with `is_active=true`. iOS (event ID 0
or absent) and the website both resolve `GET /api/events/current`. A positive
`VOChallengeEventID` or website `?event_id=123` explicitly pins an event.

## Participant flow

1. Resolve the event and show its server-provided criteria.
2. Register with consent, nickname and phone. A phone is unique within an event.
3. Store the returned token in the device Keychain. Never send it to the website.
4. `GET /api/events/{id}/session` restores remaining attempts and latest result.
5. `GET /api/events/{id}/challenge` returns the fixed starting version.
6. Submit changed poses to `POST /api/events/{id}/attempts` with a stable
   `Idempotency-Key`. `ios_objects: []` explicitly submits the unchanged start.
7. Save the pending request and key before sending. Retry the same request after
   interruption, including after the event closes or the attempt limit is reached.
8. Show the result and offer a fresh start while `remaining_attempts > 0`.

Challenge submissions can only move/rotate existing objects. Object creation,
size/category changes, duplicate IDs, malformed matrices and nonfinite numbers
are rejected. Room bounds, furniture geometry and scoring come from server data.

The public leaderboard includes one best valid attempt per participant. Order:
score descending, usable area descending, elapsed time ascending (null last),
submission time ascending, attempt ID ascending. It never includes phone/token.

## Website transport

The HTTPS Sites Worker serves existing assets and proxies only read-only ranking
routes to the fixed AWS backend. It resolves the event once and loads that exact
event's leaderboard, allows only public response fields, and sends `no-store`.
Browser clients never contact the HTTP AWS origin directly.

## Deployment

Merge to `dev` through normal review. Deployment stops on errors, installs runtime
dependencies, runs `alembic upgrade head`, restarts the service, and checks `/health`.
Before enabling participation:

- Confirm current-event and current-leaderboard routes appear in `/openapi.json`.
- Use the authenticated owner API `POST /api/events` with an existing room,
  starting version and valid AI version. Event creation checks the AI layout.
- Confirm `/api/events/current` and the website select the same event ID.
- Verify a submission from an iOS device appears on the website after refresh.

Do not create production participants for automated tests. Tests use SQLite and
mock S3/scoring; scoring geometry has separate real tests. PostgreSQL concurrent
submission locking and actual S3/RealityKit rendering need deployment/device QA.

## Tests

```sh
python -m pip install -r requirements-test.txt
DATABASE_URL=sqlite:// ASYNC_DATABASE_URL=sqlite+aiosqlite:// ENV=test \
AWS_EC2_METADATA_DISABLED=true python -m pytest \
  tests/test_event_api.py tests/test_event_rules.py tests/test_layout_scoring.py -q
```
