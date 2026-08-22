# CtekNjorder

A Home Assistant add-on that replaces the **CTEK Nanogrid Air**, and drives up
to six chargers instead of one.

It connects to each charger's own MQTT broker, impersonates the adapter, and
shares the available current between the cars that are charging — using
real-time readings from Home Assistant so it never exceeds your main fuse.

The protocol it speaks is not documented by CTEK. It was reverse-engineered
from live captures: see **[PROTOCOL.md](PROTOCOL.md)**.

> **The charger's MQTT broker has no authentication.** Anyone who can reach it
> on your network can command your charging current. Keep it off untrusted
> network segments — see [SECURITY.md](SECURITY.md).

## Install

Add this repository in Home Assistant under **Settings → Add-ons → Add-on
Store → ⋮ → Repositories**:

```
https://github.com/AndreasFridh/CtekNjorder
```

Then install **CtekNjorder**, start it, and open its Web UI. Everything is
configured there, including a picker for your meter entities.

Requires Home Assistant OS or Supervised. It ships with `dry_run` on, so it
watches and decides without commanding anything until you turn that off.

> Only one controller may run at a time. The charger obeys whoever publishes a
> setpoint, so take the original adapter off the network first.

**Setup, options and troubleshooting: [ctek_njord_sim/DOCS.md](ctek_njord_sim/DOCS.md)**

## What it does

- **Load balances** against your main fuse, per phase, from live current in
  Home Assistant.
- **Shares between up to six chargers.** Spare current from a car that cannot
  use its allowance goes to one that can.
- **Charges when you choose.** Point it at an entity your price automation
  switches, and charging waits for cheap hours.
- **Tracks cost.** Per-session energy and cost, priced at the rate in force at
  the time, with a browsable session log.
- **Watches the link** to each charger, so a marginal Wi-Fi connection is
  visible rather than mysterious.

## Development

```bash
python -m venv .venv
./.venv/Scripts/pip install paho-mqtt aiohttp pytest
./.venv/Scripts/python -m pytest tests/ -q
```

`tools/` holds what was used to work the protocol out — a port scanner, a
traffic sniffer, an analyser, and a replay harness that runs a real capture
through the balancer. It also has a fake charger and a fake Home Assistant, so
the add-on can be run against nothing real:

```bash
python tools/mock_charger.py --port 18830
python tools/mock_hass.py --port 18123 --mqtt-port 18830
```

Several of this project's more interesting bugs were found that way rather than
by inspection, because they only appear once a car reacts to its own setpoint.

Run the add-on outside Home Assistant by pointing `CTEK_OPTIONS` at an options
JSON file and `CTEK_HASS_WS` at a Home Assistant WebSocket URL.

## Status

Working and in use. What remains is in [todo.md](todo.md); the substantive item
is characterising what the charger does when its controller goes silent, which
needs hardware to answer.

## License

Copyright (C) 2026 Andreas Fridh. **AGPL-3.0-or-later** — see
[LICENSE](LICENSE).

Use, study, modify and share it freely. If you distribute a modified version,
or run one that other people interact with over a network, you must offer them
the source under the same license.

[PROTOCOL.md](PROTOCOL.md) records an interoperability finding about a
third-party product, taken from observed network traffic.
