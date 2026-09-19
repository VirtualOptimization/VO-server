"""Extract layout rules page-by-page using all related context crops."""

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

import certifi

# macOS framework Python may not include a usable CA bundle for HTTPS requests.
os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image


TARGETS = {"table", "chair", "bed", "desk", "sofa", "wardrobe", "window", "door", "room", "wall"}
TYPES = {"clearance", "passage", "door_swing", "placement", "dimension"}
DIRECTIONS = {"front", "back", "side", "around", "left", "right", "against", "beside", "near", "none", "parallel", "on", "in"}
MAX_PAGE_TEXT = 7000
MAX_IMAGES_PER_PAGE = 8
RESIDENTIAL_PAGES = range(232, 266)
RESIDENTIAL_TERMS = {
    "table", "dining table", "masa", "chair", "sandalye", "bed", "yatak",
    "desk", "sofa", "divan", "kanape", "wardrobe", "dolap", "door", "kapı",
    "kapi", "window", "pencere", "wall", "duvar", "room", "oda",
    "nightstand", "bedside table", "komidin", "kiler", "pantry", "mutfak", "kitchen",
    "mutfak dolabı", "kitchen furniture", "kitchen work surface",
    "lavabo", "duş küveti", "dus kuvveti", "banyo küveti", "bathroom", "pisuar",
    "surrounding area", "movement area",
    "pantry", "kitchen", "kitchen cabinet", "sink", "bedside table",
    "dining table", "dining room", "bedroom", "living room", "bathroom",
    "entrance", "side table", "shelving", "dining set", "shower", "bathtub",
    "double sink", "dishwasher", "mirror", "toilet", "work surface",
    "cooking area", "upper cabinet", "lower cabinet", "radiator",
}

ENTITY_ALIASES = {
    "kiler": "pantry", "kitchen pantry": "pantry", "pantry room": "pantry",
    "mutfak": "kitchen", "mutfak dolabı": "kitchen cabinet", "kitchen furniture": "kitchen cabinet",
    "dolap": "wardrobe", "gardrop": "wardrobe", "gardırop": "wardrobe", "wardrobe cabinet": "wardrobe",
    "gömme dolap": "wardrobe", "yerleşik dolap": "wardrobe", "dolap odaları": "wardrobe",
    "komidin": "bedside table", "nightstand": "bedside table", "bedside cabinet": "bedside table",
    "yatak": "bed", "sandalye": "chair", "masa": "table", "yemek masası": "dining table", "dining table": "dining table",
    "yemek odası": "dining room", "yatak odası": "bedroom", "oturma odası": "living room", "salon": "living room",
    "banyo": "bathroom", "duş": "shower", "dus": "shower", "duş küveti": "bathtub", "küvet": "bathtub",
    "gömme küvet": "bathtub", "banyo küveti": "bathtub",
    "antre": "entrance", "anteroom": "entrance", "giriş": "entrance", "etajer": "shelving",
    "sofra takımı": "dining set", "side table": "side table",
    "duvar": "wall", "oda": "room", "kapı": "door", "kapi": "door",
    "pencere": "window", "lavabo": "sink",
    "wardrobe doors": "wardrobe", "movement area": "room", "surrounding space": "room",
    "hallway": "entrance", "person": "room",
    "çift lavabo": "double sink", "double sink": "double sink",
    "bulaşık makinesi": "dishwasher",
    "bed head": "bed", "yatak başı": "bed", "yatak vagon": "bed",
    "ayna": "mirror", "full-length mirror": "mirror",
    "klozet": "toilet", "duvara asılı klozet": "toilet", "wc": "toilet", "tuvalet": "toilet",
    "çalışma yüzeyi": "work surface", "working area": "work surface",
    "pişirme alanı": "cooking area", "cooking area": "cooking area",
    "üst dolap": "upper cabinet", "üst dolaplar": "upper cabinet",
    "alt dolap": "lower cabinet", "alt dolaplar": "lower cabinet",
    "konvektör": "radiator",
}


def canonical_entity(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value.casefold().strip())
    return ENTITY_ALIASES.get(normalized, normalized)

