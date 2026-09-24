"""
Which setpoints get sent again, and spotting a second controller.

Re-sending 0 A to a paused charger was heard as the car starting and stopping
every heartbeat while charging was not allowed.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ctek_njord_sim"))

from app.protocol import (  # noqa: E402
    PAUSE_BREACH, PAUSE_RESEND_EVERY, resend_needed,
)


def test_an_idle_pause_is_never_repeated():
    # The field report: every repeated 0 was followed by State 3, 4, then 2 -
    # the car starting and stopping on each heartbeat. Idle draw while a car
    # wakes was 0.3-1.1 A, which must not count as charging.
    for drawn in (0.0, 0.5, 1.1):
        assert not resend_needed(0, drawn, since_sent=60.0, heartbeat=True)


def test_a_pause_the_car_charges_through_is_sent_again():
    assert resend_needed(0, PAUSE_BREACH, since_sent=PAUSE_RESEND_EVERY,
                         heartbeat=False), "must not wait for the heartbeat"
    assert resend_needed(0, 16.0, since_sent=60.0, heartbeat=False)


def test_a_breached_pause_is_not_sent_every_tick():
    assert not resend_needed(0, 16.0, since_sent=PAUSE_RESEND_EVERY - 1,
                             heartbeat=True)


def test_a_charging_setpoint_is_refreshed_on_the_heartbeat_only():
    # The charger's behaviour on controller silence is unknown, so a current
    # it is using keeps being refreshed exactly as before.
    assert resend_needed(10, 10.0, since_sent=15.0, heartbeat=True)
    assert resend_needed(6, 0.0, since_sent=15.0, heartbeat=True)
    assert not resend_needed(10, 10.0, since_sent=3.0, heartbeat=False)


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


# ---------- the charger's own setpoints, answering our meter data ----------

from app.protocol import REASSERT_EVERY, should_reassert  # noqa: E402


def test_a_higher_foreign_setpoint_is_countered_at_once():
    # Field log: our 0 A pause overridden by 6 A / 16 A every ten seconds.
    assert should_reassert(0, 16, since_last=10.0)
    assert should_reassert(0, "6", since_last=10.0)
    assert should_reassert(7, 16, since_last=10.0)


def test_a_lower_foreign_setpoint_is_left_alone():
    # Less current is the safe direction; overriding it is not ours to do.
    assert not should_reassert(16, 6, since_last=10.0)
    assert not should_reassert(7, 7, since_last=10.0)


def test_countering_is_rate_limited():
    assert not should_reassert(0, 16, since_last=REASSERT_EVERY - 0.1)


def test_nothing_is_countered_before_we_have_said_anything():
    assert not should_reassert(None, 16, since_last=100.0)
    assert not should_reassert(0, {"junk": 1}, since_last=100.0)
