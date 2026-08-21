"""
Safety properties of the load balancer.

The 15-minute capture never produced a real overload - the house baseline
peaked at 5.5 A against a >=25 A service - so the throttling path has no
real-world evidence behind it. These tests are the only thing standing behind
it, which makes them the most important code in the repo.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ctek_njord_sim"))

from app.balancer import (  # noqa: E402
    Balancer, BalancerConfig, car_draw_for_baseline,
)

CEILING = 16
MIN = 6


def make(**kw):
    cfg = BalancerConfig(main_fuse=kw.pop("main_fuse", 25), **kw)
    return Balancer(cfg)


def warm(bal, setpoint=CEILING):
    """
    Skip the cold-start step so steady-state behaviour can be tested.

    Set the field directly rather than driving compute() twice - going through
    compute would also arm the raise timer, which is not what these tests mean.
    """
    bal.setpoint = setpoint
    return bal


# ---------- cold start ----------

def test_cold_start_opens_at_minimum():
    """The real adapter commands 6 A before trusting its first reading."""
    bal = make()
    d = bal.compute(now=0, house_current=[5, 5, 5], house_age=0,
                    charger_current=[0, 0, 0], ev_uses_phase=[1, 1, 1])
    assert d.setpoint == MIN


def test_cold_start_still_pauses_when_already_overloaded():
    """Cold start must not blindly command 6 A into a fuse that is already full."""
    bal = make(main_fuse=20)
    d = bal.compute(now=0, house_current=[19.5, 19.5, 19.5], house_age=0,
                    charger_current=[0, 0, 0], ev_uses_phase=[1, 1, 1])
    assert d.setpoint == 0


# ---------- the ceiling ----------

def test_never_exceeds_charger_fuse_rating():
    """A huge service must still not push the charger past its own 16 A."""
    bal = warm(make(main_fuse=125))
    d = bal.compute(now=1000, house_current=[0, 0, 0], house_age=0,
                    charger_current=[0, 0, 0], ev_uses_phase=[1, 1, 1])
    assert d.setpoint <= CEILING


def test_user_max_is_respected_below_charger_rating():
    bal = make(max_charge_current=10, main_fuse=125)
    warm(bal)
    d = bal.compute(now=1000, house_current=[0, 0, 0], house_age=0,
                    charger_current=[0, 0, 0], ev_uses_phase=[1, 1, 1])
    assert d.setpoint <= 10


# ---------- overload response ----------

def test_overload_throttles_immediately_without_raise_delay():
    """Shedding load must never wait for the raise timer."""
    bal = make(main_fuse=25, raise_delay=60)
    warm(bal)  # established at full current

    # A 15 A appliance turns on. Baseline 15 A, so headroom = 25 - 1 - 15 = 9 A.
    d = bal.compute(now=1.0, house_current=[15, 15, 15], house_age=0,
                    charger_current=[0, 0, 0], ev_uses_phase=[1, 1, 1])
    assert d.setpoint == 9
    assert d.changed


def test_severe_overload_pauses_rather_than_charging_below_minimum():
    """Below MinAllowedCurrent the only legal command is 0."""
    bal = make(main_fuse=20)
    bal.setpoint = 16
    d = bal.compute(now=1.0, house_current=[18, 18, 18], house_age=0,
                    charger_current=[0, 0, 0], ev_uses_phase=[1, 1, 1])
    assert d.setpoint == 0


@pytest.mark.parametrize("baseline", [x * 0.5 for x in range(0, 60)])
def test_never_emits_an_illegal_setpoint(baseline):
    """Across the whole range, output is always 0 or 6..16 - never 1..5."""
    bal = make(main_fuse=25)
    bal.setpoint = 16
    d = bal.compute(now=1.0, house_current=[baseline] * 3, house_age=0,
                    charger_current=[0, 0, 0], ev_uses_phase=[1, 1, 1])
    assert d.setpoint == 0 or MIN <= d.setpoint <= CEILING


@pytest.mark.parametrize("baseline", [x * 0.5 for x in range(0, 60)])
def test_setpoint_plus_baseline_never_exceeds_the_fuse(baseline):
    """The property that actually matters: we must not trip the main fuse."""
    fuse, margin = 25.0, 1.0
    bal = make(main_fuse=fuse, safety_margin=margin)
    bal.setpoint = 16
    d = bal.compute(now=1.0, house_current=[baseline] * 3, house_age=0,
                    charger_current=[0, 0, 0], ev_uses_phase=[1, 1, 1])
    if d.setpoint > 0:
        assert baseline + d.setpoint <= fuse, (
            f"baseline {baseline}A + setpoint {d.setpoint}A exceeds {fuse}A fuse"
        )
    if baseline > fuse - margin:
        assert d.setpoint == 0, (
            f"house alone draws {baseline}A of a {fuse}A fuse - must pause, "
            f"got {d.setpoint}A"
        )


# ---------- the car's own draw must be excluded ----------

def test_car_draw_is_subtracted_from_the_meter_reading():
    """
    The meter includes the car. If we failed to subtract it we would see a
    20 A house, throttle, then see less load and raise - oscillating forever.
    """
    bal = make(main_fuse=25)
    warm(bal)
    d = bal.compute(now=1.0, house_current=[20, 20, 20], house_age=0,
                    charger_current=[16, 16, 16], ev_uses_phase=[1, 1, 1])
    assert d.baseline == [4.0, 4.0, 4.0]
    assert d.setpoint == CEILING  # 25 - 1 - 4 = 20, capped at the ceiling


# ---------- staleness ----------

def test_stale_house_data_falls_back():
    bal = make(stale_timeout=30, fallback_current=6)
    bal.setpoint = 16
    d = bal.compute(now=1.0, house_current=[5, 5, 5], house_age=120,
                    charger_current=[0, 0, 0], ev_uses_phase=[1, 1, 1])
    assert d.setpoint == 6
    assert "FALLBACK" in d.reason


def test_missing_house_data_falls_back():
    bal = make(fallback_current=6)
    bal.setpoint = 16
    d = bal.compute(now=1.0, house_current=None, house_age=0,
                    charger_current=[0, 0, 0], ev_uses_phase=[1, 1, 1])
    assert d.setpoint == 6


# ---------- hysteresis ----------

def test_raise_is_delayed_but_eventually_applied():
    bal = make(main_fuse=25, raise_delay=30)
    bal.setpoint = 8

    held = bal.compute(now=100.0, house_current=[2, 2, 2], house_age=0,
                       charger_current=[0, 0, 0], ev_uses_phase=[1, 1, 1])
    assert held.setpoint == 8, "must not jump up instantly"

    later = bal.compute(now=131.0, house_current=[2, 2, 2], house_age=0,
                        charger_current=[0, 0, 0], ev_uses_phase=[1, 1, 1])
    assert later.setpoint == CEILING, "must raise once the delay has elapsed"


def test_a_dip_during_the_raise_delay_cancels_the_raise():
    bal = make(main_fuse=25, raise_delay=30)
    bal.setpoint = 8
    bal.compute(now=100.0, house_current=[2, 2, 2], house_age=0,
                charger_current=[0, 0, 0], ev_uses_phase=[1, 1, 1])
    # Load returns before the timer expires; the pending raise must be dropped.
    bal.compute(now=110.0, house_current=[16, 16, 16], house_age=0,
                charger_current=[0, 0, 0], ev_uses_phase=[1, 1, 1])
    d = bal.compute(now=125.0, house_current=[2, 2, 2], house_age=0,
                    charger_current=[0, 0, 0], ev_uses_phase=[1, 1, 1])
    assert d.setpoint == 8, "raise timer should have restarted"


# ---------- phase handling ----------

def test_the_worst_phase_constrains_the_setpoint():
    """Single-phase loads are the common case; the busiest phase must win."""
    bal = make(main_fuse=25)
    bal.setpoint = 16
    d = bal.compute(now=1.0, house_current=[2, 2, 18], house_age=0,
                    charger_current=[0, 0, 0], ev_uses_phase=[1, 1, 1])
    assert d.setpoint == 6  # 25 - 1 - 18 = 6


def test_unused_phases_do_not_constrain_a_single_phase_car():
    """A car on L1 only must not be throttled by a heavy load on L3."""
    bal = make(main_fuse=25)
    bal.setpoint = 16
    d = bal.compute(now=1.0, house_current=[2, 2, 24], house_age=0,
                    charger_current=[0, 0, 0], ev_uses_phase=[1, 0, 0])
    assert d.setpoint == CEILING


def test_phase_rotation_maps_the_car_onto_the_right_meter_phase():
    """
    With STR, station phase 0 feeds meter phase 1. A 16 A car draw reported on
    station phase 0 must be subtracted from meter phase 1, not meter phase 0.
    """
    bal = make(main_fuse=25, phase_rotation="STR")
    warm(bal)
    d = bal.compute(now=1.0, house_current=[4, 20, 4], house_age=0,
                    charger_current=[16, 0, 0], ev_uses_phase=[1, 1, 1])
    assert d.baseline == [4.0, 4.0, 4.0]


# ---------- settle guard ----------
#
# Found by the offline simulation rig: after each setpoint change the car takes
# seconds to follow, and during that slew the meter still reports its previous
# draw. `house - car` then over-states the baseline and the balancer throttles
# against its own transient, oscillating 16->10->7->10->9->8->9.

def test_holds_while_the_car_is_still_slewing():
    bal = make(main_fuse=25, settle_window=10, settle_tolerance=1.5)
    bal.setpoint = 10
    bal._changed_at = 100.0

    # Car is still at 16 A on its way down to 10 A, so the meter still contains
    # the old draw and the derived baseline is inflated.
    d = bal.compute(now=101.0, house_current=[30, 30, 30], house_age=0,
                    charger_current=[16, 16, 16], ev_uses_phase=[1, 1, 1])
    assert d.setpoint == 10, "must not chase a transient down to 7A"
    assert "settles" in d.reason


def test_settle_guard_never_delays_a_pause():
    """A real overload must still pause instantly, mid-slew or not."""
    bal = make(main_fuse=20, settle_window=10)
    bal.setpoint = 16
    bal._changed_at = 100.0
    # Baseline 14 A of a 20 A fuse leaves 5 A - under the 6 A floor, so the
    # only legal answer is 0. The car is mid-slew, which must not delay it.
    d = bal.compute(now=101.0, house_current=[22, 22, 22], house_age=0,
                    charger_current=[8, 8, 8], ev_uses_phase=[1, 1, 1])
    assert d.setpoint == 0


def test_settle_guard_expires_so_a_finished_car_cannot_freeze_it():
    """
    A car that has finished charging draws far below its allowance forever.
    Without the time bound that would look like a permanent slew and pin the
    setpoint - exactly the taper we saw in the real capture at 4.2 A.
    """
    bal = make(main_fuse=25, settle_window=10)
    bal.setpoint = 16
    bal._changed_at = 100.0
    d = bal.compute(now=200.0, house_current=[8, 8, 8], house_age=0,
                    charger_current=[4.2, 4.2, 4.2], ev_uses_phase=[1, 1, 1])
    assert "settles" not in d.reason, "guard should have expired after 10s"


# ---------- attributing the meter reading to the car ----------
#
# Subtracting the car's draw is what keeps its own consumption from counting
# against its own allowance. The subtraction is only valid while we can see
# what it draws: subtract more than it really takes and we invent headroom.

def test_car_draw_is_ignored_once_charger_telemetry_goes_stale():
    fresh = car_draw_for_baseline([16, 16, 16], charger_age=2, stale_timeout=30)
    assert fresh == [16, 16, 16]
    stale = car_draw_for_baseline([16, 16, 16], charger_age=99, stale_timeout=30)
    assert stale == [0.0, 0.0, 0.0]


def test_stale_charger_does_not_invent_headroom():
    """
    The regression this guards: assuming a silent charger still draws its last
    setpoint. If the car is in fact idle, the whole meter reading is house
    load, and subtracting a phantom 16 A understates the baseline by 16 A.
    """
    bal = make(main_fuse=25)
    warm(bal)
    house = [20.0, 20.0, 20.0]        # all of it is house load; the car is idle

    phantom = bal.compute(now=1.0, house_current=house, house_age=0,
                          charger_current=[16, 16, 16], ev_uses_phase=[1, 1, 1])

    bal2 = make(main_fuse=25)
    warm(bal2)
    honest = bal2.compute(
        now=1.0, house_current=house, house_age=0,
        charger_current=car_draw_for_baseline([16, 16, 16], 99, 30),
        ev_uses_phase=[1, 1, 1])

    assert phantom.setpoint == 16, "the old behaviour handed out the full 16 A"
    assert honest.setpoint == 0, "with no visibility it must not hand out current"
    # 20 A of house plus the 16 A it would have allowed blows a 25 A fuse.
    assert 20.0 + phantom.setpoint > 25
    assert 20.0 + honest.setpoint <= 25
