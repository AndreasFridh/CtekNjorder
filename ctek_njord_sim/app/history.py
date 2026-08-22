"""
Chart history, in two tiers.

Live tier: one sample per second, last 30 minutes, in memory. This is what the
dashboard shows by default and it needs full resolution.

Long tier: one bucket per minute, last 7 days, persisted to /data so it
survives an add-on restart. Each bucket keeps the *worst* value it saw - the
highest house and car current, the lowest setpoint - so a brief spike is still
visible after downsampling rather than being averaged away.

Storage is append-only JSONL: roughly 60 bytes once a minute. Home Assistant
often runs from an SD card, so rewriting a whole file every minute would be a
meaningful amount of flash wear for a chart. The file is compacted only when it
has grown well past the retention window.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque

_LOG = logging.getLogger(__name__)

LIVE_SECONDS = 1800          # 30 minutes at 1 Hz
BUCKET_SECONDS = 60          # long-tier resolution
RETENTION_BUCKETS = 7 * 24 * 60   # 7 days of minutes
COMPACT_AT = RETENTION_BUCKETS * 2


def _worst(a: list[float] | None, b: list[float] | None) -> list[float] | None:
    """Element-wise max, tolerating either side being absent."""
    if a is None:
        return list(b) if b else None
    if b is None:
        return list(a)
    return [max(x, y) for x, y in zip(a, b)]


class History:
    def __init__(self, data_dir: str | None = None):
        self.live: deque = deque(maxlen=LIVE_SECONDS)
        self.long: deque = deque(maxlen=RETENTION_BUCKETS)

        data_dir = data_dir or os.environ.get("CTEK_DATA", "/data")
        self.path = os.path.join(data_dir, "history.jsonl") if data_dir else None
        self._appended = 0
        self._bucket_t: float | None = None
        self._house = None
        self._car = None
        self._setpoint = None

        self._load()

    # ---------- persistence ----------

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        rows, bad = [], 0
        try:
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        t, house, car, sp = json.loads(line)
                        rows.append((t, house, car, sp))
                    except Exception:
                        bad += 1
        except Exception as e:
            _LOG.warning("Could not read chart history: %s", e)
            return

        cutoff = time.time() - RETENTION_BUCKETS * BUCKET_SECONDS
        rows = [r for r in rows if r[0] >= cutoff]
        self.long.extend(rows[-RETENTION_BUCKETS:])
        self._appended = len(rows)
        _LOG.info("Restored %d minutes of chart history%s",
                  len(self.long), f" ({bad} unreadable lines skipped)" if bad else "")
        if bad or self._appended > COMPACT_AT:
            self._compact()

    def _append(self, row) -> None:
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
            self._appended += 1
            if self._appended > COMPACT_AT:
                self._compact()
        except Exception as e:
            # Losing chart history must never take the balancer down with it.
            # Deliberately broad: a bad path raises ValueError, not OSError, and
            # the control loop calling this has no business dying over a chart.
            _LOG.warning("Could not persist chart history, continuing without "
                         "it: %s", e)
            self.path = None

    def _compact(self) -> None:
        """Rewrite the file down to what we actually keep."""
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for row in self.long:
                    f.write(json.dumps(list(row), separators=(",", ":")) + "\n")
            os.replace(tmp, self.path)
            self._appended = len(self.long)
            _LOG.info("Compacted chart history to %d rows", self._appended)
        except Exception as e:
            _LOG.warning("Could not compact chart history: %s", e)

    # ---------- recording ----------

    def add(self, now: float, house, car, setpoint) -> None:
        self.live.append((round(now, 1), house, car, setpoint))

        bucket = now - (now % BUCKET_SECONDS)
        if self._bucket_t is None:
            self._bucket_t = bucket
        elif bucket != self._bucket_t:
            self._flush()
            self._bucket_t = bucket

        self._house = _worst(self._house, house)
        self._car = _worst(self._car, car)
        if setpoint is not None:
            self._setpoint = setpoint if self._setpoint is None else min(self._setpoint, setpoint)

    def _flush(self) -> None:
        row = [round(self._bucket_t, 0), self._house, self._car, self._setpoint]
        self.long.append(tuple(row))
        self._append(row)
        self._house = self._car = self._setpoint = None

    def close(self) -> None:
        """Persist the bucket in progress so a restart does not lose it."""
        if self._bucket_t is not None and (self._house or self._car):
            self._flush()

    # ---------- reading ----------

    def series(self, minutes: int) -> dict:
        """
        Live resolution when the live tier actually covers the window, minute
        buckets otherwise.

        Coverage matters as much as capacity. The live tier is memory-only, so
        for the first half hour after a restart it holds almost nothing - and
        falling back to the persisted buckets is what stops the default view
        from being blank exactly when someone has just restarted the add-on and
        wants to see what happened.
        """
        window = minutes * 60
        cutoff = time.time() - window
        live = [r for r in self.live if r[0] >= cutoff]
        long = [r for r in self.long if r[0] >= cutoff]

        if window <= LIVE_SECONDS and live:
            covered = time.time() - live[0][0]
            # Prefer the fine tier once it spans most of the asked-for window,
            # or when there is nothing coarser to fall back to anyway.
            if covered >= window * 0.8 or not long:
                return self._pack(live, "1s")

        if long:
            return self._pack(long, "1m")
        return self._pack(live, "1s")

    @staticmethod
    def _pack(rows, resolution: str) -> dict:
        return {
            "t": [r[0] for r in rows],
            "house": [r[1] for r in rows],
            "car": [r[2] for r in rows],
            "setpoint": [r[3] for r in rows],
            "resolution": resolution,
        }
