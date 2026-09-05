"""
Deciding how much current EV charging may take, from the meter alone.

The previous approach subtracted the cars out of the meter reading to recover
the house load, then handed back whatever was left:

    headroom = fuse - margin - (meter - car)
             = fuse - margin - meter + car

That trailing `+ car` is a feedback path with a gain of exactly one. What we
allow becomes what the car draws, which comes back through the meter a few
seconds later and sets what we allow next. A loop with unity gain and a
transport delay oscillates; that is not a defect that can be found and removed,
it is what the equation does. Gating it - refusing to sample while a car ramps,
waiting a fixed lag - makes the loop run less often without changing its gain,
which is why the cycling kept coming back on a slower meter.

So the car is not subtracted at all here. The meter reading is the quantity
that must stay under the fuse, so it is regulated directly:

    error = (fuse - margin) - meter
    allowed += gain * error

Now the response is ours to choose, and the two directions are chosen
differently. Raising moves half the error, so the loop converges geometrically
instead of ringing. Shedding goes straight to the answer, measured from what
the cars are actually drawing at the same instant the meter was read - immediate
because the safe direction must never be slow, and anchored on draw rather than
on the allowance so that a lagging meter cannot walk the setpoint downward a
few amps at a time.

Two things make this work with a slow meter, and both are automatic:

* A step is only taken on a reading that postdates the last change by at least
  one of the meter's own reporting periods, so every correction is computed
  from a reading that actually reflects the previous one. The period is
  measured rather than configured, because it differs per household.

* An allowance cannot be banked. It may exceed what the cars are really taking
  by a small margin and no more, so the total cannot creep up to the ceiling
  while a car declines it and then overshoot when it finally draws.

  That bound is deliberately measured against what the cars DRAW, never against
  what they were commanded. Clamping to the command instead looks equivalent and
  deadlocks: the balancer holds the setpoint down while it waits out its raise
  delay, so clamping to it throws away the pending raise every tick, the target
  falls back to the current setpoint, the delay restarts, and the allowance
  never rises off the cold-start minimum. Draw is physical; the command is our
  own opinion, and an integrator must not be reset by its own output.

Pure and side-effect free.
"""
from __future__ import annotations

import logging
import statistics
from collections import deque

_LOG = logging.getLogger(__name__)

PHASES = 3


class MeterCadence:
    """
    How often the meter actually reports, measured rather than configured.

    Home Assistant only sends an event when a value changes, so intervals
    between arrivals are an upper bound on the meter's period, never a lower
    one. The median of recent gaps is therefore a fair estimate while anything
    is moving, and a steady house simply keeps the last estimate - which is
    correct, because nothing needs deciding while nothing changes.
    """

    DEFAULT = 10.0        # a P1 meter's usual period, until we have measured
    MIN = 1.0
    MAX = 120.0

    def __init__(self, samples: int = 20):
        self._gaps: deque[float] = deque(maxlen=samples)
        self._last: float | None = None

    def observe(self, arrived: float) -> None:
        if self._last is not None:
            gap = arrived - self._last
            # Ignore duplicates and absurd gaps; a reconnection can leave a
            # minutes-long hole that says nothing about the meter's rate.
            if self.MIN <= gap <= self.MAX:
                self._gaps.append(gap)
        self._last = arrived

    @property
    def period(self) -> float:
        if not self._gaps:
            return self.DEFAULT
        return statistics.median(self._gaps)

    @property
    def measured(self) -> bool:
        return len(self._gaps) >= 3

    def as_dict(self) -> dict:
        return {
            "period": round(self.period, 1),
            "measured": self.measured,
            "samples": len(self._gaps),
        }


