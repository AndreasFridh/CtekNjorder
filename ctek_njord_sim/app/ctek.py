"""
MQTT client that impersonates the Nanogrid Air on the charger's own broker.

The charger IS the broker, so there is no separate MQTT server to configure.
paho runs its own network thread; everything it writes is guarded by a lock and
read from the asyncio control loop.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from collections import deque

import paho.mqtt.client as mqtt

from . import protocol
from .protocol import Topics

_LOG = logging.getLogger(__name__)

# Retained configuration topics for any charger, used before we know the serial.
DISCOVERY_TOPIC = "ctek/ng-v2/client/+/configuration"

# The discovered serial is taken straight out of a topic name, and the charger's
# broker accepts anonymous publishes from anywhere on the network. Anyone on the
# LAN can therefore retain a topic containing whatever they like. Constrain it to
# the shape of a real CTEK serial before we adopt it, build topics from it, or
# hand it to the web UI.
SERIAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,63}$")


class ChargerState:
    """Latest known state of the charger. Thread-safe."""

    # ~2 minutes of 1 Hz samples: enough to look back to any meter reading.
    HISTORY = 120

    def __init__(self):
        self._lock = threading.Lock()
        self.state: int | None = None
        self.current: list[float] = [0.0, 0.0, 0.0]
        self.history: deque[tuple[float, list[float]]] = deque(maxlen=self.HISTORY)
        self.ev_uses_phase: list[int] = [1, 1, 1]
        self.max_allowed_current: int | None = None
        self.fuse_rating: int | None = None
        self.min_allowed_current: int | None = None
        self.phase_rotation: str | None = None
        self.energy: int | None = None
        self.power: int | None = None
        self.updated_at: float = 0.0

    def current_at(self, when: float) -> list[float]:
        """
        The car's draw as of `when`, not as of now.

        The meter reading we are about to subtract this from was taken in the
        past, and while the car is ramping its draw moves ~2 A/s. Subtracting
        the car's present draw from a meter reading that still contains its
        previous draw skews the computed baseline and makes the setpoint
        oscillate. Line the two up instead.
        """
        with self._lock:
            if not self.history:
                return list(self.current)
            best = min(self.history, key=lambda s: abs(s[0] - when))
            return list(best[1])

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "current": list(self.current),
                "ev_uses_phase": list(self.ev_uses_phase),
                "max_allowed_current": self.max_allowed_current,
                "fuse_rating": self.fuse_rating,
                "min_allowed_current": self.min_allowed_current,
                "phase_rotation": self.phase_rotation,
                "energy": self.energy,
                "power": self.power,
                "age": time.time() - self.updated_at if self.updated_at else float("inf"),
            }


class CtekClient:
    def __init__(
        self,
        host: str,
        port: int = 1883,
        charger_serial: str = "",
        adapter_serial: str = "",
        username: str = "",
        password: str = "",
        dry_run: bool = True,
        meter_interval: float = 10.0,
        name: str = "",
    ):
        self.host = host
        self.port = port
        # Each charger runs its own broker, so the address is the natural
        # identity - the serial is not known until we have connected.
        self.id = f"{host}:{port}"
        self.name = name or self.id
        self.meter_interval = meter_interval
        self.charger_serial = charger_serial
        # Impersonating a plausible adapter serial keeps the charger's own logs
        # readable; any stable string appears to work.
        self.adapter_serial = adapter_serial or "40542O36W4000074"
        self.dry_run = dry_run
        self.state = ChargerState()
        self.connected = threading.Event()
        self.topics: Topics | None = None
        self._announced = False

        cid = f"ctek-ha-sim-{uuid.uuid4().hex[:8]}"
        self._c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid,
                              clean_session=True)
        if username:
            self._c.username_pw_set(username, password)
        self._c.on_connect = self._on_connect
        self._c.on_message = self._on_message
        self._c.on_disconnect = self._on_disconnect
        self._c.reconnect_delay_set(min_delay=1, max_delay=30)

    # ---------- lifecycle ----------

    def start(self) -> None:
        _LOG.info("[%s] connecting to charger broker at %s:%s", self.name, self.host, self.port)
        self._c.connect_async(self.host, self.port, keepalive=30)
        self._c.loop_start()

    def stop(self) -> None:
        self._c.loop_stop()
        try:
            self._c.disconnect()
        except Exception:
            pass

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc != 0:
            _LOG.error("[%s] charger refused MQTT connection: %s", self.name, rc)
            return
        self.connected.set()
        if self.charger_serial:
            self._bind(self.charger_serial)
        else:
            _LOG.info("No charger_serial configured; discovering from retained topics")
            client.subscribe(DISCOVERY_TOPIC, qos=0)

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        self.connected.clear()
        self._announced = False  # re-announce on reconnect
        _LOG.warning("[%s] disconnected from charger broker (%s)", self.name, rc)

    def _bind(self, serial: str) -> None:
        """Lock onto a charger serial and subscribe to its topics."""
        if not SERIAL_RE.match(serial or ""):
            _LOG.warning("[%s] ignoring implausible charger serial from the broker: %r",
                         self.name, serial)
            return
        if self.topics and self.topics.charger == serial:
            return
        self.charger_serial = serial
        self.topics = Topics(charger=serial, adapter=self.adapter_serial)
        _LOG.info("[%s] bound to charger %s", self.name, serial)
        for t in self.topics.subscriptions():
            self._c.subscribe(t, qos=0)
        self.announce()

    # ---------- inbound ----------

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return

        # Discovery: any retained client configuration reveals a charger serial.
        if self.topics is None and topic.startswith("ctek/ng-v2/client/"):
            parts = topic.split("/")
            if len(parts) >= 4:
                self._bind(parts[3])
            return

        t = self.topics
        if t is None:
            return

        st = self.state
        with st._lock:
            if topic == t.outlet_update:
                parsed = protocol.parse_outlet_update(payload)
                st.state = parsed["state"]
                st.current = parsed["current"]
                st.ev_uses_phase = parsed["ev_uses_phase"]
                st.max_allowed_current = parsed["max_allowed_current"]
                st.updated_at = time.time()
                st.history.append((st.updated_at, list(st.current)))
            elif topic == t.outlet_config:
                parsed = protocol.parse_outlet_config(payload)
                st.fuse_rating = parsed["fuse_rating"]
                st.min_allowed_current = parsed["min_allowed_current"]
                _LOG.info(
                    "[%s] limits: FuseRating=%sA MinAllowedCurrent=%sA",
                    self.name, st.fuse_rating, st.min_allowed_current,
                )
            elif topic == t.station_config:
                st.phase_rotation = payload.get("StationPhaseRotation")
                _LOG.info("[%s] FW=%s phaseRotation=%s",
                          self.name, payload.get("FW"), st.phase_rotation)
            elif topic == t.outlet_info:
                st.energy = payload.get("energy")
                st.power = payload.get("power")

    # ---------- outbound ----------

    def _publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
        if self.dry_run:
            _LOG.info("[DRY-RUN] would publish %s %s%s",
                      topic, payload.decode(), " (retain)" if retain else "")
            return
        self._c.publish(topic, payload, qos=0, retain=retain)

    def announce(self) -> None:
        """
        Reproduce the adapter's announcement sequence: identity, meter type,
        then the cadence we intend to publish meterdata at. Order and content
        follow the real adapter's restart observed in the capture.
        """
        if self.topics is None or self._announced:
            return
        info = protocol.adapter_info_payload(self.adapter_serial)
        meta = protocol.meter_info_payload()
        every = protocol.interval_payload(int(self.meter_interval))
        for topic in self.topics.adapter_info_topics:
            self._publish(topic, info, retain=True)
        for topic in self.topics.meter_info_topics:
            self._publish(topic, meta, retain=True)
        for topic in self.topics.interval_topics:
            self._publish(topic, every)
        self._announced = True
        _LOG.info("[%s] announced as adapter %s (meterdata every %ss)",
                  self.name, self.adapter_serial, int(self.meter_interval))

    def publish_meter_data(
        self,
        current: list[float],
        voltage: list[float],
        power_in: float,
        power_out: float = 0.0,
    ) -> None:
        if self.topics is None:
            return
        payload = protocol.meter_data_payload(current, voltage, power_in, power_out)
        for topic in self.topics.meter_data_topics:
            self._publish(topic, payload)

    def publish_setpoint(self, amps: int) -> None:
        """The one message that actually steers the charger."""
        if self.topics is None:
            return
        self._publish(self.topics.control_current, protocol.control_current_payload(amps))
