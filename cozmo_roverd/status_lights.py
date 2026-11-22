"""Cozmo backpack status LED states.

The two side LEDs show the independent PyCozmo and MRR connections.
The three center LEDs indicate docking/charging; the charger base itself
does not expose a controllable light through the Cozmo protocol.
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

    # Side facing LEDs: local robot transport vs remote MRR registration.
    left = solid((0, 230, 45)) if robot_online else pulse((255, 85, 0))
    right = solid((0, 230, 45)) if mrr_online else pulse((255, 110, 0), 12, 18)

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
