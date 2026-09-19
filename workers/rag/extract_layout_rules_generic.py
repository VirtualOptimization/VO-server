"""Generic page-level extraction, standardization, and validation for residential rules."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
from collections import defaultdict
from io import BytesIO
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image


ALIASES = {
    "table": ("table", "dining table", "masa", "yemek masası"),
    "chair": ("chair", "sandalye", "oturma yeri"),
    "bed": ("bed", "yatak", "frankfurt yatağı", "katlanabilir yatak"),
    "desk": ("desk", "çalışma masası", "calisma masasi"),
    "sofa": ("sofa", "divan", "kanape", "kanape"),
    "wardrobe": ("wardrobe", "dolap", "gardırop", "gardrop"),
    "nightstand": ("nightstand", "bedside table", "komidin"),
    "door": ("door", "kapı", "kapi"),
    "window": ("window", "pencere"),
    "wall": ("wall", "duvar"),
    "room": ("room", "oda", "mekan", "mekân", "surrounding area", "movement area"),
    "kitchen": ("kitchen", "mutfak"),
    "pantry": ("pantry", "kiler"),
    "sink": ("sink", "lavabo"),
    "bathtub": ("bathtub", "bath", "küvet", "duş küveti", "dus kuvveti"),
    "toilet": ("toilet", "wc", "klozet", "pisuar"),
}
GENERIC_NAMES = {"furniture", "object", "item", "passage width", "clearance", "space"}
RELATIONS = {"between", "to_wall", "to_window", "to_door", "to_bed", "passage", "swing", "around", "against", "beside", "near", "placement", "dimension"}
TYPES = {"clearance", "passage", "door_swing", "placement", "dimension"}
DIRECTIONS = {"front", "back", "side", "around", "left", "right", "against", "beside", "near", "none"}

PROMPT = """Extract actionable residential space-planning rules from all images and OCR text for one page.
Return JSON only with this shape:
{"rules":[{"subject":"...","object":"...","relation":"...","type":"clearance",
"direction":"front","min_distance_m":null,"max_distance_m":null,
"recommended_distance_m":null,"width_m":null,"depth_m":null,"height_m":null,
"source_value":"...","source_unit":"cm","confidence":0.0,"evidence":"..."}]}

This is a general extraction task. Do not assume a particular page, figure, language, or number.
Extract only rules that are explicitly supported by the page image or OCR text. Identify the
subject, object, and relation before assigning a measurement.

Rule types:
- clearance: distance between two named objects or an object and a wall, door, or window
- passage: walking or access space around furniture or through a room
- door_swing: space or direction needed for a door to open
- placement: explicit placement condition such as near, beside, against, or parallel to another object
- dimension: actual width, depth, or height of a named object

For measurements, preserve the distinction between required/minimum, recommended/ideal, and
maximum values. Use min_distance_m for required minimum, recommended_distance_m for ideal,
and max_distance_m only when the text explicitly states a maximum. Use width/depth/height
only for the object's own dimension. Convert mm and cm to meters.

