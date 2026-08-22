"""
Charging sessions and what they cost.

Two things are tracked per charger: how much energy a session has taken, and
what that energy cost at the price in force while it was taken.

Energy needs no new plumbing. The charger already publishes a lifetime Wh
counter, so the difference between two readings is the energy used between
them. Cost is accumulated the same way, increment by increment, because on an
hourly tariff the price changes *during* a session - multiplying the final
total by the final price would be wrong for every session that spans a change.

Pure and side-effect free, so the awkward parts - a counter that resets when a
charger reboots, a price that arrives late, a session that ends while the price
is unknown - can be tested without hardware.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

_LOG = logging.getLogger(__name__)

# Home Assistant reports booleans in several spellings depending on the entity
# type: input_boolean and switch use on/off, a template sensor may well say
# "true" or "1". Accept the lot.
TRUTHY = {"1", "true", "on", "yes", "enable", "enabled", "open", "home"}
FALSEY = {"0", "false", "off", "no", "disable", "disabled", "closed", "away"}


def charging_allowed(state: str | None) -> bool:
    """
    Should charging be permitted at all?

    Unset, unknown, unavailable and unrecognised all mean **yes**. This gate
    exists to save money, not to keep anyone safe, and the failure it must not
    have is a sensor dropping out overnight and silently leaving a car
    uncharged. Refusing to charge is the answer only when something explicitly
    says so.
    """
    if state is None:
        return True
    value = str(state).strip().lower()
    if value in FALSEY:
        return False
    if value in TRUTHY:
        return True
    if value in ("unknown", "unavailable", "none", ""):
        return True
    # A number is a fair way to express a gate too.
    try:
        return float(value) != 0.0
    except ValueError:
        _LOG.debug("Unrecognised charge-enable state %r; treating as enabled", state)
        return True


def price_per_kwh(value: float | None, unit: str | None) -> float | None:
    """
    Normalise a price entity to currency per kWh.

    Nordic tariff sensors publish in both SEK/kWh and öre/kWh, and the factor
    of a hundred between them is the difference between a plausible number and
    a nonsensical one, so the unit is read rather than assumed.
    """
    if value is None:
        return None
    u = (unit or "").strip().lower()
    if u.startswith(("öre", "ore", "cent", "øre", "p/")):
        return value / 100.0
    return value


@dataclass
class Session:
    started: float
    energy_wh: float = 0.0
    cost: float = 0.0
    ended: float | None = None
    peak_current: float = 0.0
    charging_s: float = 0.0        # time actually drawing, which is not the
                                   # same as elapsed once load balancing pauses
                                   # a car through a household peak
    min_price: float | None = None
    max_price: float | None = None
    _counter: int | None = field(default=None, repr=False)
    _last_seen: float | None = field(default=None, repr=False)

    @property
    def duration(self) -> float:
        return (self.ended or time.time()) - self.started

    @property
    def avg_current(self) -> float:
        """Averaged over time spent charging, not over the whole session."""
        if self.charging_s <= 0:
            return 0.0
        # Energy in Wh over hours charging gives watts; three phases at ~230 V.
        watts = self.energy_wh / (self.charging_s / 3600.0)
        return watts / (3 * 230.0)

    def as_record(self, charger_id: str, name: str, currency: str) -> dict:
        energy_kwh = self.energy_wh / 1000.0
        return {
            "charger": charger_id,
            "name": name,
            "started": round(self.started, 1),
            "ended": round(self.ended or time.time(), 1),
            "duration_s": round(self.duration),
            "charging_s": round(self.charging_s),
            "energy_kwh": round(energy_kwh, 3),
            "cost": round(self.cost, 3),
            "currency": currency,
            "peak_current": round(self.peak_current, 1),
            "avg_current": round(self.avg_current, 1),
            "avg_price": round(self.cost / energy_kwh, 3) if energy_kwh > 0.001 else None,
            "min_price": self.min_price,
            "max_price": self.max_price,
        }


class CostTracker:
    """Follows each charger's current session and totals the cost."""

    START_ABOVE = 0.5      # amps that count as "a session has begun"
    IDLE_GRACE = 120.0     # seconds below that before the session is closed
    KEEP_SESSIONS = 20     # completed sessions retained per charger

    def __init__(self, on_complete=None):
        self.active: dict[str, Session] = {}
        self.completed: dict[str, list[Session]] = {}
        self._drawing_since: dict[str, float] = {}
        # Called with (charger_id, Session) when a session ends, so storage is
        # somebody else's problem.
        self._on_complete = on_complete

    def update(self, now: float, cid: str, drawn: float,
               energy_counter: int | None, price: float | None) -> None:
        drawing = drawn > self.START_ABOVE
        if drawing:
            self._drawing_since[cid] = now

        session = self.active.get(cid)

        if drawing and session is None:
            session = Session(started=now, _counter=energy_counter, _last_seen=now)
            self.active[cid] = session
            _LOG.info("[%s] charging session started", cid)

        if session is not None:
            # Elapsed time is not charging time: load balancing pauses a car
            # through a household peak, and a session that ran four hours but
            # charged for one should say so.
            if session._last_seen is not None:
                elapsed = max(0.0, now - session._last_seen)
                if drawing:
                    session.charging_s += elapsed
            session._last_seen = now
            session.peak_current = max(session.peak_current, drawn)
            if price is not None:
                session.min_price = (price if session.min_price is None
                                     else min(session.min_price, price))
                session.max_price = (price if session.max_price is None
                                     else max(session.max_price, price))

        if session is not None and energy_counter is not None:
            if session._counter is None:
                session._counter = energy_counter
            delta = energy_counter - session._counter
            # The counter is monotonic in normal use; a drop means the charger
            # restarted, so re-base rather than book a negative amount.
            if delta < 0:
                _LOG.info("[%s] energy counter went backwards, re-basing", cid)
            elif delta > 0:
                session.energy_wh += delta
                if price is not None:
                    session.cost += (delta / 1000.0) * price
            session._counter = energy_counter

        if session is not None and not drawing:
            last = self._drawing_since.get(cid, session.started)
            if now - last >= self.IDLE_GRACE:
                session.ended = last
                self.completed.setdefault(cid, []).append(session)
                del self.completed[cid][:-self.KEEP_SESSIONS]
                self.active.pop(cid, None)
                _LOG.info("[%s] session ended: %.2f kWh, cost %.2f, %.0f min",
                          cid, session.energy_wh / 1000, session.cost,
                          session.duration / 60)
                if self._on_complete is not None:
                    try:
                        self._on_complete(cid, session)
                    except Exception as e:
                        # Recording history must never disturb charging.
                        _LOG.warning("Could not record the session: %s", e)

    def cost_per_hour(self, power_w: int | None, price: float | None) -> float | None:
        """What this charger is costing right now, per hour, at the current price."""
        if power_w is None or price is None:
            return None
        return (power_w / 1000.0) * price

    def session_for(self, cid: str) -> Session | None:
        return self.active.get(cid)

    def last_completed(self, cid: str) -> Session | None:
        done = self.completed.get(cid)
        return done[-1] if done else None
