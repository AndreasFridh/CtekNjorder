# CTEK Njord GO ↔ Nanogrid Air — MQTT protocol

Reverse-engineered from live captures on 2026-08-21 (a 60 s and a 15 min run).
Not documented by CTEK; derived entirely from observed traffic.

## Participants

| Role | Serial | Firmware | Notes |
|---|---|---|---|
| Charger (Njord GO) | `40000A00X0000001` | `r3.2.2-0-g673feded_mmiR1` | **Runs the MQTT broker** on `192.168.1.50:1883` |
| Nanogrid Air | `40000B00Y0000002` | `ngair.1.3.2-0-g388a64c` | Meter gateway + load-balancing controller — **this is what we replace** |
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
{"State": 2, "EvUsesPhase": [1,1,1], "MaxAllowedCurrent": 16, "Current": [16.0,16.0,16.0]}
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
{"ids": "40000A00X0000001,", "status": [2,0,9,64]}
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
{"serialno": "40000B00Y0000002", "fwVersion": "ngair.1.3.2-0-g388a64c", "vendor": "CTEK"}
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
{"activePowerIn": 13.8, "activePowerOut": 0.0, "current": [20.0,20.0,20.0], "voltage": [230.0,230.0,230.0]}
```
Whole-house totals at the grid connection point, **including** the car.
Values in the examples throughout are round illustrative figures, not readings
lifted from a capture; the field names, units and types are what was observed.
Power in **kW** — note the charger's own `info.power` is in **W**.
`activePowerOut` is export (solar); it stayed `0.0` throughout.

> **The charger publishes here too (field log, 2026-09-24).** With no
> Nanogrid Air on the network, a setpoint the add-on did not send arrived on
> this topic **0.1–0.3 s after every meterdata message the add-on
> published** (every 10 s): `6` for the first minute, then `16`, the same
> cold-start shape attributed to the adapter below. The charger appears to
> run its own load balancing on the meter data it is fed, publish the result
> here, and obey **whichever setpoint arrived last**. MQTT cannot say who
> published a message, so in the original captures some of the "adapter"
> setpoints may have been the charger's. Consequences seen: a `0` pause
> overridden within seconds (the car restarted every cycle), and a steady `7`
> overridden to `16` for a few seconds every ten. Since 0.20.0 the add-on
> immediately re-sends its own setpoint whenever a higher foreign one arrives.
> **Confirmed in the field (2026-09-24):** with that in place the start-stop
> cycling stopped. A charger overridden for ~0.1-0.3 s does not act on it.

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

So a meter reading `[20.0, 20.0, 20.0]` with the car taking `[16.0, 16.0, 16.0]`
leaves a house baseline of `[4.0, 4.0, 4.0]` A.

`setpoint` must then snap to the legal set: **0, or 6–16 A**. There is no valid
value between 1 and 5 — below `MinAllowedCurrent` the only option is to pause.

**This describes the Nanogrid Air, not this add-on.** The subtraction above is
a feedback loop with a gain of one, and it oscillates on any meter slower than
the captures' — presumably why the adapter waits three readings before raising.
This add-on no longer copies it; see `app/regulator.py`. The snapping rule
above is the charger's, and still applies.

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
   Only `6` and `16` have been observed in a capture. Field report
   (2026-09, add-on 0.17.1): with `0` re-published every 15 s the car was
   heard starting and stopping repeatedly, drawing ~0.7 A in bursts. Suspected
   cause: each repeated `0` restarts the charger's pause.

   The 0.17.2 logs contradicted that. `MaxAllowedCurrent` read **16
   throughout**, while we commanded 0 and while we commanded 6, and each pause
   (`State` 4) held only ~2 s after our command before charging resumed. That
   fits a **second controller** - a Nanogrid Air still on the network -
   sending 16 on the same 12-15 s heartbeat, overriding each of our commands
   two seconds later. The add-on now subscribes to its own control topic and
   reports any setpoint it did not send. Whether a lone `0` holds is still
   unconfirmed.

   Unplugging the Nanogrid Air did **not** stop it, so that was not the
   cause. What the logs show is every `0` being followed by `State` 3 → 4 →
   (~2 s) → 2, i.e. **each `0` starts a fresh pause sequence**, and
   `MaxAllowedCurrent` never shows the `0` (nor a commanded `6`). Since 0.19.0
   a `0` is sent once and repeated only if the car draws ≥ 2.5 A through it.
   Still open: does a lone `0` hold, or does the charger resume charging
   after it?

   Related field observation (0.19.0, charging allowed): with a steady
   setpoint of 7 the car sat at ~7 A but spiked to 13-16 A for a few seconds
   roughly every 25-30 s, the meter rising with it. So non-zero setpoints do
   not hold steadily either. Needed to explain it: a `tools/sniff.py` capture
   showing our `controller/.../current` messages against the charger's 1 Hz
   `update` around a spike.

   Field log, 2026-09-24 (0.19.0, Nanogrid Air unplugged, charging blocked):
   a **`16` appears on `controller/{CB}/1/current` that the add-on did not
   send**, and every time the charger goes `State` 4 → 2 (resumes) 1-3 s
   later. Cycle ≈ 20 s: our `0` → `State` 3 for 6-12 s → foreign `16` →
   `State` 4 → `State` 2, car waking at ~0.5 A. The source is unknown:
   either the charger publishes its default to the control topic as it
   resets after a `0`, or another client on the broker does. 0.19.2 logs
   each one with its delay after our last command to tell them apart.

   Resolved by the 0.19.3 log: the foreign setpoints arrive every 10.0 s, in
   step with our meterdata, not with our commands. See the note under the
   control topic above.
3. **`State` enum.** Field report (2026-09, add-on 0.17.2, car plugged in)
   saw five values. Meanings are inferred, not confirmed:

   | `State` | Seen when |
   |---|---|
   | `1` | Mock charger only - not yet seen on hardware |
   | `2` | Charging; also ~0.5 A while a car wakes and starts |
   | `3` | Brief (~1-5 s) step between `2` and `4` as charging is cut |
   | `4` | Paused at 0 A with the car still plugged in |
   | `8` | On connect, car plugged in, nothing drawn - after a long pause |

   The sequence `2 → 3 → 4 → 2` repeated every ~15 s while two controllers
   were fighting (see below).
4. **Does the charger require `adapterinfo`** before honouring a setpoint, or is
   the control topic sufficient alone?
5. **No overload event captured.** The house baseline never exceeded 5.5 A, so
   real throttling behaviour against a main fuse is still untested.
