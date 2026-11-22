"""Readable adaptive Cozmo face at its native 128x32 monochrome resolution.

Speech owns the left panel and is sized per message/page. The separate right
panel shows actual measured voltage and an *estimated* charge bar. Physical
OLED scanline interlacing belongs to Cozmo firmware, not to this bitmap.
"""
from __future__ import annotations

import math
from functools import lru_cache
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 128, 32
PANEL_LEFT = 91
TEXT_WIDTH = PANEL_LEFT - 4
TEXT_HEIGHT = HEIGHT - 2

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


@lru_cache(maxsize=64)
def _font(size, choices):
    for name in choices:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, ValueError):
            continue
    # Pillow's scalable built-in font works even if Fedora has no TTFs.
    # Older versions fall back to the native-size bitmap, never downsampled.
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _width(draw, value, font):
    box = draw.textbbox((0, 0), value, font=font)
    return box[2] - box[0]


def _height(draw, font):
    box = draw.textbbox((0, 0), "Agyp", font=font)
    return max(1, box[3] - box[1])


def _wrap(message, draw, font, width):
    """Wrap by measured pixels and split long words without elision."""
    words = message.split()
    lines = []
    line = ""
    for word in words:
        candidate = (line + " " + word).strip()
        if _width(draw, candidate, font) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
            line = ""
        while word and _width(draw, word, font) > width:
            take = 1
            while (take < len(word) and
                   _width(draw, word[:take + 1], font) <= width):
                take += 1
            lines.append(word[:take])
            word = word[take:]
        line = word
    if line:
        lines.append(line)
    return lines or ["NO MESSAGE"]


def _layout(message, draw):
    """Fit the entire message as large as possible, or paginate at 15px.

    Short TTS can fill a line at 25px. Multi-line text uses the largest
    font that fits in 30px. Long TTS stays readable instead of shrinking
    the entire message into microscopic type.
    """
    for size in range(25, 13, -1):
        font = _font(size, _FONT_CHOICES)
        step = _height(draw, font) + 2
        lines = _wrap(message, draw, font, TEXT_WIDTH)
        if len(lines) * step - 2 <= TEXT_HEIGHT:
            return font, [lines], step
    font = _font(15, _FONT_CHOICES)
    step = _height(draw, font) + 2
    per_page = max(1, (TEXT_HEIGHT + 2) // step)
    lines = _wrap(message, draw, font, TEXT_WIDTH)
    return font, [lines[i:i + per_page] for i in range(0, len(lines), per_page)], step


def _battery_panel(draw, voltage, percent, charging):
    # Fixed separator and battery panel; speech never overlaps voltage.
    draw.line((PANEL_LEFT, 1, PANEL_LEFT, HEIGHT - 2), fill=1)
    try:
        v = float(voltage)
        valid = math.isfinite(v) and 2.7 <= v <= 4.4
    except (ValueError, TypeError, OverflowError):
        valid = False
        v = 0.0
    label = f"{v:.2f}V" if valid else "--.--"
    font = _font(10, _VOLTAGE_CHOICES)
    box = draw.textbbox((0, 0), label, font=font)
    tw = box[2] - box[0]
    draw.text((PANEL_LEFT + 2 + max(0, (33 - tw) // 2), 2 - box[1]),
              label, font=font, fill=1)

    # Battery outline, terminal and four distinct charge segments.
    x, y, w, h = PANEL_LEFT + 4, 19, 27, 10
    draw.rectangle((x, y, x + w, y + h), outline=1)
    draw.rectangle((x + w + 1, y + 3, x + w + 3, y + 7), fill=1)
    try:
        soc = float(percent)
        valid_soc = math.isfinite(soc)
    except (TypeError, ValueError, OverflowError):
        valid_soc = False
        soc = 0.0
    if valid_soc:
        count = min(4, max(0, math.ceil(max(0.0, min(100.0, soc)) / 25)))
        for i in range(count):
            x0 = x + 2 + i * 6
            draw.rectangle((x0, y + 2, x0 + 4, y + h - 2), fill=1)
    if charging:
        # Lightning-shaped cutout distinguishes actively charging.
        draw.line((x + 12, y + 1, x + 9, y + 6, x + 14, y + 6,
                   x + 11, y + 9), fill=0, width=1)


def status_image(*, voltage=None, percent=None, charging=False, last_tts="",
                 scroll=0, **_unused):
    """Render a dynamically fitted speech page and separate battery gauge."""
    image = Image.new("1", (WIDTH, HEIGHT), 0)
    draw = ImageDraw.Draw(image)
    message = " ".join(str(last_tts or "").split()) or "NO MESSAGE"
    font, pages, step = _layout(message, draw)
    lines = pages[int(scroll) % len(pages)]
    used = len(lines) * step - 2
    y = max(0, (HEIGHT - used) // 2)
    for line in lines:
        bounds = draw.textbbox((0, 0), line, font=font)
        tw = bounds[2] - bounds[0]
        x = max(1, (PANEL_LEFT - tw) // 2)
        draw.text((x - bounds[0], y - bounds[1]), line, font=font, fill=1)
        y += step
    _battery_panel(draw, voltage, percent, bool(charging))
    return image


def compact_status_image(*, voltage=None, percent=None, charging=False,
                         last_tts="", scroll=0, battery_only=False,
                         message_limit=14, **_unused):
    """Lower-entropy fallback when encoded frame exceeds one transport packet."""
    if battery_only:
        last_tts = ""
    elif message_limit:
        last_tts = " ".join(str(last_tts or "").split())[:message_limit]
    return status_image(voltage=voltage, percent=percent, charging=charging,
                        last_tts=last_tts, scroll=scroll)
