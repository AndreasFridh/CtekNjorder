"""
CtekNjorder
Copyright (C) 2026 Andreas Fridh

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. See the LICENSE file, or <https://www.gnu.org/licenses/>.

Entry point: wires Home Assistant, the balancer, the chargers and the web UI
together.

Each charger hosts its own MQTT broker, so several chargers means several
connections. The meter is singular though, and sees all of them at once, so the
house baseline is the meter reading minus *every* car - and whatever that
leaves has to be divided rather than handed to one charger.

Two decisions, deliberately kept apart:

    balancer    how much current may EV charging take in total right now
    allocator   how that total is split between the chargers

Control loop runs at 1 Hz. Setpoints are republished on a heartbeat so no
charger times out, and immediately whenever a decision changes, so a sudden
household load is shed without waiting for the next heartbeat.
"""
from __future__ import annotations

import asyncio
import logging
import re
import signal
import sys
import time
import unicodedata

from .allocator import (
    ChargerDemand, DemandTracker, allocate, apply_dwell,
)
from .balancer import (
    Balancer, BalancerConfig, car_draw_for_baseline,
)
from .config import Options
from .costs import (
    CostTracker, charging_allowed, gate_explicitly_on, price_per_kwh,
)
from .ctek import CtekClient
from .hass import HassClient
from .netmon import LinkMonitor
from .history import History
from .optionspec import LIVE_KEYS
from .protocol import PHASE_ROTATIONS, resend_needed
from .regulator import Regulator
from .sessions import SessionLog
from .web import WebUI
from . import supervisor

_LOG = logging.getLogger("ctek")

DEFAULT_VOLTAGE = 230.0
TICK = 1.0

# Published back to Home Assistant, so automations and dashboards can see when
# the cheap-power gate is what has a car charging.
LOW_PRICE_ENTITY = "binary_sensor.ctek_njorder_charging_low_price"
# Entities posted over REST are forgotten when Home Assistant restarts, so the
# state is re-posted on this interval even when nothing has changed.
HA_REFRESH = 60.0

# Seconds a charger holds its setpoint before a one-amp nudge is applied.
ALLOC_DWELL = 20.0


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )


