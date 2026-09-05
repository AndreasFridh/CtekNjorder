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

    def step(self, meter, limit, drawn, ceiling, min_current):
        self.t += self.dt
        return self.r.step(self.t, meter, limit, drawn, ceiling, min_current)


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


# ---------- the control law ----------

def test_it_climbs_toward_the_limit_rather_than_jumping():
    """Half the error each step, so the loop converges instead of ringing."""
    r = Regulator()
    clk = Clock(r)
    first = clk.step(three(4.0), LIMIT, three(0.0), CEILING, MIN)[0]
    assert 0 < first <= CEILING
    assert first < CEILING, "a single step must not go straight to full current"


def test_being_over_the_limit_is_corrected_at_once():
    """Shedding is the safe direction and is never delayed."""
    r = Regulator()
    clk = Clock(r)
    r.allowed = three(16.0)
    after = clk.step(three(30.0), LIMIT, three(16.0), CEILING, MIN)[0]
    assert after == pytest.approx(16.0 - 6.0), "the whole 6 A overshoot, in one step"


def test_shedding_measures_from_the_draw_not_from_the_allowance():
    """
    While a car ramps - or simply declines its offer - the allowance sits above
    what is actually being taken. Subtracting the overshoot from the allowance
    then cuts past the right answer, and the next reading cuts past it again,
    walking the setpoint down several amps at a time. That is what turned a
    house with 10 A to spare into a car held at 6 A.
    """
    r = Regulator()
    clk = Clock(r)
    r.allowed = three(13.0)          # granted
    drawn = 12.0                     # actually taken
    house = 14.0
    after = clk.step(three(house + drawn), LIMIT, three(drawn), CEILING, MIN)[0]

    assert after == pytest.approx(LIMIT - house), "should land on the real answer"
    assert after > 6.0, "must not walk down toward the minimum"


def test_a_shed_holds_once_it_is_right_rather_than_creeping_down():
    r = Regulator()
    clk = Clock(r)
    r.allowed = three(13.0)
    drawn = 12.0
    house = 14.0
    first = clk.step(three(house + drawn), LIMIT, three(drawn), CEILING, MIN)[0]
    # The car obeys, and the meter now reflects it.
    second = clk.step(three(house + first), LIMIT, three(first), CEILING, MIN)[0]
    assert second == pytest.approx(first), "shed twice for one overload"


def test_the_allowance_never_exceeds_the_ceiling():
    r = Regulator()
    clk = Clock(r)
    for _ in range(30):
        clk.step(three(0.0), LIMIT, three(16.0), CEILING, MIN)
    assert max(r.allowed) <= CEILING


def test_the_allowance_never_goes_negative():
    r = Regulator()
    clk = Clock(r)
    for _ in range(30):
        clk.step(three(60.0), LIMIT, three(0.0), CEILING, MIN)
    assert min(r.allowed) >= 0.0


# ---------- anti-windup ----------

def test_allowance_cannot_be_banked_while_a_car_declines_it():
    """
    Otherwise the total drifts up to the ceiling against a car that is not
    taking it, and overshoots the moment the car finally draws.
    """
    r = Regulator()
    clk = Clock(r)
    for _ in range(20):
        clk.step(three(5.0), LIMIT, three(4.0), CEILING, MIN)   # car stuck at 4 A
    assert max(r.allowed) <= 4.0 + r.SLACK + 0.01


def test_a_car_that_has_not_started_is_still_offered_enough_to_start():
    """A strict drawn-plus-slack cap would forbid the 6 A a car needs to begin."""
    r = Regulator()
    clk = Clock(r)
    for _ in range(10):
        clk.step(three(4.0), LIMIT, three(0.0), CEILING, MIN)
    assert max(r.allowed) >= MIN


def test_a_car_winding_down_cannot_ratchet_its_own_allowance_to_zero():
    """
    The mirror of the ratchet above, and the reason the two directions read
    opposite ends of the draw window.

    While the car comes down, the reading still contains its old higher draw.
    Cutting relative to the draw the car has ALREADY reached charges it twice
    for the same amps, and each cut lowers the draw again - walking a car that
    could have had 10 A all the way to a pause.
    """
    r = Regulator()
    clk = Clock(r, step_seconds=2.0)
    house = 14.0                     # 10 A genuinely available
    car = 13.0
    r.allowed = three(13.0)
    stale_meter = house + 13.0       # the meter still sees the old draw

    for _ in range(6):
        allowed = clk.step(three(stale_meter), LIMIT, three(car), CEILING, MIN)[0]
        car = allowed                # the car obeys immediately

    assert allowed >= 9.0, (
        f"walked down to {allowed:.1f}A with {LIMIT - house:.0f}A available"
    )


