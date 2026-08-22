"""
Sharing the available current between several chargers.

This is the thing the Nanogrid Air cannot do: it controls exactly one charger.
With more than one, the meter reading contains every car at once, so the house
baseline is `meter - sum(all cars)`, and whatever headroom that leaves has to be
divided.

Two decisions matter and they are separate:

* **Who gets any current at all.** A charger with no car plugged in must not
  hold an allocation, or it strands current nobody is using. A car that has
  finished charging looks identical to an empty charger from the outside, which
  is why demand is inferred from behaviour rather than trusted from a flag.

* **How much each one gets.** `even` splits equally. `optimal` also notices
  that a car is not taking everything it was offered - because of its own
  internal limit, or because it is tapering near full - and hands the surplus
  to a car that can use it.

Allocation is max-min fair with per-charger caps and per-phase limits: everyone
rises together until they hit their own ceiling or a phase fills up, and
whatever they leave behind is shared out again. Pure and side-effect free, so
the awkward cases can be tested without hardware.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

_LOG = logging.getLogger(__name__)

EPS = 1e-9

# Distinct from None, which is a State the charger can actually report.
_UNSET = object()


@dataclass
class ChargerDemand:
    """What one charger could use, as the allocator sees it."""

    id: str
    order: int                       # config order, used to break ties
    phases: tuple[int, ...] = (0, 1, 2)   # meter phases this charger loads
    min_current: int = 6
    max_current: int = 16
    cap: float = 16.0                # what it can actually use right now
    wants: bool = True               # is there a car asking to charge
    charging: bool = False           # is it drawing now (preferred when scarce)


@dataclass
class Allocation:
    per_charger: dict[str, int] = field(default_factory=dict)
    served: list[str] = field(default_factory=list)
    starved: list[str] = field(default_factory=list)
    reason: str = ""


def _phase_used(alloc: dict[str, float], chargers: list[ChargerDemand], p: int) -> float:
    return sum(alloc[c.id] for c in chargers if p in c.phases)


def water_fill(headroom: list[float], chargers: list[ChargerDemand]) -> dict[str, float]:
    """
    Max-min fair share subject to each charger's cap and each phase's limit.

    Everyone rises at the same rate. A charger stops rising when it reaches its
    own cap, or when a phase it sits on is full; the rest carry on with what is
    left. That is what redistributes the current a limited car declines.
    """
    alloc = {c.id: 0.0 for c in chargers}
    if not chargers:
        return alloc

    unsaturated = {c.id for c in chargers}
    # Each pass either retires a charger or fills a phase, so this terminates
    # well inside the bound; the bound only guards against a float edge case.
    for _ in range(len(chargers) * 4 + 8):
        if not unsaturated:
            break

        counts = [sum(1 for c in chargers if c.id in unsaturated and p in c.phases)
                  for p in range(len(headroom))]
        steps = [(headroom[p] - _phase_used(alloc, chargers, p)) / counts[p]
                 for p in range(len(headroom)) if counts[p] > 0]
        if not steps:
            break
        step = min(steps)

        room = min(c.cap - alloc[c.id] for c in chargers if c.id in unsaturated)
        step = min(step, room)
        if step <= EPS:
            break

        for c in chargers:
            if c.id in unsaturated:
                alloc[c.id] += step

        for c in chargers:
            if c.id not in unsaturated:
                continue
            at_cap = alloc[c.id] >= c.cap - EPS
            phase_full = any(
                headroom[p] - _phase_used(alloc, chargers, p) <= EPS for p in c.phases
            )
            if at_cap or phase_full:
                unsaturated.discard(c.id)

    return alloc


def allocate(
    headroom: list[float],
    demands: list[ChargerDemand],
    strategy: str = "optimal",
    total_cap: float | None = None,
) -> Allocation:
    """
    Turn available headroom into one integer setpoint per charger.

    Chargers with no car asking are given 0 rather than a share, so an empty
    charger never strands current a waiting car could use.

    `total_cap` limits the sum across all chargers regardless of phase. It is
    how the balancer's own judgement - cold start, the raise delay, the
    fallback when meter data goes stale - keeps governing the total once the
    allocator is deciding the split.
    """
    result = Allocation(per_charger={d.id: 0 for d in demands})
    # Report whichever limit is actually binding. During the balancer's raise
    # delay the total cap is far below the phase headroom, and blaming the
    # phases for that would send someone hunting a house-load problem that is
    # not there.
    spare = min(headroom) if headroom else 0.0
    if total_cap is not None:
        spare = min(spare, max(0.0, total_cap))

    active = [d for d in demands if d.wants]
    if not active:
        result.reason = "no charger has a car asking to charge"
        return result

    if total_cap is not None:
        # An extra limit that every charger counts against, which is exactly
        # what "no more than this in total" means.
        limit_index = len(headroom)
        headroom = list(headroom) + [max(0.0, total_cap)]
        active = [
            ChargerDemand(**{**d.__dict__,
                             "phases": tuple(d.phases) + (limit_index,)})
            for d in active
        ]

    if strategy == "even":
        # Ignore what a car will actually take, and split equally. Predictable,
        # but it leaves current idle when one car cannot use its share.
        active = [
            ChargerDemand(**{**d.__dict__, "cap": float(d.max_current)}) for d in active
        ]

    # Below its minimum a car must stop rather than charge slowly, so when
    # there is not enough for everyone we serve fewer of them properly instead
    # of all of them illegally. Cars already charging are kept in preference to
    # ones that have not started, which stops the set churning every tick.
    order = sorted(active, key=lambda d: (not d.charging, d.order))
    candidates = list(order)

    while candidates:
        raw = water_fill(headroom, candidates)
        if all(raw[c.id] >= c.min_current - EPS for c in candidates):
            break
        candidates.pop()
    else:
        result.reason = (
            f"only {spare:.1f}A available - not enough for any charger's "
            f"{active[0].min_current}A minimum"
        )
        result.starved = [d.id for d in active]
        return result

    # Floor rather than round: rounding up would spend headroom we do not have.
    for c in candidates:
        result.per_charger[c.id] = min(int(raw[c.id]), c.max_current)

    result.served = [c.id for c in candidates]
    result.starved = [d.id for d in active if d.id not in result.per_charger
                      or result.per_charger[d.id] == 0]

    _assert_within_phase_limits(headroom, candidates, result.per_charger)

    shared = sum(result.per_charger.values())
    if len(candidates) == 1:
        result.reason = f"{shared}A to the only active charger"
    else:
        result.reason = (
            f"{shared}A shared between {len(candidates)} charging "
            f"({strategy})"
        )
        if result.starved:
            result.reason += f", {len(result.starved)} waiting for capacity"
    return result


def _assert_within_phase_limits(headroom, chargers, alloc) -> None:
    """
    Last line of defence.

    The allocator is the only thing standing between several cars and a main
    fuse, so the result is checked against the limit it was derived from rather
    than assumed correct. Flooring can only reduce a total, so tripping this
    means a real bug.
    """
    for p in range(len(headroom)):
        total = sum(alloc[c.id] for c in chargers if p in c.phases)
        if total > headroom[p] + EPS:
            _LOG.error(
                "Allocation exceeded phase %d headroom (%.1fA > %.1fA); "
                "scaling back", p + 1, total, headroom[p]
            )
            for c in chargers:
                if p in c.phases:
                    alloc[c.id] = 0


def apply_dwell(
    now: float,
    proposed: dict[str, int],
    previous: dict[str, int],
    changed_at: dict[str, float],
    dwell: float,
    restart_hold: float = 90.0,
    stopped_at: dict[str, float] | None = None,
) -> dict[str, int]:
    """
    Hold a charger steady before changing it again.

    Every change makes a car ramp, and while it ramps the meter reading and the
    reported draw describe different instants - so the inferred baseline moves,
    the headroom moves, and the split comes out slightly different. With two
    cars that feedback sustains itself and the setpoints never stop twitching.

    A one-amp adjustment therefore waits out `dwell`. One amp is roughly 0.2 kW
    and nothing is at risk meanwhile, whereas an allowance that jitters every
    few seconds is a genuine annoyance.

    **Stopping is always immediate. Restarting never is.** That asymmetry is the
    important part. Cars do not treat a charging session as free: several
    stop-start cycles in quick succession and many will fault and refuse to
    charge until they are unplugged and plugged back in. So pausing stays
    instant, because that is the direction that protects the fuse, but coming
    back off zero waits `restart_hold` - long enough for a slow meter to have
    caught up, so the restart is made on real information rather than on the
    tail of the last change.
    """
    out: dict[str, int] = {}
    stopped_at = stopped_at if stopped_at is not None else {}

    for cid, want in proposed.items():
        prev = previous.get(cid, 0)

        if want <= 0:
            if prev > 0:
                stopped_at[cid] = now
                changed_at[cid] = now
            out[cid] = 0
            continue

        if prev <= 0:
            waited = now - stopped_at.get(cid, -1e9)
            if waited < restart_hold:
                out[cid] = 0          # still settling after the last stop
                continue
            changed_at[cid] = now
            out[cid] = want
            continue

        urgent = want <= prev - 2
        if not urgent and want != prev and (now - changed_at.get(cid, -1e9)) < dwell:
            want = prev
        if want != prev:
            changed_at[cid] = now
        out[cid] = want
    return out


class DemandTracker:
    """
    Works out what each charger can actually use, from what it does.

    The charger reports a `State`, but only the charging value has ever been
    observed on real hardware, so its other values cannot be relied on. What can
    be relied on is current: a car that is offered 16 A and steadily takes 11 is
    limited to about 11, and a car taking nothing at all is not charging.

    Both readings are only meaningful once the car has had time to respond to
    the last thing we told it, which is why every judgement here is gated on how
    long the offer has been stable.
    """

    DRAWING_THRESHOLD = 0.5     # amps that count as "a car is taking current"
    PROBE_EVERY = 300.0         # how often to re-offer a charger we think is empty
    PROBE_FOR = 45.0            # how long each of those offers stands
    SETTLE = 20.0               # seconds before a draw is treated as settled
    IDLE_AFTER = 120.0          # seconds of nothing before we call it idle
    HEADROOM = 1.0              # amps of slack left above an observed limit

    # How close to its allowance a car must be before we call it satisfied.
    # This must be TIGHTER than HEADROOM, and the two must not be the same
    # constant: a cap of `drawn + HEADROOM` means a limited car settles exactly
    # HEADROOM below its allowance, so testing satisfaction at that same
    # distance declares it satisfied and the surplus is never noticed. The
    # result is a stable, silently wasteful equilibrium - two cars pinned at
    # 8 A with 20 A available, which is precisely what this caused.
    SATISFIED_WITHIN = 0.4

    def __init__(self):
        self._since: dict[str, float] = {}
        self._last_setpoint: dict[str, int] = {}
        self._last_draw: dict[str, float] = {}
        self._cap: dict[str, float] = {}
        self._state: dict[str, object] = {}
        self._empty_since: dict[str, float] = {}

    def update(self, now: float, cid: str, setpoint: int, drawn: float) -> None:
        """
        Note the current offer, and decide whether it restarts the clock.

        Only a LARGER offer does. Restarting on any change looks right and is
        badly wrong: capping a limited car lowers its setpoint, which would
        restart the clock, which would drop the cap, which would raise the
        setpoint again. That feedback loop makes the setpoint flap every
        settle window and the cap can never survive its own effect.

        A smaller offer asks nothing new of the car, so nothing is re-judged.
        """
        previous = self._last_setpoint.get(cid)
        if previous is None or setpoint > previous:
            self._since[cid] = now
            self._cap.pop(cid, None)     # a bigger offer deserves a fresh answer
        self._last_setpoint[cid] = setpoint
        self._last_draw[cid] = drawn

    def stable_for(self, now: float, cid: str) -> float:
        return now - self._since.get(cid, now)

    def wants_current(self, now: float, cid: str, state: int | None, drawn: float) -> bool:
        """
        Is there a car here asking to charge?

        `State == 2` is the one value confirmed against real hardware. Anything
        drawing current is obviously active regardless. A charger we have
        already paused draws nothing by definition, so it cannot be judged this
        way at all - see `should_probe`.
        """
        if drawn > self.DRAWING_THRESHOLD:
            self._empty_since.pop(cid, None)
            return True

        # A change of State means something happened at the charger, and a car
        # being plugged in is the likeliest thing. Only 2 has ever been
        # confirmed, but a *change* is informative whatever the values mean.
        previous = self._state.get(cid, _UNSET)
        self._state[cid] = state
        if previous is not _UNSET and previous != state:
            self._empty_since.pop(cid, None)
            return True

        empty_since = self._empty_since.get(cid)
        if empty_since is None:
            # Current offered and not taken, for long enough, settles it -
            # whatever State says. Draw is measured; State's meaning is
            # unverified beyond "2 happens while charging", and a charger that
            # reports 2 with nothing plugged in would otherwise hold an
            # allocation for ever on the strength of a flag we cannot read.
            #
            # A charger we paused cannot draw what it was not offered, so its
            # silence proves nothing there. Only an unused offer is evidence.
            offered = self._last_setpoint.get(cid, 0) > 0
            if offered and self.stable_for(now, cid) >= self.IDLE_AFTER:
                self._empty_since[cid] = now
                return False
            if state == 2:
                return True
            return True

        # Concluded empty. Look again now and then, because a car plugged in
        # while the charger was paused has no way of telling us.
        cycle = (now - empty_since) % (self.PROBE_EVERY + self.PROBE_FOR)
        return cycle >= self.PROBE_EVERY

    def believed_empty(self, cid: str) -> bool:
        """Whether we have concluded there is no car here."""
        return cid in self._empty_since

    def cap_for(self, now: float, cid: str, setpoint: int, drawn: float,
                max_current: int, min_current: int = 6) -> float:
        """
        The most this car looks able to use.

        Only narrowed once the offer has been stable long enough for the car to
        have reached it - during a ramp the draw always trails the offer, and
        reading that as an internal limit would ratchet the car down to nothing.
        """
        if setpoint <= 0 or self.stable_for(now, cid) < self.SETTLE:
            # Hold whatever we already concluded rather than reverting to the
            # maximum, which would undo the cap the moment it took effect.
            return self._cap.get(cid, float(max_current))

        # Drawing nothing at all is absence, not a limit. Reading it as one
        # recorded an empty charger as a ~1 A car - below the legal minimum, so
        # it could never be served again, and because a paused charger cannot
        # demonstrate demand nothing would ever correct it. That deadlock shows
        # up as "waiting for capacity" with capacity plainly available.
        if drawn <= self.DRAWING_THRESHOLD:
            self._cap.pop(cid, None)
            return float(max_current)

        if drawn >= setpoint - self.SATISFIED_WITHIN:
            # Taking everything offered - but that only means it wants MORE if
            # it was offered at least as much as we already believe it can use.
            # A car capped at 8 that happens to be allocated 7 will always look
            # satisfied, and treating that as evidence re-probes it forever:
            # raise it, watch it not use the extra, cap it again, repeat.
            # Compared against the cap itself, not its whole-amp floor: a cap
            # of 7.9 floors to 7, and an allocation of 7 would then look like a
            # full offer and clear it. Jitter alone would keep re-probing.
            remembered = self._cap.get(cid)
            if remembered is None or setpoint >= remembered - EPS:
                self._cap.pop(cid, None)
                return float(max_current)
            return remembered

        # Straight from the observed draw, deliberately without rounding up
        # first: ceiling before adding the slack stacks two lots of headroom
        # and the car ends up holding ~2 A it will never take. One amp of slack
        # is enough for the car to show it wants more - the moment it does take
        # its allowance the branch above lifts the cap again.
        cap = min(self._cap.get(cid, float(max_current)), drawn + self.HEADROOM)
        # Never below the minimum: the only legal setpoints are 0 or
        # min_current upward, so a lower cap cannot be acted on - it just makes
        # the charger unservable while looking like a considered decision.
        cap = max(cap, float(min_current))
        self._cap[cid] = cap
        return cap
