"""전시 이벤트 상위 참가자 연락처를 CSV로 내보낸다 (경품 연락용).

순위는 공개 순위표 API와 같은 코드(server/services/event_ranking.py)로 계산하므로
화면에 보인 순위와 내보낸 순위가 항상 같다. 조회만 하고 DB는 수정하지 않는다.

사용법 (서버 .env가 있는 VO-server 폴더에서):
    python scripts/export_winners.py --event 3 --top 10
    python scripts/export_winners.py --event 3 --top 10 --out winners.csv

전화번호가 담긴 파일이므로 연락이 끝나면 바로 지운다.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from server.services.event_ranking import best_attempts  # noqa: E402
from shared.db import SessionLocal  # noqa: E402
from shared.models.event import Event  # noqa: E402

COLUMNS = ["rank", "name", "phone", "total_score", "usable_area_m2", "elapsed_seconds", "submitted_at"]


def main() -> int:
    parser = argparse.ArgumentParser(description="전시 이벤트 상위 참가자 연락처 내보내기")
    parser.add_argument("--event", type=int, required=True, help="이벤트 ID")
    parser.add_argument("--top", type=int, default=10, help="몇 위까지 내보낼지 (기본 10)")
    parser.add_argument("--out", type=Path, help="저장할 CSV 경로 (없으면 화면에 출력)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        event = db.query(Event).filter(Event.id == args.event).first()
        if event is None:
            print(f"이벤트 {args.event}를 찾을 수 없어요.", file=sys.stderr)
            return 1
        winners = best_attempts(db, event.id).limit(args.top).all()
        rows = [
            {
                "rank": position,
                "name": attempt.participant.name,
                "phone": attempt.participant.phone,
                "total_score": attempt.total_score,
                "usable_area_m2": attempt.usable_area_m2,
                "elapsed_seconds": attempt.elapsed_seconds,
                "submitted_at": attempt.created_at.isoformat(),
            }
            for position, attempt in enumerate(winners, start=1)
        ]
    finally:
        db.close()

    output = args.out.open("w", newline="", encoding="utf-8-sig") if args.out else sys.stdout
    try:
        writer = csv.DictWriter(output, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    finally:
        if args.out:
            output.close()

    status = "진행 중" if event.is_active else "마감"
    print(f"[{event.name} · {status}] 상위 {len(rows)}명을 내보냈어요.", file=sys.stderr)
    if event.is_active:
        print("아직 진행 중인 이벤트라 순위가 바뀔 수 있어요.", file=sys.stderr)
    if args.out:
        print(f"저장 위치: {args.out} (연락이 끝나면 지워 주세요)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
