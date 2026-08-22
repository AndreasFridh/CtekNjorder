"""
The charge-enable gate, price normalisation, and session cost.

The gate's default is the important part. It exists to save money, not to keep
anyone safe, so every ambiguous case has to resolve to "charge". The failure it
must never have is a sensor dropping out overnight and quietly leaving a car
uncharged.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ctek_njord_sim"))

from app.costs import (  # noqa: E402
    CostTracker, charging_allowed, price_per_kwh,
)


# ---------- the gate defaults to permitting ----------

@pytest.mark.parametrize("state", [
    None,               # option not configured at all
    "unavailable",      # entity exists but is not reporting
    "unknown",
    "",
    "something odd",    # a spelling we do not recognise
])
def test_anything_ambiguous_still_charges(state):
    assert charging_allowed(state) is True, (
        "an unclear gate must not silently stop charging"
    )


@pytest.mark.parametrize("state", ["off", "false", "0", "no", "disabled", "OFF"])
def test_only_an_explicit_no_stops_charging(state):
    assert charging_allowed(state) is False


@pytest.mark.parametrize("state", ["on", "true", "1", "yes", "enabled", "ON", 1])
def test_explicit_yes_charges(state):
    assert charging_allowed(state) is True


def test_a_number_works_as_a_gate():
    assert charging_allowed("0.0") is False
    assert charging_allowed("1.0") is True
    assert charging_allowed("2") is True


# ---------- price units ----------

def test_whole_currency_units_pass_through():
    assert price_per_kwh(2.45, "SEK/kWh") == pytest.approx(2.45)
    assert price_per_kwh(2.45, None) == pytest.approx(2.45)


@pytest.mark.parametrize("unit", ["öre/kWh", "ore/kWh", "cents/kWh", "øre/kWh"])
def test_sub_units_are_scaled(unit):
    """
    The factor of a hundred here is the difference between a plausible bill and
    a nonsensical one, so the unit is read rather than assumed.
    """
    assert price_per_kwh(245.0, unit) == pytest.approx(2.45)


def test_no_price_is_not_a_zero_price():
    assert price_per_kwh(None, "SEK/kWh") is None


# ---------- sessions ----------

def make(cid="a"):
    return CostTracker(), cid


def test_a_session_starts_when_the_car_draws():
    t, cid = make()
    t.update(0.0, cid, drawn=0.0, energy_counter=1000, price=2.0)
    assert t.session_for(cid) is None

    t.update(1.0, cid, drawn=9.0, energy_counter=1000, price=2.0)
    assert t.session_for(cid) is not None


def test_energy_comes_from_the_lifetime_counter():
    t, cid = make()
    t.update(0.0, cid, drawn=9.0, energy_counter=1_000_000, price=2.0)
    t.update(10.0, cid, drawn=9.0, energy_counter=1_002_000, price=2.0)
    s = t.session_for(cid)
    assert s.energy_wh == pytest.approx(2000)
    assert s.cost == pytest.approx(2.0 * 2.0)      # 2 kWh at 2.00


def test_cost_follows_the_price_in_force_at_the_time():
    """
    On an hourly tariff the price changes mid-session. Costing the total at the
    final price would be wrong for every session that spans a change.
    """
    t, cid = make()
    t.update(0.0, cid, drawn=9.0, energy_counter=0, price=1.0)
    t.update(10.0, cid, drawn=9.0, energy_counter=1000, price=1.0)   # 1 kWh @ 1.00
    t.update(20.0, cid, drawn=9.0, energy_counter=2000, price=3.0)   # 1 kWh @ 3.00
    assert t.session_for(cid).cost == pytest.approx(4.0)
    assert t.session_for(cid).cost != pytest.approx(2 * 3.0), "priced at the last rate"


def test_a_counter_reset_does_not_book_negative_energy():
    """A charger reboot restarts its lifetime counter."""
    t, cid = make()
    t.update(0.0, cid, drawn=9.0, energy_counter=5_000_000, price=2.0)
    t.update(10.0, cid, drawn=9.0, energy_counter=5_001_000, price=2.0)
    before = t.session_for(cid).energy_wh
    t.update(20.0, cid, drawn=9.0, energy_counter=0, price=2.0)       # rebooted
    t.update(30.0, cid, drawn=9.0, energy_counter=500, price=2.0)
    s = t.session_for(cid)
    assert s.energy_wh >= before
    assert s.cost >= 0


def test_energy_is_still_counted_when_no_price_is_available():
    t, cid = make()
    t.update(0.0, cid, drawn=9.0, energy_counter=0, price=None)
    t.update(10.0, cid, drawn=9.0, energy_counter=1000, price=None)
    s = t.session_for(cid)
    assert s.energy_wh == pytest.approx(1000)
    assert s.cost == 0.0


def test_a_session_ends_only_after_the_grace_period():
    """
    A car pausing for a moment - or being throttled to zero through a load peak
    - is not the end of its session.
    """
    t, cid = make()
    t.update(0.0, cid, drawn=9.0, energy_counter=0, price=2.0)
    t.update(30.0, cid, drawn=0.0, energy_counter=1000, price=2.0)
    assert t.session_for(cid) is not None, "ended far too eagerly"

    t.update(30.0 + t.IDLE_GRACE + 1, cid, drawn=0.0, energy_counter=1000, price=2.0)
    assert t.session_for(cid) is None
    assert t.last_completed(cid).energy_wh == pytest.approx(1000)


def test_completed_sessions_do_not_accumulate_without_bound():
    t, cid = make()
    for i in range(t.KEEP_SESSIONS + 15):
        base = i * 10_000.0
        t.update(base, cid, drawn=9.0, energy_counter=i * 100, price=1.0)
        t.update(base + t.IDLE_GRACE + 1, cid, drawn=0.0, energy_counter=i * 100, price=1.0)
    assert len(t.completed[cid]) <= t.KEEP_SESSIONS


def test_cost_per_hour_is_power_times_price():
    t, _ = make()
    assert t.cost_per_hour(11_000, 2.5) == pytest.approx(27.5)
    assert t.cost_per_hour(None, 2.5) is None
    assert t.cost_per_hour(11_000, None) is None
