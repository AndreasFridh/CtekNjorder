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
    type: str  # int | float | str | password | bool | select | entity | entity3
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
    # --- charger ---
    Spec("charger_host", "Charger address", "str", "Charger", restart=True,
         help="IP of the Njord. The charger hosts the MQTT broker itself."),
    Spec("charger_port", "MQTT port", "int", "Charger", min=1, max=65535,
         restart=True, help="Normally 1883. Plain MQTT, no TLS."),
    Spec("charger_serial", "Charger serial", "str", "Charger", restart=True,
         help="Leave blank to discover it from the broker's retained topics."),
    Spec("adapter_serial", "Adapter serial", "str", "Charger", restart=True,
         advanced=True,
         help="The serial we announce as. Any stable string works."),
    Spec("charger_username", "MQTT username", "str", "Charger", restart=True,
         advanced=True, help="Not required - the broker accepts anonymous clients."),
    Spec("charger_password", "MQTT password", "password", "Charger", restart=True,
         advanced=True, help="Not required."),

    # --- limits ---
    Spec("main_fuse", "Main fuse", "int", "Limits", min=6, max=125, unit="A",
         help="Your property's MAIN breaker, per phase - not the charger's. "
              "If unsure, pick the lower value: too low charges slowly, "
              "too high trips the breaker."),
    Spec("max_charge_current", "Max charge current", "int", "Limits",
         min=6, max=32, unit="A",
         help="Your own cap. The charger's own rating still applies on top."),
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

    if spec.type in ("entity3",):
        items = [str(v).strip() for v in (value or [])]
        items = [v for v in items if v]
        # An empty list is legitimate for the optional voltage triple.
        if items and len(items) != 3:
            raise ValueError(f"{spec.label} needs exactly 3 entities, or none")
        return items

    return str(value or "")
