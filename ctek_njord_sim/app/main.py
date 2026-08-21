"""
Entry point: wires Home Assistant, the balancer, and the charger together.

Control loop runs at 1 Hz. The setpoint is republished on a heartbeat so the
charger never times out, and immediately whenever the decision changes, so a
sudden household load is shed without waiting for the next heartbeat.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from .balancer import Balancer, BalancerConfig
from .config import Options
from .ctek import CtekClient
from .hass import HassClient

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
        self._last_control = 0.0
        self._last_meter = 0.0
        self._last_status = 0.0

    # ---------- measurement plumbing ----------

    def house_current(self) -> list[float] | None:
        vals = [self.hass.value(e) for e in self.opts.current_entities]
        if any(v is None for v in vals):
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
            age = self.hass.newest_age(opts.current_entities)

            # The charger's own configuration always wins over our defaults.
            if charger["fuse_rating"]:
                self.balancer.cfg.charger_fuse_rating = charger["fuse_rating"]
            if charger["min_allowed_current"]:
                self.balancer.cfg.min_allowed_current = charger["min_allowed_current"]
            if charger["phase_rotation"]:
                self.balancer.cfg.phase_rotation = charger["phase_rotation"]

            # Subtract the car's draw AS OF the meter reading, not as of now.
            # The meter lags by up to meter_interval, and a ramping car moves
            # ~2 A/s, so using the present draw skews the baseline and makes
            # the setpoint oscillate.
            house_ts = self.hass.reading_ts(opts.current_entities)
            charger_current = (
                self.ctek.state.current_at(house_ts) if house_ts else charger["current"]
            )

            # If the charger itself has gone quiet, its reported draw is stale;
            # assume it is drawing its full setpoint rather than nothing.
            if charger["age"] > opts.stale_timeout:
                assumed = float(self.balancer.setpoint or 0)
                charger_current = [assumed] * 3

            decision = self.balancer.compute(
                now=now,
                house_current=current,
                house_age=age,
                charger_current=charger_current,
                ev_uses_phase=charger["ev_uses_phase"],
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
        tasks = [
            asyncio.create_task(self.hass.run()),
            asyncio.create_task(self.control_loop()),
        ]
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            self.ctek.stop()


async def amain() -> int:
    opts = Options.load()
    setup_logging(opts.log_level)

    problems = opts.validate()
    if problems:
        for p in problems:
            _LOG.error("Configuration error: %s", p)
        _LOG.error("Fix the add-on configuration and restart.")
        return 1

    _LOG.info(
        "main_fuse=%sA max_charge=%sA margin=%.1fA fallback=%sA dry_run=%s",
        opts.main_fuse, opts.max_charge_current, opts.safety_margin,
        opts.fallback_current, opts.dry_run,
    )
    if opts.dry_run:
        _LOG.warning("DRY RUN: decisions are logged but nothing is sent to the charger.")

    service = Service(opts)

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
    _LOG.info("Stopped.")
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(amain()))
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