class Service:
    def __init__(self, opts: Options):
        self.opts = opts
        self.started_at = time.time()
        self.version: str | None = None
        self.hass = HassClient(
            opts.current_entities
            + opts.voltage_entities
            + [e for e in (opts.power_in_entity, opts.power_out_entity,
                           opts.charge_enable_entity, opts.price_entity) if e]
        )

        self.clients: list[CtekClient] = [
            CtekClient(
                host=c["host"],
                port=c["port"],
                charger_serial=c.get("serial", ""),
                adapter_serial=opts.adapter_serial,
                username=opts.charger_username,
                password=opts.charger_password,
                dry_run=opts.dry_run,
                meter_interval=opts.meter_interval,
                name=c["name"],
            )
            for c in opts.active_chargers()
        ]

        self.balancer = Balancer(
            BalancerConfig(
                main_fuse=opts.main_fuse,
                max_charge_current=opts.max_charge_current,
                safety_margin=opts.safety_margin,
                # The summed car draw is already expressed in meter phases,
                # because rotation is applied per charger before summing.
                phase_rotation="RST",
                raise_delay=opts.raise_delay,
                stale_timeout=opts.stale_timeout,
                fallback_current=opts.fallback_current,
                settle_window=opts.settle_window,
                settle_tolerance=opts.settle_tolerance,
            )
        )
        self.demand = DemandTracker()
        self.session_log = SessionLog()
        self.costs = CostTracker(on_complete=self._record_session)
        self.links = LinkMonitor()
        self.regulator = Regulator()
        self.web = WebUI(self)
        self.history = History()

        self.allocation: dict[str, int] = {c.id: 0 for c in self.clients}
        self.last_demands: dict = {}
        self._alloc_changed_at: dict[str, float] = {}
        self._alloc_stopped_at: dict[str, float] = {}
        self.alloc_reason = "starting up"
        self.over_fuse = 0.0
        self.permitted = True
        self.cars_drawing = False
        self.last_decision = None
        self.last_house: list[float] | None = None
        self.last_baseline: list[float] | None = None
        self.last_step_at: float | None = None
        self.last_error: str | None = None
        self._last_control = 0.0
        self._published: dict[str, int] = {}
        self._sent_at: dict[str, float] = {}
        self._reported: dict[str, tuple] = {}
        # When each charger's car started drawing clearly above its setpoint,
        # and when that was last warned about.
        self._overdraw_since: dict[str, float] = {}
        self._overdraw_warned: dict[str, float] = {}
        self.overdraw: dict[str, float] = {}
        self._last_meter = 0.0
        self._last_status = 0.0

    def _record_session(self, cid: str, session) -> None:
        """Hand a finished session to the log, named as the user named it."""
        name = next((c.name for c in self.clients if c.id == cid), cid)
        self.session_log.add(session.as_record(cid, name, self.opts.currency))

    # ---------- measurement plumbing ----------

    def house_current(self) -> list[float] | None:
        vals = [self.hass.value(e) for e in self.opts.current_entities]
        if not vals or any(v is None for v in vals):
            return None
        return vals  # type: ignore[return-value]

    def house_voltage(self) -> list[float]:
        if len(self.opts.voltage_entities) == 3:
            vals = [self.hass.value(e) for e in self.opts.voltage_entities]
            if all(v is not None for v in vals):
                return vals  # type: ignore[return-value]
        return [DEFAULT_VOLTAGE] * 3

    def house_power(self, current: list[float], voltage: list[float]) -> tuple[float, float]:
        """Returns (import_kW, export_kW), measured if available, else derived."""
        p_in = self.hass.value(self.opts.power_in_entity) if self.opts.power_in_entity else None
        p_out = self.hass.value(self.opts.power_out_entity) if self.opts.power_out_entity else None
        if p_in is None:
            p_in = sum(c * v for c, v in zip(current, voltage)) / 1000.0
        return p_in, (p_out or 0.0)

    def price(self) -> float | None:
        """Current price per kWh, normalised for the entity's unit."""
        if not self.opts.price_entity:
            return None
        return price_per_kwh(self.hass.value(self.opts.price_entity),
                             self.hass.units.get(self.opts.price_entity))

    def charging_permitted(self) -> bool:
        if not self.opts.charge_enable_entity:
            return True
        return charging_allowed(self.hass.state(self.opts.charge_enable_entity))

    @staticmethod
    def _to_meter_phases(current: list[float], rotation: str | None) -> list[float]:
        """
        Express one charger's per-phase draw in meter phases.

        Each charger reports its own StationPhaseRotation and they need not
        agree, so this has to happen per charger, before anything is summed.
        """
        rot = PHASE_ROTATIONS.get(rotation or "RST", (0, 1, 2))
        out = [0.0, 0.0, 0.0]
        for station_phase, meter_phase in enumerate(rot):
            if station_phase < len(current):
                out[meter_phase] += current[station_phase]
        return out

    def _phases_of(self, state: dict) -> tuple[int, ...]:
        """Which meter phases this charger's car actually loads."""
        rot = PHASE_ROTATIONS.get(state.get("phase_rotation") or "RST", (0, 1, 2))
        uses = state.get("ev_uses_phase") or [1, 1, 1]
        phases = tuple(rot[i] for i in range(3) if i < len(uses) and uses[i])
        return phases or (0, 1, 2)

    def _ceiling_for(self, state: dict) -> int:
        """One charger's hard limit: its own rating, and the user's cap."""
        rating = state.get("fuse_rating") or self.opts.max_charge_current
        return int(min(self.opts.max_charge_current, rating))

    # ---------- control ----------

    async def control_loop(self) -> None:
        opts = self.opts
        while True:
            await asyncio.sleep(TICK)
            now = asyncio.get_running_loop().time()
            wall = time.time()

            # Only chargers we can actually talk to take part. A charger that
            # is switched off, unplugged or simply not owned must not hold an
            # allocation - that current belongs to the cars that are here.
            # Being bound is not enough; the link has to be up right now.
            bound = [
                c for c in self.clients
                if c.topics is not None and c.connected.is_set()
            ]
            if not bound:
                continue

            current = self.house_current()
            self.last_house = current
            age = self.hass.feed_age(opts.current_entities)
            house_ts = self.hass.reading_ts(opts.current_entities)

            states = {c.id: c.state.snapshot() for c in bound}

            # Every car is in the meter reading, so every car has to come back
            # out of it before what remains can be called house load.
            total_car = [0.0, 0.0, 0.0]
            for client in bound:
                st = states[client.id]
                raw = client.state.current_at(house_ts) if house_ts else st["current"]
                aligned = car_draw_for_baseline(raw, st["age"], opts.stale_timeout)
                mapped = self._to_meter_phases(aligned, st["phase_rotation"])
                for p in range(3):
                    total_car[p] += mapped[p]

            # How much may EV charging take in total? Cold start, the raise
            # delay and the stale-data fallback all apply to this one number.
            self.balancer.cfg.min_allowed_current = min(
                (states[c.id]["min_allowed_current"] or 6) for c in bound
            )
            aggregate_ceiling = sum(self._ceiling_for(states[c.id]) for c in bound)
            self.balancer.cfg.max_charge_current = aggregate_ceiling
            self.balancer.cfg.charger_fuse_rating = aggregate_ceiling

            used_phases: set[int] = set()
            for client in bound:
                used_phases.update(self._phases_of(states[client.id]))
            ev_uses = [1 if p in used_phases else 0 for p in range(3)]

            # What the meter must stay under, and what the cars are taking.
            limit = opts.main_fuse - opts.safety_margin
            ceiling = float(aggregate_ceiling)

            # The house baseline is no longer derived at all. Subtracting the
            # cars out of the meter puts what we allow back into what we allow
            # next, with a gain of one and a delay of one meter period, and that
            # oscillates however carefully it is gated. The meter reading is the
            # quantity that has to stay under the fuse, so it is regulated
            # directly instead - see regulator.py.
            # Recorded for the cadence estimate only. Whether we may step is a
            # question of elapsed time, not of the value having moved - see
            # `Regulator.ready`.
            if house_ts:
                self.regulator.note_reading(house_ts)
            settled_since = max(
                (self._alloc_changed_at.get(c.id, -1e9) for c in bound),
                default=-1e9,
            )
            ready = self.regulator.ready(now, settled_since, opts.meter_lag)

            if current and ready:
                allowed = self.regulator.step(
                    now, current, limit, total_car, ceiling)
                self.last_step_at = wall
                # The one line that explains any setpoint this thing chooses.
                _LOG.debug(
                    "regulator: meter=%s limit=%.1f cars=%s -> allowed=%s "
                    "(meter reports every %.1fs)",
                    [round(c, 1) for c in current], limit,
                    [round(c, 1) for c in total_car],
                    [round(a, 1) for a in allowed],
                    self.regulator.cadence.period,
                )
            else:
                # The reading in hand predates the last change, or the last
                # correction has not had time to show up yet. Correcting against
                # a reading taken before the change is what started the cycling.
                allowed = self.regulator.hold()

            # Kept only so the dashboard can still show what the house is doing.
            self.last_baseline = (
                [max(0.0, current[p] - total_car[p]) for p in range(3)]
                if current else None
            )

            decision = self.balancer.from_headroom(
                now=now,
                headroom=allowed if current else None,
                data_age=age,
                charger_current=total_car,
                ev_uses_phase=ev_uses,
                baseline=self.last_baseline or [],
            )
            self.last_decision = decision

            # ...and how that total is divided.
            demands = []
            for order, client in enumerate(bound):
                st = states[client.id]
                drawn = max(st["current"]) if st["current"] else 0.0
                setpoint = self.allocation.get(client.id, 0)
                self.demand.update(wall, client.id, setpoint, drawn)
                ceiling = self._ceiling_for(st)
                demands.append(ChargerDemand(
                    id=client.id,
                    order=order,
                    phases=self._phases_of(st),
                    min_current=st["min_allowed_current"] or 6,
                    max_current=ceiling,
                    cap=self.demand.cap_for(wall, client.id, setpoint, drawn,
                                            ceiling, st["min_allowed_current"] or 6),
                    wants=self.demand.wants_current(wall, client.id, st["state"], drawn),
                    charging=drawn > self.demand.DRAWING_THRESHOLD,
                ))

            self.last_demands = {d.id: d for d in demands}
            # The charge-enable gate may only ever withhold current. It caps
            # the total at zero rather than bypassing the balancer, so load
            # balancing still applies underneath it - "enabled" never means
            # "unlimited".
            permitted = self.charging_permitted()
            cap = float(decision.setpoint)

            # Backstop straight off the meter, with no subtraction in it and so
            # nothing for the alignment to get wrong. If the reading itself is
            # over the limit we are over the limit now, whatever the derived
            # baseline believes, and the shortfall comes off what is allowed.
            self.over_fuse = 0.0
            if current:
                over = max(current) - limit
                if over > 0:
                    self.over_fuse = round(over, 1)
                    # Anchored on what the cars were drawing when the reading
                    # was taken, not on what they are currently allowed. Taking
                    # the overshoot off the allowance ratchets: the allowance
                    # drops, the stale reading still shows the same overshoot,
                    # and it comes off again - which walked a car with 10 A of
                    # room down to a pause in two ticks.
                    cap = min(cap, self.regulator.shed_target(current, limit))

            allocation = allocate(
                decision.headroom or [0.0, 0.0, 0.0],
                demands,
                strategy=opts.allocation_strategy,
                total_cap=0.0 if not permitted else cap,
            )
            if self.over_fuse and permitted:
                allocation.reason = (
                    f"meter is {self.over_fuse}A over the limit - shedding now"
                )
            if not permitted:
                allocation.reason = "charging not allowed due to high price"
            self.permitted = permitted
            settled = apply_dwell(
                now, allocation.per_charger, self.allocation,
                self._alloc_changed_at, ALLOC_DWELL,
                restart_hold=opts.restart_hold,
                stopped_at=self._alloc_stopped_at,
            )
            changed = settled != self.allocation
            self.allocation = settled
            self.alloc_reason = allocation.reason
            # "Charging" for the Home Assistant output means we are giving
            # current to a car that asks for it - not the instantaneous draw,
            # which flickers across the threshold while a car starts up and
            # made the entity flap on and off.
            self.cars_drawing = any(
                self.allocation.get(d.id, 0) > 0 and d.wants for d in demands)

            # Per charger: a change is sent at once. An unchanged current is
            # refreshed on the heartbeat; an unchanged pause only if the car
            # is seen charging through it (see `resend_needed`).
            heartbeat = (now - self._last_control) >= opts.control_interval
            # A charger that dropped off may have lost what we last told it.
            for client in self.clients:
                if client not in bound:
                    self._published.pop(client.id, None)
            for client in bound:
                sp = self.allocation.get(client.id, 0)
                st = states[client.id]
                drawn = max(st["current"]) if st["current"] else 0.0
                since = now - self._sent_at.get(client.id, -1e9)
                changed_here = sp != self._published.get(client.id)
                if not changed_here and not resend_needed(sp, drawn, since, heartbeat):
                    continue
                if not changed_here and sp == 0:
                    _LOG.warning("[%s] car drawing %.1fA through a 0A pause - "
                                 "sending 0 again", client.name, drawn)
                client.publish_setpoint(sp)
                self._published[client.id] = sp
                self._sent_at[client.id] = now
            if heartbeat:
                self._last_control = now
            self._log_charger_changes(bound, states)
            self._check_overdraw(now, wall, bound, states)

            # Every charger runs its own broker, so meter data goes to each.
            if current and (now - self._last_meter) >= opts.meter_interval:
                voltage = self.house_voltage()
                p_in, p_out = self.house_power(current, voltage)
                for client in bound:
                    client.publish_meter_data(current, voltage, p_in, p_out)
                self._last_meter = now

            price = self.price()
            for client in bound:
                st = states[client.id]
                self.costs.update(
                    wall, client.id,
                    max(st["current"]) if st["current"] else 0.0,
                    st["energy"], price,
                )

            self.history.add(
                wall,
                [round(c, 1) for c in current] if current else None,
                [round(total_car[p], 1) for p in range(3)],
                sum(self.allocation.values()),
                blocked=not permitted,
            )

            if (now - self._last_status) >= 60:
                self._last_status = now
                per = ", ".join(
                    f"{c.name}={self.allocation.get(c.id, 0)}A"
                    f"(drawing {max(states[c.id]['current'] or [0]):.0f})"
                    for c in bound
                )
                _LOG.info(
                    "house=%s A | cars=%s A | total allowed=%sA | %s | %s",
                    [round(c, 1) for c in current] if current else "n/a",
                    [round(c, 1) for c in total_car],
                    decision.setpoint, per, allocation.reason,
                )

    def _log_charger_changes(self, bound, states) -> None:
        """
        Log every change in what a charger reports about itself.

        Only `State` 2 is understood, and nothing but this shows how the real
        charger and car react to what we command - a pause, a restart, a car
        waking up. One line per change, so it stays readable at info level.
        """
        for client in bound:
            st = states[client.id]
            drawn = max(st["current"]) if st["current"] else 0.0
            seen = (st["state"], st["max_allowed_current"],
                    drawn > self.demand.DRAWING_THRESHOLD)
            if seen != self._reported.get(client.id):
                self._reported[client.id] = seen
                _LOG.info(
                    "[%s] charger reports State=%s MaxAllowedCurrent=%s, "
                    "car drawing %.1fA (we command %sA)",
                    client.name, st["state"], st["max_allowed_current"], drawn,
                    self.allocation.get(client.id, 0))

    OVERDRAW_MARGIN = 2.0      # amps above the setpoint that are not jitter
    OVERDRAW_FOR = 2.0         # seconds it must last
    # A car may take up to 5 s to follow a reduction (IEC 61851), so nothing is
    # judged until the setpoint has been unchanged for longer than that.
    OVERDRAW_SETTLE = 6.0
    OVERDRAW_LOG_EVERY = 60.0

    def _check_overdraw(self, now, wall, bound, states) -> None:
        """
        Notice a car drawing clearly more than we allow it.

        Everything here assumes the charger holds the setpoint it is given. A
        field chart showed a car allowed 7 A spiking to 13-16 A every half
        minute, with the meter following it up towards the fuse. That is the
        charger overriding us, and nothing downstream can correct for it, so
        it is surfaced rather than silently absorbed.
        """
        for client in bound:
            st = states[client.id]
            drawn = max(st["current"]) if st["current"] else 0.0
            sp = self.allocation.get(client.id, 0)
            settled = now - self._alloc_changed_at.get(client.id, -1e9) >= self.OVERDRAW_SETTLE
            if sp > 0 and settled and drawn > sp + self.OVERDRAW_MARGIN:
                since = self._overdraw_since.setdefault(client.id, wall)
                if wall - since >= self.OVERDRAW_FOR:
                    self.overdraw[client.id] = wall
                    if wall - self._overdraw_warned.get(client.id, -1e9) >= self.OVERDRAW_LOG_EVERY:
                        self._overdraw_warned[client.id] = wall
                        _LOG.warning(
                            "[%s] car drawing %.1fA with %sA allowed - the charger "
                            "is not holding our setpoint (charger reports "
                            "State=%s MaxAllowedCurrent=%s)", client.name, drawn,
                            sp, st["state"], st["max_allowed_current"])
            else:
                self._overdraw_since.pop(client.id, None)

    # ---------- state for the web UI ----------

    def snapshot(self) -> dict:
        d = self.last_decision
        price = self.price()
        chargers = []
        session_total = 0.0
        hourly_total = 0.0
        for client in self.clients:
            st = client.state.snapshot()
            drawn = max(st["current"]) if st["current"] else 0.0

            ceiling = self._ceiling_for(st)
            # What the house could spare, not what is currently on offer. The
            # allowance is deliberately held near what the cars actually draw,
            # so reporting it here answered "how much are we offering" when the
            # card is asking "how much could I charge at".
            spare = None
            if d and d.headroom:
                room = self.regulator.spare if self.last_house else None
                spare = int(min(min(room or d.headroom), ceiling))
                if spare < (st["min_allowed_current"] or 6):
                    spare = 0

            active = self.costs.session_for(client.id)
            last = self.costs.last_completed(client.id)
            shown = active or last
            session_info = None
            if shown is not None:
                session_info = {
                    "active": active is not None,
                    "energy_kwh": round(shown.energy_wh / 1000.0, 3),
                    "cost": round(shown.cost, 3),
                    "minutes": round(shown.duration / 60.0),
                }
            if active is not None:
                session_total += active.cost
            hourly = self.costs.cost_per_hour(st["power"], price) if active else None
            if hourly:
                hourly_total += hourly

            chargers.append({
                "id": client.id,
                "name": client.name,
                "host": client.host,
                "serial": client.charger_serial,
                "connected": client.connected.is_set(),
                "bound": client.topics is not None and client.connected.is_set(),
                "ever_seen": client.topics is not None,
                "state": st["state"],
                "current": st["current"],
                "drawn": round(drawn, 1),
                "allocated": self.allocation.get(client.id, 0),
                "max_allowed_current": st["max_allowed_current"],
                "fuse_rating": st["fuse_rating"],
                "min_allowed_current": st["min_allowed_current"],
                "energy": st["energy"],
                "power": st["power"],
                # Read, never re-judged: judging records State, and doing it
                # from here could swallow the change that means a car arrived.
                "wants": self.demand.last_wants(client.id),
                # What the car looks able to use. Below its ceiling means we
                # have concluded it is limited and handed the surplus on.
                "cap": (round(self.last_demands[client.id].cap, 1)
                        if client.id in self.last_demands else None),
                "session": session_info,
                "cost_per_hour": (round(hourly, 4) if hourly is not None else None),
                "link": {**self.links.stats(client.id),
                         "series": self.links.series(client.id, 40)},
                # What this charger could be given right now, so a card can say
                # "ready, N A available" instead of implying something is wrong.
                "available": spare,
                "believed_empty": self.demand.believed_empty(client.id),
                "age": None if st["age"] == float("inf") else round(st["age"], 1),
            })

        return {
            "now": time.time(),
            "uptime": time.time() - self.started_at,
            "version": self.version,
            "dry_run": self.opts.dry_run,
            "strategy": self.opts.allocation_strategy,
            "charging_permitted": self.permitted,
            "gate_entity": self.opts.charge_enable_entity,
            "price": round(price, 4) if price is not None else None,
            "currency": self.opts.currency,
            "cost_now": {
                "sessions": round(session_total, 3),
                "per_hour": round(hourly_total, 3),
            },
            "connected": {
                "chargers_total": len(self.clients),
                "chargers_bound": sum(1 for c in self.clients if c.topics is not None),
                "hass": self.hass.connected,
            },
            "chargers": chargers,
            "meter": {
                **self.regulator.cadence.as_dict(),
                "allowed": [round(a, 1) for a in self.regulator.allowed],
                "last_step_age": (round(time.time() - self.last_step_at, 1)
                                  if self.last_step_at else None),
            },
            "house": {
                "current": self.last_house,
                "voltage": self.house_voltage(),
                # Time since the reading last CHANGED, which is informative but
                # is not what staleness is judged on - see HassClient.feed_age.
                "age": self._finite(self.hass.newest_age(self.opts.current_entities)),
                "stale": self.hass.feed_age(self.opts.current_entities) > self.opts.stale_timeout,
                "entities": self.opts.current_entities,
            },
            "decision": {
                "setpoint": d.setpoint if d else None,
                "reason": d.reason if d else "starting up",
                "headroom": d.headroom if d else [],
                "baseline": d.baseline if d else [],
                "allocation": self.alloc_reason,
            },
            "limits": {
                "main_fuse": self.opts.main_fuse,
                "safety_margin": self.opts.safety_margin,
                "max_charge_current": self.opts.max_charge_current,
                "ceiling": self.balancer.cfg.charger_fuse_rating,
                "min_allowed_current": self.balancer.cfg.min_allowed_current,
            },
            "error": self.last_error,
        }

    @staticmethod
    def _finite(value: float):
        return None if value == float("inf") else round(value, 1)

    def history_series(self, minutes: int = 30) -> dict:
        series = self.history.series(minutes)
        series["main_fuse"] = self.opts.main_fuse
        return series

    # ---------- live reconfiguration ----------

    def apply_live_settings(self, changes: dict) -> list[str]:
        """
        Push the settings that do not need a restart into the running objects.

        Returns the keys that actually took effect now; the caller reports the
        rest as needing a restart.
        """
        applied = []
        for key, value in changes.items():
            setattr(self.opts, key, value)
            if key not in LIVE_KEYS:
                continue
            applied.append(key)

            if key == "main_fuse":
                self.balancer.cfg.main_fuse = value
            elif key == "safety_margin":
                self.balancer.cfg.safety_margin = value
            elif key == "fallback_current":
                self.balancer.cfg.fallback_current = value
            elif key == "stale_timeout":
                self.balancer.cfg.stale_timeout = value
            elif key == "raise_delay":
                self.balancer.cfg.raise_delay = value
            elif key == "settle_window":
                self.balancer.cfg.settle_window = value
            elif key == "settle_tolerance":
                self.balancer.cfg.settle_tolerance = value
            elif key == "dry_run":
                for client in self.clients:
                    client.dry_run = value
                _LOG.warning(
                    "Dry run %s via the web UI",
                    "ENABLED" if value else "DISABLED - now controlling the chargers")
            elif key == "log_level":
                logging.getLogger().setLevel(
                    getattr(logging, str(value).upper(), logging.INFO))
            # max_charge_current, allocation_strategy and phase_rotation are
            # read from opts on the next tick, so the setattr above suffices.
        return applied

    # ---------- outputs to Home Assistant ----------

    def low_price_charging(self) -> bool:
        """
        Is a car charging, with the charge-enable gate explicitly on?

        Explicitly: a gate that is unavailable still permits charging, but
        that charging is not happening because power is cheap.
        """
        if not self.opts.charge_enable_entity:
            return False
        gate = self.hass.state(self.opts.charge_enable_entity)
        return self.cars_drawing and gate_explicitly_on(gate)

    @staticmethod
    def _slug(name: str) -> str:
        """An entity-id fragment from a charger's name: 'Garage' -> 'garage'."""
        ascii_name = (unicodedata.normalize("NFKD", name)
                      .encode("ascii", "ignore").decode())
        return re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")

    def ha_entities(self) -> dict[str, tuple[str, dict]]:
        """
        Everything this add-on shows in Home Assistant, as entity -> (state, attrs).

        Power and energy come straight from the charger's own `info` message.
        The energy counter is lifetime and only ever rises, which is what the
        Energy dashboard wants (`total_increasing`); a charger reboot that
        resets it is handled by Home Assistant as a new cycle.
        """
        low = self.low_price_charging()
        out = {
            LOW_PRICE_ENTITY: ("on" if low else "off", {
                "friendly_name": "Charging Active Due to Low Price",
                "icon": "mdi:ev-station" if low else "mdi:ev-plug-type2",
                "gate_entity": self.opts.charge_enable_entity or None,
                "gate_state": (self.hass.state(self.opts.charge_enable_entity)
                               if self.opts.charge_enable_entity else None),
                "car_charging": self.cars_drawing,
            }),
        }
        for client in self.clients:
            slug = self._slug(client.name) or client.id
            st = client.state.snapshot()
            online = client.topics is not None and client.connected.is_set()
            power, energy = st["power"], st["energy"]
            out[f"sensor.ctek_njorder_{slug}_power"] = (
                str(int(power)) if online and power is not None else "unavailable", {
                    "friendly_name": f"{client.name} charging power",
                    "unit_of_measurement": "W",
                    "device_class": "power",
                    "state_class": "measurement",
                    "icon": "mdi:ev-station",
                })
            out[f"sensor.ctek_njorder_{slug}_energy"] = (
                f"{energy / 1000.0:.3f}" if online and energy is not None
                else "unavailable", {
                    "friendly_name": f"{client.name} energy",
                    "unit_of_measurement": "kWh",
                    "device_class": "energy",
                    "state_class": "total_increasing",
                    "icon": "mdi:lightning-bolt",
                })
        return out

    async def ha_output_loop(self) -> None:
        """Keep the add-on's own entities in Home Assistant up to date."""
        last: dict[str, str] = {}
        posted_at: dict[str, float] = {}
        retry_at = 0.0
        while True:
            await asyncio.sleep(TICK)
            now = time.monotonic()
            if now < retry_at:
                continue
            for eid, (state, attrs) in self.ha_entities().items():
                if state == last.get(eid) and now - posted_at.get(eid, -1e9) < HA_REFRESH:
                    continue
                try:
                    await supervisor.set_state(eid, state, attrs)
                except Exception as exc:                 # noqa: BLE001
                    # Retried later; the output is informational and must
                    # never get in the way of control.
                    _LOG.debug("could not publish %s: %s", eid, exc)
                    retry_at = now + 30.0
                    break
                if eid == LOW_PRICE_ENTITY and state != last.get(eid):
                    _LOG.info("%s -> %s", eid, state)
                last[eid], posted_at[eid] = state, now

    # ---------- lifecycle ----------

    async def link_loop(self) -> None:
        """Measure the path to each charger, off the event loop."""
        while True:
            await asyncio.sleep(self.opts.ping_interval)
            await asyncio.gather(
                *(asyncio.to_thread(self.links.measure, c.id, c.host, c.port)
                  for c in self.clients),
                return_exceptions=True,
            )

    async def reconnect_loop(self) -> None:
        """Keep trying anything that is not answering."""
        while True:
            await asyncio.sleep(5)
            down = [c for c in self.clients if not c.connected.is_set()]
            if down:
                # reconnect() blocks on the TCP attempt, and an unreachable
                # host blocks for the OS timeout - so never on the event loop.
                await asyncio.gather(
                    *(asyncio.to_thread(c.ensure_connected) for c in down),
                    return_exceptions=True,
                )

    async def run(self) -> None:
        if supervisor.available():
            # Best effort: the UI just shows "unknown" if the Supervisor is not
            # there, which is the case on the offline rig.
            try:
                self.version = await supervisor.get_version()
                _LOG.info("CtekNjorder %s", self.version)
            except Exception as exc:                     # noqa: BLE001
                _LOG.debug("could not read the add-on version: %s", exc)
        for client in self.clients:
            client.start()
        await self.web.start()
        tasks = [
            asyncio.create_task(self.hass.run()),
            asyncio.create_task(self.control_loop()),
            asyncio.create_task(self.reconnect_loop()),
            asyncio.create_task(self.link_loop()),
        ]
        if supervisor.available():
            tasks.append(asyncio.create_task(self.ha_output_loop()))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await self.web.stop()
            for client in self.clients:
                client.stop()
            self.history.close()


