"""Kodi posters (2:3): the thumbnail over a blurred copy of itself, plus a white title card with a
YouTube badge and the channel name, so tiles are recognisable even when the thumbnail has no text."""
from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

POSTER_SIZE = (1000, 1500)  # Kodi's recommended poster size (2:3)
_DESIGN = (680, 1020)       # layout units below are for this size; drawn at 2x and scaled to POSTER_SIZE,
_SCALE = 2                  # which also anti-aliases the rounded corners and the logo
_MARGIN = 28
_RADIUS = 22
_PADDING = 26
_MAX_LINES = 4
_TITLE_SIZES = range(58, 35, -2)
_YOUTUBE_RED = (255, 0, 0)
# Emoji and similar symbols are missing from regular fonts and would render as empty boxes.
_UNDRAWABLE_RE = re.compile("[\U00010000-\U0010FFFF☀-➿︀-️‍]")


_FONT_CANDIDATES = {
    True: ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
           "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf"),
    False: ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf",
            "/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf"),
}


@functools.lru_cache(maxsize=None)
def _font_file(bold: bool) -> str | None:
    """A system sans-serif font: fontconfig on Linux, known paths on Windows/macOS."""
    if sys.platform not in ("win32", "darwin"):
        try:
            res = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold" if bold else "sans"],
                                 capture_output=True, text=True, timeout=10)
            if res.stdout.strip():
                return res.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return next((path for path in _FONT_CANDIDATES[bold] if os.path.exists(path)), None)


@functools.lru_cache(maxsize=None)
def _font(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    path = _font_file(bold)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", _UNDRAWABLE_RE.sub("", text or "")).strip()


def wrap(text: str, font: ImageFont.FreeTypeFont, width: int, max_lines: int) -> tuple[list[str], bool]:
    """Greedy word wrap. Returns (lines, fits); overflowing text ends the last line with '…'."""
    draw = ImageDraw.Draw(Image.new("L", (1, 1)))
    fits_width = lambda s: draw.textlength(s, font=font) <= width  # noqa: E731
    lines: list[str] = []
    for word in text.split(" "):
        candidate = f"{lines[-1]} {word}" if lines else word
        if lines and fits_width(candidate):
            lines[-1] = candidate
            continue
        while not fits_width(word):  # a single word wider than the card: break it
            cut = len(word)
            while cut > 1 and not fits_width(word[:cut]):
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        lines.append(word)
    if len(lines) <= max_lines:
        return lines, True
    last = lines[max_lines - 1]
    while last and not fits_width(last + "…"):
        last = last[:-1].rstrip()
    return lines[:max_lines - 1] + [last + "…"], False


def _rounded(image: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, *image.size), radius, fill=255)
    out = image.convert("RGBA")
    out.putalpha(mask)
    return out


def _background(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """The image zoomed to cover the poster, blurred and darkened (blurred small, then enlarged)."""
    w, h = size
    scale = max(w / image.width, h / image.height)
    cover = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.BILINEAR)
    left, top = (cover.width - w) // 2, (cover.height - h) // 2
    cover = cover.crop((left, top, left + w, top + h))
    small = cover.resize((max(w // 10, 1), max(h // 10, 1)), Image.Resampling.BILINEAR)
    blurred = small.filter(ImageFilter.GaussianBlur(3)).resize(size, Image.Resampling.BICUBIC)
    return ImageEnhance.Brightness(blurred).enhance(0.7).convert("RGBA")


def _youtube_logo(height: int) -> Image.Image:
    width = round(height * 1.42)
    logo = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(logo)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), round(height * 0.28), fill=_YOUTUBE_RED)
    draw.polygon([(width * 0.40, height * 0.29), (width * 0.40, height * 0.71), (width * 0.67, height * 0.50)],
                 fill="white")
    return logo


def _title_card(title: str, channel: str, width: int) -> Image.Image:
    s = _SCALE
    pad, inner = _PADDING * s, width - 2 * _PADDING * s
    logo = _youtube_logo(40 * s)
    channel_font = _font(28 * s, bold=False)
    channel_lines, _ = wrap(clean_text(channel) or "YouTube", channel_font, inner - logo.width - 14 * s, 1)

    for size in _TITLE_SIZES:
        title_font = _font(size * s, bold=True)
        lines, fits = wrap(clean_text(title), title_font, inner, _MAX_LINES)
        if fits:
            break
    ascent, descent = title_font.getmetrics()
    line_height = round((ascent + descent) * 1.08)
    height = pad + logo.height + 18 * s + line_height * len(lines) + pad - (line_height - ascent - descent)

    card = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), _RADIUS * s, fill="white")
    card.alpha_composite(logo, (pad, pad))
    draw.text((pad + logo.width + 14 * s, pad + logo.height // 2), channel_lines[0], font=channel_font,
              fill=(96, 96, 96), anchor="lm")
    y = pad + logo.height + 18 * s
    for line in lines:
        draw.text((pad, y), line, font=title_font, fill=(17, 17, 17))
        y += line_height
    return card


def make_poster(src: Path, dst: Path, title: str, channel: str = "") -> bool:
    """Writes a 1000x1500 poster for src (any image) with a title card. Returns False on failure."""
    try:
        with Image.open(src) as im:
            image = im.convert("RGB")
    except (OSError, ValueError):
        return False
    s = _SCALE
    size = (_DESIGN[0] * s, _DESIGN[1] * s)
    margin, inner_w = _MARGIN * s, size[0] - 2 * _MARGIN * s
    canvas = _background(image, size)
    card = _title_card(title, channel, inner_w)

    room = size[1] - card.height - 3 * margin  # height left for the thumbnail
    scale = min(inner_w / image.width, room / image.height)
    thumb = _rounded(image.resize((round(image.width * scale), round(image.height * scale)),
                                  Image.Resampling.LANCZOS), _RADIUS * s)
    top = (size[1] - (thumb.height + margin + card.height)) // 2

    shadow = Image.new("RGBA", size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    for box in ((thumb, (size[0] - thumb.width) // 2, top), (card, margin, top + thumb.height + margin)):
        img, x, y = box
        shadow_draw.rounded_rectangle((x, y + 8 * s, x + img.width, y + img.height + 8 * s), _RADIUS * s,
                                      fill=(0, 0, 0, 140))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(12 * s)))
    canvas.alpha_composite(thumb, ((size[0] - thumb.width) // 2, top))
    canvas.alpha_composite(card, (margin, top + thumb.height + margin))

    canvas.convert("RGB").resize(POSTER_SIZE, Image.Resampling.LANCZOS).save(dst, "JPEG", quality=90)
    return True
