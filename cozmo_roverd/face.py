"""Composed 128x32 monochrome status graphics for Cozmo's OLED face.

Every update is an image, rendered at 4x resolution with custom graphic
widgets, a measured text ticker, and a final 1-bit dither-free conversion.
No terminal-style fixed text rows and no direct user text entry.
"""
from __future__ import annotations

import math
from PIL import Image, ImageDraw, ImageFont

WIDTH, HEIGHT, SCALE = 128, 32, 4
INK, PAPER = 255, 0


def _font(px: int):
    # Pillow's scalable fonts avoid the coarse built-in default bitmap font.
    for name in ("DejaVuSansMono.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"):
        try:
            return ImageFont.truetype(name, px * SCALE)
        except OSError:
            continue
    return ImageFont.load_default()


_SMALL = _font(7)
_TINY = _font(6)
_BOLD = _font(8)


def _p(value):
    return int(round(value * SCALE))


def _rect(draw, bounds, **kwargs):
    draw.rectangle(tuple(map(_p, bounds)), **kwargs)


def _line(draw, points, *, width=1, **kwargs):
    draw.line(tuple(map(_p, points)), width=_p(width), **kwargs)


def _ellipse(draw, bounds, **kwargs):
    draw.ellipse(tuple(map(_p, bounds)), **kwargs)


def _text(draw, pos, message, font=_SMALL):
    draw.text(tuple(map(_p, pos)), str(message), fill=INK, font=font,
              stroke_width=0)


def _battery(draw, percent, charging):
    """Graphic battery gauge with proportional fill and a charging bolt."""
    x, y, w, h = 3, 2, 22, 9
    _rect(draw, (x, y, x+w-2, y+h), outline=INK, width=_p(1))
    _rect(draw, (x+w-2, y+3, x+w, y+h-3), fill=INK)
    try:
        value = max(0, min(100, float(percent)))
        filled = int((w-6) * value / 100)
        if filled:
            _rect(draw, (x+2, y+2, x+2+filled, y+h-2), fill=INK)
    except (ValueError, TypeError):
        pass
    if charging:
        # Negative-space lightning glyph remains legible even when full.
        draw.polygon([(_p(x+12), _p(y+1)), (_p(x+8), _p(y+5)),
                      (_p(x+11), _p(y+5)), (_p(x+9), _p(y+8)),
                      (_p(x+15), _p(y+3)), (_p(x+12), _p(y+3))],
                     fill=PAPER)


def _status_icon(draw, x, y, kind):
    """Compact vector icons, rendered as real bitmap geometry."""
    if kind == "dock":
        _line(draw, (x, y+5, x+2, y+7, x+9, y+7, x+11, y+5), width=1, fill=INK)
        _rect(draw, (x+2, y+1, x+3, y+5), fill=INK)
        _rect(draw, (x+8, y+1, x+9, y+5), fill=INK)
    elif kind == "cliff":
        draw.polygon([(_p(x+5), _p(y)), (_p(x), _p(y+8)),
                      (_p(x+10), _p(y+8))], outline=INK)
        _line(draw, (x+5, y+3, x+5, y+5), fill=INK)
        _ellipse(draw, (x+4.5, y+6, x+5.5, y+7), fill=INK)
    elif kind == "pickup":
        _line(draw, (x+5, y+8, x+5, y+1), fill=INK)
        _line(draw, (x+5, y+1, x+2, y+4), fill=INK)
        _line(draw, (x+5, y+1, x+8, y+4), fill=INK)
        _line(draw, (x, y+8, x+10, y+8), fill=INK)
    elif kind == "speaker":
        draw.polygon([(_p(x), _p(y+3)), (_p(x+3), _p(y+3)),
                      (_p(x+6), _p(y)), (_p(x+6), _p(y+8)),
                      (_p(x+3), _p(y+5)), (_p(x), _p(y+5))], fill=INK)
        draw.arc(tuple(map(_p, (x+4, y, x+11, y+8))),
                 start=-65, end=65, fill=INK, width=_p(1))
    elif kind == "error":
        _rect(draw, (x, y, x+8, y+8), outline=INK, width=_p(1))
        _line(draw, (x+2, y+2, x+6, y+6), fill=INK)
        _line(draw, (x+6, y+2, x+2, y+6), fill=INK)


