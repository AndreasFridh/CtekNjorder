"""
Regulating the meter reading directly.

The property that matters is convergence. The old approach subtracted the cars
out of the meter, which put what we allow back into what we allow next with a
gain of one and a delay of one meter period - and that oscillates whatever
gating is placed around it. These tests drive the regulator through a simulated
meter that lags, and assert it settles instead of ringing.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ctek_njord_sim"))

from app.regulator import MeterCadence, Regulator  # noqa: E402

LIMIT = 24.0        # 25 A fuse, 1 A margin
CEILING = 16.0
MIN = 6.0


def three(v):
    return [float(v)] * 3


class Clock:
    """
    Steps the regulator with time moving on.

    The draw window slides with the meter's period, so a test that stepped at a
    frozen instant would keep the very first draw in scope for ever and measure
    something the running loop never does.
    """

    def __init__(self, regulator, step_seconds=10.0):
        self.r = regulator
        self.t = 0.0
        self.dt = step_seconds

    def step(self, meter, limit, drawn, ceiling):
        self.t += self.dt
        return self.r.step(self.t, meter, limit, drawn, ceiling)


# ---------- measuring the meter, rather than being told ----------

def test_the_reporting_period_is_measured():
    c = MeterCadence()
    for i in range(10):
        c.observe(i * 10.0)
    assert c.period == pytest.approx(10.0)
    assert c.measured


def test_an_unmeasured_meter_assumes_a_typical_p1():
    c = MeterCadence()
    assert c.period == c.DEFAULT
    assert not c.measured


def test_a_reconnection_gap_is_not_mistaken_for_the_meter_rate():
    """Minutes of silence after a dropout says nothing about how fast it reports."""
    c = MeterCadence()
    for i in range(6):
        c.observe(i * 10.0)
    c.observe(600.0)                    # ten minutes later
    assert c.period == pytest.approx(10.0)


def test_duplicate_readings_are_ignored():
    r = Regulator()
    clk = Clock(r)
    assert r.note_reading(100.0) is True
    assert r.note_reading(100.0) is False
    assert r.note_reading(99.0) is False
    assert r.note_reading(110.0) is True


# ---------- pacing ----------

def test_no_step_is_taken_until_the_meter_could_have_caught_up():
    """
    Acting on a reading taken before the last change means correcting against
    the world as it was - which is exactly how the cycling started.
    """
    r = Regulator()
    clk = Clock(r)
    for i in range(6):
        r.note_reading(i * 10.0)        # a ten-second meter

    assert not r.ready(now=100.0, changed_at=95.0), "only five seconds since the change"
    assert r.ready(now=100.0, changed_at=85.0), "a full period has passed"


def test_a_fast_meter_is_still_paced_by_the_lag_floor():
    """
    Arrivals are a lower bound on staleness, never an upper one: a meter can
    publish every two seconds and still describe the house as it was ten
    seconds ago. Pacing on the arrival rate alone steps several times before
    the first correction is visible, and each step compounds the last.
    """
    r = Regulator()
    for i in range(6):
        r.note_reading(i * 2.0)         # arrives every two seconds
    assert r.cadence.period == pytest.approx(2.0), "the arrival rate is measured honestly"

    assert not r.ready(now=100.0, changed_at=95.0), "five seconds is not enough"
    assert r.ready(now=100.0, changed_at=88.0), "past the floor"


def test_a_slow_meter_is_paced_by_its_own_rate_not_the_floor():
    r = Regulator()
    for i in range(6):
        r.note_reading(i * 30.0)
    assert not r.ready(now=100.0, changed_at=80.0), "20s < the meter's own 30s"
    assert r.ready(now=100.0, changed_at=65.0)


def test_a_configured_floor_can_slow_the_pacing_further():
    r = Regulator()
    for i in range(6):
        r.note_reading(i * 2.0)
    assert r.ready(now=100.0, changed_at=88.0)
    assert not r.ready(now=100.0, changed_at=88.0, min_period=20.0)


def test_a_steady_house_does_not_freeze_the_allowance():
    """
    Home Assistant sends nothing when a value repeats, so a quiet house looks
    identical to a dead feed. Requiring the reading to have CHANGED froze the
    allowance exactly when the house was quietest and there was most room to
    give away - a charger sat at 10 A with 16 A free, indefinitely.
    """
    r = Regulator()
    r.note_reading(100.0)              # one reading, and then nothing ever again
    t = 100.0
    for _ in range(6):
        t += r.LAG_FLOOR + 1
        if r.ready(t, changed_at=0.0):
            r.step(t, three(4.0), LIMIT, three(0.0), CEILING)
    assert max(r.allowed) == pytest.approx(CEILING), (
        "a house that stopped changing should not stop the allowance rising"
    )


def test_steps_cannot_outrun_the_meter_even_when_the_allocation_sits_still():
    """The other half: pacing must hold whether or not the allocation moves."""
    r = Regulator()
    assert r.ready(1000.0, changed_at=0.0), "nothing stepped yet"
    r.step(1000.0, three(4.0), LIMIT, three(0.0), CEILING)
    assert not r.ready(1000.0 + r.LAG_FLOOR - 1, changed_at=0.0)
    assert r.ready(1000.0 + r.LAG_FLOOR + 1, changed_at=0.0)


# ---------- the control law ----------

def test_it_climbs_toward_the_limit_rather_than_jumping():
    """Half the error each step, so the loop converges instead of ringing."""
    r = Regulator()
    clk = Clock(r)
    first = clk.step(three(4.0), LIMIT, three(0.0), CEILING)[0]
    assert 0 < first <= CEILING
    assert first < CEILING, "a single step must not go straight to full current"


def test_being_over_the_limit_is_corrected_at_once():
    """Shedding is the safe direction and is never delayed."""
    r = Regulator()
    clk = Clock(r)
    r.allowed = three(16.0)
    after = clk.step(three(30.0), LIMIT, three(16.0), CEILING)[0]
    assert after == pytest.approx(16.0 - 6.0), "the whole 6 A overshoot, in one step"


def test_shedding_measures_from_the_draw_not_from_the_allowance():
    """
    While a car ramps, or declines its offer, the allowance sits above what is
    actually taken - so cutting from the allowance overshoots, and the next
    reading overshoots again, walking the setpoint down several amps at a time.
    """
    r = Regulator()
    clk = Clock(r)
    r.allowed = three(13.0)          # granted
    drawn = 12.0                     # actually taken
    house = 14.0
    after = clk.step(three(house + drawn), LIMIT, three(drawn), CEILING)[0]

    assert after == pytest.approx(LIMIT - house), "should land on the real answer"
    assert after > 6.0, "must not walk down toward the minimum"


def test_a_shed_holds_once_it_is_right_rather_than_creeping_down():
    r = Regulator()
    clk = Clock(r)
    r.allowed = three(13.0)
    drawn = 12.0
    house = 14.0
    first = clk.step(three(house + drawn), LIMIT, three(drawn), CEILING)[0]
    # The car obeys, and the meter now reflects it.
    second = clk.step(three(house + first), LIMIT, three(first), CEILING)[0]
    assert second == pytest.approx(first), "shed twice for one overload"


def test_the_allowance_never_exceeds_the_ceiling():
    r = Regulator()
    clk = Clock(r)
    for _ in range(30):
        clk.step(three(0.0), LIMIT, three(16.0), CEILING)
    assert max(r.allowed) <= CEILING


def test_the_allowance_never_goes_negative():
    r = Regulator()
    clk = Clock(r)
    for _ in range(30):
        clk.step(three(60.0), LIMIT, three(0.0), CEILING)
    assert min(r.allowed) >= 0.0


# ---------- anti-windup ----------

def test_a_car_declining_its_offer_does_not_shrink_the_offer():
    """
    The allowance answers "how much could be drawn", not "how much is being
    drawn". A car taking 4 A of a quiet house has not made the house busier,
    and throttling the offer to just above its draw only means the next car -
    or the same car waking up - has to climb back from nothing.

    What must hold is that the offer stays inside what the house can absorb.
    """
    r = Regulator()
    clk = Clock(r)
    house, drawn = 1.0, 4.0
    for _ in range(20):
        clk.step(three(house + drawn), LIMIT, three(drawn), CEILING)
    assert max(r.allowed) == pytest.approx(CEILING), "a quiet house has room to spare"
    assert max(r.allowed) + house <= LIMIT, "and the offer still fits under the fuse"


def test_a_car_that_has_not_started_is_still_offered_enough_to_start():
    """A strict drawn-plus-slack cap would forbid the 6 A a car needs to begin."""
    r = Regulator()
    clk = Clock(r)
    for _ in range(10):
        clk.step(three(4.0), LIMIT, three(0.0), CEILING)
    assert max(r.allowed) >= MIN


def test_a_car_winding_down_cannot_ratchet_its_own_allowance_to_zero():
    """
    The mirror of the ratchet above, and why the two directions read opposite
    ends of the window. While a car comes down the reading still holds its old
    higher draw, so cutting against the draw it has already reached charges it
    twice for the same amps - and each cut lowers the draw again.
    """
    r = Regulator()
    clk = Clock(r, step_seconds=2.0)
    house = 14.0                     # 10 A genuinely available
    car = 13.0
    r.allowed = three(13.0)
    stale_meter = house + 13.0       # the meter still sees the old draw

    for _ in range(6):
        allowed = clk.step(three(stale_meter), LIMIT, three(car), CEILING)[0]
        car = allowed                # the car obeys immediately

    assert allowed >= 9.0, (
        f"walked down to {allowed:.1f}A with {LIMIT - house:.0f}A available"
    )


def test_a_ramping_car_cannot_ratchet_past_what_the_house_can_absorb():
    """
    A stale reading paired with a live draw once let a car climb without the
    meter getting a say, until the reading caught up and forced a hard cut to
    zero. Pairing the reading with a draw of matching age bounds the offer by
    the room that genuinely exists, however far behind the meter is.
    """
    r = Regulator()
    clk = Clock(r, step_seconds=2.0)     # stepping faster than the meter reports
    house = 4.0
    car = 6.0
    stale_meter = house + car            # the meter is stuck on this

    for _ in range(6):
        allowed = clk.step(three(stale_meter), LIMIT, three(car), CEILING)[0]
        car = allowed                    # the car takes whatever it is offered

    assert allowed + house <= LIMIT + 0.01, (
        f"offered {allowed:.1f}A into a house drawing {house}A, past a {LIMIT}A limit"
    )


def test_a_paused_car_is_not_offered_room_that_does_not_exist():
    """
    The dangerous case. A paused car draws nothing, so it is absent from the
    reading and the error stays positive however loaded the house is - nothing
    about a positive error looks like an overload.
    """
    r = Regulator()
    clk = Clock(r)
    house = 20.5                     # only 3.5 A spare
    r.allowed = three(6.0)

    for _ in range(5):
        allowed = clk.step(three(house), LIMIT, three(0.0), CEILING)[0]

    assert allowed <= LIMIT - house + 0.01, (
        f"offered {allowed:.1f}A into {LIMIT - house:.1f}A of capacity"
    )
    assert allowed < 6.0, "below the legal minimum, so this must snap to a pause"


def test_capacity_does_not_get_in_the_way_when_there_is_plenty():
    """The cap above must bind only when it should, or nothing ever starts."""
    r = Regulator()
    clk = Clock(r)
    for _ in range(5):
        allowed = clk.step(three(4.0), LIMIT, three(0.0), CEILING)[0]
    assert allowed == pytest.approx(CEILING), (
        "a quiet house should offer the charger's full rating"
    )


def test_the_backstop_lands_on_the_answer_instead_of_ratcheting():
    """
    The control loop has an independent backstop that fires straight off a
    reading over the limit, without waiting for a step to be due. It used to
    take the overshoot off the current allowance, which ratchets: the allowance
    drops, the stale reading still shows the same overshoot, and it comes off
    again. On the rig that walked a car with 10 A of room down to a pause in
    two ticks, and then restarted it - the stop-start cycle that faults cars.
    """
    r = Regulator()
    clk = Clock(r, step_seconds=2.0)
    house, car = 14.0, 16.0
    clk.step(three(house + car), LIMIT, three(car), CEILING)

    over_limit = three(house + car)          # 30 A against a 24 A limit
    first = r.shed_target(over_limit, LIMIT)
    assert first == pytest.approx(LIMIT - house), "should land on the real answer"

    # Called again on the same stale reading, it must not cut deeper.
    assert r.shed_target(over_limit, LIMIT) == pytest.approx(first)
    assert first >= 6.0, "must not walk down into a pause"


def test_the_backstop_is_read_only():
    """It runs on ticks where no step is due, so it must not advance state."""
    r = Regulator()
    clk = Clock(r)
    clk.step(three(10.0), LIMIT, three(6.0), CEILING)
    before = list(r.allowed), r._stepped_at
    r.shed_target(three(30.0), LIMIT)
    assert (list(r.allowed), r._stepped_at) == before


def test_the_draw_window_keeps_the_previous_step():
    """
    Steps are paced one settling period apart, so the previous sample lands
    exactly on the window cutoff and a fraction of a second of jitter drops it.
    That leaves only the draw taken at this very moment - the one the reading
    definitely does not reflect - and a shed then measures against the car's
    own already-reduced draw and stops it.
    """
    r = Regulator()
    period = r.settling_period()
    house = 14.0
    r.step(1000.0, three(house + 16.0), LIMIT, three(16.0), CEILING)
    # A hair over a full period later, as the real loop does.
    r.step(1000.0 + period + 0.01, three(house + 16.0), LIMIT, three(9.0), CEILING)

    assert r.allowed[0] == pytest.approx(LIMIT - house, abs=0.2), (
        "should shed to the room that exists, not past it"
    )
    assert r.allowed[0] >= 6.0, "and must not walk into a pause"


# ---------- the whole point: no oscillation under lag ----------

def simulate(meter_lag_steps: int, steps: int = 40, house: float = 4.0):
    """
    Drive the regulator through a meter that reports the world as it was
    `meter_lag_steps` readings ago, with a car that follows its allowance.
    """
    r = Regulator()
    clk = Clock(r)
    car = 0.0
    history: list[float] = []
    pipeline = [house] * max(1, meter_lag_steps)

    for _ in range(steps):
        reading = pipeline.pop(0)
        allowed = clk.step(three(reading), LIMIT, three(car), CEILING)[0]
        # The car follows what it is allowed, and the meter sees it later.
        car = min(allowed, CEILING)
        pipeline.append(house + car)
        history.append(allowed)
    return history


@pytest.mark.parametrize("lag", [1, 2, 3])
def test_it_settles_instead_of_oscillating(lag):
    history = simulate(meter_lag_steps=lag)
    tail = history[-10:]
    assert max(tail) - min(tail) < 1.0, (
        f"still swinging by {max(tail) - min(tail):.1f}A after 40 steps at lag {lag}"
    )


@pytest.mark.parametrize("lag", [1, 2, 3])
def test_it_never_lets_the_meter_exceed_the_limit(lag):
    """The whole exercise is pointless if it converges above the fuse."""
    history = simulate(meter_lag_steps=lag)
    house = 4.0
    assert max(history[-10:]) + house <= LIMIT + 0.5


def test_it_reaches_a_useful_current_not_just_a_safe_one():
    """Converging to zero would be stable and useless."""
    history = simulate(meter_lag_steps=2)
    assert history[-1] >= 15.0, "should end up near the ceiling with a quiet house"


def test_a_step_change_in_house_load_is_absorbed_without_ringing():
    r = Regulator()
    clk = Clock(r)
    car = 0.0
    pipeline = [4.0, 4.0]
    for _ in range(20):                     # settle at a quiet house
        reading = pipeline.pop(0)
        allowed = clk.step(three(reading), LIMIT, three(car), CEILING)[0]
        car = allowed
        pipeline.append(4.0 + car)

    house = 14.0                            # an oven comes on
    tail = []
    for _ in range(25):
        reading = pipeline.pop(0)
        allowed = clk.step(three(reading), LIMIT, three(car), CEILING)[0]
        car = allowed
        pipeline.append(house + car)
        tail.append(allowed)

    assert max(tail[-8:]) - min(tail[-8:]) < 1.0, "did not settle after the step"
    assert max(tail[-8:]) + house <= LIMIT + 0.5


# ---------- the allowance is bounded by draw, never by our own command ----------

def test_holding_does_not_forget_a_raise_the_balancer_is_waiting_on():
    """
    The balancer holds its setpoint down while it waits out `raise_delay`, so
    an allowance clamped to that held command loses the pending raise on every
    tick that takes no step - the target falls back to the current setpoint,
    the timer restarts, and charging never leaves the cold-start minimum.
    """
    r = Regulator()
    clk = Clock(r)
    clk.step(three(10.0), LIMIT, three(6.0), CEILING)
    raised = r.allowed[0]
    assert raised > 6.0, "should be asking for more than the car has"

    # Several ticks with no new reading, exactly as the loop does.
    for _ in range(5):
        assert r.hold()[0] == pytest.approx(raised), "the pending raise was lost"


def test_the_allowance_is_bounded_by_the_house_not_by_our_own_command():
    """
    The command is our own opinion; an integrator reset by its own output
    cannot climb. What legitimately bounds the offer is the meter.
    """
    r = Regulator()
    clk = Clock(r)
    house, drawn = 4.0, 8.0
    for _ in range(20):
        clk.step(three(house + drawn), LIMIT, three(drawn), CEILING)
    assert max(r.allowed) + house <= LIMIT + 0.01
    assert max(r.allowed) > drawn, "spare room should be offered, not withheld"


def test_a_gate_that_stops_the_car_does_not_shrink_the_offer():
    """
    Withholding charging is the allocator's job, through the total cap - not
    the regulator's. A car that stops drawing has not made the house busier, so
    the room on offer is unchanged; it simply is not handed out.
    """
    r = Regulator()
    clk = Clock(r)
    r.allowed = three(16.0)
    for _ in range(10):
        clk.step(three(4.0), LIMIT, three(0.0), CEILING)
    assert max(r.allowed) == pytest.approx(CEILING)
    assert max(r.allowed) + 4.0 <= LIMIT
