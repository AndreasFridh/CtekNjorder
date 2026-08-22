"""
A record of every charging session.

Sessions are what someone actually wants to look back at - when a car charged,
for how long, how much it took and what it cost - so they have to outlive a
restart. Kept the same way as the chart history: append-only JSONL in /data,
one line per completed session, bounded and compacted rather than growing
without end.

A session is small, so a couple of years of daily charging is well under a
megabyte. Failing to write one must never disturb charging, which is why every
disk operation here is allowed to fail quietly.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque

_LOG = logging.getLogger(__name__)

KEEP = 500                 # sessions retained; years of daily charging
COMPACT_AT = KEEP * 2


class SessionLog:
    def __init__(self, data_dir: str | None = None):
        data_dir = data_dir or os.environ.get("CTEK_DATA", "/data")
        self.path = os.path.join(data_dir, "sessions.jsonl") if data_dir else None
        self.sessions: deque[dict] = deque(maxlen=KEEP)
        self._appended = 0
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
                        row = json.loads(line)
                        if isinstance(row, dict) and "started" in row:
                            rows.append(row)
                        else:
                            bad += 1
                    except Exception:
                        bad += 1
        except Exception as e:
            _LOG.warning("Could not read the session log: %s", e)
            return

        rows.sort(key=lambda r: r.get("started", 0))
        self.sessions.extend(rows[-KEEP:])
        self._appended = len(rows)
        _LOG.info("Restored %d charging sessions%s", len(self.sessions),
                  f" ({bad} unreadable lines skipped)" if bad else "")
        if bad or self._appended > COMPACT_AT:
            self._compact()

    def _compact(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for row in self.sessions:
                    f.write(json.dumps(row, separators=(",", ":")) + "\n")
            os.replace(tmp, self.path)
            self._appended = len(self.sessions)
            _LOG.info("Compacted the session log to %d rows", self._appended)
        except Exception as e:
            _LOG.warning("Could not compact the session log: %s", e)

    # ---------- recording ----------

    def add(self, record: dict) -> None:
        self.sessions.append(record)
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, separators=(",", ":")) + "\n")
            self._appended += 1
            if self._appended > COMPACT_AT:
                self._compact()
        except Exception as e:
            # Losing the log is a nuisance. Interrupting charging is not.
            _LOG.warning("Could not write the session log, continuing without "
                         "it: %s", e)
            self.path = None

    # ---------- reading ----------

    def list(self, limit: int = 100, charger: str | None = None) -> list[dict]:
        """Newest first, which is the order anyone reads a log in."""
        rows = [r for r in self.sessions
                if charger is None or r.get("charger") == charger]
        return list(reversed(rows))[:limit]

    def totals(self, since: float | None = None, charger: str | None = None) -> dict:
        rows = [r for r in self.sessions
                if (charger is None or r.get("charger") == charger)
                and (since is None or r.get("started", 0) >= since)]
        energy = sum(r.get("energy_kwh", 0.0) for r in rows)
        cost = sum(r.get("cost", 0.0) for r in rows)
        return {
            "sessions": len(rows),
            "energy_kwh": round(energy, 3),
            "cost": round(cost, 2),
            "hours": round(sum(r.get("duration_s", 0) for r in rows) / 3600.0, 1),
            # What the energy averaged out at, which is the number that says
            # whether charging at cheap times is actually working.
            "avg_price": round(cost / energy, 3) if energy > 0.01 else None,
        }

    def summary(self) -> dict:
        day = 24 * 3600
        now = time.time()
        return {
            "all": self.totals(),
            "month": self.totals(since=now - 30 * day),
            "week": self.totals(since=now - 7 * day),
        }
