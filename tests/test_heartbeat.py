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


# ---------- a second controller on the same charger ----------

from app.protocol import ForeignCommands  # noqa: E402


def test_our_own_echo_is_not_foreign():
    f = ForeignCommands()
    f.sent(100.0, 0)
    assert not f.seen(100.2, 0)
    assert f.last_value is None


def test_a_value_we_did_not_send_is_foreign():
    # The field report: we sent 0, and 16 kept arriving from the Nanogrid Air.
    f = ForeignCommands()
    f.sent(100.0, 0)
    assert not f.seen(100.1, 0)
    assert f.seen(102.0, 16)
    assert f.last_value == 16


def test_each_send_accounts_for_only_one_echo():
    # Both controllers saying 0 is harmless, but a second 0 we never sent is
    # still someone else's.
    f = ForeignCommands()
    f.sent(100.0, 0)
    assert not f.seen(100.1, 0)
    assert f.seen(101.0, 0)


def test_an_old_send_does_not_excuse_a_later_value():
    f = ForeignCommands()
    f.sent(100.0, 16)
    assert f.seen(100.0 + ForeignCommands.ECHO_WINDOW + 1, 16)


def test_garbage_on_the_topic_is_ignored():
    f = ForeignCommands()
    assert not f.seen(1.0, {"not": "a setpoint"})
