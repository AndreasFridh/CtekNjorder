"""
Which setpoints the heartbeat re-sends.

Re-sending 0 A to a charger that had already paused was heard as the car
starting and stopping every heartbeat while charging was not allowed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ctek_njord_sim"))

from app.protocol import ECHO_FRESH, heartbeat_needed  # noqa: E402


def test_a_confirmed_pause_is_not_sent_again():
    assert not heartbeat_needed(0, echoed=0, echo_age=1.0)
    assert not heartbeat_needed(0, echoed=0.0, echo_age=1.0)


def test_a_pause_the_charger_has_not_confirmed_is_sent_again():
    assert heartbeat_needed(0, echoed=6, echo_age=1.0)
    assert heartbeat_needed(0, echoed=None, echo_age=1.0)


def test_a_stale_echo_is_not_trusted():
    assert heartbeat_needed(0, echoed=0, echo_age=ECHO_FRESH + 1)
    assert heartbeat_needed(0, echoed=0, echo_age=float("inf"))


def test_a_charging_setpoint_is_always_refreshed():
    # The charger's behaviour on controller silence is unknown, so a current
    # it is actually using keeps being refreshed exactly as before.
    assert heartbeat_needed(10, echoed=10, echo_age=1.0)
    assert heartbeat_needed(6, echoed=6, echo_age=0.0)