def test_a_ramping_car_cannot_ratchet_its_own_allowance_up():
    """
    Observed on the rig with a meter ten seconds behind: the reading still
    described a house drawing 6 A while the car had already ramped to 8, so the
    allowance was granted from a draw the meter had not seen. The car took it,
    the allowance grew again, and it climbed 9 - 11 - 13 without the meter ever
    getting a say, until the reading finally caught up and forced a hard cut to
    zero. Pairing the stale reading with an equally stale draw stops it.
    """
    r = Regulator()
    clk = Clock(r, step_seconds=2.0)     # stepping faster than the meter reports
    stale_meter = 10.0                   # the house as it was, car at 6 A
    car = 6.0

    for _ in range(6):
        allowed = clk.step(three(stale_meter), LIMIT, three(car), CEILING, MIN)[0]
        car = allowed                    # the car takes whatever it is offered

    assert allowed <= 6.0 + r.SLACK + 0.01, (
        f"allowance ratcheted to {allowed:.1f}A on a meter that never moved"
    )


def test_a_paused_car_is_not_offered_room_that_does_not_exist():
    """
    The dangerous case, seen on the rig. A paused car draws nothing, so the
    meter reading does not contain it and the error is positive even when the
    house has left almost no room. The start floor alone then offered 6 A into
    3.5 A of capacity, which would have taken a 24 A limit to 26.5 A - and it
    sat there for the best part of a minute, because nothing about a positive
    error looks like an overload.
    """
    r = Regulator()
    clk = Clock(r)
    house = 20.5                     # only 3.5 A spare
    r.allowed = three(6.0)

    for _ in range(5):
        allowed = clk.step(three(house), LIMIT, three(0.0), CEILING, MIN)[0]

    assert allowed <= LIMIT - house + 0.01, (
        f"offered {allowed:.1f}A into {LIMIT - house:.1f}A of capacity"
    )
    assert allowed < MIN, "below the legal minimum, so this must snap to a pause"


def test_capacity_does_not_get_in_the_way_when_there_is_plenty():
    """The cap above must bind only when it should, or nothing ever starts."""
    r = Regulator()
    clk = Clock(r)
    for _ in range(5):
        allowed = clk.step(three(4.0), LIMIT, three(0.0), CEILING, MIN)[0]
    assert allowed >= MIN


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
        allowed = clk.step(three(reading), LIMIT, three(car), CEILING, MIN)[0]
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
        allowed = clk.step(three(reading), LIMIT, three(car), CEILING, MIN)[0]
        car = allowed
        pipeline.append(4.0 + car)

    house = 14.0                            # an oven comes on
    tail = []
    for _ in range(25):
        reading = pipeline.pop(0)
        allowed = clk.step(three(reading), LIMIT, three(car), CEILING, MIN)[0]
        car = allowed
        pipeline.append(house + car)
        tail.append(allowed)

    assert max(tail[-8:]) - min(tail[-8:]) < 1.0, "did not settle after the step"
    assert max(tail[-8:]) + house <= LIMIT + 0.5


# ---------- the allowance is bounded by draw, never by our own command ----------

def test_holding_does_not_forget_a_raise_the_balancer_is_waiting_on():
    """
    The regression that made the first version of this useless.

    The balancer holds its setpoint down while it waits out `raise_delay`. If
    the allowance is clamped to that held command, then on any tick where no
    step is taken `hold()` returns the clamped value, the balancer sees a target
    equal to its current setpoint, cancels the pending raise, and restarts the
    timer - which never expires. Charging sits at the cold-start minimum for
    ever with the fuse barely loaded.
    """
    r = Regulator()
    clk = Clock(r)
    clk.step(three(10.0), LIMIT, three(6.0), CEILING, MIN)
    raised = r.allowed[0]
    assert raised > 6.0, "should be asking for more than the car has"

    # Several ticks with no new reading, exactly as the loop does.
    for _ in range(5):
        assert r.hold()[0] == pytest.approx(raised), "the pending raise was lost"


def test_the_allowance_is_bounded_by_draw_not_by_what_was_commanded():
    """
    Draw is measured; the command is our own opinion. An integrator reset by
    its own output cannot climb.
    """
    r = Regulator()
    clk = Clock(r)
    for _ in range(20):
        # Above the 6 A floor, so the draw bound is what is being tested.
        clk.step(three(12.0), LIMIT, three(8.0), CEILING, MIN)
    assert max(r.allowed) <= 8.0 + r.SLACK + 0.01


def test_a_gate_that_stops_the_car_collapses_the_allowance_on_its_own():
    """
    No explicit clamp is needed when charging is withheld: the cars draw
    nothing, and the draw bound alone pulls the allowance back to the floor.
    """
    r = Regulator()
    clk = Clock(r)
    r.allowed = three(16.0)
    for _ in range(10):
        clk.step(three(4.0), LIMIT, three(0.0), CEILING, MIN)
    assert max(r.allowed) == pytest.approx(MIN), (
        "an idle car should leave only enough on offer to restart"
    )
