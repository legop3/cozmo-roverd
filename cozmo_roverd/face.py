"""Cozmo's face shows only battery voltage and the most recent TTS text.

Render a 128x32 1-bit image for PyCozmo. No motion, docking, charging,
battery percentage, decorations, or other robot-observable status.
"""
from __future__ import annotations

import math
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT, SCALE = 128, 32, 4


def _font(size):
    for name in ("DejaVuSansMono.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, size * SCALE)
        except OSError:
            continue
    return ImageFont.load_default()


_VOLTAGE_FONT = _font(15)
_MESSAGE_FONT = _font(10)


def _text_strip(text, font, width, height, scroll):
    """Draw text into a clipped viewport, scrolling only when necessary."""
    dest = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(dest)
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = max(1, bbox[2] - bbox[0])
    y = max(0, (height - (bbox[3] - bbox[1])) // 2) - bbox[1]
    if text_width <= width:
        draw.text((max(0, (width - text_width) // 2), y), text, font=font, fill=255)
    else:
        gap = 16 * SCALE
        period = text_width + gap
        track = Image.new("L", (period * 2 + width, height), 0)
        td = ImageDraw.Draw(track)
        td.text((0, y), text, font=font, fill=255)
        td.text((period, y), text, font=font, fill=255)
        offset = (scroll * 4 * SCALE) % period
        dest = track.crop((offset, 0, offset + width, height))
    return dest


def status_image(*, voltage=None, last_tts="", scroll=0, **_unused):
    """Return the simple voltage + latest-message display."""
    try:
        v = float(voltage)
        voltage_text = f"{v:.2f} V" if math.isfinite(v) else "--.-- V"
    except (ValueError, TypeError, OverflowError):
        voltage_text = "--.-- V"

    message = " ".join(str(last_tts or "").split()) or "NO TTS YET"
    canvas = Image.new("L", (WIDTH * SCALE, HEIGHT * SCALE), 0)
    voltage_strip = _text_strip(voltage_text, _VOLTAGE_FONT,
                                WIDTH * SCALE, 19 * SCALE, 0)
    message_strip = _text_strip(message, _MESSAGE_FONT,
                                WIDTH * SCALE, 12 * SCALE, scroll)
    canvas.paste(voltage_strip, (0, 0))
    canvas.paste(message_strip, (0, 20 * SCALE))
    reduced = canvas.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    return reduced.point(lambda px: 255 if px >= 100 else 0, mode="1")


def compact_status_image(*, voltage=None, last_tts="", scroll=0,
                         battery_only=False, message_limit=14, **_unused):
    """Smaller text fallback if a full image cannot fit one UDP packet."""
    if battery_only:
        last_tts = ""
    elif message_limit:
        msg = " ".join(str(last_tts or "").split())
        if len(msg) > message_limit:
            shift = scroll % (len(msg) + 3)
            last_tts = (msg + "   " + msg)[shift:shift + message_limit]
    return status_image(voltage=voltage, last_tts=last_tts, scroll=0)