class Regulator:
    """Per-phase incremental control of the total current EV charging may take."""

    RAISE_GAIN = 0.5      # half the error, so the loop converges rather than rings
    SLACK = 3.0           # amps of allowance permitted above what is drawn

    # How long to assume a reading takes to reflect a change, at minimum.
    #
    # `MeterCadence` measures how often readings ARRIVE, which is not the same
    # as how old they are - a meter can publish every two seconds and still be
    # reporting the house as it was ten seconds ago, and several do. Pacing on
    # the arrival rate alone then steps five times before the first correction
    # is visible, and each step compounds the last.
    #
    # There is no way to measure the difference from arrivals alone, so it is
    # floored instead. Ten seconds is the usual P1 period and no meter of this
    # kind reflects a change faster. Being slower than necessary only makes
    # charging ramp up a little more gently; being faster than the truth is
    # what makes it cycle.
    LAG_FLOOR = 10.0

    def __init__(self):
        self.allowed: list[float] = [0.0] * PHASES
        self.cadence = MeterCadence()
        self._last_reading: float | None = None
        self._draws: deque[tuple[float, list[float]]] = deque()

    # ---------- pacing ----------

    def note_reading(self, arrived: float) -> bool:
        """Record a meter arrival. True if this is one we have not seen."""
        if self._last_reading is not None and arrived <= self._last_reading:
            return False
        self._last_reading = arrived
        self.cadence.observe(arrived)
        return True

    def settling_period(self, min_period: float = 0.0) -> float:
        """
        How long a change needs before a reading can be expected to show it.

        The measured arrival rate is a lower bound on this, never an upper one,
        so it is floored - see LAG_FLOOR. `min_period` is the user's own floor
        on top, for a meter whose readings arrive irregularly.
        """
        return max(self.cadence.period, self.LAG_FLOOR, min_period)

    def ready(self, now: float, changed_at: float, min_period: float = 0.0) -> bool:
        """
        Is it safe to act on the reading in hand?

        Only once a full settling period has passed since the last change, so
        the reading being used was taken after that change took effect. Acting
        sooner means correcting against the world as it was before, which is
        precisely how the oscillation started.
        """
        return (now - changed_at) >= self.settling_period(min_period)

    # ---------- control ----------

    def draw_window(self, now: float, drawn: list[float]
                    ) -> tuple[list[float], list[float]]:
        """
        The lowest and highest car draw over the last reporting period.

        The reading in hand describes the house as it was up to a period ago,
        so the car draw inside it is that old too, and what the car is taking
        this second may be nothing like it. Rather than guess which moment the
        reading belongs to, each direction is paired with the end of the window
        that cannot cost us anything:

        * Granting more uses the LOWEST recent draw, so current is never handed
          out against amps the meter has not seen yet. Without this a ramping
          car ratchets its own allowance - granted from a draw the meter is
          blind to, it takes it, and is granted more, climbing until the reading
          catches up and forces a hard cut to zero.

        * Cutting back uses the HIGHEST recent draw, because that is what the
          reading still contains. Cutting relative to a draw the car has already
          come down to charges it twice for the same amps, and the second cut
          lowers the draw again - a downward ratchet that walks a car with 10 A
          available all the way to a pause.

        Both ends therefore err toward less current, which is the direction to
        be wrong in.
        """
        self._draws.append((now, list(drawn)))
        cutoff = now - self.settling_period()
        while len(self._draws) > 1 and self._draws[0][0] < cutoff:
            self._draws.popleft()
        cols = [[d[1][p] if p < len(d[1]) else 0.0 for d in self._draws]
                for p in range(PHASES)]
        return [min(c) for c in cols], [max(c) for c in cols]

    def step(self, now: float, meter: list[float], limit: float,
             drawn: list[float], ceiling: float,
             min_current: float) -> list[float]:
        """
        Move the allowance one step toward the limit. Returns amps per phase.

        `meter` is what the meter says, cars included. `drawn` is what the cars
        are actually taking; it is used through `draw_window` so that it
        describes the same moment the reading does.
        """
        low, high = self.draw_window(now, drawn)
        for p in range(PHASES):
            reading = meter[p] if p < len(meter) else 0.0
            error = limit - reading

            # What the cars could take without putting the meter over. The
            # draw term matters because the reading already contains it: the
            # spare capacity is what is left over PLUS what the cars are
            # already accounted for in that reading.
            if error < 0:
                # Over the limit. Go straight to the answer - the safe
                # direction is never approached gradually - measured against
                # the highest recent draw, which is what the reading still
                # contains. Anchoring on the allowance instead over-sheds
                # whenever the allowance sits above the draw, and each cut then
                # over-sheds again: a house needing 10 A of headroom walked all
                # the way down to a pause.
                #
                # This is the same arithmetic as the old `limit - house` and it
                # does NOT bring back the oscillation, because it is only ever
                # allowed to LOWER the allowance. The ringing came from that
                # formula raising as well: what it granted became draw, which
                # came back through the meter and granted more. Raising is
                # damped and gradual below; shedding is exact and immediate.
                nxt = high[p] + error
            else:
                capacity = low[p] + error
                nxt = self.allowed[p] + self.RAISE_GAIN * error
                # Anti-windup. Allowance the cars are not taking must not
                # accumulate, or the total drifts to the ceiling while a car
                # declines it and then overshoots when it finally draws. The
                # floor keeps enough on offer for a car to be able to start at
                # all, which a strict drawn+slack cap would forbid.
                nxt = min(nxt, max(low[p] + self.SLACK, min_current))
                # ...but capacity outranks that floor. A paused car sees a
                # meter reading that does not include it, so the error is
                # positive even when there is no room: with the house at 20.5 A
                # of a 24 A limit the floor alone offered 6 A, which would have
                # taken the meter to 26.5 A. Below the legal minimum this snaps
                # to a pause further up, which is the right answer.
                nxt = min(nxt, capacity)

            self.allowed[p] = max(0.0, min(nxt, ceiling))
        return list(self.allowed)

    def hold(self) -> list[float]:
        return list(self.allowed)
