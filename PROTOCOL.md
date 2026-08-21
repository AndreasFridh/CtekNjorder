# CTEK Njord GO ↔ Nanogrid Air — MQTT protocol

Reverse-engineered from live captures on 2026-08-21 (a 60 s and a 15 min run).
Not documented by CTEK; derived entirely from observed traffic.

## Participants

| Role | Serial | Firmware | Notes |
|---|---|---|---|
| Charger (Njord GO) | `40353I37W4008218` | `r3.2.2-0-g673feded_mmiR1` | **Runs the MQTT broker** on `192.168.5.40:1883` |
| Nanogrid Air | `40542O36W4000074` | `ngair.1.3.2-0-g388a64c` | Meter gateway + load-balancing controller — **this is what we replace** |
| Meter | — | — | `meterType: "P1"`, `vendor: "KAM"` (Kamstrup, via P1/HAN) |

The charger is the broker. **Authentication is not enforced**: anonymous
connect, subscribe *and* publish all succeed (verified with `tools/test_write.py`).
Supplying `ctek` / a password also works, so the broker appears to ignore
credentials entirely. Plain MQTT 3.1.1, no TLS.

Throughout, `CB` = charger serial, `NGA` = adapter serial, `1` = outlet index.

## Topic map

### Published by the CHARGER (we subscribe)

**`ctek/ng-v2/client/{CB}/configuration`** — retained, static
```json
{"FW": "r3.2.2-0-g673feded_mmiR1", "StationPhaseRotation": "RST"}
```

**`ctek/ng-v2/client/{CB}/1/configuration`** — retained, static
```json
{"FuseRating": 16, "MinAllowedCurrent": 6, "PhaseConnected": [true,true,true], "PrimaryPhase": 1}
```
The charger's own limits. `FuseRating` 16 A is the charger's ceiling;
`MinAllowedCurrent` 6 A is the EV standard floor — below this a car must stop
rather than charge slower.

**`ctek/ng-v2/client/{CB}/1/update`** — every **1.0 s**
```json
{"State": 2, "EvUsesPhase": [1,1,1], "MaxAllowedCurrent": 16, "Current": [15.8,15.7,16.1]}
```
`Current` is the EV's actual per-phase draw. `MaxAllowedCurrent` echoes the
setpoint the charger is currently honouring — use it to confirm our commands
land. `State: 2` = charging (the only value seen; the car never unplugged).

**`ctek/ng-v2/client/{CB}/1/info`** — every **10 s**
```json
{"energy": 7824262, "power": 10130}
```
`energy` in Wh (lifetime, monotonic), `power` in W.

**`ctek/ng-v2/debug`** — retained, every **6.0 s**
```json
{"ids": "40353I37W4008218,", "status": [2,0,9,64]}
```
Confirmed charger-published: it kept a metronomic 6 s cadence straight through
an adapter restart. `ids` is a comma-terminated list of known charger serials.
`status[0]` tracks `State`. `status[2..3]` vary (`9,64` → `255,63` → `79,82`)
and remain unidentified — they are not needed for control.

### Published by the NANOGRID AIR (we must reproduce)

Meter data goes out on **two parallel topic trees** with identical payloads,
about 1 s apart. The `sma` tree ("smart meter adapter") is addressed to the
charger; the `nga` tree is keyed by the adapter's own serial.

**`ctek/client/{CB}/sma/adapterinfo`** and **`ctek/nga/{CB}/adapterinfo`** — retained
```json
{"serialno": "40542O36W4000074", "fwVersion": "ngair.1.3.2-0-g388a64c", "vendor": "CTEK"}
```
Note the asymmetry: `adapterinfo` uses the **charger's** serial on both trees,
while `meterinfo`/`meterdata` use the **adapter's** serial on the `nga` tree.
It reads as an announcement: "adapter X is now serving charger Y".

**`ctek/client/{CB}/sma/meterinfo`** and **`ctek/nga/{NGA}/meterinfo`** — retained
```json
{"meterId": "", "meterType": "P1", "vendor": "KAM"}
```

**`ctek/client/{CB}/sma/interval`** and **`ctek/nga/{NGA}/interval`** — on announce
```
10
```
Bare integer. The meter-data cadence in seconds, and it matches the observed
10 s `meterdata` period. Published only as part of the announcement sequence.

