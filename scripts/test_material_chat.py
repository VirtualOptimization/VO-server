#!/usr/bin/env python3
"""Generate an AI material preview locally without starting FastAPI or using S3."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from server.services.material_chat_service import (
    generate_material_preview_bytes,
    interpret_material_request,
)


def _extension_for(content_type: str) -> str:
    if content_type == "image/jpeg":
        return ".jpg"
    if content_type == "image/webp":
        return ".webp"
    return ".png"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("message", help="예: 따뜻한 월넛 나무결 텍스처")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("/Users/kwon-yewon/Desktop/Duksung/2026/AJA/ai_texture_png"),
        help="생성된 이미지와 JSON을 저장할 경로",
    )
    args = parser.parse_args()

    interpretation = await interpret_material_request(args.message)
    image_bytes, content_type = await generate_material_preview_bytes(interpretation)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    image_path = args.output_dir / f"preview{_extension_for(content_type)}"
    metadata_path = args.output_dir / "preview.json"
    image_path.write_bytes(image_bytes)
    metadata_path.write_text(
        json.dumps(
            {
                "request": args.message,
                "material_type": interpretation.material_type,
                "material_name": interpretation.material_name,
                "image_prompt": interpretation.image_prompt,
                "assistant_message": interpretation.assistant_message,
                "content_type": content_type,
                "image_path": str(image_path),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Preview: {image_path}")
    print(f"Metadata: {metadata_path}")
    print(interpretation.assistant_message)


if __name__ == "__main__":
    asyncio.run(main())
