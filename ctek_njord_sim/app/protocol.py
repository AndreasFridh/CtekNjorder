"""
Wire format for the CTEK Njord GO MQTT broker.

Everything here is derived from a live capture of a real Nanogrid Air; see
PROTOCOL.md at the repo root. Kept free of I/O so it can be unit-tested.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

# StationPhaseRotation maps the charger's phase i onto a meter phase index.
# "RST" is straight-through; the others describe a rotated/swapped connection.
PHASE_ROTATIONS: dict[str, tuple[int, int, int]] = {
    "RST": (0, 1, 2),
    "RTS": (0, 2, 1),
    "SRT": (1, 0, 2),
    "STR": (1, 2, 0),
    "TRS": (2, 0, 1),
    "TSR": (2, 1, 0),
}

# Identify ourselves exactly as the real adapter does. The charger appears to
# accept any adapter, but matching the vendor strings costs nothing.
ADAPTER_VENDOR = "CTEK"
ADAPTER_FW = "ngair.1.3.2-0-g388a64c"
METER_TYPE = "P1"
METER_VENDOR = "KAM"


@dataclass(frozen=True)
class Topics:
    """Every topic is keyed by the charger serial, the adapter serial, or both."""

    charger: str
    adapter: str
    outlet: int = 1

    # --- published by the charger (we subscribe) ---

    @property
    def station_config(self) -> str:
        return f"ctek/ng-v2/client/{self.charger}/configuration"

    @property
    def outlet_config(self) -> str:
        return f"ctek/ng-v2/client/{self.charger}/{self.outlet}/configuration"

    @property
    def outlet_update(self) -> str:
        return f"ctek/ng-v2/client/{self.charger}/{self.outlet}/update"

    @property
    def outlet_info(self) -> str:
        return f"ctek/ng-v2/client/{self.charger}/{self.outlet}/info"

    # --- published by us, impersonating the adapter ---

    @property
    def control_current(self) -> str:
        """The one topic that actually steers the charger."""
        return f"ctek/ng-v2/controller/{self.charger}/{self.outlet}/current"

    @property
    def adapter_info_topics(self) -> tuple[str, str]:
        # Note: adapterinfo is keyed by the CHARGER serial on both trees.
        return (
            f"ctek/client/{self.charger}/sma/adapterinfo",
            f"ctek/nga/{self.charger}/adapterinfo",
        )

    @property
    def meter_info_topics(self) -> tuple[str, str]:
        # ...but meterinfo and meterdata use the ADAPTER serial on the nga tree.
        return (
            f"ctek/client/{self.charger}/sma/meterinfo",
            f"ctek/nga/{self.adapter}/meterinfo",
        )

    @property
    def interval_topics(self) -> tuple[str, str]:
        return (
            f"ctek/client/{self.charger}/sma/interval",
            f"ctek/nga/{self.adapter}/interval",
        )

    @property
    def meter_data_topics(self) -> tuple[str, str]:
        return (
            f"ctek/client/{self.charger}/sma/meterdata",
            f"ctek/nga/{self.adapter}/meterdata",
        )

    def subscriptions(self) -> list[str]:
        return [
            self.station_config,
            self.outlet_config,
            self.outlet_update,
            self.outlet_info,
            # Our own control topic, to hear anyone else commanding the charger.
            self.control_current,
        ]


def adapter_info_payload(adapter_serial: str) -> bytes:
    return json.dumps(
        {"serialno": adapter_serial, "fwVersion": ADAPTER_FW, "vendor": ADAPTER_VENDOR}
    ).encode()


def meter_info_payload(meter_id: str = "") -> bytes:
    return json.dumps(
        {"meterId": meter_id, "meterType": METER_TYPE, "vendor": METER_VENDOR}
    ).encode()


def meter_data_payload(
    current: list[float],
    voltage: list[float],
    active_power_in: float,
    active_power_out: float = 0.0,
) -> bytes:
    """activePower* are in kW - the charger's own info.power is in W."""
    return json.dumps(
        {
            "activePowerIn": round(active_power_in, 3),
            "activePowerOut": round(active_power_out, 3),
            "current": [round(c, 1) for c in current],
            "voltage": [round(v, 1) for v in voltage],
        }
    ).encode()


def interval_payload(seconds: int = 10) -> bytes:
    """Bare integer: how often we will publish meterdata."""
    return str(int(seconds)).encode()


def control_current_payload(amps: int) -> bytes:
    """A bare integer, not JSON. This is what the real adapter sends."""
    return str(int(amps)).encode()


# How recent the charger's echo of its setpoint must be to be believed.
ECHO_FRESH = 5.0


def heartbeat_needed(setpoint: int, echoed, echo_age: float) -> bool:
    """
    On a heartbeat, does this setpoint have to be sent again?

    Always, except for a pause the charger has already confirmed. Re-sending 0
    to a charger that is paused appeared to make it go through the pause
    again every heartbeat - the car could be heard starting and stopping every
    15 s while charging was not allowed. Once `MaxAllowedCurrent` echoes 0 there
    is nothing to refresh; should the charger ever report anything else, or
    stop reporting, the next heartbeat sends 0 again.

    Non-zero setpoints are left exactly as they were: the charger's behaviour
    on controller silence is unknown, so they keep being refreshed.
    """
    if setpoint != 0:
        return True
    return not (echoed == 0 and echo_age <= ECHO_FRESH)


class ForeignCommands:
    """
    Tells our own setpoints apart from anyone else's on the control topic.

    The broker echoes our publishes back to us, so a value we did not send
    recently is someone else's - in practice a Nanogrid Air that is still
    plugged in. Two controllers on one charger fight: ours said 0 A, it said
    16 A a couple of seconds later, and the car started and stopped every
    heartbeat. The charger gives no other sign of it; its `MaxAllowedCurrent`
    simply showed the other controller's value throughout.
    """

    ECHO_WINDOW = 5.0

    def __init__(self):
        self._sent: list[tuple[float, int]] = []
        self.last_value: int | None = None
        self.last_at: float | None = None

    def sent(self, now: float, value: int) -> None:
        self._sent = [(t, v) for t, v in self._sent if now - t <= self.ECHO_WINDOW]
        self._sent.append((now, int(value)))

    def seen(self, now: float, value) -> bool:
        """Record a value heard on the control topic. True if it was not ours."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            return False
        for i, (t, v) in enumerate(self._sent):
            if v == value and now - t <= self.ECHO_WINDOW:
                del self._sent[i]            # each echo accounts for one send
                return False
        self.last_value, self.last_at = value, now
        return True


def parse_outlet_update(payload: dict) -> dict:
    """Normalise the charger's 1 Hz status message."""
    return {
        "state": payload.get("State"),
        "ev_uses_phase": payload.get("EvUsesPhase") or [1, 1, 1],
        "max_allowed_current": payload.get("MaxAllowedCurrent"),
        "current": [float(c) for c in (payload.get("Current") or [0.0, 0.0, 0.0])],
    }


def parse_outlet_config(payload: dict) -> dict:
    """The charger's own limits, which override our optimistic defaults."""
    return {
        "fuse_rating": payload.get("FuseRating"),
        "min_allowed_current": payload.get("MinAllowedCurrent"),
        "phase_connected": payload.get("PhaseConnected") or [True, True, True],
        "primary_phase": payload.get("PrimaryPhase", 1),
    }
