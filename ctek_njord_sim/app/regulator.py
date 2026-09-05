"""
Deciding how much current EV charging may take, from the meter alone.

The obvious approach is to recover the house load by subtracting the cars out
of the meter reading, then hand back whatever is left:

    headroom = fuse - margin - (meter - car)
             = fuse - margin - meter + car

That trailing `+ car` is a feedback path with a gain of exactly one: what we
allow becomes what the car draws, which returns through the meter and sets what
we allow next. A unity-gain loop with a transport delay oscillates - not as a
defect to be found and removed, but as what the equation does. Gating it makes
it run less often without changing its gain, which is why the cycling here
survived several rounds of that.

So the car is not subtracted. The meter reading is the quantity that must stay
under the fuse, so it is corrected directly and the response becomes a choice:

    error = (fuse - margin) - meter

Raising moves half the error, so the loop converges instead of ringing.
Shedding goes straight to the answer, because the safe direction is never
slowed.

The reading is old, though, and two consequences of that are handled here:

* Nothing is decided until a reading has arrived that postdates the last
  change - see `settling_period`.
* The reading is weighed against a car draw of matching age - see
  `draw_window`.

Pure and side-effect free.
"""
from __future__ import annotations

import statistics
from collections import deque

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

    # Minimum age to assume of a reading. Arrivals say how often the meter
    # publishes, not how stale its value is, and nothing in them can reveal the
    # difference - so it is floored at the usual P1 period. Erring slow only
    # ramps charging up more gently; erring fast is what makes it cycle.
    LAG_FLOOR = 10.0

    def __init__(self):
        self.allowed: list[float] = [0.0] * PHASES
        # What the house could actually spare, as opposed to what is currently
        # on offer. The two differ whenever a car is taking less than the house
        # could support, which is most of the time - and it is the spare, not
        # the offer, that answers "how much could I charge at right now".
        self.spare: list[float] = [0.0] * PHASES
        self.cadence = MeterCadence()
        self._last_reading: float | None = None
        self._stepped_at: float = -1e9
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

        Two clocks, both needing a full settling period: since the allocation
        last changed, so the reading reflects that change rather than the world
        before it; and since we last stepped, so corrections cannot outrun the
        meter even while the allocation sits still.

        Deliberately not conditioned on the reading having CHANGED. Home
        Assistant sends nothing when a value repeats, so a genuinely steady
        house looks identical to a dead feed - and requiring a change froze the
        allowance whenever the house went quiet, which is exactly when there is
        most room to give away. An unchanged reading is still a current
        reading; a feed that has actually died is caught by `stale_timeout`.
        """
        period = self.settling_period(min_period)
        return (now - changed_at) >= period and (now - self._stepped_at) >= period

    # ---------- control ----------

    def draw_window(self, now: float, drawn: list[float]
                    ) -> tuple[list[float], list[float]]:
        """
        The lowest and highest car draw over the last reporting period.

        The reading describes the house as it was up to a period ago, so the
        car draw inside it is that old too and may be nothing like what the car
        is taking now. Rather than guess which moment it belongs to, each
        direction uses the end of the range that cannot cost anything:

        * Granting uses the LOWEST recent draw, so current is never handed out
          against amps the meter has not seen. Otherwise a ramping car ratchets
          its own allowance up until the reading catches up and forces a hard
          cut to zero.
        * Cutting uses the HIGHEST, because that is what the reading still
          contains. Cutting against a draw the car has already come down to
          charges it twice for the same amps, and lowers the draw again - the
          same ratchet downward, ending in a needless pause.
        """
        self._draws.append((now, list(drawn)))
        cutoff = now - self.settling_period()
        # Never below two samples. Steps are paced one settling period apart, so
        # the previous one sits exactly on the cutoff and a fraction of a second
        # of jitter drops it - leaving only the draw taken at the same moment as
        # this call, which is the one the reading definitely does NOT reflect.
        # That is what a shed anchored on the window needs least: it made a car
        # with 10 A of room shed against its own already-reduced draw and stop.
        while len(self._draws) > 2 and self._draws[0][0] < cutoff:
            self._draws.popleft()
        return self._window_bounds()

    def _window_bounds(self) -> tuple[list[float], list[float]]:
        if not self._draws:
            return [0.0] * PHASES, [0.0] * PHASES
        cols = [[d[1][p] if p < len(d[1]) else 0.0 for d in self._draws]
                for p in range(PHASES)]
        return [min(c) for c in cols], [max(c) for c in cols]

    def shed_target(self, meter: list[float], limit: float) -> float:
        """
        The most the cars may take, given a reading that is over the limit.

        The same arithmetic the shed branch of `step` uses, exposed so the
        control loop's independent backstop can reach the same answer without
        waiting for a step to be due. Read-only: it consults the draw window
        but does not advance it.

        The anchor is what the cars were drawing when the reading was taken.
        Subtracting the overshoot from what is currently ALLOWED instead ratchets
        - the allowance falls, the stale reading still shows the same overshoot,
        so it is subtracted again, and a car with room to spare is walked down
        to a pause.
        """
        _, high = self._window_bounds()
        return min(
            max(0.0, high[p] + (limit - (meter[p] if p < len(meter) else 0.0)))
            for p in range(PHASES)
        )

    def step(self, now: float, meter: list[float], limit: float,
             drawn: list[float], ceiling: float) -> list[float]:
        """
        Move the allowance one step toward the limit. Returns amps per phase.

        `meter` is what the meter says, cars included. `drawn` is what the cars
        are actually taking; it is used through `draw_window` so that it
        describes the same moment the reading does.
        """
        self._stepped_at = now
        low, high = self.draw_window(now, drawn)
        for p in range(PHASES):
            reading = meter[p] if p < len(meter) else 0.0
            error = limit - reading
            self.spare[p] = max(0.0, low[p] + error)

            if error < 0:
                # Over the limit: straight to the answer, never gradually.
                #
                # This is the same arithmetic as the old `limit - house`, and it
                # does not bring back the oscillation because it may only ever
                # LOWER the allowance. The ringing came from that formula
                # raising too - what it granted became draw, which came back
                # through the meter and granted more.
                nxt = high[p] + error
            else:
                # Approach the room that actually exists, half the distance at
                # a time. `capacity` is what the house could absorb: the draw
                # already inside the reading, plus whatever is left under the
                # limit. Offering up to it means that even if the cars took
                # every amp on offer, the meter lands at the limit and no
                # higher - so no separate guard against the allowance running
                # ahead of what is drawn is needed. A car that declines its
                # offer simply declines it.
                #
                # A paused car is not in the reading, so the error stays
                # positive even with no room left; capacity is what catches
                # that, and below the legal minimum it snaps to a pause.
                nxt = min(self.allowed[p] + self.RAISE_GAIN * error,
                          low[p] + error)

            self.allowed[p] = max(0.0, min(nxt, ceiling))
        return list(self.allowed)

    def hold(self) -> list[float]:
        return list(self.allowed)
