"""Compact, always-on Cozmo face status (128x32 monochrome)."""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont


def status_image(*, voltage=None, percent=None, charging=None, docked=None,
                 cliff=None, picked_up=None, moving=None, wheels=(0, 0),
                 last_tts="", scroll=0, audio_error=None):
    """Create three independent status rows, with a scrolling TTS preview.

    Rendering is separate from playback and telemetry; nothing here changes
    drive state. Cozmo's screen is 128x32 and uses a binary image.
    """
    image = Image.new("1", (128, 32), color=0)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    voltage_text = f"{voltage:.2f}V" if isinstance(voltage, (int, float)) else "--.--V"
    charge_text = f"{percent:.0f}%" if isinstance(percent, (int, float)) else "--%"
    power = "CHG" if charging else "DOCK" if docked else "BAT"
    draw.text((1, 0), f"{power} {voltage_text} {charge_text}", font=font, fill=1)

    left, right = wheels or (0, 0)
    flags = ("C" if cliff else "-") + ("P" if picked_up else "-") + ("!" if audio_error else "-")
    draw.text((1, 10), f"L{left:+.0f} R{right:+.0f} {flags}", font=font, fill=1)

    msg = " ".join(str(last_tts).split())
    if msg:
        # Keep last speech visible indefinitely; a narrow 128px display
        # scrolls longer messages instead of clipping them permanently.
        ticker = "TTS " + msg
        if len(ticker) > 20:
            ticker = ticker + "   "
            pos = scroll % len(ticker)
            ticker = (ticker + ticker)[pos:pos + 20]
    elif audio_error:
        ticker = "TTS ERROR"
    else:
        ticker = "TTS --"
    draw.text((1, 21), ticker[:20], font=font, fill=1)
    return image
