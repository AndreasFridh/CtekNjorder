"""
Sharing current between several chargers.

The single-charger case only had to answer "how much". With several, it also
has to answer "to whom", and the failure modes are different: current stranded
on an empty charger, a tapering car holding capacity it cannot use, or - the
one that matters - the sum of several correct-looking allocations exceeding the
main fuse.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ctek_njord_sim"))

from app.allocator import (  # noqa: E402
    ChargerDemand, DemandTracker, allocate, water_fill,
)

THREE_PHASE = (0, 1, 2)


def charger(cid, order=0, cap=16.0, wants=True, charging=False,
            phases=THREE_PHASE, min_current=6, max_current=16):
    return ChargerDemand(id=cid, order=order, phases=phases, cap=cap, wants=wants,
                         charging=charging, min_current=min_current,
                         max_current=max_current)


# ---------- the basics ----------

def test_a_single_charger_takes_what_it_can_use():
    a = allocate([20, 20, 20], [charger("a")])
    assert a.per_charger["a"] == 16          # capped by the charger's own rating


def test_two_chargers_split_what_is_there():
    a = allocate([20, 20, 20], [charger("a", 0), charger("b", 1)])
    assert a.per_charger["a"] == 10
    assert a.per_charger["b"] == 10


def test_three_chargers_split_three_ways():
    a = allocate([24, 24, 24], [charger(x, i) for i, x in enumerate("abc")])
    assert sorted(a.per_charger.values()) == [8, 8, 8]


# ---------- a charger with no car must not hold capacity ----------

def test_an_idle_charger_is_given_nothing():
    a = allocate([20, 20, 20], [charger("a"), charger("idle", 1, wants=False)])
    assert a.per_charger["idle"] == 0
    assert a.per_charger["a"] == 16, "the whole lot should go to the active car"


def test_all_idle_means_all_zero():
    a = allocate([25, 25, 25], [charger("a", wants=False), charger("b", 1, wants=False)])
    assert set(a.per_charger.values()) == {0}
    assert "no charger" in a.reason


# ---------- cars that cannot use their share ----------

def test_optimal_hands_a_limited_cars_surplus_to_the_other():
    """
    A car pinned at 7 A by its own onboard charger should not sit on 10 A while
    the other car is throttled to 10 A.
    """
    a = allocate([20, 20, 20], [charger("limited", 0, cap=7.0), charger("hungry", 1)])
    assert a.per_charger["limited"] == 7
    assert a.per_charger["hungry"] == 13
    assert sum(a.per_charger.values()) <= 20


def test_even_split_ignores_what_a_car_can_use():
    """The point of `even` is predictability, even at the cost of idle current."""
    a = allocate([20, 20, 20], [charger("limited", 0, cap=7.0), charger("hungry", 1)],
                 strategy="even")
    assert a.per_charger["limited"] == 10
    assert a.per_charger["hungry"] == 10


def test_surplus_from_two_limited_cars_reaches_the_third():
    a = allocate([30, 30, 30], [
        charger("a", 0, cap=6.0), charger("b", 1, cap=7.0), charger("c", 2),
    ])
    assert a.per_charger["a"] == 6
    assert a.per_charger["b"] == 7
    assert a.per_charger["c"] == 16      # its own ceiling, not the fair share
    assert sum(a.per_charger.values()) <= 30


# ---------- scarcity ----------

def test_when_there_is_not_enough_for_both_one_charges_properly():
    """
    Below 6 A a car must stop, so splitting 9 A two ways would leave both
    unable to charge. Better to run one properly.
    """
    a = allocate([9, 9, 9], [charger("a", 0), charger("b", 1)])
    assert sorted(a.per_charger.values()) == [0, 9]
    assert len(a.served) == 1
    assert len(a.starved) == 1


def test_a_car_already_charging_keeps_priority_over_one_that_has_not_started():
    """Otherwise the served set churns every tick and neither car gets anywhere."""
    a = allocate([9, 9, 9], [
        charger("new", 0, charging=False),
        charger("running", 1, charging=True),
    ])
    assert a.per_charger["running"] == 9
    assert a.per_charger["new"] == 0


def test_nothing_at_all_when_there_is_not_even_one_minimum():
    a = allocate([4, 4, 4], [charger("a"), charger("b", 1)])
    assert set(a.per_charger.values()) == {0}
    assert "not enough" in a.reason


# ---------- phases ----------

def test_single_phase_chargers_on_different_phases_do_not_compete():
    a = allocate([16, 16, 16], [
        charger("l1", 0, phases=(0,)),
        charger("l2", 1, phases=(1,)),
    ])
    assert a.per_charger["l1"] == 16
    assert a.per_charger["l2"] == 16, "different phases, so no need to share"


def test_single_phase_chargers_on_the_same_phase_do_compete():
    a = allocate([16, 16, 16], [
        charger("a", 0, phases=(0,)),
        charger("b", 1, phases=(0,)),
    ])
    assert a.per_charger["a"] == 8
    assert a.per_charger["b"] == 8


def test_the_busiest_phase_constrains_a_three_phase_charger():
    a = allocate([20, 20, 7], [charger("a")])
    assert a.per_charger["a"] == 7


def test_a_loaded_phase_does_not_throttle_a_charger_that_avoids_it():
    a = allocate([20, 20, 2], [charger("a", phases=(0, 1))])
    assert a.per_charger["a"] == 16


# ---------- the property that actually matters ----------

@pytest.mark.parametrize("spare", [x * 0.5 for x in range(0, 80)])
@pytest.mark.parametrize("n", [1, 2, 3, 6])
def test_the_total_never_exceeds_the_headroom(spare, n):
    demands = [charger(f"c{i}", i) for i in range(n)]
    a = allocate([spare] * 3, demands)
    total = sum(a.per_charger.values())
    assert total <= spare + 1e-9, (
        f"{n} chargers were allocated {total}A out of {spare}A available"
    )


@pytest.mark.parametrize("spare", [x * 0.5 for x in range(0, 80)])
def test_no_charger_is_ever_given_an_illegal_setpoint(spare):
    demands = [charger(f"c{i}", i) for i in range(3)]
    a = allocate([spare] * 3, demands)
    for value in a.per_charger.values():
        assert value == 0 or 6 <= value <= 16


def test_mixed_phases_respect_every_phase_limit():
    """L1 is nearly full; only the charger avoiding it should get much."""
    a = allocate([7, 20, 20], [
        charger("three", 0, phases=THREE_PHASE),
        charger("l2l3", 1, phases=(1, 2)),
    ])
    on_l1 = a.per_charger["three"]
    assert on_l1 <= 7
    assert a.per_charger["three"] + a.per_charger["l2l3"] <= 20


# ---------- water_fill directly ----------

def test_water_fill_gives_away_everything_it_can():
    alloc = water_fill([20, 20, 20], [charger("a", 0, cap=5.0), charger("b", 1)])
    assert alloc["a"] == pytest.approx(5.0)
    assert alloc["b"] == pytest.approx(15.0)


def test_water_fill_terminates_on_zero_headroom():
    alloc = water_fill([0, 0, 0], [charger("a"), charger("b", 1)])
    assert alloc == {"a": 0.0, "b": 0.0}


# ---------- inferring demand from behaviour ----------

def test_a_ramping_car_is_not_mistaken_for_a_limited_one():
    """
    During a ramp the draw always trails the offer. Reading that as an internal
    limit would ratchet the car down and it would never reach full current.
    """
    t = DemandTracker()
    t.update(100.0, "a", setpoint=16, drawn=2.0)
    assert t.cap_for(101.0, "a", 16, 2.0, 16) == 16.0, "too soon to judge"


def test_a_settled_low_draw_is_read_as_the_cars_own_limit():
    t = DemandTracker()
    t.update(100.0, "a", setpoint=16, drawn=11.0)
    cap = t.cap_for(100.0 + t.SETTLE + 1, "a", 16, 11.0, 16)
    assert 11.0 <= cap <= 13.0


def test_a_car_taking_everything_offered_keeps_its_full_cap():
    t = DemandTracker()
    t.update(100.0, "a", setpoint=10, drawn=9.8)
    assert t.cap_for(100.0 + t.SETTLE + 1, "a", 10, 9.8, 16) == 16.0


def test_changing_the_offer_restarts_the_settling_clock():
    t = DemandTracker()
    t.update(100.0, "a", setpoint=16, drawn=11.0)
    t.update(100.0 + t.SETTLE + 1, "a", setpoint=10, drawn=11.0)
    assert t.cap_for(100.0 + t.SETTLE + 2, "a", 10, 11.0, 16) == 16.0


def test_drawing_current_always_counts_as_wanting_it():
    t = DemandTracker()
    t.update(100.0, "a", setpoint=16, drawn=9.0)
    assert t.wants_current(1e6, "a", state=None, drawn=9.0)


def test_a_charger_drawing_nothing_for_long_enough_is_treated_as_idle():
    t = DemandTracker()
    t.update(100.0, "a", setpoint=16, drawn=0.0)
    assert not t.wants_current(100.0 + t.IDLE_AFTER + 1, "a", state=None, drawn=0.0)


def test_the_one_confirmed_state_value_does_not_outlive_the_evidence():
    """
    Superseded deliberately. State 2 used to keep a charger "wanting" for ever,
    which meant one reporting 2 with nothing plugged in held an allocation
    indefinitely. Current offered and not taken is measured; State's meaning is
    not, so the measurement wins once there is any.
    """
    t = DemandTracker()
    t.update(100.0, "a", setpoint=16, drawn=0.0)
    assert t.wants_current(105.0, "a", state=2, drawn=0.0), "no evidence yet"
    assert not t.wants_current(100.0 + t.IDLE_AFTER + 1, "a", state=2, drawn=0.0)


def test_a_car_settled_just_below_its_allowance_is_still_read_as_limited():
    """
    Regression, found by running two chargers against the simulator.

    The cap is `drawn + HEADROOM`, so a limited car settles exactly HEADROOM
    below its allowance. Testing satisfaction at that same distance declared it
    satisfied, no surplus was ever detected, and two cars sat at 8 A each with
    20 A available - stable, and quietly wasting a third of the capacity.
    """
    t = DemandTracker()
    t.update(0.0, "d", setpoint=8, drawn=7.0)      # exactly HEADROOM below
    cap = t.cap_for(1000.0, "d", 8, 7.0, 16)
    assert cap < 16.0, "a car pinned at 7 A must not look like it wants 16"
    assert cap == pytest.approx(8.0)


def test_the_surplus_actually_reaches_the_other_car():
    """The whole point: end to end, from observed draw to allocation."""
    t = DemandTracker()
    t.update(0.0, "hungry", setpoint=8, drawn=8.1)
    t.update(0.0, "limited", setpoint=8, drawn=7.0)

    demands = [
        charger("hungry", 0, cap=t.cap_for(1000.0, "hungry", 8, 8.1, 16), charging=True),
        charger("limited", 1, cap=t.cap_for(1000.0, "limited", 8, 7.0, 16), charging=True),
    ]
    a = allocate([20, 20, 20], demands, "optimal")
    assert a.per_charger["limited"] == 8
    assert a.per_charger["hungry"] > 8, "the spare current must go somewhere"
    assert sum(a.per_charger.values()) <= 20


def test_a_limited_car_strands_at_most_about_an_amp():
    """
    A capped car inevitably holds a little more than it takes, so that it can
    signal it wants more. That slack must stay small: rounding the draw up
    before adding it stacked two lots of headroom and left ~2 A stranded per
    car, which on a 20 A budget is most of a third car's minimum.
    """
    t = DemandTracker()
    for drawn in (6.2, 7.0, 7.1, 9.8, 11.4):
        t.update(0.0, "d", setpoint=16, drawn=drawn)
        cap = t.cap_for(1000.0, "d", 16, drawn, 16)
        assert cap - drawn <= 1.01, f"drawing {drawn}A but holding {cap}A"


def test_a_car_whose_limit_rises_is_not_pinned_forever():
    """Capping must be reversible, or a car that warms up never speeds up."""
    t = DemandTracker()
    t.update(0.0, "d", setpoint=16, drawn=7.0)
    assert t.cap_for(1000.0, "d", 16, 7.0, 16) == pytest.approx(8.0)

    # Allowed 8 and now taking all of it: the limit has lifted.
    t.update(2000.0, "d", setpoint=8, drawn=8.0)
    assert t.cap_for(3000.0, "d", 8, 8.0, 16) == 16.0


# ---------- the cap must survive its own effect ----------

def test_capping_a_car_does_not_undo_itself():
    """
    Regression from the two-charger simulation, visible as setpoints flapping
    every settle window with a one-second revert.

    Capping a limited car lowers its setpoint. If that restarts the settling
    clock, the next tick has no settled reading, the cap reverts to maximum,
    the setpoint goes back up - and the whole thing repeats forever.
    """
    t = DemandTracker()
    t.update(0.0, "d", setpoint=9, drawn=7.0)
    capped = t.cap_for(100.0, "d", 9, 7.0, 16)
    assert capped == pytest.approx(8.0)

    # The allocator acts on it: the offer drops to 8.
    t.update(101.0, "d", setpoint=8, drawn=7.0)
    assert t.cap_for(102.0, "d", 8, 7.0, 16) == pytest.approx(8.0), (
        "the cap reverted as soon as it was applied - this is the flap"
    )


def test_a_larger_offer_does_restart_the_judgement():
    """A car must get a fresh chance whenever it is genuinely offered more."""
    t = DemandTracker()
    t.update(0.0, "d", setpoint=9, drawn=7.0)
    assert t.cap_for(100.0, "d", 9, 7.0, 16) == pytest.approx(8.0)

    t.update(101.0, "d", setpoint=14, drawn=7.0)      # more headroom appeared
    assert t.cap_for(102.0, "d", 14, 7.0, 16) == 16.0, "too soon to re-judge"


def test_the_cap_only_tightens_while_the_car_stays_limited():
    t = DemandTracker()
    t.update(0.0, "d", setpoint=16, drawn=11.0)
    first = t.cap_for(100.0, "d", 16, 11.0, 16)
    t.update(101.0, "d", setpoint=12, drawn=9.0)      # allocator applied it
    second = t.cap_for(200.0, "d", 12, 9.0, 16)
    assert second <= first, "a cap should not loosen without the car asking"


def test_a_paused_charger_is_never_written_off_as_empty():
    """
    It cannot draw what it was not offered. Judging it idle would mean a car
    paused through a load peak is never given current again.
    """
    t = DemandTracker()
    t.update(0.0, "d", setpoint=0, drawn=0.0)
    assert t.wants_current(1e6, "d", state=None, drawn=0.0)


def test_an_unused_offer_is_still_evidence_of_an_empty_charger():
    t = DemandTracker()
    t.update(0.0, "d", setpoint=10, drawn=0.0)
    assert not t.wants_current(t.IDLE_AFTER + 1, "d", state=None, drawn=0.0)


# ---------- damping the last amp of chatter ----------

def test_a_one_amp_nudge_waits_but_a_real_drop_does_not():
    from app.allocator import apply_dwell
    changed = {"a": 100.0}
    prev = {"a": 9}

    held = apply_dwell(105.0, {"a": 10}, prev, dict(changed), 20.0)
    assert held["a"] == 9, "a single amp is not worth the ramp"

    held = apply_dwell(105.0, {"a": 8}, prev, dict(changed), 20.0)
    assert held["a"] == 9, "nor is a single amp downward"

    urgent = apply_dwell(105.0, {"a": 7}, prev, dict(changed), 20.0)
    assert urgent["a"] == 7, "a real reduction must not wait"

    paused = apply_dwell(105.0, {"a": 0}, prev, dict(changed), 20.0)
    assert paused["a"] == 0, "pausing must never wait"


def test_a_charger_starting_from_zero_does_not_wait():
    from app.allocator import apply_dwell
    started = apply_dwell(105.0, {"a": 6}, {"a": 0}, {"a": 100.0}, 20.0)
    assert started["a"] == 6


def test_the_nudge_lands_once_the_dwell_has_passed():
    from app.allocator import apply_dwell
    later = apply_dwell(130.0, {"a": 10}, {"a": 9}, {"a": 100.0}, 20.0)
    assert later["a"] == 10


def test_a_capped_car_is_not_re_probed_just_for_using_a_smaller_offer():
    """
    A car limited to 7 A that is allocated 7 A takes all of it, and so looks
    satisfied. Reading that as "it wants more" restarts the whole cycle -
    raise, watch it decline the extra, cap it, and round again - which is what
    kept two chargers hunting instead of settling.
    """
    t = DemandTracker()
    t.update(0.0, "d", setpoint=9, drawn=7.0)
    assert t.cap_for(100.0, "d", 9, 7.0, 16) == pytest.approx(8.0)

    # Now allocated only 7, and taking all 7. Still limited, not hungry.
    t.update(101.0, "d", setpoint=7, drawn=7.0)
    assert t.cap_for(200.0, "d", 7, 7.0, 16) == pytest.approx(8.0)


def test_taking_the_full_capped_amount_does_lift_the_cap():
    """If the car really can use its cap, the limit has moved and we re-judge."""
    t = DemandTracker()
    t.update(0.0, "d", setpoint=9, drawn=7.0)
    assert t.cap_for(100.0, "d", 9, 7.0, 16) == pytest.approx(8.0)

    t.update(101.0, "d", setpoint=8, drawn=8.0)   # took all 8 this time
    assert t.cap_for(200.0, "d", 8, 8.0, 16) == 16.0


def test_jitter_alone_cannot_clear_a_cap():
    """
    A cap lands on fractional amps - 6.9 A drawn gives 7.9 - while allocations
    are whole. Comparing the offer against the cap's floor made an allocation
    of 7 look like a full offer, so ordinary meter jitter re-probed the car
    every time round.
    """
    t = DemandTracker()
    t.update(0.0, "d", setpoint=9, drawn=6.9)
    cap = t.cap_for(100.0, "d", 9, 6.9, 16)
    assert cap == pytest.approx(7.9)

    t.update(101.0, "d", setpoint=7, drawn=7.0)     # allocated floor(7.9)
    assert t.cap_for(200.0, "d", 7, 7.0, 16) == pytest.approx(7.9)


def test_the_fallback_reaches_a_charger():
    """End to end of the regression above: 6 A of headroom must be handed out."""
    a = allocate([6.0, 6.0, 6.0], [charger("a", 0, charging=True)], total_cap=6.0)
    assert a.per_charger["a"] == 6


def test_a_scarce_fallback_serves_one_charger_properly():
    a = allocate([6.0, 6.0, 6.0],
                 [charger("a", 0, charging=True), charger("b", 1)], total_cap=6.0)
    assert sorted(a.per_charger.values()) == [0, 6]


# ---------- an empty charger is not a tiny car ----------

def test_a_charger_drawing_nothing_is_not_capped_at_nothing():
    """
    The deadlock behind "waiting for capacity" with capacity plainly available.

    An empty charger offered current takes none, and that was recorded as a
    ~1 A car. One amp is below the 6 A legal minimum, so the allocator could
    never serve it - and a paused charger cannot demonstrate demand, so nothing
    ever revisited the conclusion.
    """
    t = DemandTracker()
    t.update(0.0, "empty", setpoint=9, drawn=0.0)
    cap = t.cap_for(100.0, "empty", 9, 0.0, 16, 6)
    assert cap == 16.0, "absence must not be read as a limit"


def test_a_cap_is_never_below_the_minimum_that_can_be_commanded():
    """The legal setpoints are 0 or min upward; a lower cap cannot be acted on."""
    t = DemandTracker()
    t.update(0.0, "d", setpoint=16, drawn=2.0)
    assert t.cap_for(100.0, "d", 16, 2.0, 16, 6) >= 6.0


def test_an_empty_charger_stops_holding_capacity():
    t = DemandTracker()
    t.update(0.0, "empty", setpoint=9, drawn=0.0)
    assert t.wants_current(t.IDLE_AFTER + 1, "empty", state=None, drawn=0.0) is False
    assert t.believed_empty("empty")


def test_an_empty_charger_is_offered_current_again_now_and_then():
    """
    A car plugged in while the charger is paused cannot draw, so it has no way
    to announce itself. Something has to look.
    """
    t = DemandTracker()
    t.update(0.0, "empty", setpoint=9, drawn=0.0)
    t.wants_current(t.IDLE_AFTER + 1, "empty", state=None, drawn=0.0)
    base = t.IDLE_AFTER + 1

    assert not t.wants_current(base + 60, "empty", state=None, drawn=0.0)
    assert t.wants_current(base + t.PROBE_EVERY + 5, "empty", state=None, drawn=0.0), (
        "an empty charger must be re-offered current eventually"
    )


def test_a_change_of_state_wakes_an_empty_charger_immediately():
    """
    Only State 2 is confirmed, but a *change* means something happened at the
    charger - most likely a car being plugged in - whatever the values mean.
    """
    t = DemandTracker()
    t.update(0.0, "d", setpoint=9, drawn=0.0)
    t.wants_current(10.0, "d", state=1, drawn=0.0)
    t.wants_current(t.IDLE_AFTER + 1, "d", state=1, drawn=0.0)
    assert t.believed_empty("d")

    assert t.wants_current(t.IDLE_AFTER + 5, "d", state=3, drawn=0.0)
    assert not t.believed_empty("d")


def test_drawing_current_clears_the_empty_conclusion():
    t = DemandTracker()
    t.update(0.0, "d", setpoint=9, drawn=0.0)
    t.wants_current(t.IDLE_AFTER + 1, "d", state=None, drawn=0.0)
    assert t.believed_empty("d")
    assert t.wants_current(t.IDLE_AFTER + 2, "d", state=None, drawn=8.0)
    assert not t.believed_empty("d")


def test_spare_capacity_is_offered_to_a_charger_that_wants_it():
    """The whole point: 9 A spare must reach a charger asking for current."""
    a = allocate([9.7, 9.7, 9.7], [charger("a", 0, cap=16.0)], total_cap=9.0)
    assert a.per_charger["a"] == 9


def test_an_unused_offer_outranks_a_state_that_claims_charging():
    """
    Draw is measured; State's meaning is unverified beyond "2 happens while
    charging". A charger reporting 2 with nothing plugged in would otherwise
    hold an allocation for ever on the strength of a flag we cannot read.
    """
    t = DemandTracker()
    t.update(0.0, "d", setpoint=16, drawn=0.0)
    t.wants_current(10.0, "d", state=2, drawn=0.0)
    assert not t.wants_current(t.IDLE_AFTER + 1, "d", state=2, drawn=0.0)
    assert t.believed_empty("d")


def test_state_two_still_counts_before_an_offer_has_been_ignored():
    """Early on there is no evidence either way, so the flag is worth having."""
    t = DemandTracker()
    t.update(0.0, "d", setpoint=16, drawn=0.0)
    assert t.wants_current(5.0, "d", state=2, drawn=0.0)


# ---------- protecting the car from repeated stop-start ----------

def test_stopping_is_immediate_but_restarting_waits():
    """
    Many cars fault and refuse to charge after several quick stop-start
    cycles, so the two directions cannot be treated alike. Pausing protects
    the fuse and must never wait; restarting protects the car and must.
    """
    from app.allocator import apply_dwell
    changed, stopped = {}, {}

    out = apply_dwell(100.0, {"a": 0}, {"a": 16}, changed, 20.0,
                      restart_hold=90.0, stopped_at=stopped)
    assert out["a"] == 0, "pausing must be instant"

    out = apply_dwell(110.0, {"a": 16}, {"a": 0}, changed, 20.0,
                      restart_hold=90.0, stopped_at=stopped)
    assert out["a"] == 0, "restarting ten seconds later would cycle the car"

    out = apply_dwell(100.0 + 95, {"a": 16}, {"a": 0}, changed, 20.0,
                      restart_hold=90.0, stopped_at=stopped)
    assert out["a"] == 16, "but it does start again once the hold has passed"


def test_a_car_cannot_be_cycled_repeatedly():
    """The failure that reached real hardware: stop, start, stop, start."""
    from app.allocator import apply_dwell
    changed, stopped = {}, {}
    state = {"a": 16}
    starts = 0

    # Headroom flapping every few seconds, as a lagging meter makes it do.
    for i in range(60):
        now = 100.0 + i * 5
        want = 16 if i % 2 else 0
        state = apply_dwell(now, {"a": want}, state, changed, 20.0,
                            restart_hold=90.0, stopped_at=stopped)
        if state["a"] > 0 and want == 16:
            starts += 1

    assert starts <= 4, f"the car was started {starts} times in five minutes"


def test_the_hold_does_not_delay_a_charger_that_never_stopped():
    from app.allocator import apply_dwell
    out = apply_dwell(100.0, {"a": 10}, {"a": 8}, {"a": 0.0}, 20.0,
                      restart_hold=90.0, stopped_at={})
    assert out["a"] == 10
