#!/usr/bin/env python3
"""
A minimal fake Home Assistant WebSocket API for testing the add-on.

Serves three per-phase current sensors. The reading is deliberately built the
way a real meter works - house baseline PLUS the car's actual draw, which it
reads back off MQTT - so the control loop genuinely closes and we can catch
mistakes like forgetting to subtract the car.

Walks a scripted load profile that drives the balancer through throttle, pause
and recovery, none of which we can safely produce in a real house.

  python tools/mock_hass.py --port 18123 --mqtt-port 18830
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time

import paho.mqtt.client as mqtt
from aiohttp import WSMsgType, web

ENTITIES = ["sensor.mock_l1", "sensor.mock_l2", "sensor.mock_l3"]
# Extra entities so the charge-enable gate and the price input can be exercised.
GATE = "input_boolean.charge_enable"
PRICE = "sensor.electricity_price"
# Wildcard: with several chargers we do not know the serials in advance, and a
# real meter would see all of them regardless.
T_UPDATE = "ctek/ng-v2/client/+/1/update"

# (until_seconds, baseline_amps_per_phase, label)
PROFILE = [
    (60, 4.0, "quiet house"),
    (120, 14.0, "oven + kettle on"),
    (180, 20.5, "heavy load - car must pause"),
    (10**9, 4.0, "back to quiet"),
]

log = logging.getLogger("mock-hass")


class World:
    """Shared truth: the house baseline plus whatever the car is drawing."""

    def __init__(self):
        self.cars: dict[str, list[float]] = {}     # keyed by broker+serial
        self.gate = "on"
        self.price = 2.45
        self.start = time.time()
        self._label = None

    @property
    def car(self) -> list[float]:
        """Every car at once, which is what the meter actually measures."""
        return [sum(v[p] for v in self.cars.values()) for p in range(3)]

    def baseline(self) -> float:
        t = time.time() - self.start
        for until, amps, label in PROFILE:
            if t < until:
                if label != self._label:
                    log.info("--- load profile: %s (baseline %.1f A/phase) ---", label, amps)
                    self._label = label
                return amps
        return 4.0

    def house(self) -> list[float]:
        b = self.baseline()
        return [round(b + c, 1) for c in self.car]


def start_mqtt(world: World, host: str, port: int) -> None:
    def on_connect(c, u, flags, rc, props=None):
        c.subscribe(T_UPDATE, qos=0)

    def on_message(c, u, msg):
        try:
            key = f"{port}:{msg.topic.split('/')[3]}"
            world.cars[key] = [float(x) for x in json.loads(msg.payload)["Current"]]
        except Exception:
            pass

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                    client_id=f"mock-hass-{port}")
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect_async(host, port, keepalive=30)
    c.loop_start()


def make_app(world: World) -> web.Application:
    async def websocket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required", "ha_version": "2026.8.0"})

        subscribed = False
        push_task = None

        async def push():
            """Emit state_changed events, as a real P1 integration would."""
            while True:
                await asyncio.sleep(2.0)
                extra = [(GATE, world.gate), (PRICE, world.price)]
                for eid, val in list(zip(ENTITIES, world.house())) + extra:
                    await ws.send_json({
                        "type": "event",
                        "event": {
                            "event_type": "state_changed",
                            "data": {
                                "entity_id": eid,
                                "new_state": {"entity_id": eid, "state": str(val)},
                            },
                        },
                    })

        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                mtype, mid = data.get("type"), data.get("id")

                if mtype == "auth":
                    await ws.send_json({"type": "auth_ok", "ha_version": "2026.8.0"})
                elif mtype == "get_states":
                    await ws.send_json({
                        "id": mid, "type": "result", "success": True,
                        "result": [
                            {"entity_id": e, "state": str(v),
                             "attributes": {"unit_of_measurement": "A"}}
                            for e, v in zip(ENTITIES, world.house())
                        ] + [
                            {"entity_id": GATE, "state": world.gate,
                             "attributes": {}},
                            {"entity_id": PRICE, "state": str(world.price),
                             "attributes": {"unit_of_measurement": "SEK/kWh"}},
                        ],
                    })
                elif mtype == "subscribe_events":
                    await ws.send_json({"id": mid, "type": "result", "success": True})
                    if not subscribed:
                        subscribed = True
                        push_task = asyncio.create_task(push())
        finally:
            if push_task:
                push_task.cancel()
        return ws

    async def set_gate(request):
        """Flip the charge-enable entity, so the gate can be exercised live."""
        world.gate = request.match_info["value"]
        log.info("--- charge enable -> %s ---", world.gate)
        return web.json_response({"gate": world.gate})

    async def set_price(request):
        world.price = float(request.match_info["value"])
        log.info("--- price -> %s ---", world.price)
        return web.json_response({"price": world.price})

    app = web.Application()
    app.router.add_get("/api/websocket", websocket)
    app.router.add_get("/gate/{value}", set_gate)
    app.router.add_get("/price/{value}", set_price)
    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18123)
    ap.add_argument("--mqtt-host", default="127.0.0.1")
    ap.add_argument("--mqtt-port", default="18830",
                    help="comma-separated broker ports, one per charger")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)-14s %(message)s", datefmt="%H:%M:%S")

    world = World()
    for port in str(args.mqtt_port).split(","):
        start_mqtt(world, args.mqtt_host, int(port.strip()))
    log.info("fake Home Assistant on ws://127.0.0.1:%s/api/websocket", args.port)
    log.info("entities: %s", ", ".join(ENTITIES))
    web.run_app(make_app(world), host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
