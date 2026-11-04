"""Draw a guestbook card as a PNG that looks like the website's card.

Same layout as `.guest-card` on the website: the photo on top (or the lined
"V-O" square when there is none), then the mood stamp, nickname, message and
date. Sizes are the website's CSS pixels, drawn at SCALE for sharp text.
"""
from __future__ import annotations

import io
from datetime import date
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
SCALE = 2

MARGIN = 24  # page background around the card, so its border shows
CARD_WIDTH = 340
PHOTO_INSET = 12
TEXT_INSET = 20
PHOTO_RATIO = 194 / 305  # height / width of the website's photo box

PAGE = "#f3f5f8"
CARD = "#ffffff"
CARD_BORDER = "#e9edf3"
TEXT = "#303b4b"
DATE_TEXT = "#636f80"
ACCENT = "#b3c7e7"
MOOD_TEXT = "#7395c7"


@lru_cache(maxsize=None)
def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_DIR / f"Pretendard-{weight}.otf"), size * SCALE)


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """Break text into lines that fit `width`, at spaces when possible (Korean may break anywhere)."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split(" "):
            candidate = f"{line} {word}" if line else word
            if font.getlength(candidate) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            line = ""
            for char in word:
                if font.getlength(line + char) > width and line:
                    lines.append(line)
                    line = ""
                line += char
        lines.append(line)
    return lines


def _photo(photo: bytes, width: int, height: int) -> Image.Image:
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(photo))).convert("RGB")
    return ImageOps.fit(image, (width, height), Image.LANCZOS)


def _placeholder(width: int) -> Image.Image:
    """The website's lined square with "V-O", shown when a card has no photo."""
    image = Image.new("RGB", (width, width), CARD)
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rectangle((0, 0, width, width), fill=(179, 199, 231, 18))
    for y in range(25 * SCALE, width, 26 * SCALE):
        draw.rectangle((0, y, width, y + SCALE - 1), fill=(179, 199, 231, 38))
    font = _font("SemiBold", 30)
    draw.text((width / 2, width / 2), "V-O", font=font, fill=ACCENT, anchor="mm")
    return image


def render_card(nickname: str, message: str, mood: str | None, photo: bytes | None, written_on: date) -> bytes:
    """Return the card as PNG bytes. `photo` must already be a checked image file."""
    s = SCALE
    inner = (CARD_WIDTH - 2 * PHOTO_INSET) * s
    text_width = (CARD_WIDTH - 2 * TEXT_INSET) * s
    picture = _photo(photo, inner, round(inner * PHOTO_RATIO)) if photo else _placeholder(inner)

    name_font, body_font = _font("SemiBold", 20), _font("Regular", 17)
    mood_font, date_font = _font("Regular", 14), _font("Regular", 14)
    body_lines = _wrap(message, body_font, text_width)
    name_lines = _wrap(nickname, name_font, text_width)
    body_line, name_line = round(29.75 * s), 28 * s

    # Heights, top to bottom, in drawn pixels.
    mood_height = 30 * s if mood else 0
    height = (
        PHOTO_INSET * s + picture.height + 14 * s
        + (mood_height + 10 * s if mood else 0)
        + name_line * len(name_lines) + 8 * s
        + body_line * len(body_lines) + 18 * s
        + 22 * s + 20 * s
    )
    canvas = Image.new("RGB", (CARD_WIDTH * s + 2 * MARGIN * s, height + 2 * MARGIN * s), PAGE)
    draw = ImageDraw.Draw(canvas)
    left, top = MARGIN * s, MARGIN * s
    draw.rounded_rectangle(
        (left, top, left + CARD_WIDTH * s - 1, top + height - 1), radius=5 * s, fill=CARD, outline=CARD_BORDER, width=s
    )

    y = top + PHOTO_INSET * s
    canvas.paste(picture, (left + PHOTO_INSET * s, y))
    y += picture.height + 14 * s
    x = left + TEXT_INSET * s

    if mood:
        mood_width = round(mood_font.getlength(mood)) + 16 * s
        draw.rounded_rectangle((x, y, x + mood_width, y + mood_height), radius=4 * s, outline=ACCENT, width=s)
        draw.text((x + mood_width / 2, y + mood_height / 2), mood, font=mood_font, fill=MOOD_TEXT, anchor="mm")
        y += mood_height + 10 * s

    for line in name_lines:
        draw.text((x, y + name_line / 2), line, font=name_font, fill=TEXT, anchor="lm")
        y += name_line
    y += 8 * s
    for line in body_lines:
        draw.text((x, y + body_line / 2), line, font=body_font, fill=TEXT, anchor="lm")
        y += body_line
    y += 18 * s
    draw.text((x, y + 11 * s), written_on.strftime("%Y. %m. %d."), font=date_font, fill=DATE_TEXT, anchor="lm")

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()
