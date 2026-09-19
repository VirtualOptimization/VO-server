"""Extract structured furniture-layout rules from crop images with OpenAI Vision."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
from io import BytesIO
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image


ALLOWED_TARGETS = {"table", "chair", "bed", "desk", "sofa", "wardrobe", "window", "door", "room"}
ALLOWED_TYPES = {"clearance", "passage", "door_swing", "placement", "dimension"}
ALLOWED_DIRECTIONS = {"front", "back", "side", "around", "left", "right", "none"}
MAX_PAGE_TEXT_CHARS = 3500

PROMPT = """Analyze the architectural diagram image together with the supplementary OCR text.
Return JSON only in this format:
{"rules":[{"target":"chair","type":"clearance","direction":"front",
"min_distance_m":0.75,"max_distance_m":null,"width_m":null,"depth_m":null,
"height_m":null,"source_value":"75","source_unit":"cm","confidence":0.9,
"evidence":"exact visible number and its explicit relation"}]}

Classify each reliable rule as one of:
- clearance: distance from named furniture to another object, wall, door, or window
- passage: walking or access width needed around furniture or through a door
- door_swing: space or direction required for a door to open
- placement: explicit placement condition such as against a wall or beside a window
- dimension: actual furniture width, depth, or height

The target must be exactly one of: table, chair, bed, desk, sofa, wardrobe, window, door, room.
For clearance/passage/door_swing use direction front, back, side, around, left, or right.
For placement or dimension use direction "none".
Never use category phrases as target. Never output placeholders or the schema example.
Use only measurements visibly shown in the image; OCR is supporting context and may be noisy.
Each numerical rule must cite one distinct visible measurement in evidence and repeat it in
source_value/source_unit. Convert cm and mm to meters.
For clearance, passage, and door_swing, use distance fields. For dimension, use only
width_m/depth_m/height_m and leave distance fields null. Placement may be non-numerical
only when explicitly stated in the text or diagram.
Exclude page numbers, labels, room capacity, area per person, table diameter, seat surface,
and facility-specific requirements that cannot be mapped to furniture layout. Never turn an
object’s own dimensions into a clearance rule. Do not use room-wide dimensions or guesses.
Return at most three rules per image. If target, relation, unit, or meaning is uncertain,
return {"rules":[]}. Do not use likely, probably, may indicate, or indicating.
"""


def load_page_texts(path: Path | None) -> dict[int, str]:
    if path is None:
        return {}
    result: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        number = row.get("page_number")
        text = row.get("text") or row.get("content") or ""
        if number is not None:
            result[int(number)] = str(text)
    return result


def resolve_image(raw_path: str, input_path: Path, image_root: Path | None) -> Path | None:
    source = Path(raw_path)
    candidates = [source]
    if image_root:
        candidates.append(image_root / source.name)
    candidates.append(input_path.parent / source.name)
    return next((path for path in candidates if path.exists()), None)


def image_data_url(path: Path, max_side: int) -> str:
    image = Image.open(path).convert("RGB")
    scale = min(1.0, max_side / max(image.size))
    if scale < 1.0:
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=88, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def context_text(page_text: str, ocr_numbers: Any) -> str:
    text = re.sub(r"\s+", " ", page_text).strip()[:MAX_PAGE_TEXT_CHARS]
    return (
        "\n\nSupplementary OCR text (not authoritative):\n"
        f"{text or '[none]'}\nOCR numbers near crop:\n"
        f"{json.dumps(ocr_numbers or [], ensure_ascii=False)}"
    )


def extract(client: OpenAI, model: str, image_url: str, context: str) -> dict[str, Any]:
    response = client.chat.completions.create(
        model=model,
        temperature=0,
        max_tokens=1200,
        response_format={"type": "json_object"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT + context},
                {"type": "image_url", "image_url": {"url": image_url, "detail": "high"}},
            ],
        }],
    )
    return json.loads(response.choices[0].message.content or '{"rules":[]}')


def normalize_rule(rule: Any) -> dict[str, Any] | None:
    if not isinstance(rule, dict) or rule.get("target") not in ALLOWED_TARGETS:
        return None
    rule_type = rule.get("type")
    if rule_type not in ALLOWED_TYPES:
        return None
    direction = rule.get("direction", "none")
    if direction not in ALLOWED_DIRECTIONS:
        return None
    evidence = rule.get("evidence")
    if not isinstance(evidence, str) or (rule_type != "placement" and not re.search(r"\d", evidence)):
        return None
    evidence_lower = evidence.casefold()
    if "short description" in evidence_lower:
        return None
    if any(word in evidence_lower for word in ("likely", "probably", "may indicate", "indicating", "possibly")):
        return None
    normalized = dict(rule)
    if rule_type == "placement":
        if not evidence.strip() or direction != "none":
            return None
    else:
        source_value = rule.get("source_value")
        source_unit = rule.get("source_unit")
        if not isinstance(source_value, str) or not re.search(r"\d", source_value):
            return None
        if source_unit not in {"mm", "cm", "m"}:
            return None
        if rule_type == "dimension":
            fields = ("width_m", "depth_m", "height_m")
        else:
            fields = ("min_distance_m", "max_distance_m")
        for field in fields:
            if normalized.get(field) == 0.0:
                normalized[field] = None
        if not any(isinstance(normalized.get(field), (int, float)) and normalized[field] > 0 for field in fields):
            return None
        if rule_type != "dimension":
            minimum, maximum = normalized.get("min_distance_m"), normalized.get("max_distance_m")
            if minimum is not None and maximum is not None and minimum > maximum:
                return None
        if rule_type == "dimension" and not any(
            isinstance(normalized.get(field), (int, float)) and normalized[field] > 0
            for field in fields
        ):
            return None
    try:
        if float(normalized.get("confidence", 0)) < 0.80:
            return None
    except (TypeError, ValueError):
        return None
    return normalized


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("input_jsonl", type=Path)
    parser.add_argument("output_jsonl", type=Path)
    parser.add_argument("--review-jsonl", type=Path, required=True)
    parser.add_argument("--image-root", type=Path)
    parser.add_argument("--pages-jsonl", type=Path)
    parser.add_argument("--model", default=os.getenv("OPENAI_VISION_MODEL", "gpt-4.1-mini"))
    parser.add_argument("--max-side", type=int, default=768)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY가 .env 또는 환경변수에 없습니다.")
    client = OpenAI()
    page_texts = load_page_texts(args.pages_jsonl)
    rows = [json.loads(line) for line in args.input_jsonl.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        rows = rows[:args.limit]

    accepted: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    for index, row in enumerate(rows, start=1):
        image_path = resolve_image(row.get("image_path", ""), args.input_jsonl, args.image_root)
        base = {"page_number": row.get("page_number"), "image_path": str(image_path or "")}
        if image_path is None:
            review.append({**base, "reason": "image not found"})
            continue
        try:
            page_text = page_texts.get(int(row["page_number"]), "") if row.get("page_number") is not None else ""
            result = extract(client, args.model, image_data_url(image_path, args.max_side), context_text(page_text, row.get("ocr_numbers")))
            rules = [normalized for rule in result.get("rules", []) if (normalized := normalize_rule(rule)) is not None][:3]
            if not rules:
                review.append({**base, "raw_result": result, "reason": "no actionable numerical clearance rule"})
            for rule in rules:
                key = (
                    row.get("page_number"), rule.get("target"), rule.get("type"),
                    rule.get("direction"), rule.get("min_distance_m"),
                    rule.get("max_distance_m"), rule.get("width_m"),
                    rule.get("depth_m"), rule.get("height_m"),
                )
                if key not in seen:
                    seen.add(key)
                    accepted.append({**base, "parsed_rule": rule})
        except Exception as exc:
            review.append({**base, "reason": f"{type(exc).__name__}: {exc}"})
        if index % 10 == 0 or index == len(rows):
            print(f"{index}/{len(rows)} accepted={len(accepted)} review={len(review)}", flush=True)

    for path, values in ((args.output_jsonl, accepted), (args.review_jsonl, review)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(value, ensure_ascii=False) for value in values) + ("\n" if values else ""), encoding="utf-8")
    print(f"완료: 자동 승인 {len(accepted)}개, 검수 필요 {len(review)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
