# CTEK Njord Load Balancer

A Home Assistant add-on that replaces the **CTEK Nanogrid Air**. It impersonates
the adapter on the charger's own MQTT broker and load-balances EV charging
against real-time current readings from Home Assistant.

The MQTT protocol between the Njord GO and the Nanogrid Air is not documented by
CTEK. It was reverse-engineered from live captures — see **[PROTOCOL.md](PROTOCOL.md)**.

## Status

| Piece | State |
|---|---|
| Protocol discovery | Done — full topic map, verified against a 15 min live capture |
| Balancer | Done — 137 tests, reproduces the real adapter's decisions 100% in steady state |
| MQTT client / adapter impersonation | Done — verified end-to-end against the real charger in dry-run |
| Offline simulation rig | Done — fake charger + fake HA, exercises overload and recovery |
| Live control (`dry_run: false`) | Verified against the simulator; **not yet against the real charger** |
| Home Assistant data source | Done — **needs your entity IDs** |

## Install

This is a Home Assistant **add-on repository**. Add it once, then install the
add-on from the store.

1. In Home Assistant: **Settings → Add-ons → Add-on Store**
2. Top-right **⋮ → Repositories**
3. Paste `https://github.com/AndreasFridh/CtekNjorder` and click **Add**
4. Close the dialog; **CTEK Njord Load Balancer** appears in the store
5. **Install**, then set `main_fuse` and `current_entities` on the Configuration tab
6. **Start**

Requires Home Assistant OS or Supervised — the add-on store is not available on
Core or Container installs.

The add-on ships with `dry_run: true`, so it will log the setpoints it would
send without sending any. Full setup and options are in
[ctek_njord_sim/DOCS.md](ctek_njord_sim/DOCS.md).

> **Block the Nanogrid Air before turning `dry_run` off.** Two controllers
> publishing to the same topic will fight over the charging current.

## How it works

```
  P1 meter ──► Home Assistant ──► this add-on ──► charger's MQTT broker
                (per-phase A)      (balancer)     ctek/ng-v2/controller/{serial}/1/current
```

The charger runs the MQTT broker itself. We connect to it as a client, publish
meter data exactly as the Nanogrid Air does, and command an allowed current.

Each tick the add-on:

1. Reads per-phase house current from HA (push, via the WebSocket API).
2. Subtracts the car's own draw — reported by the charger — to get the house baseline.
3. Computes headroom: `main_fuse - safety_margin - baseline`.
4. Snaps to the legal set (`0`, or `6..16 A`) and publishes it.

Meter data is republished to the charger every 10 s and the setpoint every 15 s,
matching the real adapter's cadence.

### Safety behaviour

- **Cold start at 6 A.** Like the real adapter, it opens at the minimum and only
  raises once it has valid readings.
- **Shed immediately, restore slowly.** Overload throttles on the next tick;
  raising waits `raise_delay` (30 s) so the setpoint cannot oscillate.
- **Ignores its own transients.** For a few seconds after each change the car
  is still slewing, and the meter still reports its previous draw — so
  `house - car` briefly over-states the baseline. The balancer holds rather
  than throttling against its own wake. Commanding 0 A is exempt, so a real
  overload is never delayed.
- **Stale data falls back.** If HA current readings age past `stale_timeout`,
  the add-on drops to `fallback_current` rather than guessing.
- **Never emits an illegal value.** Output is always `0` or `6..16 A`; there is
  no valid setpoint between 1 and 5 A.
- **Pauses instead of overloading.** If the house alone fills the fuse, it
  commands 0 rather than a value that would trip the main breaker.

## Before you install

**Block the Nanogrid Air first.** Two controllers publishing to the same topic
will fight. In UniFi: Client Devices → the Nanogrid Air → **Block**. This is
reversible and needs no reset; the device recovers on its own when unblocked.

## Configuration

