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
                 scroll=0, voltage_only=False, **_unused):
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
    if voltage_only:
        # Preserve a large message font without spending the limited OLED
        # keyframe buffer on the decorative battery outline and charge bar.
        draw.line((PANEL_LEFT, 1, PANEL_LEFT, HEIGHT - 2), fill=1)
        try:
            v = float(voltage)
            label = f"{v:.2f}V" if math.isfinite(v) and 2.7 <= v <= 4.4 else "--.--"
        except (TypeError, ValueError, OverflowError):
            label = "--.--"
        font_v = _font(10, _VOLTAGE_CHOICES)
        bounds = draw.textbbox((0, 0), label, font=font_v)
        draw.text((PANEL_LEFT + 2, 12 - bounds[1]), label, font=font_v, fill=1)
    else:
        _battery_panel(draw, voltage, percent, bool(charging))
    return image


def static_status_image(*, voltage=None, percent=None, charging=False,
                        last_tts="", max_encoded_bytes=None):
    """One large-text screen and the original battery icon; never paginate.

    Clip at the screen boundary, then shorten only the visible text if the
    compressed display packet is too large. Work is bounded regardless of
    incoming TTS length. Never use the tiny one-character page fallback.
    """
    from pycozmo.image_encoder import ImageEncoder
    from pycozmo.protocol_declaration import MAX_FRAME_PAYLOAD_SIZE
    if max_encoded_bytes is None:
        max_encoded_bytes = MAX_FRAME_PAYLOAD_SIZE - 6

    message = " ".join(str(last_tts or "").split()) or "NO MESSAGE"
    # No part of the message after this prefix can appear on a single screen.
    message = message[:64]
    canvas = Image.new("1", (WIDTH, HEIGHT), 0)
    measure = ImageDraw.Draw(canvas)
    # Use large readable fonts, and a fixed two-line maximum. Long messages
    # are clipped, not reduced into tiny characters or multiple pages.
    sizes = (17, 15, 14) if len(message) > 16 else ((22, 19, 17, 15, 14) if len(message) > 8 else (25, 22, 19, 17, 15, 14))
    for size in sizes:
        font = _font(size, _FONT_CHOICES)
        step = _height(measure, font) + 2
        max_lines = max(1, (TEXT_HEIGHT + 2) // step)
        if max_lines > 2:
            max_lines = 2
        lines = _wrap(message, measure, font, TEXT_WIDTH)[:max_lines]
        if not lines:
            continue
        # Preserve the original battery outline, segments, charging marker
        # and voltage in every candidate; don't switch to voltage-only mode.
        for n in range(len(" ".join(lines)), 7, -1):
            text = " ".join(lines)[:n].rstrip()
            if not text:
                break
            shown = _wrap(text, measure, font, TEXT_WIDTH)[:max_lines]
            image = Image.new("1", (WIDTH, HEIGHT), 0)
            draw = ImageDraw.Draw(image)
            used = len(shown) * step - 2
            y = max(0, (HEIGHT - used) // 2)
            for line in shown:
                bounds = draw.textbbox((0, 0), line, font=font)
                x = max(1, (PANEL_LEFT - (bounds[2] - bounds[0])) // 2)
                draw.text((x - bounds[0], y - bounds[1]), line,
                          font=font, fill=1)
                y += step
            _battery_panel(draw, voltage, percent, bool(charging))
            if len(ImageEncoder(image).encode()) <= max_encoded_bytes:
                return image
    # If even short, readable text won't fit, retain the original battery
    # icon and report the missing text through diagnostics rather than
    # sending dangerous oversized packets or single-letter pages.
    image = Image.new("1", (WIDTH, HEIGHT), 0)
    _battery_panel(ImageDraw.Draw(image), voltage, percent, bool(charging))
    if len(ImageEncoder(image).encode()) <= max_encoded_bytes:
        return image
    raise ValueError("Battery status image exceeds Cozmo display packet limit")


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


def wire_status_image(*, voltage=None, percent=None, charging=False,
                      last_tts="", scroll=0, page_chars=6):
    """Sparse, paginated fallback for Cozmo's 184-byte image data limit.

    Page by characters to preserve the *whole* TTS message over successive
    refreshes, even if only a few characters can fit in one packet.
    """
    image = Image.new("1", (WIDTH, HEIGHT), 0)
    draw = ImageDraw.Draw(image)
    font = _font(8, _FONT_CHOICES)
    message = " ".join(str(last_tts or "").split()) or "NO MESSAGE"
    pages = [message[i:i + page_chars] for i in range(0, len(message), page_chars)]
    line = pages[int(scroll) % len(pages)]
    box = draw.textbbox((0, 0), line, font=font)
    draw.text((1 - box[0], 7 - box[1]), line, fill=1, font=font)

    try:
        v = float(voltage)
        label = f"{v:.1f}V" if math.isfinite(v) else "--V"
    except (TypeError, ValueError, OverflowError):
        label = "--V"
    font_v = _font(8, _VOLTAGE_CHOICES)
    draw.text((94, 2), label, fill=1, font=font_v)
    try:
        charge = min(100, max(0, float(percent)))
    except (TypeError, ValueError, OverflowError):
        charge = 0
    if charge:
        draw.line((95, 22, 95 + int(charge * 0.28), 22), fill=1)
    if charging:
        draw.point((126, 22), fill=1)
    return image


def bounded_status_pages(*, voltage=None, percent=None, charging=False,
                         last_tts="", max_encoded_bytes=184):
    """Build complete, readable text pages under Cozmo's image data limit.

    The original adaptive 14-25px fonts remain in use. If a full dashboard
    frame is too large, display only voltage beside shorter text pages.
    Never send a large encoded frame or silently truncate the message.
    """
    from pycozmo.image_encoder import ImageEncoder

    message = " ".join(str(last_tts or "").split()) or "NO MESSAGE"
    draw = ImageDraw.Draw(Image.new("1", (WIDTH, HEIGHT)))
    _, layouts, _ = _layout(message, draw)
    if len(layouts) == 1:
        original = status_image(voltage=voltage, percent=percent,
                                charging=charging, last_tts=message)
        if len(ImageEncoder(original).encode()) <= max_encoded_bytes:
            return (original,)

    pages = []
    remaining = message
    while remaining:
        selected = None
        # A handful of candidate page lengths rather than a costly scan of
        # every possible substring. Each candidate must render on one page.
        for count in (32, 26, 22, 18, 14, 11, 8, 6, 4, 2, 1):
            if count > len(remaining):
                count = len(remaining)
            segment = remaining[:count].rstrip()
            if not segment:
                continue
            _, page_layouts, _ = _layout(segment, draw)
            if len(page_layouts) != 1:
                continue
            frame = status_image(voltage=voltage, percent=percent,
                                 charging=charging, last_tts=segment,
                                 voltage_only=True)
            if len(ImageEncoder(frame).encode()) <= max_encoded_bytes:
                selected = (count, frame)
                break
        if selected is None:
            raise ValueError("Even a one-character status page exceeds the OLED buffer")
        taken, frame = selected
        pages.append(frame)
        remaining = remaining[taken:].lstrip()
    return tuple(pages)