**`ctek/client/{CB}/sma/meterdata`** and **`ctek/nga/{NGA}/meterdata`** — every **10 s**
```json
{"activePowerIn": 12.918, "activePowerOut": 0.0, "current": [18.6,19.8,19.5], "voltage": [230.9,232.5,231.8]}
```
Whole-house totals at the grid connection point, **including** the car.
Power in **kW** — note the charger's own `info.power` is in **W**.
`activePowerOut` is export (solar); it stayed `0.0` throughout.

**`ctek/ng-v2/controller/{CB}/1/current`** — every **12–15 s** ← **the control channel**
```
16
```
A bare integer, **not JSON**: the load-balancing setpoint in amps. This single
topic is the entire control surface. It is a **heartbeat** — republished on
cadence whether or not the value changed (65 messages in 15 min).

## Observed adapter startup sequence

At t≈438 s in the long capture the Nanogrid Air restarted, giving us its
cold-start behaviour for free:

```
t=438.1  nga/{CB}/adapterinfo        ─┐
t=438.2  nga/{NGA}/meterinfo          │ re-announce
t=438.5  client/{CB}/sma/meterinfo    │
t=439.9  client/{CB}/sma/adapterinfo ─┘
t=446.0  controller/{CB}/1/current  6   ← commands the SAFE MINIMUM first
t=446.4  client/{CB}/sma/meterdata      ← first meter reading
t=448.2  client/{CB}/sma/interval  10
t=456.6  client/{CB}/sma/meterdata      ← second reading
t=465.9  client/{CB}/sma/meterdata      ← third reading
t=471.7  controller/{CB}/1/current  16  ← jumps straight to full, ~26 s later
```

Two behaviours worth copying:

1. **Start at `MinAllowedCurrent`, not at the computed maximum.** The adapter
   commands 6 A before it has any meter data, and only then raises.
2. **Raise after roughly 25–30 s** (about three meter readings), and raise in
   **one step** to the computed value rather than ramping gradually.

Throughout this window the house baseline never exceeded 5.5 A, confirming the
6 A was a cold start, **not** an overload response.

## Load-balancing model

House baseline (everything except the car) is derived, not measured:

```
baseline[p] = meter.current[p] - charger.Current[p]
allowed[p]  = main_fuse - baseline[p] - safety_margin
setpoint    = clamp(min(allowed over phases the EV uses), 0, min(FuseRating, user_max))
```

From the capture: meter `[18.6, 19.8, 19.5]` minus car `[15.8, 15.7, 16.1]`
gives a house baseline of roughly `[2.8, 4.1, 3.4]` A.

`setpoint` must then snap to the legal set: **0, or 6–16 A**. There is no valid
value between 1 and 5 — below `MinAllowedCurrent` the only option is to pause.

### Phase rotation

`StationPhaseRotation: "RST"` is straight-through (charger L1→meter L1, etc.).
Other values would mean the charger's phases are cross-wired relative to the
meter, and comparing `meter.current[p]` to `charger.Current[p]` directly would
then throttle against the wrong phase. `PrimaryPhase: 1` and
`EvUsesPhase: [1,1,1]` identify which phases the EV actually loads.

## Resolved

- **Auth** — not required for read or write.
- **Cold start** — command 6 A, gather ~3 meter readings, then jump to target.
- **Control cadence** — heartbeat every 12–15 s, not only on change.
- **`debug` publisher** — the charger.

## Still open

1. **Failsafe on controller silence.** If `controller/.../current` stops, does
   the charger hold the last setpoint, decay to `MinAllowedCurrent`, or stop?
   Not yet observed — the adapter never went silent for long enough. Until this
   is known, treat our add-on crashing as *unsafe* and keep the watchdog.
2. **Is `0` accepted** to pause charging, and are non-integers accepted?
   Only `6` and `16` have been observed.
3. **`State` enum.** Only `2` (charging) seen. Idle/connected/finished/fault
   values unknown.
4. **Does the charger require `adapterinfo`** before honouring a setpoint, or is
   the control topic sufficient alone?
5. **No overload event captured.** The house baseline never exceeded 5.5 A, so
   real throttling behaviour against a main fuse is still untested.