| Option | Default | Notes |
|---|---|---|
| `charger_host` | `192.168.5.40` | The charger. It *is* the broker. |
| `charger_port` | `1883` | Plain MQTT, no TLS. |
| `charger_username` / `charger_password` | empty | Not required — the broker accepts anonymous connections. Present in case a firmware update changes that. |
| `charger_serial` | empty | Auto-discovered from retained topics when blank. |
| `main_fuse` | `25` | **Set this to your actual main fuse.** The hard ceiling. |
| `max_charge_current` | `16` | Your own cap. Also limited by the charger's 16 A `FuseRating`. |
| `safety_margin` | `1.0` | Amps held back from the fuse. |
| `phase_rotation` | `RST` | Overridden by the charger's own `StationPhaseRotation`. |
| `current_entities` | — | **Required.** Exactly 3 entity IDs, one per phase. |
| `voltage_entities` | — | Optional; defaults to 230 V per phase. |
| `power_in_entity` / `power_out_entity` | — | Optional; derived from current × voltage if unset. |
| `stale_timeout` | `30` | Seconds before HA data is considered stale. |
| `fallback_current` | `6` | Commanded when flying blind. |
| `raise_delay` | `30` | Seconds of sustained headroom before raising. |
| `settle_window` | `10` | Seconds after a change during which the baseline is treated as unreliable. |
| `settle_tolerance` | `1.5` | Amps of mismatch between command and actual draw that counts as "still slewing". |
| `dry_run` | `true` | **Starts safe.** Logs decisions without sending them. |

## Tools

These run against a live charger from any machine on the network.

```bash
python tools/probe.py      --host 192.168.5.40          # find the broker, scan ports
python -u tools/test_write.py --host 192.168.5.40       # check publish permission
python tools/sniff.py      --host 192.168.5.40 --duration 300   # capture traffic
python tools/analyze.py    captures/<file>.jsonl        # derive the schema
python tools/replay.py     captures/<file>.jsonl --main-fuse 25 # replay through the balancer
python tools/replay.py     captures/<file>.jsonl --sweep        # compare fuse sizes
```

`sniff.py` uses a randomised client ID so it will not disconnect a running
Nanogrid Air — it is safe to run alongside the real hardware.

> The captures behind PROTOCOL.md are **not** included in this repo: they carry
> device serials and ~15 minutes of real household consumption. Everything they
> proved is written up in PROTOCOL.md, and `sniff.py` will produce equivalent
> captures from your own charger in a few minutes.

### Offline simulation

A fake charger (broker included, as the real one is) and a fake Home Assistant,
so the add-on can be run with `dry_run: false` against nothing real. The mock
models the car ramping toward its allowance and walks a load profile through
throttle, pause and recovery — scenarios that cannot safely be staged in a
real house.

```bash
python tools/mock_charger.py --port 18830          # terminal 1
python tools/mock_hass.py --port 18123 --mqtt-port 18830   # terminal 2
CTEK_OPTIONS=sim-options.json \n  CTEK_HASS_WS=ws://127.0.0.1:18123/api/websocket \n  python -m app.main                               # terminal 3, from ctek_njord_sim/
```

The oscillation the settle guard fixes was found this way, not by inspection.

## Development

```bash
python -m venv .venv && ./.venv/Scripts/pip install paho-mqtt aiohttp pytest
./.venv/Scripts/python -m pytest tests/ -q
```

Run the add-on outside Home Assistant by pointing `CTEK_OPTIONS` at an options
JSON file and `CTEK_HASS_WS` at a Home Assistant WebSocket URL.

## Known unknowns

Carried from [PROTOCOL.md](PROTOCOL.md); these are not yet answered:

1. **What the charger does when the controller goes silent** — hold, decay to
   6 A, or stop. Until this is known, treat an add-on crash as unsafe.
2. **Whether `0` is accepted** to pause charging. Only `6` and `16` observed.
3. **The `State` enum** beyond `2` (charging).
4. **Real overload behaviour** — the capture's house baseline never exceeded
   5.5 A, so throttling is backed by unit tests, not field evidence.