PROMPT = """You are extracting reliable architectural layout facts from one document page.
You receive several overlapping crops from the same page, the page OCR text, and OCR numbers.
Use all of them together. Extract ALL independently actionable rules on the page, not just
the single clearest rule. A page may contain several rules from the prose and several more
from dimensioned diagrams; return each separately when the subject, object, relation, or
measurement differs. Do not stop after finding one rule. Return JSON only:
{"rules": [{
  "subject":"table", "object":"wall", "relation":"to_wall",
  "type":"clearance", "constraint_type":"numeric", "priority":"hard", "direction":"side",
  "min_distance_m":0.8, "max_distance_m":null, "recommended_distance_m":null,
  "width_m":null, "depth_m":null, "height_m":null,
  "source_value":"80", "source_unit":"cm", "confidence":0.95,
"evidence":"exact text or visible dimension and what it connects"
}]}

Allowed type:
- clearance: distance between two named objects or an object and wall/window/door
- passage: walking/access width
- door_swing: space or direction needed for a door to open
- placement: explicit placement condition, such as against a wall or beside a window
- dimension: actual width/depth/height of a named furniture item

Use concise noun phrases for subject/object names, for example table, chair, pantry, oven,
wall, or room. Never use generic phrases such as "furniture" or "passage width".
Allowed direction: front, back, side, around, left, right, against, beside, near, none, parallel, on, in.
Use constraint_type="numeric" for distance/dimension rules and constraint_type="qualitative"
for non-numeric placement relationships. Use priority="hard" only when the text states a
requirement or prohibition; otherwise use priority="soft" or "preferred".
For every rule, identify subject, relation, and object. Never use generic targets such as
"furniture" or "passage width". For numerical rules, source_value and source_unit are required.
For clearance/passage/door_swing use distance fields. For dimension use width_m/depth_m/height_m.
For placement, a non-numerical rule is allowed only when explicitly written in the OCR/text.
When the text distinguishes minimum/required and recommended/ideal values, preserve both:
put the required value in min_distance_m and the ideal value in recommended_distance_m;
do not treat the ideal value as max_distance_m.
Do extract explicit residential placement statements even when they have no number. For
example, if the text says "kilerin mutfağın yakınında bulunması gerekir", return:
{"subject":"kiler","object":"mutfak","relation":"near","type":"placement",
"direction":"near","evidence":"kilerin mutfağın yakınında bulunması gerekir",
"confidence":0.95}. Do not return an empty list when an explicit placement statement exists.

If the evidence says free movement, walking space, or passage and does not name a second
object, classify it as type "passage", object "room", and relation "passage". Do not classify
that rule as clearance to a wall or door. The object must match the relation explicitly stated
in the evidence: duvar/wall means wall, kapı/door means door, and pencere/window means window.
If the object and evidence do not match, omit the rule.

Reject page numbers, labels, room-wide dimensions, furniture dimensions misread as clearance,
table diameter, seat surface, height of a counter, area per person, and facility-specific facts
that cannot be used in furniture layout. Do not infer a relation from a number alone. Do not
reuse one measurement for unrelated objects. If uncertain, omit the rule. Evidence must contain
the exact measurement or explicit placement wording. A dimension line spanning the edges of
one object is a dimension rule, not clearance. A clearance line must visibly start at one
object and end at a different object or an open movement area. A number in a sentence about
height, width, depth, diameter, or an object's length is not clearance. Focus only on residential
rooms such as bedrooms, living rooms, dining rooms, kitchens, storage rooms, and bathrooms.
Ignore bicycles, hospitals, schools, offices, restaurants, sports facilities, and industrial spaces.
For residential furniture optimization, prioritize bed, table, chair, desk, sofa, wardrobe, door,
window, wall, and room. When a sentence contains both a required minimum and an ideal value,
return one rule with both values. For example, "at least 60 cm, ideally 75 cm" becomes
min_distance_m=0.60 and recommended_distance_m=0.75. For "one metre is needed per person",
return a rule with min_distance_m=1.0 and preserve the exact sentence as evidence. Read the
full page top to bottom, including the prose column, captions, labels, and every numbered
diagram. Return at most 20 rules per page.
"""


def load_page_texts(path: Path | None) -> dict[int, str]:
    if path is None:
        return {}
    result: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            number = row.get("page_number")
            if number is not None:
                result[int(number)] = str(row.get("text") or row.get("content") or "")
    return result


def resolve_image(raw: str, input_path: Path, root: Path | None) -> Path | None:
    source = Path(raw)
    candidates = [source]
    if root:
        candidates.append(root / source.name)
    candidates.append(input_path.parent / source.name)
    return next((path for path in candidates if path.exists()), None)


