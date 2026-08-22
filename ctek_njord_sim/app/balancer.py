"""
Load-balancing decision logic.

Pure and side-effect free: feed it measurements, get a setpoint in amps. All
the safety-relevant reasoning lives here so it can be exercised offline against
recorded captures without touching a real charger.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field

from .protocol import PHASE_ROTATIONS

_LOG = logging.getLogger(__name__)


class BaselineFilter:
    """
    Steadies the derived house baseline without ever under-stating it.

    The baseline is not measured, it is inferred: meter minus every car. Both
    terms are sampled at different moments, so while a car ramps the
    subtraction is briefly wrong - and with two cars the errors compound. Left
    raw, the inferred baseline swings several amps when the real one is flat,
    which moves the headroom, which moves the allocation, which makes the cars
    ramp again. The loop feeds itself.

    Holding the highest value seen in the last few seconds fixes it in the only
    direction that is safe. A genuine rise in house load is adopted at once,
    because it is immediately the maximum. A dip is only believed once it has
    persisted for the whole window, which is exactly the behaviour wanted from
    something standing in front of a main fuse: quick to see danger, slow to
    assume it has passed.
    """

    def __init__(self, window: float = 15.0):
        self.window = window
        self._samples: deque[tuple[float, list[float]]] = deque()
        self._last: list[float] | None = None

    def update(self, now: float, baseline: list[float] | None,
               trust: bool = True) -> list[float] | None:
        """
        Add a reading and return the filtered baseline.

        `trust=False` says a car is mid-ramp, so this particular reading is
        known to be wrong and must not be recorded. That matters more than it
        sounds: holding the maximum LATCHES a bad sample for the whole window,
        so a single ramp artefact becomes a sustained phantom load, which cuts
        the allocation, which starts another ramp. Filtering the noise is not
        enough - the corrupted samples have to be kept out in the first place.

        Ramps last a few seconds and the window is much longer, so the previous
        good samples carry through.
        """
        if baseline is None:
            return self._last
        if trust:
            self._samples.append((now, list(baseline)))
        cutoff = now - self.window
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        if not self._samples:
            # Everything aged out during a long ramp; the raw reading is all
            # there is, and over-stating the baseline is the safe way to err.
            return self._last if self._last is not None else list(baseline)
        width = len(baseline)
        self._last = [max(s[1][p] for s in self._samples) for p in range(width)]
        return self._last


@dataclass
class BalancerConfig:
    main_fuse: float
    max_charge_current: int = 16
    safety_margin: float = 1.0
    phase_rotation: str = "RST"
    raise_delay: float = 30.0
    stale_timeout: float = 30.0
    fallback_current: int = 6
    # After changing the setpoint the car takes a few seconds to follow. While
    # it slews, meter reading and reported draw describe different instants and
    # the derived baseline is meaningless - so we stop deciding on it.
    settle_window: float = 10.0
    settle_tolerance: float = 1.5
    # Defaults, overwritten by the charger's retained configuration topic.
    min_allowed_current: int = 6
    charger_fuse_rating: int = 16


def car_draw_for_baseline(
    charger_current: list[float] | None,
    charger_age: float,
    stale_timeout: float,
) -> list[float]:
    """
    How much of the meter reading to attribute to the car.

    Subtracting the car is what stops its own draw from counting against its
    own allowance. But the subtraction is only safe while we can still see
    what it is drawing: subtract MORE than the car really takes and the
    baseline is understated, the headroom overstated, and we hand out current
    that is not there.

    So when the charger's telemetry goes stale, attribute nothing. The meter
    reading is real regardless, and treating all of it as house load can only
    under-estimate the spare capacity - never over-estimate it. That is the
    direction we want to be wrong in.
    """
    if charger_age > stale_timeout:
        return [0.0, 0.0, 0.0]
    return list(charger_current or [0.0, 0.0, 0.0])


@dataclass
class Decision:
    setpoint: int
    reason: str
    headroom: list[float] = field(default_factory=list)
    baseline: list[float] = field(default_factory=list)
    changed: bool = False


class Balancer:
    def __init__(self, cfg: BalancerConfig):
        self.cfg = cfg
        self.setpoint: int | None = None
        self._raise_pending_since: float | None = None
        self._changed_at: float | None = None

    def _is_settling(self, now: float, charger_current: list[float]) -> bool:
        """
        True while the car is still moving toward a setpoint we recently issued.

        Time-bounded on purpose: a car that has finished charging draws less
        than its allowance forever, and that must not freeze the setpoint.
        """
        if self.setpoint is None or self._changed_at is None:
            return False
        if (now - self._changed_at) >= self.cfg.settle_window:
            return False
        drawn = max(charger_current) if charger_current else 0.0
        return abs(drawn - self.setpoint) > self.cfg.settle_tolerance

    def _ceiling(self) -> int:
        """Never exceed the user's limit or the charger's own fuse rating."""
        return min(self.cfg.max_charge_current, self.cfg.charger_fuse_rating)

    def _snap(self, amps: float) -> int:
        """
        Clamp to the legal set: 0, or min_allowed..ceiling.

        There is no valid value between 1 and min_allowed - below that floor an
        EV must stop rather than charge slower, so we command 0 (pause).
        """
        target = int(amps)  # floor, never round up toward the fuse
        target = min(target, self._ceiling())
        if target < self.cfg.min_allowed_current:
            return 0
        return target

    def compute(
        self,
        now: float,
        house_current: list[float] | None,
        house_age: float,
        charger_current: list[float] | None,
        ev_uses_phase: list[int] | None,
    ) -> Decision:
        cfg = self.cfg

        # Flying blind is the one case where we must not compute a setpoint.
        if not house_current or house_age > cfg.stale_timeout:
            why = "no house data" if not house_current else f"house data {house_age:.0f}s old"
            self._raise_pending_since = None
            # Carry the fallback as headroom, not just as a setpoint. The
            # allocator divides headroom, so returning none of it would hand
            # every charger 0 A and the configured fallback would never reach
            # one - the setting would look present and do nothing.
            return self._commit(
                now, cfg.fallback_current, f"FALLBACK: {why}",
                [float(cfg.fallback_current)] * 3, [],
            )

        rot = PHASE_ROTATIONS.get(cfg.phase_rotation, (0, 1, 2))
        charger_current = charger_current or [0.0, 0.0, 0.0]

        # The meter sees the whole house including the car. Subtract the car's
        # own draw to recover the rest of the load, mapping each station phase
        # onto the meter phase it actually feeds.
        baseline = list(house_current)
        for station_phase, meter_phase in enumerate(rot):
            if station_phase < len(charger_current) and meter_phase < len(baseline):
                baseline[meter_phase] -= charger_current[station_phase]
        baseline = [max(0.0, b) for b in baseline]  # clamp measurement noise

        headroom = [cfg.main_fuse - cfg.safety_margin - b for b in baseline]

        # Only the phases the EV actually loads can constrain it.
        used = [
            i
            for i in range(3)
            if not ev_uses_phase or (i < len(ev_uses_phase) and ev_uses_phase[i])
        ]
        if not used:
            used = [0, 1, 2]
        limiting = min(headroom[rot[i]] for i in used if rot[i] < len(headroom))

        target = self._snap(limiting)

        # Cold start: the real adapter commands MinAllowedCurrent before it
        # trusts its first readings, then jumps to the computed value ~26s
        # later. Copy that - it is the conservative order of operations.
        if self.setpoint is None:
            return self._commit(
                now,
                min(target, cfg.min_allowed_current),
                f"cold start at minimum ({target}A pending)",
                headroom,
                baseline,
            )

        # While the car is still slewing toward a setpoint we just issued, the
        # meter still reflects its previous draw, so `house - car` over-states
        # the baseline and we would throttle against our own transient. Hold
        # until it settles.
        #
        # Pausing is exempt: commanding 0 A is always safe, so a genuine
        # overload is never delayed by this guard.
        if target > 0 and self._is_settling(now, charger_current):
            return Decision(
                self.setpoint,
                f"holding {self.setpoint}A while the car settles "
                f"(transient target {target}A ignored)",
                headroom,
                baseline,
            )

        # Asymmetric response: shed load immediately, restore it slowly.
        # Raising eagerly makes the setpoint oscillate, because the car ramps up
        # to follow it and that new draw eats the very headroom that allowed it.
        if self.setpoint is not None and target > self.setpoint:
            if self._raise_pending_since is None:
                self._raise_pending_since = now
            waited = now - self._raise_pending_since
            if waited < cfg.raise_delay:
                remaining = cfg.raise_delay - waited
                return Decision(
                    self.setpoint,
                    f"holding {self.setpoint}A, raise to {target}A in {remaining:.0f}s",
                    headroom,
                    baseline,
                )
        else:
            self._raise_pending_since = None

        return self._commit(
            now, target, f"headroom {limiting:.1f}A limits phases {used}",
            headroom, baseline
        )

    def _commit(self, now: float, setpoint: int, reason: str,
                headroom=None, baseline=None) -> Decision:
        self._raise_pending_since = None
        changed = setpoint != self.setpoint
        previous = self.setpoint
        self.setpoint = setpoint
        if changed:
            self._changed_at = now
            _LOG.info("setpoint %sA -> %sA (%s)", previous, setpoint, reason)
        return Decision(setpoint, reason, headroom or [], baseline or [], changed)
