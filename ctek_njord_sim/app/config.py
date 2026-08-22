"""
Add-on options.

Home Assistant writes the user's choices to /data/options.json. For running
outside the Supervisor (tests, a laptop) point CTEK_OPTIONS at any JSON file.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

OPTIONS_PATH = os.environ.get("CTEK_OPTIONS", "/data/options.json")

# Six is arbitrary but generous: one Nanogrid Air controls exactly one charger,
# and a domestic service runs out of current long before it runs out of slots.
MAX_CHARGERS = 6


@dataclass
class Options:
    charger_host: str = ""
    charger_port: int = 1883
    charger_username: str = ""
    charger_password: str = ""
    # Blank means "discover from the broker's retained topics".
    charger_serial: str = ""
    adapter_serial: str = ""

    # Each charger hosts its own MQTT broker, so each needs its own address.
    # [{name, host, port, serial, enabled}]
    chargers: list[dict] = field(default_factory=list)
    allocation_strategy: str = "optimal"

    main_fuse: int = 25
    max_charge_current: int = 16
    safety_margin: float = 1.0
    phase_rotation: str = "RST"

    current_entities: list[str] = field(default_factory=list)
    voltage_entities: list[str] = field(default_factory=list)
    power_in_entity: str = ""
    power_out_entity: str = ""

    charge_enable_entity: str = ""
    price_entity: str = ""
    currency: str = "SEK"

    stale_timeout: int = 30
    fallback_current: int = 6
    raise_delay: int = 30
    settle_window: float = 10.0
    settle_tolerance: float = 1.5
    ping_interval: int = 30
    control_interval: float = 15.0
    meter_interval: float = 10.0

    dry_run: bool = True
    restrict_api: bool = True
    log_level: str = "info"

    @classmethod
    def load(cls, path: str = OPTIONS_PATH) -> "Options":
        data: dict = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        known = {f.name for f in cls.__dataclass_fields__.values()}
        opts = cls(**{k: v for k, v in data.items() if k in known})
        opts.current_entities = [e for e in (opts.current_entities or []) if e]
        opts.voltage_entities = [e for e in (opts.voltage_entities or []) if e]
        opts._normalise_chargers()
        return opts

    def _normalise_chargers(self) -> None:
        """
        Clean the charger list, and carry a single-charger install forward.

        Versions before multi-charger support configured one charger through
        flat `charger_*` keys. Those installs must keep working untouched, so
        when the list is empty the flat keys are promoted into it.
        """
        cleaned: list[dict] = []
        for i, c in enumerate(self.chargers or []):
            if not isinstance(c, dict):
                continue
            host = str(c.get("host") or "").strip()
            if not host:
                continue          # a blank row is how the UI represents "unused"
            try:
                port = int(c.get("port") or 1883)
            except (TypeError, ValueError):
                port = 1883
            cleaned.append({
                "name": str(c.get("name") or "").strip() or f"Charger {i + 1}",
                "host": host,
                "port": port,
                "serial": str(c.get("serial") or "").strip(),
                "enabled": bool(c.get("enabled", True)),
            })

        if not cleaned and self.charger_host:
            cleaned = [{
                "name": "Charger 1",
                "host": self.charger_host,
                "port": self.charger_port,
                "serial": self.charger_serial,
                "enabled": True,
            }]

        self.chargers = cleaned[:MAX_CHARGERS]

    def active_chargers(self) -> list[dict]:
        return [c for c in self.chargers if c.get("enabled", True)]

    def validate(self) -> list[str]:
        """Return a list of fatal problems; empty means good to run."""
        problems = []
        if not self.active_chargers():
            problems.append("no chargers configured - add at least one address")
        seen = {}
        for c in self.active_chargers():
            key = (c["host"], c["port"])
            if key in seen:
                problems.append(
                    f"{c['name']} and {seen[key]} share the address "
                    f"{c['host']}:{c['port']}"
                )
            seen[key] = c["name"]
        if len(self.current_entities) != 3:
            problems.append(
                f"current_entities must list exactly 3 entity_ids (one per phase), "
                f"got {len(self.current_entities)}"
            )
        if self.max_charge_current > self.main_fuse:
            problems.append(
                f"max_charge_current ({self.max_charge_current}A) exceeds "
                f"main_fuse ({self.main_fuse}A)"
            )
        if self.fallback_current > self.max_charge_current:
            problems.append(
                f"fallback_current ({self.fallback_current}A) exceeds "
                f"max_charge_current ({self.max_charge_current}A)"
            )
        return problems
