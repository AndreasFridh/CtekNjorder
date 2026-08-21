"""
Supervisor and Home Assistant REST access.

Both are reached through the Supervisor's internal proxy using SUPERVISOR_TOKEN,
so nothing here needs a user-supplied token and none of it works (or is needed)
outside the add-on container.
"""
from __future__ import annotations

import logging
import os

import aiohttp

_LOG = logging.getLogger(__name__)

SUPERVISOR = "http://supervisor"
TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


def available() -> bool:
    return bool(TOKEN)


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


async def _json(method: str, url: str, payload: dict | None = None, timeout: float = 15.0):
    async with aiohttp.ClientSession() as session:
        async with session.request(
            method, url, headers=_headers(), json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status >= 400:
                raise RuntimeError(
                    f"{method} {url} -> {resp.status}: "
                    f"{(body or {}).get('message', body)}"
                )
            return body


async def get_options() -> dict:
    """The add-on's currently saved options, as the Supervisor holds them."""
    body = await _json("GET", f"{SUPERVISOR}/addons/self/info")
    return (body.get("data") or {}).get("options") or {}


async def set_options(changes: dict) -> None:
    """
    Persist option changes.

    Merged against what the Supervisor already has: POSTing options replaces
    the whole object, so sending only the changed keys would silently wipe
    every setting the user did not happen to touch.
    """
    current = await get_options()
    merged = {**current, **changes}
    await _json("POST", f"{SUPERVISOR}/addons/self/options", {"options": merged})
    _LOG.info("Saved options: %s", ", ".join(sorted(changes)))


async def restart_addon() -> None:
    _LOG.warning("Restarting add-on at the request of the web UI")
    await _json("POST", f"{SUPERVISOR}/addons/self/restart", timeout=60.0)


# Units that identify a sensor as a plausible source for each field.
_UNITS = {
    "current": ("A", "a"),
    "voltage": ("V", "v"),
    "power": ("kW", "W", "kw", "w"),
}


async def list_entities() -> dict[str, list[dict]]:
    """
    Candidate entities for the settings pickers, grouped by what they measure.

    Filtered by unit rather than by name so it works regardless of which P1
    integration the user runs.
    """
    try:
        states = await _json("GET", f"{SUPERVISOR}/core/api/states")
    except Exception as e:
        _LOG.warning("Could not list entities: %s", e)
        return {"current": [], "voltage": [], "power": []}

    out: dict[str, list[dict]] = {"current": [], "voltage": [], "power": []}
    for st in states or []:
        eid = st.get("entity_id", "")
        if not eid.startswith("sensor."):
            continue
        attrs = st.get("attributes") or {}
        unit = attrs.get("unit_of_measurement")
        if not unit:
            continue
        entry = {
            "entity_id": eid,
            "name": attrs.get("friendly_name") or eid,
            "unit": unit,
            "state": st.get("state"),
            "device_class": attrs.get("device_class"),
        }
        for kind, units in _UNITS.items():
            if unit in units:
                out[kind].append(entry)
                break

    for kind in out:
        # Surface the likeliest candidates first: a correct device_class is a
        # stronger signal than the unit alone.
        out[kind].sort(key=lambda e: (e["device_class"] != kind, e["entity_id"]))
    return out
