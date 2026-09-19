"""Extract numeric furniture-clearance rules with a local Ollama VLM."""

from __future__ import annotations

import argparse
import base64
import json
import re
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from PIL import Image


ALLOWED_TARGETS = {
    "table",
    "chair",
    "bed",
    "desk",
    "sofa",
    "wardrobe",
    "window",
    "door",
    "room",
}

CLASSIFY_PROMPT = """Decide whether this image and supplementary OCR describe at least one
reliable numerical furniture-layout clearance rule that can be used to place furniture.
Return JSON only in this exact format:
{"has_clearance":true,"reason":"short reason"}

Return true only when a visible number represents free space, passage width, access space,
or distance between furniture and another object/wall/door/window.
Return false for furniture dimensions, table diameter, seat surface or area per person,
room capacity, labels, page numbers, and facility-specific measurements. Do not guess.
If uncertain, return false.
"""

PROMPT = """Analyze this architectural furniture diagram together with the supplementary OCR text.
Return JSON only. The output schema is:
{"rules":[{"target":"chair","type":"clearance","direction":"front",
"min_distance_m":0.0,"max_distance_m":null,
"confidence":0.0,"evidence":"short description including the exact visible number"}]}

The target field MUST be exactly one of these object names:
table, chair, bed, desk, sofa, wardrobe, window, door, room.
Never use a rule description or category as target. Invalid targets include:
furniture-to-furniture clearance, passage width, furniture-to-wall distance,
space needed for use or movement, and clearance.

Extract ONLY actionable numerical clearance rules:
- furniture-to-furniture clearance
- passage width
- furniture-to-wall, door, or window distance
- space needed for use or movement

Rules:
- Use only numbers visibly shown in the image.
- Use OCR text only as supporting context; the image is the final authority.
- Convert cm to meters.
- At least one of min_distance_m or max_distance_m must be numeric.
- Exclude furniture height, width, depth, and the door's own dimensions.
- Exclude seating surface/seat width, furniture dimensions, room capacity, and area per person.
- Exclude page numbers, labels, and non-numerical descriptions.
- Each rule must be tied to a distinct visible measurement and the named target.
- Do not reuse one measurement for multiple targets unless the diagram explicitly shows that relationship.
- Return at most one rule per distinct target/measurement relationship.
- If the target object or the clearance relationship cannot be identified, return no rule.
- Do not copy the schema example, placeholder values, or the phrase "short description" into the output.
- A value of 0.0 or confidence 0.0 is never a valid extracted rule; return {"rules":[]} instead.
- If the page contains only furniture dimensions (width, depth, height) and no free-space measurement, return {"rules":[]}.
- Never guess or estimate a value.
- If no reliable numerical clearance is visible, return {"rules":[]}.
"""

MAX_PAGE_TEXT_CHARS = 3500


def parse_json(text: str) -> dict[str, Any] | None:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def resolve_image(raw_path: str, input_path: Path, image_root: Path | None) -> Path | None:
    source = Path(raw_path)
    candidates = [source]
    if image_root:
        candidates.append(image_root / source.name)
    candidates.append(input_path.parent / source.name)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def load_page_texts(path: Path | None) -> dict[int, str]:
    if path is None:
        return {}
    page_texts: dict[int, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        page_number = row.get("page_number")
        text = row.get("text") or row.get("content") or ""
        if page_number is not None and text:
            page_texts[int(page_number)] = str(text)
    return page_texts


def supplementary_context(page_text: str, ocr_numbers: Any) -> str:
    text = re.sub(r"\s+", " ", page_text).strip()
    if len(text) > MAX_PAGE_TEXT_CHARS:
        text = text[:MAX_PAGE_TEXT_CHARS] + "..."
    numbers = json.dumps(ocr_numbers or [], ensure_ascii=False)
    return (
        "\n\nSupplementary OCR context (possibly noisy; do not invent facts from it):\n"
        f"{text or '[no page OCR text available]'}\n"
        "OCR numbers near this crop:\n"
        f"{numbers}"
    )


def image_as_base64(path: Path, max_side: int) -> str:
    image = Image.open(path).convert("RGB")
    scale = min(1.0, max_side / max(image.size))
    if scale < 1.0:
        image = image.resize((int(image.width * scale), int(image.height * scale)))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=88, optimize=True)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def ask_ollama(
    url: str,
    model: str,
    image_b64: str,
    num_ctx: int,
    instruction: str,
    context: str,
) -> dict[str, Any] | None:
    response = requests.post(
        f"{url.rstrip('/')}/api/chat",
        json={
            "model": model,
            "stream": False,
            "format": "json",
            "options": {"num_ctx": num_ctx, "temperature": 0},
            "messages": [{
                "role": "user",
                "content": instruction + context,
                "images": [image_b64],
            }],
        },
        timeout=300,
    )
    if not response.ok:
        raise RuntimeError(f"Ollama request failed ({response.status_code}): {response.text[:500]}")
    payload = response.json()
    return parse_json(payload.get("message", {}).get("content", ""))


