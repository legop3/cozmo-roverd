"""Simple, steady Cozmo backpack and charger light colors.

Outer backpack LEDs are physically red-only; the three center LEDs are RGB.
No status animations, charging pulses, or MRR connection indications.
"""
from __future__ import annotations


def _solid(rgb):
    from pycozmo.lights import Color
    from pycozmo.protocol_encoder import LightState
    color = Color(rgb=rgb).to_int16()
    return LightState(on_color=color, off_color=color)


def backpack_lights():
    """Five steady backpack LEDs, ordered left to right."""
    return (
        _solid((255, 0, 0)),    # Red-only left outer LED
        _solid((0, 80, 255)),   # Blue
        _solid((0, 255, 0)),    # Green
        _solid((255, 175, 0)),  # Amber
        _solid((255, 0, 0)),    # Red-only right outer LED
    )


def charger_signature(*, docked, charging=None, docking=False):
    """The charger LEDs depend on dock contact and assist, not charge current."""
    return "assist" if docking else "docked" if docked is True else "undocked"


def charger_lights(*, docked: bool | None, charging: bool | None = None,
                   docking: bool = False):
    """All four charger LEDs: purple assisting, green docked, red otherwise."""
    rgb = ((150, 0, 255) if docking else
           (0, 255, 0) if docked is True else
           (255, 0, 0))
    light = _solid(rgb)
    return (light, light, light, light)
