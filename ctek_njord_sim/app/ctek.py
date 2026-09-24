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
        # Setpoints on our control topic that we did not send.
        self.foreign = protocol.ForeignCommands()

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
                "foreign_value": self.foreign.last_value,
                "foreign_age": (time.time() - self.foreign.last_at
                                if self.foreign.last_at else None),
            }


class CtekClient:
    # How often to retry a charger that is not answering.
    RECONNECT_INTERVAL = 15.0

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
        self.adapter_serial = adapter_serial or "40000B00Y0000002"
        self.dry_run = dry_run
        self.state = ChargerState()
        self.connected = threading.Event()
        self.topics: Topics | None = None
        self._announced = False
        self._last_reconnect = 0.0
        self._foreign_warned = -1e9
        self._sent_value: int | None = None
        self._sent_at = 0.0
        self._clients_seen: str | None = None
        self._foreign_last = None
        self._reasserted_at = -1e9

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
        was_down = not self.connected.is_set()
        self.connected.set()
        self._announced = False
        if was_down and self.topics is not None:
            _LOG.info("[%s] reconnected", self.name)
        if self.charger_serial:
            self._bind(self.charger_serial)
        else:
            _LOG.info("[%s] no serial configured; discovering from retained topics",
                      self.name)
            client.subscribe(DISCOVERY_TOPIC, qos=0)

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        self.connected.clear()
        self._announced = False  # re-announce on reconnect
        _LOG.warning("[%s] disconnected from charger broker (%s)", self.name, rc)

    def _bind(self, serial: str) -> None:
        """
        Lock onto a charger serial, and (re)subscribe to its topics.

        The subscribing happens on EVERY connect, not just the first. We
        connect with a clean session, so the broker forgets our subscriptions
        the moment the link drops - and a reconnect that skipped this left the
        charger permanently silent while still looking bound and still holding
        an allocation. Being bound is not the same as being subscribed.
        """
        if not SERIAL_RE.match(serial or ""):
            _LOG.warning("[%s] ignoring implausible charger serial from the broker: %r",
                         self.name, serial)
            return
        if self.topics is not None and self.topics.charger != serial:
            _LOG.warning("[%s] already serving %s, ignoring %s",
                         self.name, self.topics.charger, serial)
            return

        if self.topics is None:
            self.charger_serial = serial
            self.topics = Topics(charger=serial, adapter=self.adapter_serial)
            _LOG.info("[%s] bound to charger %s", self.name, serial)

        for t in self.topics.subscriptions():
            self._c.subscribe(t, qos=0)
        # Diagnostic only; many small brokers publish nothing here.
        self._c.subscribe("$SYS/broker/clients/connected", qos=0)
        self.announce()

    def ensure_connected(self) -> None:
        """
        Nudge a dropped connection back up. Blocking - call it off the loop.

        paho retries on its own, but a charger that is simply switched off is
        the expected case here rather than an error, so the retry is made
        explicit and logged at a sane interval instead of being assumed.
        """
        if self.connected.is_set():
            return
        now = time.time()
        if now - self._last_reconnect < self.RECONNECT_INTERVAL:
            return
        self._last_reconnect = now
        try:
            self._c.reconnect()
        except Exception as e:
            _LOG.debug("[%s] still unreachable (%s)", self.name, e)

    # ---------- inbound ----------

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        if topic.startswith("$SYS/"):
            # How many clients the broker has. More than us (and the charger's
            # own, if it counts) means someone else is connected.
            text = msg.payload.decode("utf-8", "replace").strip()
            if text != self._clients_seen:
                self._clients_seen = text
                _LOG.info("[%s] broker reports %s = %s", self.name, topic, text)
            return
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
        if topic == t.control_current:
            if not msg.retain:
                self._heard_command(payload)
            return
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
        if not self.dry_run:
            with self.state._lock:
                self.state.foreign.sent(time.time(), amps)
            (_LOG.info if amps != self._sent_value else _LOG.debug)(
                "[%s] sent setpoint %sA", self.name, amps)
            self._sent_value, self._sent_at = amps, time.time()
        self._publish(self.topics.control_current, protocol.control_current_payload(amps))

    FOREIGN_WARN_EVERY = 300.0

    def _heard_command(self, value) -> None:
        """
        A setpoint on our control topic that we may not have sent.

        Every foreign one is logged, not a sample: what matters is WHEN they
        arrive relative to our own. A fixed delay after each of our commands
        means the charger itself answers them; a schedule of its own means
        another controller - see PROTOCOL.md.
        """
        now = time.time()
        with self.state._lock:
            foreign = self.state.foreign.seen(now, value)
        if not foreign:
            return
        if self._sent_value is None:
            after = "before we have sent anything"
        else:
            after = f"{now - self._sent_at:.1f}s after our last command ({self._sent_value}A)"
        if self.dry_run:
            _LOG.info("[%s] foreign setpoint %sA, %s (dry run: expected)",
                      self.name, value, after)
            return

        # Logged at info when it changes, debug when it repeats: it arrives
        # every ten seconds for as long as we feed the charger meter data.
        (_LOG.info if value != self._foreign_last else _LOG.debug)(
            "[%s] foreign setpoint %sA on our control topic, %s",
            self.name, value, after)
        self._foreign_last = value

        if protocol.should_reassert(self._sent_value, value, now - self._reasserted_at):
            self._reasserted_at = now
            with self.state._lock:
                self.state.foreign.sent(now, self._sent_value)
            self._publish(self.topics.control_current,
                          protocol.control_current_payload(self._sent_value))
            _LOG.debug("[%s] re-asserted %sA over foreign %sA",
                       self.name, self._sent_value, value)

        if now - self._foreign_warned >= self.FOREIGN_WARN_EVERY:
            self._foreign_warned = now
            _LOG.warning(
                "[%s] the charger is also receiving setpoints this add-on did not "
                "send (%sA) - apparently its own load balancing, answering our "
                "meter data. Any higher than ours are overridden at once.",
                self.name, value)
