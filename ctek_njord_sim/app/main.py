"""
CTEK Njord Load Balancer
Copyright (C) 2026 Andreas Fridh

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version. See the LICENSE file, or <https://www.gnu.org/licenses/>.

Entry point: wires Home Assistant, the balancer, the charger and the web UI
together.

Control loop runs at 1 Hz. The setpoint is republished on a heartbeat so the
charger never times out, and immediately whenever the decision changes, so a
sudden household load is shed without waiting for the next heartbeat.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time

from .balancer import Balancer, BalancerConfig, car_draw_for_baseline
from .config import Options
from .ctek import CtekClient
from .hass import HassClient
from .history import History
from .optionspec import LIVE_KEYS
from .web import WebUI

_LOG = logging.getLogger("ctek")

DEFAULT_VOLTAGE = 230.0
TICK = 1.0


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
        self.hass = HassClient(
            opts.current_entities
            + opts.voltage_entities
            + [e for e in (opts.power_in_entity, opts.power_out_entity) if e]
        )
        self.ctek = CtekClient(
            host=opts.charger_host,
            port=opts.charger_port,
            charger_serial=opts.charger_serial,
            adapter_serial=opts.adapter_serial,
            username=opts.charger_username,
            password=opts.charger_password,
            dry_run=opts.dry_run,
            meter_interval=opts.meter_interval,
        )
        self.balancer = Balancer(
            BalancerConfig(
                main_fuse=opts.main_fuse,
                max_charge_current=opts.max_charge_current,
                safety_margin=opts.safety_margin,
                phase_rotation=opts.phase_rotation,
                raise_delay=opts.raise_delay,
                stale_timeout=opts.stale_timeout,
                fallback_current=opts.fallback_current,
                settle_window=opts.settle_window,
                settle_tolerance=opts.settle_tolerance,
            )
        )
        self.web = WebUI(self)

        self.history = History()
        self.last_decision = None
        self.last_house: list[float] | None = None
        self.last_error: str | None = None
        self._last_control = 0.0
        self._last_meter = 0.0
        self._last_status = 0.0

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

    # ---------- state for the web UI ----------

    def snapshot(self) -> dict:
        charger = self.ctek.state.snapshot()
        d = self.last_decision
        ceiling = min(self.opts.max_charge_current,
                      self.balancer.cfg.charger_fuse_rating)
        return {
            "now": time.time(),
            "uptime": time.time() - self.started_at,
            "dry_run": self.opts.dry_run,
            "connected": {
                "charger": self.ctek.connected.is_set(),
                "hass": self.hass.connected,
                "bound": self.ctek.topics is not None,
            },
            "charger": {
                "serial": self.ctek.charger_serial,
                "state": charger["state"],
                "current": charger["current"],
                "ev_uses_phase": charger["ev_uses_phase"],
                "max_allowed_current": charger["max_allowed_current"],
                "fuse_rating": charger["fuse_rating"],
                "min_allowed_current": charger["min_allowed_current"],
                "phase_rotation": charger["phase_rotation"],
                "energy": charger["energy"],
                "power": charger["power"],
                "age": None if charger["age"] == float("inf") else charger["age"],
            },
            "house": {
                "current": self.last_house,
                "voltage": self.house_voltage(),
                "age": self._finite(self.hass.newest_age(self.opts.current_entities)),
                "entities": self.opts.current_entities,
            },
            "decision": {
                "setpoint": d.setpoint if d else None,
                "reason": d.reason if d else "starting up",
                "headroom": d.headroom if d else [],
                "baseline": d.baseline if d else [],
            },
            "limits": {
                "main_fuse": self.opts.main_fuse,
                "safety_margin": self.opts.safety_margin,
                "max_charge_current": self.opts.max_charge_current,
                "ceiling": ceiling,
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
            elif key == "max_charge_current":
                self.balancer.cfg.max_charge_current = value
            elif key == "safety_margin":
                self.balancer.cfg.safety_margin = value
            elif key == "fallback_current":
                self.balancer.cfg.fallback_current = value
            elif key == "phase_rotation":
                self.balancer.cfg.phase_rotation = value
            elif key == "stale_timeout":
                self.balancer.cfg.stale_timeout = value
            elif key == "raise_delay":
                self.balancer.cfg.raise_delay = value
            elif key == "settle_window":
                self.balancer.cfg.settle_window = value
            elif key == "settle_tolerance":
                self.balancer.cfg.settle_tolerance = value
            elif key == "dry_run":
                self.ctek.dry_run = value
                _LOG.warning("Dry run %s via the web UI",
                             "ENABLED" if value else "DISABLED - now controlling the charger")
            elif key == "log_level":
                logging.getLogger().setLevel(
                    getattr(logging, str(value).upper(), logging.INFO))
        return applied

    # ---------- control ----------

    async def control_loop(self) -> None:
        opts = self.opts
        while True:
            await asyncio.sleep(TICK)
            now = asyncio.get_running_loop().time()

            if self.ctek.topics is None:
                continue  # not bound to a charger yet

            charger = self.ctek.state.snapshot()
            current = self.house_current()
            self.last_house = current
            age = self.hass.newest_age(opts.current_entities)

            # The charger's own configuration always wins over our defaults.
            if charger["fuse_rating"]:
                self.balancer.cfg.charger_fuse_rating = charger["fuse_rating"]
            if charger["min_allowed_current"]:
                self.balancer.cfg.min_allowed_current = charger["min_allowed_current"]
            if charger["phase_rotation"]:
                self.balancer.cfg.phase_rotation = charger["phase_rotation"]

            # Subtract the car's draw AS OF the meter reading, not as of now.
            # The meter lags, and a ramping car moves ~2 A/s, so using the
            # present draw skews the baseline and makes the setpoint oscillate.
            house_ts = self.hass.reading_ts(opts.current_entities)
            charger_current = (
                self.ctek.state.current_at(house_ts) if house_ts else charger["current"]
            )

            # If the charger has gone quiet we can no longer tell how much of
            # the meter reading is the car, so we attribute none of it. See
            # car_draw_for_baseline: over-subtracting is the dangerous way to
            # be wrong.
            charger_current = car_draw_for_baseline(
                charger_current, charger["age"], opts.stale_timeout
            )

            decision = self.balancer.compute(
                now=now,
                house_current=current,
                house_age=age,
                charger_current=charger_current,
                ev_uses_phase=charger["ev_uses_phase"],
            )
            self.last_decision = decision

            self.history.add(
                time.time(),
                [round(c, 1) for c in current] if current else None,
                [round(c, 1) for c in charger["current"]],
                decision.setpoint,
            )

            # Heartbeat, plus an immediate send whenever the decision moves.
            if decision.changed or (now - self._last_control) >= opts.control_interval:
                self.ctek.publish_setpoint(decision.setpoint)
                self._last_control = now

            if current and (now - self._last_meter) >= opts.meter_interval:
                voltage = self.house_voltage()
                p_in, p_out = self.house_power(current, voltage)
                self.ctek.publish_meter_data(current, voltage, p_in, p_out)
                self._last_meter = now

            if (now - self._last_status) >= 60:
                self._last_status = now
                _LOG.info(
                    "house=%s A | car=%s A | setpoint=%sA (charger reports %sA) | %s",
                    [round(c, 1) for c in current] if current else "n/a",
                    [round(c, 1) for c in charger["current"]],
                    decision.setpoint,
                    charger["max_allowed_current"],
                    decision.reason,
                )

    async def run(self) -> None:
        self.ctek.start()
        await self.web.start()
        tasks = [
            asyncio.create_task(self.hass.run()),
            asyncio.create_task(self.control_loop()),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await self.web.stop()
            self.ctek.stop()
            self.history.close()


async def amain() -> int:
    opts = Options.load()
    setup_logging(opts.log_level)

    _LOG.info(
        "main_fuse=%sA max_charge=%sA margin=%.1fA fallback=%sA dry_run=%s",
        opts.main_fuse, opts.max_charge_current, opts.safety_margin,
        opts.fallback_current, opts.dry_run,
    )
    if opts.dry_run:
        _LOG.warning("DRY RUN: decisions are logged but nothing is sent to the charger.")

    service = Service(opts)

    # Configuration problems are reported in the UI rather than fatal, so the
    # user can fix them there instead of editing YAML and restarting blind.
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
    service.ctek.stop()
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
