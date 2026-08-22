#!/usr/bin/env python3
"""
A fake CTEK Njord GO: MQTT broker + charger, in one process.

Mirrors the real device, which hosts the broker itself. Lets us exercise the
add-on with dry_run disabled - including overload scenarios we cannot safely
produce in a real house - without touching real hardware.

Models the car too: it ramps toward whatever setpoint we are commanded, so the
loop between balancer and charger actually closes.

  python tools/mock_charger.py --port 18830
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random

import paho.mqtt.client as mqtt
from amqtt.broker import Broker

logging.getLogger("amqtt").setLevel(logging.WARNING)
logging.getLogger("transitions").setLevel(logging.WARNING)

DEFAULT_SERIAL = "40000A00X0000001"
FW = "r3.2.2-0-g673feded_mmiR1"
T_DEBUG = "ctek/ng-v2/debug"
FUSE_RATING = 16
MIN_ALLOWED = 6


class Car:
    """Ramps toward the allowed current the way a real EV does."""

    RAMP = 2.0  # amps per second

    def __init__(self, wants=16.0):
        self.wants = wants
        self.current = 0.0

    def step(self, allowed: float, dt: float) -> None:
        target = 0.0 if allowed <= 0 else min(allowed, self.wants)
        delta = target - self.current
        step = min(abs(delta), self.RAMP * dt)
        self.current += step if delta > 0 else -step
        self.current = max(0.0, self.current)

    def phases(self) -> list[float]:
        # A little per-phase jitter, as the real charger reports.
        return [round(self.current + random.uniform(-0.15, 0.15), 1) for _ in range(3)]


class MockCharger:
    def __init__(self, host, port, car_wants, serial=DEFAULT_SERIAL):
        self.host, self.port = host, port
        self.serial = serial
        self.t_station_cfg = f"ctek/ng-v2/client/{serial}/configuration"
        self.t_outlet_cfg = f"ctek/ng-v2/client/{serial}/1/configuration"
        self.t_update = f"ctek/ng-v2/client/{serial}/1/update"
        self.t_info = f"ctek/ng-v2/client/{serial}/1/info"
        self.t_control = f"ctek/ng-v2/controller/{serial}/1/current"
        self.car = Car(car_wants)
        self.setpoint = None
        self.energy = 7_826_000
        self.state = 2
        self.log = logging.getLogger(f"mock-{serial[-4:]}")
        self._c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                              client_id=f"mock-njord-{serial[-6:]}")
        self._c.on_connect = self._on_connect
        self._c.on_message = self._on_message

    def _on_connect(self, c, u, flags, rc, props=None):
        self.log.info("charger client connected (rc=%s)", rc)
        c.subscribe(self.t_control, qos=0)
        c.publish(self.t_station_cfg,
                  json.dumps({"FW": FW, "StationPhaseRotation": "RST"}), retain=True)
        c.publish(self.t_outlet_cfg, json.dumps({
            "FuseRating": FUSE_RATING, "MinAllowedCurrent": MIN_ALLOWED,
            "PhaseConnected": [True, True, True], "PrimaryPhase": 1,
        }), retain=True)

    def _on_message(self, c, u, msg):
        if msg.topic != self.t_control:
            return
        try:
            amps = int(msg.payload.decode())
        except ValueError:
            self.log.warning("un-parseable setpoint: %r", msg.payload)
            return
        if amps != self.setpoint:
            self.log.info(">>> SETPOINT %s -> %s A", self.setpoint, amps)
        self.setpoint = amps

    async def run(self):
        self._c.connect_async(self.host, self.port, keepalive=30)
        self._c.loop_start()
        dt, t = 1.0, 0.0
        while True:
            await asyncio.sleep(dt)
            t += dt
            # Before any controller speaks, the charger sits at its minimum.
            allowed = MIN_ALLOWED if self.setpoint is None else self.setpoint
            self.car.step(allowed, dt)
            phases = self.car.phases()

            # Only State 2 has been seen on real hardware, and only while a car
            # was charging. Reporting 2 with nothing plugged in made this mock
            # claim something no real observation supports - and hid a bug where
            # an empty charger held an allocation for ever. 1 here is a
            # placeholder for "not charging", not a decoded value.
            state = self.state if self.car.current > 0.2 else 1
            self._c.publish(self.t_update, json.dumps({
                "State": state,
                "EvUsesPhase": [1, 1, 1],
                "MaxAllowedCurrent": allowed,
                "Current": phases,
            }))

            power = int(sum(p * 231 for p in phases))
            self.energy += int(power * dt / 3600)
            if int(t) % 10 == 0:
                self._c.publish(self.t_info, json.dumps({"energy": self.energy, "power": power}))
            if int(t) % 6 == 0:
                self._c.publish(T_DEBUG, json.dumps(
                    {"ids": f"{self.serial},", "status": [state, 0, 9, 64]}),
                    retain=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18830)
    ap.add_argument("--car-wants", type=float, default=16.0,
                    help="amps the car would draw if unrestricted - set this "
                         "below 16 to simulate a car with an onboard limit")
    ap.add_argument("--serial", default=DEFAULT_SERIAL)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)-14s %(message)s", datefmt="%H:%M:%S")

    broker = Broker({
        "listeners": {"default": {"type": "tcp", "bind": f"{args.host}:{args.port}"}},
        "sys_interval": 0,
        "auth": {"allow-anonymous": True, "plugins": ["auth_anonymous"]},
        "topic-check": {"enabled": False},
    })
    await broker.start()
    logging.getLogger("mock").info(
        "broker on %s:%s as charger %s (car wants %.0f A)",
        args.host, args.port, args.serial, args.car_wants)

    await MockCharger(args.host, args.port, args.car_wants, args.serial).run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
