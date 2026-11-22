"""Battery voltage and latest TTS on Cozmo's interlaced OLED.

PyCozmo accepts a 128x32 1-bit image, but Cozmo paints those 32 rows onto
alternating physical rows of its 128x64 OLED. A 15-pixel-high font therefore
looks roughly 30 physical pixels tall. Render small, crisp glyphs directly at
the protocol resolution; never scale or downsample this bitmap.
"""
from __future__ import annotations

import math
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT = 128, 32

_FONT_CHOICES = (
    "DejaVuSansMono.ttf",
    "DejaVuSans.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/liberation-mono/LiberationMono-Regular.ttf",
)


def _font(size):
    for name in _FONT_CHOICES:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, ValueError):
            continue
    # Pillow's built-in bitmap font is ~8 pixels high. That is fine for a
    # native 128x32 image; it must NEVER be downsampled from 4x afterward.
    return ImageFont.load_default()


# Keep text about 7-8 protocol pixels tall (14-16 physical rows).
# Older 15px/11px fonts made the two lines visually oversized on the OLED.
_VOLTAGE_FONT = _font(11)
_MESSAGE_FONT = _font(10)


def _strip(text, font, width, height, scroll=0):
    im = Image.new("1", (width, height), 0)
    d = ImageDraw.Draw(im)
    bb = d.textbbox((0, 0), text, font=font)
    txt_width = max(1, bb[2] - bb[0])
    txt_height = max(1, bb[3] - bb[1])
    y = (height - txt_height) // 2 - bb[1]
    if txt_width <= width:
        d.text(((width - txt_width) // 2, y), text, fill=1, font=font)
    else:
        gap = 16
        period = txt_width + gap
        track = Image.new("1", (period * 2 + width, height), 0)
        td = ImageDraw.Draw(track)
        td.text((0, y), text, fill=1, font=font)
        td.text((period, y), text, fill=1, font=font)
        # Advance by 2 native pixels per update for readable motion.
        left = (scroll * 2) % period
        im = track.crop((left, 0, left + width, height))
    return im


def status_image(*, voltage=None, last_tts="", scroll=0, **_unused):
    """Render voltage over the persistent latest speech text, no other UI."""
    try:
        val = float(voltage)
        voltage_text = f"{val:.2f} V" if math.isfinite(val) else "--.-- V"
    except (ValueError, TypeError, OverflowError):
        voltage_text = "--.-- V"
    msg = " ".join(str(last_tts or "").split()) or "NO TTS YET"
    face = Image.new("1", (WIDTH, HEIGHT), 0)
    # Reserve separate rows with blank breathing room. Vertical coordinates
    # are protocol pixels, not physical display pixels (each is interlaced).
    face.paste(_strip(voltage_text, _VOLTAGE_FONT, WIDTH, 14), (0, 0))
    face.paste(_strip(msg, _MESSAGE_FONT, WIDTH, 15, scroll), (0, 17))
    return face


def compact_status_image(*, voltage=None, last_tts="", scroll=0,
                         battery_only=False, message_limit=14, **_unused):
    """Protocol-size fallback, still native-resolution and always legible."""
    if battery_only:
        last_tts = ""
    elif message_limit:
        msg = " ".join(str(last_tts or "").split())
        if len(msg) > message_limit:
            shift = scroll % (len(msg) + 3)
            last_tts = (msg + "   " + msg)[shift:shift + message_limit]
    return status_image(voltage=voltage, last_tts=last_tts, scroll=0)