async def amain() -> int:
    opts = Options.load()
    setup_logging(opts.log_level)

    _LOG.info(
        "main_fuse=%sA max_charge=%sA/charger margin=%.1fA fallback=%sA "
        "strategy=%s dry_run=%s",
        opts.main_fuse, opts.max_charge_current, opts.safety_margin,
        opts.fallback_current, opts.allocation_strategy, opts.dry_run,
    )
    for c in opts.active_chargers():
        _LOG.info("  charger: %s at %s:%s", c["name"], c["host"], c["port"])
    if opts.dry_run:
        _LOG.warning("DRY RUN: decisions are logged but nothing is sent to the chargers.")

    service = Service(opts)

    # Configuration problems are reported in the UI rather than being fatal, so
    # they can be fixed there instead of by editing YAML and restarting blind.
    problems = opts.validate()
    if problems:
        service.last_error = " ".join(problems)
        for p in problems:
            _LOG.error("Configuration error: %s", p)
        _LOG.error("Open the add-on's Web UI to fix this. Balancing stays "
                   "inactive until it is resolved.")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass  # Windows

    runner = asyncio.create_task(service.run())
    await asyncio.wait([runner, asyncio.create_task(stop.wait())],
                       return_when=asyncio.FIRST_COMPLETED)
    runner.cancel()
    for client in service.clients:
        client.stop()
    service.history.close()
    _LOG.info("Stopped.")
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
