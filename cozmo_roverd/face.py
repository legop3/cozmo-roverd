"""Large wrapped speech text and a compact voltage label on Cozmo's 128x32 face.

The firmware handles its physical alternating OLED scanlines. Render at the
native 1-bit protocol resolution without scaling, smoothing, or row duplication.
"""
from __future__ import annotations

import math
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 128, 32

_FONT_CHOICES = (
    "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
)
_VOLTAGE_CHOICES = (
    "DejaVuSansMono.ttf", "DejaVuSans.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)


def _font(size, choices):
    for name in choices:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, ValueError):
            continue
    # Headless installations may lack system fonts; never downsample a
    # fallback bitmap font (that previously produced near-invisible text).
    return ImageFont.load_default()


_MESSAGE_FONT = _font(12, _FONT_CHOICES)
_VOLTAGE_FONT = _font(9, _VOLTAGE_CHOICES)
_LINE_HEIGHT = 11
_LINES_PER_PAGE = 3


def _width(draw, value, font):
    box = draw.textbbox((0, 0), value, font=font)
    return box[2] - box[0]


def _pages(message, draw, widths):
    """Pixel-measured word wrap, splitting even words wider than a line."""
    words = message.split()
    pages, lines, line = [], [], ""
    while words:
        word = words.pop(0)
        limit = widths[len(lines)]
        candidate = (line + " " + word).strip()
        if _width(draw, candidate, _MESSAGE_FONT) <= limit:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ""
        else:
            # A word wider than the available row must be split, not clipped.
            count = 1
            while (count < len(word) and
                   _width(draw, word[:count + 1], _MESSAGE_FONT) <= limit):
                count += 1
            lines.append(word[:count])
            if count < len(word):
                words.insert(0, word[count:])
        if len(lines) == _LINES_PER_PAGE:
            pages.append(lines)
            lines = []
    if line:
        lines.append(line)
    if lines:
        pages.append(lines)
    return pages or [["NO TTS YET"]]


def status_image(*, voltage=None, last_tts="", scroll=0, **_unused):
    """Show three large wrapped TTS lines with voltage tucked in top-right."""
    try:
        val = float(voltage)
        voltage_text = f"{val:.2f}V" if math.isfinite(val) else "--.--V"
    except (ValueError, TypeError, OverflowError):
        voltage_text = "--.--V"

    message = " ".join(str(last_tts or "").split()) or "NO TTS YET"
    image = Image.new("1", (WIDTH, HEIGHT), 0)
    draw = ImageDraw.Draw(image)
    voltage_width = _width(draw, voltage_text, _VOLTAGE_FONT)
    first_line_width = max(32, WIDTH - voltage_width - 5)
    pages = _pages(message, draw, (first_line_width, WIDTH, WIDTH))
    lines = pages[int(scroll) % len(pages)]
    # Draw text at its glyph bounds, rather than allowing font ascenders to
    # consume blank protocol rows. Rows 0, 11, 22 share almost all the face.
    for index, line in enumerate(lines):
        box = draw.textbbox((0, 0), line, font=_MESSAGE_FONT)
        draw.text((0, index * _LINE_HEIGHT - box[1]), line,
                  font=_MESSAGE_FONT, fill=1)
    # A small overlay in otherwise unused top-right space, not an entire row.
    box = draw.textbbox((0, 0), voltage_text, font=_VOLTAGE_FONT)
    draw.text((WIDTH - voltage_width, -box[1]), voltage_text,
              font=_VOLTAGE_FONT, fill=1)
    return image


def compact_status_image(*, voltage=None, last_tts="", scroll=0,
                         battery_only=False, message_limit=14, **_unused):
    """Compact transport fallback, keeping a short readable speech excerpt."""
    if battery_only:
        last_tts = ""
    elif message_limit:
        msg = " ".join(str(last_tts or "").split())
        if len(msg) > message_limit:
            last_tts = msg[:message_limit]
    return status_image(voltage=voltage, last_tts=last_tts, scroll=0)