def image_url(path: Path, max_side: int) -> str:
    image = Image.open(path).convert("RGB")
    scale = min(1.0, max_side / max(image.size))
    if scale < 1:
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=86, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def normalize(rule: Any, residential_only: bool = False) -> dict[str, Any] | None:
    if not isinstance(rule, dict):
        return None
    subject = rule.get("subject")
    object_name = rule.get("object")
    if not isinstance(subject, str) or not re.fullmatch(r"[A-Za-zÀ-ž가-힣][A-Za-zÀ-ž가-힣0-9 ._-]{0,60}", subject.strip()):
        return None
    if not isinstance(object_name, str) or not re.fullmatch(r"[A-Za-zÀ-ž가-힣][A-Za-zÀ-ž가-힣0-9 ._-]{0,60}", object_name.strip()):
        return None
    subject = canonical_entity(subject)
    object_name = canonical_entity(object_name)
    rule = dict(rule)
    rule["subject"] = subject
    rule["object"] = object_name
    if residential_only:
        subject_lower = subject.casefold().strip()
        object_lower = object_name.casefold().strip()
        if subject_lower not in RESIDENTIAL_TERMS or object_lower not in RESIDENTIAL_TERMS:
            return None
        if rule.get("type") == "dimension":
            return None
    if rule.get("type") not in TYPES:
        return None
    if rule.get("direction", "none") not in DIRECTIONS:
        return None
    evidence = rule.get("evidence")
    if not isinstance(evidence, str) or not evidence.strip():
        return None
    if any(bad in evidence.casefold() for bad in ("likely", "probably", "may indicate", "short description")):
        return None
    evidence_lower = evidence.casefold()
    relation = rule.get("relation")
    # Normalize spatial wording before validating the model's relation labels.
    around_terms = ("around", "surrounding", "civar", "çevre", "cevre", "etraf", "yanlarında", "yanlarinda", "her iki yanında", "her iki yaninda")
    front_terms = ("front", "in front", "önünde", "onunde", "önü", "onu", "ön taraf", "on taraf", "açıkken", "acikken")
    passage_terms = ("passage", "walking", "movement", "free movement", "walking space", "geçiş", "gecis", "geçilebilir", "gecilebilir", "hareket alanı", "hareket alani", "serbest hareket", "yürüyüş", "yuruyus")
    wall_terms = ("wall", "duvar")
    between_terms = ("between", "arasında", "arasındaki", "arasindaki", "yanındakine mesafesi", "yanindakine mesafesi")
    if rule.get("type") in {"clearance", "passage"}:
        if any(term in evidence_lower for term in wall_terms) or object_name == "wall":
            rule["relation"] = "to_wall"
            rule["type"] = "clearance"
            relation = "to_wall"
        elif any(term in evidence_lower for term in passage_terms):
            rule["relation"] = "passage"
            rule["type"] = "passage"
            relation = "passage"
        elif any(term in evidence_lower for term in between_terms) and object_name not in {"room", "wall"}:
            rule["relation"] = "between"
            rule["type"] = "clearance"
            relation = "between"
        elif any(term in evidence_lower for term in around_terms):
            rule["relation"] = "around"
            relation = "around"
        if any(term in evidence_lower for term in front_terms):
            rule["direction"] = "front"
    forbidden_layout_phrases = (
        "masa yüzeyi", "masa yuzeyi", "60 x 40", "kase", "tencere", "çanak", "canak",
        "dimension lines showing",
    )
    if rule.get("type") in {"clearance", "passage"} and any(phrase in evidence_lower for phrase in forbidden_layout_phrases):
        return None
    subject_terms = {
        "table": ("table", "masa", "masanın"),
        "chair": ("chair", "sandalye", "oturan"),
    "bed": ("bed", "yatak"),
    "bedside table": ("bedside table", "nightstand", "komidin"),
        "desk": ("desk", "çalışma masası", "calisma masasi"),
        "sofa": ("sofa", "divan", "kanape"),
        "wardrobe": ("wardrobe", "dolap"),
        "window": ("window", "pencere"),
        "door": ("door", "kapı", "kapi"),
        "room": ("room", "oda", "mekan", "mekân"),
        "wall": ("wall", "duvar"),
    }
    if rule.get("type") != "placement" and rule["subject"] in subject_terms and not any(term in evidence_lower for term in subject_terms[rule["subject"]]):
        return None
    object_name = rule.get("object")
    if relation == "to_surrounding_area" or object_name.casefold() in {"surrounding area", "movement area"}:
        rule = dict(rule)
        rule["object"] = "room"
        rule["relation"] = "around"
        object_name = "room"
        relation = "around"
    movement_terms = ("free movement", "movement space", "walking", "passage", "serbest hareket", "hareket yüzeyi", "geçiş", "gecis")
    # The model sometimes labels an unnamed movement area as wall or as the table itself.
    # Preserve the useful measurement by normalizing it to a passage rule.
    if relation in {"to_wall", "around"} and any(term in evidence_lower for term in movement_terms):
        if not any(term in evidence_lower for term in ("wall", "duvar", "door", "kapı", "kapi", "window", "pencere")):
            rule = dict(rule)
            rule["object"] = "room"
            rule["relation"] = "passage"
            rule["type"] = "passage"
            object_name = "room"
            relation = "passage"
    relation_aliases = {
        "clearance": "around",
        "parallel": "parallel_to", "parallel_to": "parallel_to",
        "beside_both_sides": "beside", "next_to": "beside",
        "close_to": "near", "nearby": "near", "against_wall": "against",
    }
    relation = relation_aliases.get(str(relation).casefold().strip(), relation)
    rule["relation"] = relation
    allowed_relations = {"between", "to_wall", "to_window", "to_door", "to_bed", "passage", "swing", "around", "against", "beside", "near", "parallel_to", "placement", "dimension", "orientation", "above"}
    if relation not in allowed_relations:
        # VLMs often put the spatial relation in the relation field for a placement rule.
        if rule.get("type") == "placement" and rule.get("direction") in {"parallel", "on", "in", "beside", "near"}:
            rule["relation"] = "placement"
            relation = "placement"
        else:
            return None
    if relation == "around" and object_name == "room":
        rule["type"] = "clearance"
    if object_name == "room" and any(term in evidence_lower for term in ("per person", "each person", "her bir kişi", "her kişi")):
        rule["relation"] = "passage"
        rule["type"] = "passage"
        relation = "passage"
    relation_terms = {
        "to_wall": ("wall", "duvar"),
        "to_door": ("door", "kapı", "kapi"),
        "to_window": ("window", "pencere"),
        "passage": ("passage", "walking", "movement", "free space", "geçiş", "gecis", "hareket", "serbest", "yer", "yeri", "yer bırak", "yerin bırak", "gereklidir", "needed", "hareket alanı", "boşluk"),
    }
    if relation in relation_terms and not any(term in evidence_lower for term in relation_terms[relation]):
        return None
    if object_name.casefold() in {"wall", "duvar"} and relation == "to_door":
        return None
    if object_name.casefold() in {"door", "kapı", "kapi"} and relation == "to_wall":
        return None
    if object_name.casefold() in {"window", "pencere"} and relation in {"to_wall", "to_door"}:
        return None
    kind = rule["type"]
    numeric_fields = ("min_distance_m", "max_distance_m", "recommended_distance_m", "width_m", "depth_m", "height_m")
    if kind in {"clearance", "passage"} and not any(isinstance(rule.get(field), (int, float)) and rule[field] > 0 for field in numeric_fields):
        # A no-number spatial statement is still useful as a placement constraint.
        if any(term in evidence_lower for term in ("place", "yer olmalı", "yer olmal", "gerekir", "önemlidir", "parallel", "komidin")):
            rule["type"] = "placement"
            rule["relation"] = "placement"
            kind = "placement"
    if kind != "placement":
        if not re.search(r"\d", evidence) or rule.get("source_unit") not in {"mm", "cm", "m"}:
            return None
        source_value = rule.get("source_value")
        if not isinstance(source_value, str) or not re.search(r"\d", source_value):
            return None
        # Prevent accepting a rule whose evidence only mentions an unrelated figure number.
        source_numbers = re.findall(r"\d+(?:[.,]\d+)?", source_value)
        evidence_numbers = re.findall(r"\d+(?:[.,]\d+)?", evidence)
        normalize_number = lambda value: value.replace(",", ".").lstrip("0") or "0"
        if not any(normalize_number(number) in {normalize_number(value) for value in evidence_numbers} for number in source_numbers):
            return None
        fields = ("width_m", "depth_m", "height_m") if kind == "dimension" else ("min_distance_m", "max_distance_m", "recommended_distance_m")
        for field in fields:
            if rule.get(field) == 0.0:
                rule[field] = None
        if not any(isinstance(rule.get(field), (int, float)) and rule[field] > 0 for field in fields):
            return None
        if kind != "dimension":
            low, high = rule.get("min_distance_m"), rule.get("max_distance_m")
            if low is not None and high is not None and low > high:
                return None
    elif any(rule.get(field) is not None for field in ("min_distance_m", "max_distance_m", "width_m", "depth_m", "height_m", "source_value", "source_unit")):
        return None
    if kind == "placement":
        rule["constraint_type"] = "qualitative"
        priority = str(rule.get("priority") or "preferred").casefold().strip()
        rule["priority"] = priority if priority in {"hard", "soft", "preferred"} else "preferred"
    else:
        rule["constraint_type"] = "numeric"
        rule["priority"] = "hard"
    try:
        confidence_floor = 0.75 if kind == "placement" else 0.8
        if float(rule.get("confidence", 0)) < confidence_floor:
            return None
    except (TypeError, ValueError):
        return None
    return dict(rule)


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
    parser.add_argument("--max-images", type=int, default=MAX_IMAGES_PER_PAGE)
    parser.add_argument("--limit-pages", type=int)
    parser.add_argument("--residential-only", action="store_true")
    args = parser.parse_args()
    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY가 .env 또는 환경변수에 없습니다.")

    rows = [json.loads(line) for line in args.input_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("page_number") is not None:
            groups[int(row["page_number"])].append(row)
    page_numbers = sorted(groups)
    if args.residential_only:
        page_numbers = [page for page in page_numbers if page in RESIDENTIAL_PAGES]
    if args.limit_pages:
        page_numbers = page_numbers[: args.limit_pages]
    page_texts = load_page_texts(args.pages_jsonl)
    # Bound each API request so one stalled page cannot block the whole batch.
    client = OpenAI(timeout=60.0, max_retries=0)
    accepted: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    for index, page in enumerate(page_numbers, start=1):
        print(f"{index}/{len(page_numbers)} page {page}: preparing", flush=True)
        images: list[Path] = []
        numbers: list[Any] = []
        for row in groups[page][: args.max_images]:
            path = resolve_image(row.get("image_path", ""), args.input_jsonl, args.image_root)
            if path:
                images.append(path)
            numbers.extend(row.get("ocr_numbers") or [])
        if not images:
            review.append({"page_number": page, "reason": "no images found"})
            continue
        context = re.sub(r"\s+", " ", page_texts.get(page, "")).strip()[:MAX_PAGE_TEXT]
        user_text = PROMPT + f"\nPAGE NUMBER: {page}\nPAGE OCR TEXT:\n{context or '[none]'}\nOCR NUMBERS:\n{json.dumps(numbers, ensure_ascii=False)}"
        content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
        encoded_images = []
        for path in images:
            encoded = image_url(path, args.max_side)
            encoded_images.append((path, encoded))
            print(f"page {page}: image encoded {path.name} ({len(encoded) // 1024}KB)", flush=True)
        content.extend({"type": "image_url", "image_url": {"url": encoded, "detail": "high"}} for _, encoded in encoded_images)
        try:
            print(f"page {page}: API request started", flush=True)
            response = client.chat.completions.create(
                model=args.model,
                temperature=0,
                max_tokens=4000,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": content}],
            )
            print(f"page {page}: API response received", flush=True)
            result = json.loads(response.choices[0].message.content or '{"rules":[]}')
            raw_rules = result.get("rules", []) if isinstance(result.get("rules", []), list) else []
            rules = []
            rejected = []
            for raw in raw_rules:
                normalized = normalize(raw, args.residential_only)
                if normalized is None:
                    rejected.append(raw)
                else:
                    rules.append(normalized)
            for rule in rules:
                key = (page, rule["subject"], rule["object"], rule["relation"], rule["type"], rule.get("direction"), rule.get("min_distance_m"), rule.get("max_distance_m"), rule.get("width_m"), rule.get("depth_m"), rule.get("height_m"))
                if key not in seen:
                    seen.add(key)
                    accepted.append({"page_number": page, "image_paths": [str(path) for path in images], "parsed_rule": rule})
            if not rules:
                review.append({"page_number": page, "image_paths": [str(path) for path in images], "raw_result": result, "reason": "no validated page rule"})
            elif rejected:
                review.append({"page_number": page, "image_paths": [str(path) for path in images], "raw_result": {"rules": rejected}, "reason": "some rules rejected by validator"})
        except Exception as exc:
            review.append({"page_number": page, "image_paths": [str(path) for path in images], "reason": f"{type(exc).__name__}: {exc}"})
        print(f"{index}/{len(page_numbers)} pages accepted={len(accepted)} review={len(review)}", flush=True)

    for path, values in ((args.output_jsonl, accepted), (args.review_jsonl, review)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(value, ensure_ascii=False) for value in values) + ("\n" if values else ""), encoding="utf-8")
    print(f"완료: 규칙 {len(accepted)}개, 검토 페이지 {len(review)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
