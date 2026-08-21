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


@dataclass
class Options:
    charger_host: str = "192.168.5.40"
    charger_port: int = 1883
    charger_username: str = ""
    charger_password: str = ""
    # Blank means "discover from the broker's retained topics".
    charger_serial: str = ""
    adapter_serial: str = ""

    main_fuse: int = 25
    max_charge_current: int = 16
    safety_margin: float = 1.0
    phase_rotation: str = "RST"

    current_entities: list[str] = field(default_factory=list)
    voltage_entities: list[str] = field(default_factory=list)
    power_in_entity: str = ""
    power_out_entity: str = ""

    stale_timeout: int = 30
    fallback_current: int = 6
    raise_delay: int = 30
    settle_window: float = 10.0
    settle_tolerance: float = 1.5
    control_interval: float = 15.0
    meter_interval: float = 10.0

    dry_run: bool = True
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
        return opts

    def validate(self) -> list[str]:
        """Return a list of fatal problems; empty means good to run."""
        problems = []
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
