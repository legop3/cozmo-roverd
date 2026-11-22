"""Cozmo backpack status LED states.

The two side LEDs show the independent PyCozmo and MRR connections.
The three center LEDs indicate docking/charging. The actual charger base
also has four addressable lights via ObjectConnect, CubeId and CubeLights.
Patterns use firmware-timed LightState frames, not an asyncio blink loop.
"""
from __future__ import annotations


def light_states(*, robot_online: bool, mrr_online: bool,
                 docked: bool | None, charging: bool | None,
                 docking: bool = False):
    from pycozmo import lights
    from pycozmo.protocol_encoder import LightState

    def solid(rgb):
        color = lights.Color(rgb=rgb).to_int16()
        return LightState(on_color=color, off_color=color)

    def pulse(rgb, on=18, off=18, dim=(0, 0, 0)):
        return LightState(
            on_color=lights.Color(rgb=rgb).to_int16(),
            off_color=lights.Color(rgb=dim).to_int16(),
            on_frames=on, off_frames=off,
            transition_on_frames=8, transition_off_frames=8,
        )

    # The OUTER two Cozmo backpack LEDs are red-only (no green/blue die).
    # A green RGB command therefore leaves them completely dark. Use red
    # channel only: steady = healthy, pulsing = attempting/reconnecting.
    left = solid((255, 0, 0)) if robot_online else pulse((255, 0, 0), 18, 18)
    right = solid((255, 0, 0)) if mrr_online else pulse((255, 0, 0), 12, 18)

    if docked is True:
        # Charge state isn't always reported while docked; unknown stays cyan.
        center = (pulse((255, 175, 0), 24, 24, (14, 7, 0))
                  if charging is True else
                  solid((0, 220, 70)) if charging is False else
                  pulse((0, 170, 255), 30, 30, (0, 12, 18)))
    elif docking:
        center = pulse((145, 55, 255), 15, 15)
    else:
        center = solid((0, 0, 0))
    return (left, center, center, center, right)


def light_signature(*, robot_online, mrr_online, docked, charging, docking):
    """Only change LEDs when their *meaningful* status changes."""
    return (bool(robot_online), bool(mrr_online),
            docked is True, charging if docked is True else None,
            bool(docking) if docked is not True else False)


def charger_lights(*, docked: bool | None, charging: bool | None,
                   docking: bool = False):
    """Four dock LEDs, following pycozmo/examples/charger_lights.py.

    The fourth slot is left off, matching the official example. A steady
    green means docked but not actively charging; amber breathes while
    charging; purple guides docking. Undocked means completely dark.
    """
    from pycozmo import lights
    from pycozmo.protocol_encoder import LightState

    def make(rgb, dim=None):
        on = lights.Color(rgb=rgb).to_int16()
        if dim is None:
            return LightState(on_color=on, off_color=on)
        return LightState(
            on_color=on,
            off_color=lights.Color(rgb=dim).to_int16(),
            on_frames=26, off_frames=26,
            transition_on_frames=9, transition_off_frames=9,
        )

    if docked is True and charging is True:
        led = make((255, 160, 0), (18, 8, 0))
    elif docked is True and charging is False:
        led = make((0, 210, 65))
    elif docked is True:
        led = make((0, 175, 235), (0, 12, 18))
    elif docking:
        led = make((145, 55, 255), (8, 0, 12))
    else:
        led = lights.off_light
    return (led, led, led, lights.off_light)


def charger_signature(*, docked, charging, docking):
    """Deduplicate charger commands without conflating unknown charge state."""
    return (docked is True, charging if docked is True else None,
            bool(docking) if docked is not True else False)
