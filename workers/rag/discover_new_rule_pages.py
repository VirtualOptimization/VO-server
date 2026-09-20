"""Find Neufert pages worth extracting next, using the pgvector-backed search.

This is the same "pool hits across single-room topic queries" idea used by
select_extraction_pages.py, but against the DB-backed corpus instead of a
local JSONL + custom BM25 index, and pre-filtered against pages this project
has already extracted and reviewed.

A prior full-book run of this method (see select_extraction_pages.py) showed
that most high-scoring pages outside the residential-interior chapter were
false positives from hospitals, schools, offices, hotels, and industrial
buildings that happen to share generic spatial vocabulary ("clearance",
"passage width", "m²") with the queries below. Read the flagged pages before
extracting -- do not trust the ranking alone.

Usage (on a host with DB access, e.g. the EC2 instance):
    python -m workers.rag.discover_new_rule_pages
"""

from __future__ import annotations

from collections import defaultdict

from workers.rag.neufert_retrieval import search_chunks

# Pages already extracted, filtered, and reviewed this project (bedroom +
# storage-efficiency chapter, plus the bathroom/laundry tail found via the
# first pgvector-less pass). Re-surfacing these isn't useful.
ALREADY_COVERED = set(range(232, 266))

TOP_K = 15

QUERIES: list[tuple[str, str]] = [
    ("yatak odası mobilya yerleşimi ve mesafeleri", "bedroom furniture placement/clearance"),
    ("çalışma masası ve sandalye arasındaki mesafe", "desk-chair clearance"),
    ("gömme dolap ve gardırop yerleşimi", "built-in wardrobe/closet placement"),
    ("tek kişilik konut küçük oda düzeni", "single-occupant small room layout"),
    ("oda içinde hareket ve geçiş genişliği", "in-room movement/passage width"),
    ("kapı önü ve pencere önü mesafe", "door/window clearance"),
    ("katlanır yatak ve çok amaçlı mobilya", "folding bed / multifunctional furniture"),
    ("raf ve depolama alanı yerleşimi", "shelf/storage placement"),
    ("banyo küvet ve lavabo mesafeleri", "bathroom bathtub/sink clearance"),
    ("mutfak çalışma yüzeyi ve dolap mesafeleri", "kitchenette work surface/cabinet clearance"),
]


def main() -> int:
    page_hits: dict[int, dict[str, set[str]]] = defaultdict(
        lambda: {"queries": set(), "section_titles": set()}
    )

    for query, label in QUERIES:
        matches = search_chunks(query, top_k=TOP_K)
        for match in matches:
            if match.page_start is None:
                continue
            page_end = match.page_end or match.page_start
            for page in range(match.page_start, page_end + 1):
                entry = page_hits[page]
                entry["queries"].add(label)
                if match.section_title:
                    entry["section_titles"].add(match.section_title)
        print(f"쿼리 처리 완료: {label} ({len(matches)}개 청크 매칭)", flush=True)

    new_pages = {page: entry for page, entry in page_hits.items() if page not in ALREADY_COVERED}
    print(f"\n총 매칭 페이지: {len(page_hits)}개 (기존 범위 밖 신규: {len(new_pages)}개)")
    print("\n=== 기존 범위(232-265) 밖 신규 후보 (매칭 질의 수 순, 내용 직접 확인 후 추출 여부 판단할 것) ===")
    for page, entry in sorted(new_pages.items(), key=lambda kv: -len(kv[1]["queries"]))[:30]:
        titles = ", ".join(list(entry["section_titles"])[:2])
        print(f"  page {page:>4}  matched_queries={len(entry['queries'])}  {titles}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