def _wheel(draw, x, y, speed, side):
    """Symmetric signed-speed gauge, no fabricated speed/acceleration data."""
    _text(draw, (x, y-2), side, _TINY)
    x0, width = x+8, 30
    mid = x0 + width//2
    _line(draw, (x0, y+4, x0+width, y+4), fill=INK)
    _line(draw, (mid, y+1, mid, y+7), fill=INK)
    try:
        ratio = max(-1.0, min(1.0, float(speed)/250))
    except (TypeError, ValueError):
        ratio = 0.0
    extent = int(ratio * (width//2-2))
    if extent > 0:
        _rect(draw, (mid+1, y+2, mid+extent, y+6), fill=INK)
    elif extent < 0:
        _rect(draw, (mid+extent, y+2, mid-1, y+6), fill=INK)


def _ticker(canvas, message, scroll, audio_error):
    """Scroll in pixels within a clipped viewport; never cover the status."""
    ticker_left, ticker_top = 15, 23
    clip_width, clip_height = 110, 8
    text = " ".join(str(message or "").split())
    if not text:
        text = "TTS ERROR" if audio_error else "NO TTS YET"
    strip = Image.new("L", (_p(clip_width), _p(clip_height)), PAPER)
    if not text:
        return strip
    measure = ImageDraw.Draw(strip)
    bbox = measure.textbbox((0, 0), text, font=_SMALL)
    text_width = max(1, bbox[2]-bbox[0])
    if text_width <= strip.width:
        measure.text((0, -bbox[1]), text, fill=INK, font=_SMALL)
    else:
        gap = _p(18)
        period = text_width + gap
        offset = (_p(scroll) * 2) % period
        # A separate large text track is cropped into a static viewport.
        track = Image.new("L", (period*2+_p(2), strip.height), PAPER)
        d = ImageDraw.Draw(track)
        d.text((0, -bbox[1]), text, fill=INK, font=_SMALL)
        d.text((period, -bbox[1]), text, fill=INK, font=_SMALL)
        strip = track.crop((offset, 0, offset+strip.width, strip.height))
    canvas.paste(strip, (_p(ticker_left), _p(ticker_top)))


def status_image(*, voltage=None, percent=None, charging=None, docked=None,
                 cliff=None, picked_up=None, moving=None, wheels=(0, 0),
                 last_tts="", scroll=0, audio_error=None):
    """Return one fully composed binary face image for PyCozmo."""
    canvas = Image.new("L", (WIDTH*SCALE, HEIGHT*SCALE), PAPER)
    d = ImageDraw.Draw(canvas)
    _battery(d, percent, charging)
    try:
        voltage_label = f"{float(voltage):.2f}V" if math.isfinite(float(voltage)) else "--.--V"
    except (TypeError, ValueError):
        voltage_label = "--.--V"
    _text(d, (30, 1), voltage_label, _BOLD)
    try:
        percent_label = f"{float(percent):.0f}%"
    except (TypeError, ValueError):
        percent_label = "--%"
    _text(d, (77, 2), percent_label, _SMALL)
    if docked:
        _status_icon(d, 114, 2, "dock")
    _line(d, (2, 12, 126, 12), fill=INK)

    left, right = wheels if wheels is not None else (0, 0)
    _wheel(d, 3, 15, left, "L")
    _wheel(d, 45, 15, right, "R")
    if cliff:
        _status_icon(d, 90, 14, "cliff")
    if picked_up:
        _status_icon(d, 102, 14, "pickup")
    if audio_error:
        _status_icon(d, 116, 14, "error")
    _line(d, (2, 22, 126, 22), fill=INK)

    _status_icon(d, 3, 23, "speaker")
    _ticker(canvas, last_tts, scroll, audio_error)

    # Anti-aliased geometry is reduced into one monochrome image. Cozmo's
    # image encoder accepts the resulting exact 128x32 1-bit PIL image.
    reduced = canvas.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    return reduced.point(lambda v: 255 if v >= 105 else 0, mode="1")



def compact_status_image(*, voltage=None, percent=None, charging=None,
                         last_tts="", scroll=0, battery_only=False, message_limit=14):
    """A simpler *generated graphic* for small firmware animation buffers.

    Cozmo's internal animation-keyframe buffer is separate from the UDP
    payload limit. Keep a battery gauge and voltage at the top, and the last
    TTS message when there is enough room for it. Use sparse vector geometry
    and the same supersampled scalable font as the full dashboard.
    """
    canvas = Image.new("L", (WIDTH*SCALE, HEIGHT*SCALE), PAPER)
    draw = ImageDraw.Draw(canvas)
    _battery(draw, percent, charging)
    try:
        voltage_label = f"{float(voltage):.2f}V" if math.isfinite(float(voltage)) else "--.--V"
    except (ValueError, TypeError):
        voltage_label = "--.--V"
    _text(draw, (32, 2), voltage_label, _BOLD)
    if not battery_only:
        _status_icon(draw, 3, 20, "speaker")
        # Only the first portion is displayed per image; the caller may
        # advance scroll on subsequent status updates.
        msg = " ".join(str(last_tts or "").split())
        if msg:
            msg = msg[:256]
            position = scroll % (len(msg) + 3)
            msg = (msg + "   " + msg)[position:position + message_limit]
        else:
            msg = "READY"
        _text(draw, (17, 20), msg, _TINY)
    reduced = canvas.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    return reduced.point(lambda v: 255 if v >= 105 else 0, mode="1")
