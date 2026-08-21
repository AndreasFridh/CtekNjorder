"""
Real-time entity state from Home Assistant over the WebSocket API.

Inside the Supervisor we reach Core through the proxy at ws://supervisor/core
using SUPERVISOR_TOKEN, so the user never has to mint a long-lived token.

We subscribe to state_changed rather than polling: the balancer needs to react
to a rising house load within a second or two, and polling adds latency exactly
when it matters most.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import aiohttp

_LOG = logging.getLogger(__name__)

SUPERVISOR_WS = "ws://supervisor/core/websocket"
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


def _as_float(value) -> float | None:
    """HA states are strings, and may be 'unknown'/'unavailable'."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # NaN would silently poison every downstream comparison.
    return f if f == f else None


class HassClient:
    """Tracks a set of entities and exposes their latest numeric values."""

    def __init__(self, entity_ids: list[str], url: str = "", token: str = ""):
        self.entity_ids = [e for e in entity_ids if e]
        self.url = url or os.environ.get("CTEK_HASS_WS", SUPERVISOR_WS)
        self.token = token or SUPERVISOR_TOKEN
        self.values: dict[str, float] = {}
        self.updated_at: dict[str, float] = {}
        self.connected = False
        self._msg_id = 0

    def value(self, entity_id: str) -> float | None:
        return self.values.get(entity_id)

    def age(self, entity_id: str) -> float:
        ts = self.updated_at.get(entity_id)
        return float("inf") if ts is None else time.time() - ts

    def newest_age(self, entity_ids: list[str]) -> float:
        """Age of the STALEST of the given entities - that's what gates safety."""
        if not entity_ids:
            return float("inf")
        return max(self.age(e) for e in entity_ids)

    def reading_ts(self, entity_ids: list[str]) -> float | None:
        """
        When the current set of readings was taken.

        The oldest of the three, so we line the charger's draw up against the
        stalest phase rather than flattering ourselves with the freshest.
        """
        stamps = [self.updated_at[e] for e in entity_ids if e in self.updated_at]
        return min(stamps) if stamps else None

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    def _record(self, entity_id: str, state) -> None:
        if entity_id not in self.entity_ids:
            return
        val = _as_float(state)
        if val is None:
            # Keep the last good value but let it age out via stale_timeout,
            # so a briefly 'unavailable' sensor doesn't cause a control glitch.
            _LOG.debug("%s is non-numeric (%r), keeping previous value", entity_id, state)
            return
        self.values[entity_id] = val
        self.updated_at[entity_id] = time.time()

    async def run(self) -> None:
        """Connect and stay connected, retrying with backoff forever."""
        backoff = 1.0
        while True:
            try:
                await self._session()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                _LOG.warning("Home Assistant connection lost (%s); retry in %.0fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _session(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.url, heartbeat=30) as ws:
                msg = await ws.receive_json()
                if msg.get("type") != "auth_required":
                    raise RuntimeError(f"unexpected greeting: {msg.get('type')}")

                await ws.send_json({"type": "auth", "access_token": self.token})
                msg = await ws.receive_json()
                if msg.get("type") != "auth_ok":
                    raise RuntimeError(f"auth failed: {msg}")
                _LOG.info("Connected to Home Assistant, tracking %d entities",
                          len(self.entity_ids))

                # Snapshot first, so we can act before the first state change.
                states_id = self._next_id()
                await ws.send_json({"id": states_id, "type": "get_states"})

                sub_id = self._next_id()
                await ws.send_json(
                    {"id": sub_id, "type": "subscribe_events", "event_type": "state_changed"}
                )

                self.connected = True
                async for raw in ws:
                    if raw.type != aiohttp.WSMsgType.TEXT:
                        continue
                    msg = raw.json()
                    mtype = msg.get("type")

                    if mtype == "result" and msg.get("id") == states_id:
                        for st in msg.get("result") or []:
                            self._record(st.get("entity_id"), st.get("state"))
                        missing = [e for e in self.entity_ids if e not in self.values]
                        if missing:
                            _LOG.warning(
                                "These entities are not present in Home Assistant: %s",
                                ", ".join(missing),
                            )
                    elif mtype == "event":
                        data = (msg.get("event") or {}).get("data") or {}
                        new = data.get("new_state")
                        if new:
                            self._record(data.get("entity_id"), new.get("state"))
        self.connected = False
