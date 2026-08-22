"""
Link quality to each charger.

The chargers sit on Wi-Fi, and a marginal link shows up as charging behaviour
that looks inexplicable - setpoints that arrive late, telemetry that goes stale,
a charger that drops out and comes back. Measuring the link turns that into
something you can point at.

It measures a TCP connect to the charger's MQTT port rather than sending ICMP.
Two reasons: ICMP needs the NET_RAW capability, which this add-on does not have
and has no business asking for; and a TCP handshake to the port we actually use
tests the path that actually matters. A host can answer pings perfectly while
its broker is unreachable.
"""
from __future__ import annotations

import logging
import socket
import time
from collections import deque

_LOG = logging.getLogger(__name__)

TIMEOUT = 2.0


class LinkMonitor:
    """Rolling reachability and round-trip time, per charger."""

    def __init__(self, samples: int = 120):
        self.samples = samples
        self._history: dict[str, deque[tuple[float, float | None]]] = {}

    def probe(self, host: str, port: int) -> float | None:
        """Milliseconds to complete a TCP connect, or None if it failed."""
        start = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=TIMEOUT):
                return (time.perf_counter() - start) * 1000.0
        except OSError:
            return None

    def record(self, cid: str, ms: float | None) -> None:
        hist = self._history.setdefault(cid, deque(maxlen=self.samples))
        hist.append((time.time(), ms))

    def measure(self, cid: str, host: str, port: int) -> float | None:
        ms = self.probe(host, port)
        self.record(cid, ms)
        return ms

    def stats(self, cid: str) -> dict:
        hist = self._history.get(cid)
        if not hist:
            return {"latest": None, "avg": None, "worst": None,
                    "loss": None, "samples": 0}

        replies = [ms for _, ms in hist if ms is not None]
        latest = hist[-1][1]
        return {
            "latest": round(latest, 1) if latest is not None else None,
            "avg": round(sum(replies) / len(replies), 1) if replies else None,
            "worst": round(max(replies), 1) if replies else None,
            # Share of probes that got no answer at all - the number that
            # actually predicts trouble, more than the average does.
            "loss": round(100.0 * (len(hist) - len(replies)) / len(hist), 1),
            "samples": len(hist),
        }

    def series(self, cid: str, limit: int = 60) -> list[float | None]:
        """Recent round-trip times, oldest first, for a sparkline."""
        hist = self._history.get(cid)
        if not hist:
            return []
        return [ms for _, ms in list(hist)[-limit:]]