Reject page numbers, figure numbers, labels, room-wide dimensions, object dimensions mistaken
for clearance, table diameter, seat surface, area per person, and unsupported inferences.
If a number has no explicit relationship, omit it. If a placement condition is explicit but
has no number, it may still be returned. Use concise object names, not generic categories.
Return at most 12 rules. If no reliable rule exists, return {"rules":[]}.
"""


def load_page_texts(path: Path | None) -> dict[int, str]:
    if path is None:
        return {}
    result: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("page_number") is not None:
                result[int(row["page_number"])] = str(row.get("text") or row.get("content") or "")
    return result


def resolve_image(raw: str, input_path: Path, root: Path | None) -> Path | None:
    candidates = [Path(raw)]
    if root:
        candidates.append(root / Path(raw).name)
    candidates.append(input_path.parent / Path(raw).name)
    return next((path for path in candidates if path.exists()), None)


def image_url(path: Path, max_side: int) -> str:
    image = Image.open(path).convert("RGB")
    scale = min(1.0, max_side / max(image.size))
    if scale < 1:
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=88, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def canonical_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value.casefold().strip())
    if not text or text in GENERIC_NAMES:
        return None
    for canonical, terms in ALIASES.items():
        if text == canonical or any(term in text for term in terms):
            return canonical
    if re.fullmatch(r"[a-zà-ž가-힣][a-zà-ž가-힣0-9 ._-]{0,50}", text):
        return text
    return None


def number_in_evidence(source_value: Any, evidence: str) -> bool:
    if not isinstance(source_value, str):
        return False
    source_numbers = re.findall(r"\d+(?:[.,]\d+)?", source_value)
    evidence_numbers = {value.replace(",", ".") for value in re.findall(r"\d+(?:[.,]\d+)?", evidence)}
    return bool(source_numbers) and any(value.replace(",", ".") in evidence_numbers for value in source_numbers)


def standardize(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    rule = dict(raw)
    rule["subject"] = canonical_name(rule.get("subject"))
    rule["object"] = canonical_name(rule.get("object"))
    if not rule["subject"] or not rule["object"]:
        return None
    relation = str(rule.get("relation", "")).casefold().strip()
    if relation in {"to_surrounding_area", "to_movement_area", "to_room"}:
        relation = "around" if rule.get("type") == "clearance" else "passage"
        rule["object"] = "room"
    if relation in {"parallel_to", "next_to", "close_to"}:
        relation = {"parallel_to": "beside", "next_to": "beside", "close_to": "near"}[relation]
    rule["relation"] = relation
    rule["type"] = str(rule.get("type", "")).casefold().strip()
    rule["direction"] = str(rule.get("direction", "none")).casefold().strip()
    if rule["type"] not in TYPES or rule["relation"] not in RELATIONS or rule["direction"] not in DIRECTIONS:
        return None
    evidence = rule.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        return None
    lower = evidence.casefold()
    if any(word in lower for word in ("likely", "probably", "may indicate", "short description")):
        return None
    if rule["type"] == "placement":
        if any(rule.get(field) is not None for field in ("min_distance_m", "max_distance_m", "recommended_distance_m", "width_m", "depth_m", "height_m", "source_value", "source_unit")):
            return None
    else:
        if not number_in_evidence(rule.get("source_value"), evidence) or rule.get("source_unit") not in {"mm", "cm", "m"}:
            return None
        fields = ("width_m", "depth_m", "height_m") if rule["type"] == "dimension" else ("min_distance_m", "max_distance_m", "recommended_distance_m")
        for field in fields:
            if rule.get(field) == 0.0:
                rule[field] = None
        if not any(isinstance(rule.get(field), (int, float)) and rule[field] > 0 for field in fields):
            return None
        low, high = rule.get("min_distance_m"), rule.get("max_distance_m")
        if rule["type"] != "dimension" and low is not None and high is not None and low > high:
            return None
    try:
        if float(rule.get("confidence", 0)) < 0.8:
            return None
    except (TypeError, ValueError):
        return None
    return rule


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("input_jsonl", type=Path)
    parser.add_argument("output_jsonl", type=Path)
    parser.add_argument("--review-jsonl", type=Path, required=True)
    parser.add_argument("--image-root", type=Path)
    parser.add_argument("--pages-jsonl", type=Path)
    parser.add_argument("--model", default=os.getenv("OPENAI_VISION_MODEL", "gpt-4.1-mini"))
    parser.add_argument("--max-side", type=int, default=1600)
    parser.add_argument("--max-images", type=int, default=8)
    parser.add_argument("--limit-pages", type=int)
    args = parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY가 .env 또는 환경변수에 없습니다.")
    rows = [json.loads(line) for line in args.input_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("page_number") is not None:
            groups[int(row["page_number"])].append(row)
    pages = sorted(groups)[: args.limit_pages] if args.limit_pages else sorted(groups)
    texts = load_page_texts(args.pages_jsonl)
    client = OpenAI()
    accepted: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for index, page in enumerate(pages, 1):
        images = [path for row in groups[page][: args.max_images] if (path := resolve_image(row.get("image_path", ""), args.input_jsonl, args.image_root))]
        if not images:
            review.append({"page_number": page, "reason": "no images found"})
            continue
        context = re.sub(r"\s+", " ", texts.get(page, "")).strip()[:7000]
        prompt = PROMPT + f"\nPAGE OCR TEXT:\n{context or '[none]'}"
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content.extend({"type": "image_url", "image_url": {"url": image_url(path, args.max_side), "detail": "high"}} for path in images)
        try:
            response = client.chat.completions.create(model=args.model, temperature=0, max_tokens=3000, response_format={"type": "json_object"}, messages=[{"role": "user", "content": content}])
            raw_result = json.loads(response.choices[0].message.content or '{"rules":[]}')
            rules = [rule for item in raw_result.get("rules", []) if (rule := standardize(item)) is not None]
            for rule in rules:
                key = (page, rule["subject"], rule["object"], rule["relation"], rule["type"], rule["direction"], rule.get("min_distance_m"), rule.get("max_distance_m"), rule.get("recommended_distance_m"), rule.get("width_m"), rule.get("depth_m"), rule.get("height_m"))
                if key not in seen:
                    seen.add(key)
                    accepted.append({"page_number": page, "image_paths": [str(path) for path in images], "parsed_rule": rule})
            if not rules:
                review.append({"page_number": page, "image_paths": [str(path) for path in images], "raw_result": raw_result, "reason": "no validated rule"})
        except Exception as exc:
            review.append({"page_number": page, "image_paths": [str(path) for path in images], "reason": f"{type(exc).__name__}: {exc}"})
        print(f"{index}/{len(pages)} pages accepted={len(accepted)} review={len(review)}", flush=True)
    for path, values in ((args.output_jsonl, accepted), (args.review_jsonl, review)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(value, ensure_ascii=False) for value in values) + ("\n" if values else ""), encoding="utf-8")
    print(f"완료: 규칙 {len(accepted)}개, 검토 페이지 {len(review)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
