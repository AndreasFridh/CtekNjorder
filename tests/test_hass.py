"""
Deciding whether our picture of the house is current.

The trap here is that "how long since the value changed" and "how out of date
are we" are different questions. Home Assistant does not emit a state_changed
event when a sensor re-reports the value it already had, so a house sitting at
a steady load produces no events at all. Judging freshness by the last event
declares the best-behaved possible meter stale.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ctek_njord_sim"))

from app.hass import HassClient  # noqa: E402

PHASES = ["sensor.l1", "sensor.l2", "sensor.l3"]


def connected_client(values=("4.0", "4.1", "4.0")):
    c = HassClient(PHASES)
    for eid, v in zip(PHASES, values):
        c._record(eid, v)
    c.connected = True
    c.disconnected_since = None
    return c


def test_a_steady_reading_is_not_stale():
    """
    The regression. A quiet house holds its load, Home Assistant sends no
    events because nothing changed, and the old check called that stale and
    dropped every charger to the fallback current - with the meter working
    perfectly the whole time.
    """
    c = connected_client()
    for eid in PHASES:                       # pretend an hour has passed
        c.updated_at[eid] = time.time() - 3600

    assert c.age(PHASES[0]) > 3000, "the value genuinely has not changed"
    assert c.feed_age(PHASES) == 0.0, "but our picture is still current"


def test_an_entity_we_have_never_seen_is_stale():
    c = HassClient(PHASES)
    c.connected = True
    assert c.feed_age(PHASES) == float("inf")


def test_a_partial_set_is_stale():
    """Two of three phases is not a usable picture of the house."""
    c = HassClient(PHASES)
    c._record(PHASES[0], "4.0")
    c._record(PHASES[1], "4.0")
    c.connected = True
    assert c.feed_age(PHASES) == float("inf")


def test_an_unavailable_entity_is_stale_however_recently_it_said_so():
    c = connected_client()
    c._record(PHASES[1], "unavailable")
    assert c.age(PHASES[1]) < 1, "it reported just now"
    assert c.feed_age(PHASES) == float("inf"), "but what it reported is unusable"


def test_unknown_counts_the_same_as_unavailable():
    c = connected_client()
    c._record(PHASES[2], "unknown")
    assert c.feed_age(PHASES) == float("inf")


def test_a_dropped_connection_ages_from_the_moment_it_dropped():
    c = connected_client()
    c.connected = False
    c.disconnected_since = time.time() - 45
    assert 44 < c.feed_age(PHASES) < 47


def test_recovering_from_unavailable_makes_the_feed_current_again():
    c = connected_client()
    c._record(PHASES[1], "unavailable")
    assert c.feed_age(PHASES) == float("inf")
    c._record(PHASES[1], "4.2")
    assert c.feed_age(PHASES) == 0.0


def test_no_entities_configured_is_stale_not_fresh():
    c = HassClient([])
    c.connected = True
    assert c.feed_age([]) == float("inf")


# ---------- raw states, for the charge-enable gate ----------

def test_a_non_numeric_state_is_kept():
    """A switch reports "on", which is meaningful but is not a number."""
    c = HassClient(["input_boolean.gate"])
    c._record("input_boolean.gate", "on")
    assert c.state("input_boolean.gate") == "on"
    assert c.value("input_boolean.gate") is None


def test_a_numeric_state_is_available_both_ways():
    c = HassClient(["sensor.price"])
    c._record("sensor.price", "2.45")
    assert c.value("sensor.price") == 2.45
    assert c.state("sensor.price") == "2.45"


def test_a_bad_reading_does_not_erase_the_last_good_number():
    """
    Keep the last good value so one dropped sample is not a control glitch -
    feed_age is what notices the entity has gone unusable.
    """
    c = HassClient(["sensor.l1"])
    c._record("sensor.l1", "4.0")
    c._record("sensor.l1", "unavailable")
    assert c.value("sensor.l1") == 4.0
    assert c.state("sensor.l1") == "unavailable"
