"""
Describes every add-on option so the web UI can render and validate them
without duplicating knowledge of the schema in JavaScript.

`restart` marks options that only take effect after the add-on restarts.
Everything else is applied to the running balancer the moment it is saved,
which is why the fuse and the limits can be tuned while a car is charging.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class Spec:
    key: str
    label: str
    type: str  # int|float|str|password|bool|select|entity|entity3|chargers
    group: str
    help: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str = ""
    choices: tuple[str, ...] = ()
    restart: bool = False
    advanced: bool = False


SPECS: tuple[Spec, ...] = (
    # --- chargers ---
    Spec("chargers", "Chargers", "chargers", "Chargers", restart=True,
         help="Each charger hosts its own MQTT broker, so each needs its own "
              "address. Leave a row's address blank to ignore it. Serial is "
              "discovered automatically when left empty."),
    Spec("allocation_strategy", "Sharing", "select", "Chargers",
         choices=("optimal", "even"),
         help="How spare current is split between cars that are charging. "
              "'optimal' notices a car that is not taking everything it was "
              "offered - because of its own onboard limit, or because it is "
              "tapering near full - and gives the surplus to a car that can "
              "use it. 'even' always splits equally."),
    Spec("adapter_serial", "Adapter serial", "str", "Chargers", restart=True,
         advanced=True,
         help="The serial we announce as. Any stable string works."),
    Spec("charger_username", "MQTT username", "str", "Chargers", restart=True,
         advanced=True, help="Not required - the broker accepts anonymous clients."),
    Spec("charger_password", "MQTT password", "password", "Chargers", restart=True,
         advanced=True, help="Not required."),

    # --- limits ---
    Spec("main_fuse", "Main fuse", "int", "Limits", min=6, max=125, unit="A",
         help="Your property's MAIN breaker, per phase - not the charger's. "
              "If unsure, pick the lower value: too low charges slowly, "
              "too high trips the breaker."),
    Spec("max_charge_current", "Max per charger", "int", "Limits",
         min=6, max=32, unit="A",
         help="The most any single charger may be given. Each charger's own "
              "rating still applies on top, and the main fuse always wins."),
    Spec("safety_margin", "Safety margin", "float", "Limits",
         min=0, max=10, step=0.5, unit="A",
         help="Amps held back from the fuse."),
    Spec("fallback_current", "Fallback current", "int", "Limits",
         min=0, max=16, unit="A",
         help="Commanded when meter readings go stale. 0 pauses charging."),
    Spec("phase_rotation", "Phase rotation", "select", "Limits",
         choices=("RST", "RTS", "SRT", "STR", "TRS", "TSR"), advanced=True,
         help="Overridden by the charger's own StationPhaseRotation."),

    # --- meter ---
    Spec("current_entities", "Current sensors", "entity3", "Meter", restart=True,
         unit="A",
         help="Three entities reporting amps per phase at the grid connection "
              "point, INCLUDING the charger. The car's draw is subtracted "
              "automatically."),
    Spec("voltage_entities", "Voltage sensors", "entity3", "Meter", restart=True,
         unit="V", advanced=True,
         help="Optional. Defaults to 230 V per phase. Affects only the meter "
              "data mirrored to the charger, not balancing."),
    Spec("power_in_entity", "Import power", "entity", "Meter", restart=True,
         unit="kW", advanced=True, help="Optional. Derived from current if unset."),
    Spec("power_out_entity", "Export power", "entity", "Meter", restart=True,
         unit="kW", advanced=True, help="Optional. Solar export."),

    # --- automation ---
    Spec("charge_enable_entity", "Charge enable", "entity", "Automation",
         restart=True,
         help="Optional. An entity Home Assistant sets on or off to permit "
              "charging - use it to charge only when electricity is cheap. "
              "Leave blank and charging is always permitted. If it is set but "
              "unavailable, charging is permitted, so a dropped sensor cannot "
              "silently leave a car uncharged overnight."),
    Spec("price_entity", "Electricity price", "entity", "Automation",
         restart=True,
         help="Optional. Current price per kWh. Used to cost each charging "
              "session and show what charging is costing per hour. The unit "
              "is read from the entity, so ore/cents are handled as well as "
              "whole units."),
    Spec("currency", "Currency", "str", "Automation",
         help="Shown next to costs. Cosmetic only."),

    # --- behaviour ---
    Spec("dry_run", "Dry run", "bool", "Behaviour",
         help="Log the setpoint without sending it. Turn this off to take "
              "control - and block the Nanogrid Air first."),
    Spec("stale_timeout", "Stale timeout", "int", "Behaviour",
         min=5, max=600, unit="s",
         help="How old readings may get before falling back."),
    Spec("raise_delay", "Raise delay", "int", "Behaviour", min=0, max=600, unit="s",
         help="Sustained headroom required before increasing. Reductions are "
              "always immediate."),
    Spec("settle_window", "Settle window", "float", "Behaviour",
         min=0, max=120, step=1, unit="s", advanced=True,
         help="After a change the car is still ramping and the meter still "
              "shows its old draw, so the baseline is untrusted for this long."),
    Spec("settle_tolerance", "Settle tolerance", "float", "Behaviour",
         min=0.1, max=10, step=0.1, unit="A", advanced=True,
         help="Command-vs-actual mismatch that counts as 'still ramping'."),
    Spec("control_interval", "Setpoint heartbeat", "float", "Behaviour",
         min=1, max=60, step=1, unit="s", advanced=True,
         help="How often the setpoint is republished even when unchanged."),
    Spec("meter_interval", "Meter interval", "float", "Behaviour",
         min=1, max=60, step=1, unit="s", advanced=True,
         help="How often meter data is mirrored to the charger."),
    Spec("ping_interval", "Link check interval", "int", "Behaviour",
         min=5, max=600, unit="s", advanced=True,
         help="How often to measure the connection to each charger. A TCP "
              "connect to its MQTT port, not ICMP - it needs no extra "
              "privileges and tests the path charging actually uses."),
    Spec("log_level", "Log level", "select", "Behaviour",
         choices=("trace", "debug", "info", "warning", "error")),
    Spec("restrict_api", "Restrict API to Ingress", "bool", "Behaviour",
         restart=True, advanced=True,
         help="Only accept web requests proxied by Home Assistant. Turn off "
              "only if the UI stops loading; the log names the rejected "
              "address so you can see what to allow."),
)

BY_KEY = {s.key: s for s in SPECS}

# Applied to the running balancer without a restart.
LIVE_KEYS = {s.key for s in SPECS if not s.restart}


def as_json() -> list[dict]:
    return [asdict(s) for s in SPECS]


def coerce(key: str, value):
    """Cast a value coming from the browser into the type the schema expects."""
    spec = BY_KEY.get(key)
    if spec is None:
        raise KeyError(key)

    if spec.type == "bool":
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes", "on")
        return bool(value)

    if spec.type in ("int", "float"):
        if value in ("", None):
            raise ValueError(f"{spec.label} cannot be empty")
        number = float(value)
        if spec.min is not None and number < spec.min:
            raise ValueError(f"{spec.label} must be at least {spec.min}")
        if spec.max is not None and number > spec.max:
            raise ValueError(f"{spec.label} must be at most {spec.max}")
        return int(number) if spec.type == "int" else number

    if spec.type == "select":
        if value not in spec.choices:
            raise ValueError(f"{spec.label} must be one of {', '.join(spec.choices)}")
        return value

    if spec.type == "chargers":
        rows = []
        for row in (value or []):
            if not isinstance(row, dict):
                continue
            host = str(row.get("host") or "").strip()
            if not host:
                continue          # a blank address is how a row is disabled
            try:
                port = int(row.get("port") or 1883)
            except (TypeError, ValueError):
                raise ValueError(f"{host}: port must be a number")
            if not 1 <= port <= 65535:
                raise ValueError(f"{host}: port must be between 1 and 65535")
            rows.append({
                "name": str(row.get("name") or "").strip() or host,
                "host": host,
                "port": port,
                "serial": str(row.get("serial") or "").strip(),
                "enabled": bool(row.get("enabled", True)),
            })
        if not rows:
            raise ValueError("At least one charger address is required")
        seen = set()
        for r in rows:
            key = (r["host"], r["port"])
            if key in seen:
                raise ValueError(f"{r['host']}:{r['port']} is listed twice")
            seen.add(key)
        if len(rows) > 6:
            raise ValueError("At most 6 chargers")
        return rows

    if spec.type in ("entity3",):
        items = [str(v).strip() for v in (value or [])]
        items = [v for v in items if v]
        # An empty list is legitimate for the optional voltage triple.
        if items and len(items) != 3:
            raise ValueError(f"{spec.label} needs exactly 3 entities, or none")
        return items

    return str(value or "")
