"""Keep only rules that can be applied by the residential furniture optimizer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ALLOWED_ENTITIES = {
    "bed", "bedside table", "chair", "table", "dining table", "desk", "sofa",
    "wardrobe", "pantry", "kitchen", "door", "window", "wall", "room", "entrance",
}
ALLOWED_TYPES = {"clearance", "passage", "placement", "door_swing"}
ALLOWED_RELATIONS = {
    "to_wall", "to_window", "to_door", "to_bed", "between", "passage", "around",
    "near", "beside", "against", "parallel_to", "placement", "swing", "orientation",
}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def keep(row: dict) -> bool:
    rule = row.get("parsed_rule") or {}
    if rule.get("type") not in ALLOWED_TYPES:
        return False
    if rule.get("relation") not in ALLOWED_RELATIONS:
        return False
    if rule.get("subject") not in ALLOWED_ENTITIES or rule.get("object") not in ALLOWED_ENTITIES:
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_jsonl", type=Path)
    parser.add_argument("output_jsonl", type=Path)
    args = parser.parse_args()
    rows = [row for row in read_jsonl(args.input_jsonl) if keep(row)]
    args.output_jsonl.parent.mkdir(parents=True, exist_ok=True)
    args.output_jsonl.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    print(f"입력 {len(read_jsonl(args.input_jsonl))}개 → 최적화 규칙 {len(rows)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