def normalize_rule(rule: Any) -> dict[str, Any] | None:
    if not isinstance(rule, dict):
        return None
    if rule.get("target") not in ALLOWED_TARGETS or rule.get("type") != "clearance":
        return None
    evidence = rule.get("evidence")
    if not isinstance(evidence, str) or not re.search(r"\d", evidence):
        return None
    evidence_lower = evidence.casefold()
    forbidden_evidence = (
        "short description",
        "seating surface",
        "seat surface",
        "per person",
        "oturma yüzeyi",
        "her öğrenci",
    )
    if any(phrase in evidence_lower for phrase in forbidden_evidence):
        return None
    normalized = dict(rule)
    # Small VLMs often use 0.0 as a placeholder for an unknown bound.
    # Treat it as missing instead of accepting it as a real distance.
    for field in ("min_distance_m", "max_distance_m"):
        if normalized.get(field) == 0.0:
            normalized[field] = None
    minimum = normalized.get("min_distance_m")
    maximum = normalized.get("max_distance_m")
    if minimum is None and maximum is None:
        return None
    if minimum is not None and (not isinstance(minimum, (int, float)) or minimum <= 0):
        return None
    if maximum is not None and (not isinstance(maximum, (int, float)) or maximum <= 0):
        return None
    if minimum is not None and maximum is not None and minimum > maximum:
        return None
    try:
        if float(normalized.get("confidence", 0)) < 0.80:
            return None
    except (TypeError, ValueError):
        return None
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_jsonl", type=Path)
    parser.add_argument("output_jsonl", type=Path)
    parser.add_argument("--review-jsonl", type=Path, required=True)
    parser.add_argument("--pages-jsonl", type=Path)
    parser.add_argument("--image-root", type=Path)
    parser.add_argument("--model", default="qwen2.5vl:3b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--num-ctx", type=int, default=4096)
    parser.add_argument("--max-side", type=int, default=1024)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()

    rows = [
        json.loads(line)
        for line in args.input_jsonl.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit:
        rows = rows[: args.limit]

    page_texts = load_page_texts(args.pages_jsonl)

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
            context = supplementary_context(
                page_texts.get(int(row["page_number"]), "")
                if row.get("page_number") is not None
                else "",
                row.get("ocr_numbers", []),
            )
            image_b64 = image_as_base64(image_path, args.max_side)

            classification = ask_ollama(
                args.ollama_url,
                args.model,
                image_b64,
                args.num_ctx,
                CLASSIFY_PROMPT,
                context,
            ) or {"has_clearance": False}

            if classification.get("has_clearance") is not True:
                review.append({
                    **base,
                    "raw_result": classification,
                    "reason": "no visual clearance candidate",
                })
                continue

            result = ask_ollama(
                args.ollama_url,
                args.model,
                image_b64,
                args.num_ctx,
                PROMPT,
                context,
            ) or {"rules": []}
            valid_rules = [
                normalized
                for rule in result.get("rules", [])
                if (normalized := normalize_rule(rule)) is not None
            ]
            if not valid_rules:
                review.append({**base, "raw_result": result, "reason": "no actionable numerical clearance rule"})
            for rule in valid_rules:
                key = (
                    row.get("page_number"),
                    rule.get("target"),
                    rule.get("direction"),
                    rule.get("min_distance_m"),
                    rule.get("max_distance_m"),
                )
                if key not in seen:
                    seen.add(key)
                    accepted.append({**base, "parsed_rule": rule})
        except Exception as exc:  # keep the batch running when one image fails
            review.append({**base, "reason": f"{type(exc).__name__}: {exc}"})
        if index % 10 == 0 or index == len(rows):
            print(f"{index}/{len(rows)} accepted={len(accepted)} review={len(review)}", flush=True)

    for path, values in ((args.output_jsonl, accepted), (args.review_jsonl, review)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(value, ensure_ascii=False) for value in values) + ("\n" if values else ""),
            encoding="utf-8",
        )
    print(f"완료: 자동 승인 {len(accepted)}개, 검수 필요 {len(review)}개")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
