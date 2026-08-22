# CtekNjorder

Replaces the CTEK Nanogrid Air. The add-on connects to the charger's own MQTT
broker, impersonates the adapter, and commands a charging current based on
real-time per-phase readings from Home Assistant.

## Before you start

**Only one controller may be running at a time.** The charger accepts a
setpoint from whoever publishes one, so if the original adapter is still on the
network the two will disagree and the charging current will flap between them.

Unplug it, or block it at your router — most routers can deny a device network
access in one click, and it is reversible. Either way the adapter needs no
reset and recovers on its own if you put it back.

You do **not** need to unpair, factory-reset, or reconfigure the charger
itself.

## The web UI

Open it with **Open Web UI** on the add-on page. To keep it one click away,
turn on **Show in sidebar** there too — it then appears as *Njord*.

Everything is configurable from the UI; you never need the Configuration tab.

- **Dashboard** — the allowed current and why; the main fuse, max charge
  current and safety margin as editable boxes that apply immediately; a
  per-phase bar showing house load and car draw against your fuse; a chart of
  the meter's three phases; a chart of allowed versus actually drawn current;
  and charger state.

  Charts cover 5 minutes to 7 days and are kept across restarts in `/data`
  (about 500 KB for the full week). The last 30 minutes are held at one sample
  per second; anything longer is shown as per-minute peaks, so a brief spike
  still appears rather than being averaged out.
- **Settings** — every option, grouped, with an entity picker that lists your
  own sensors. Limits and behaviour take effect immediately, so you can tune
  the fuse or the margin while a car is charging. Connection and entity changes
  are flagged as needing a restart, and there is a button for that.

## Several chargers

Add up to six, each with its own address - each charger hosts its own MQTT
broker, so there is nothing shared to point at. Leave a row's address blank to
ignore it; blank rows are not chargers and their toggle stays greyed out.

Only chargers that actually answer take part. One that is switched off,
unplugged, or that you simply do not own is left out of the split entirely, and
its share goes to the cars that are there. Anything not answering is retried
every 15 seconds, so a charger that comes back - or is switched on for the
first time - joins on its own without restarting the add-on.

A charger that has never answered is reported separately from one that was
working and dropped off, because the first usually just means an address for a
charger that does not exist.

The meter sees every car at once, so the house baseline is the reading minus
all of them, and whatever headroom remains is divided.

**Sharing** decides how:

- **optimal** (default) notices when a car is not taking everything it was
  offered - because of its own onboard limit, or because it is tapering near
  full - and hands the surplus to a car that can use it.
- **even** always splits equally. More predictable, but it leaves current idle
  whenever one car cannot use its share.

A charger with no car plugged in is given nothing rather than a share, so it
never strands current a waiting car could use. That is worked out from
behaviour - a charger that is offered current and does not take it - because
only the charging value of the charger's `State` field has ever been confirmed
against real hardware.

When there is not enough for everyone, fewer cars charge properly rather than
all of them being pushed below the 6 A floor where a car must stop anyway. Cars
already charging keep priority, so the choice does not flip back and forth.

## Setup

### 1. Find your chargers

The charger hosts the MQTT broker itself. Its address is shown in the CTEK app
under the charger's network details, as `mqtt://<ip>` with a port (normally
1883). Put that IP in the **Chargers** table on the Settings tab, one row per
charger.

No username or password is needed — the broker accepts anonymous connections.
The credential fields exist only in case a future firmware locks that down.

Leave `charger_serial` empty and the add-on will discover it from the broker's
retained topics on connect. Check the log for `Bound to charger ...` to confirm.

### 2. Set your main fuse

**Main fuse** is the rating of your property's **main** breaker, per phase — not
the charger's fuse, and not the breaker on the charging circuit. Get this
right; everything else depends on it.

If you are unsure, choose the smaller value. Understating the fuse makes the
car charge more slowly than it could, which is harmless. Overstating it can
trip your main breaker.

### 3. Point it at your meter

**Current sensors** takes exactly three entities, one per phase, reporting
**amps** at your grid connection point. A P1/HAN meter reader is ideal, since
that is the same source the Nanogrid Air uses.

The picker lists your own sensors filtered to amps, so you can choose rather
than type. They will look something like:

```
sensor.p1_meter_current_phase_1
sensor.p1_meter_current_phase_2
sensor.p1_meter_current_phase_3
```

These must measure the **whole property**, including the charger. The add-on
subtracts the car's own draw itself; if you give it a circuit that excludes the
car, it will over-estimate your spare capacity.

`voltage_entities`, `power_in_entity` and `power_out_entity` are optional and
only affect the meter data mirrored back to the charger, not the balancing.

### 4. Watch it in dry run

`dry_run` starts **on**. The add-on connects, reads everything, and shows the
setpoint it *would* send without sending it. A banner on the dashboard says so.

Let it run through a charging session and watch the dashboard: `house` should
track your meter, `car` should track the charger, and the per-phase bars should
sit sensibly below your fuse line.

When you are satisfied, turn **Dry run** off on the Settings tab. It takes
effect immediately, with no restart — the log will say
`Dry run DISABLED - now controlling the charger`.

## Charging only when you want it

**Charge enable** takes an entity Home Assistant switches on and off. While it
is off every charger is held at 0 A; when it comes back on, load balancing
resumes. Point it at an `input_boolean` driven by whatever price automation you
already run.

Leave it blank and charging is always permitted.

If the entity is set but reports `unavailable`, `unknown`, or anything the
add-on does not recognise, **charging is permitted**. This gate exists to save
money, not to keep anyone safe, and a sensor dropping out overnight must not
quietly leave a car uncharged. Only an explicit off stops charging.

The gate can only ever withhold current. It never raises an allowance, and load
balancing still applies underneath it, so enabled does not mean unlimited.

## What charging costs

**Electricity price** takes an entity carrying the current price per kWh. With
it set, each charger's card shows the energy and cost of the session in
progress and what it is costing per hour, and the dashboard totals both.

The unit is read from the entity, so a sensor publishing öre or cents is
handled as well as one publishing whole currency units. **Currency** is the
label shown beside the figures and is cosmetic.

Cost accumulates as energy is used, at the price in force at the time. On an
hourly tariff the price changes during a session, and costing the total at the
final price would be wrong for every session that spans a change.

Energy comes from the charger's own lifetime meter, so it is the charger's
measurement rather than an estimate.

## Charging sessions

The **Sessions** tab lists every completed charging session and keeps them
across restarts: which charger, the day, start and end times, how long it ran,
how long it actually charged, energy, cost, the average price paid, and the
peak current drawn.

Elapsed time and charging time are listed separately on purpose. Load balancing
pauses a car through a household peak, so a session can run for hours while
charging for far less, and a single "duration" would hide that.

Totals are shown for the last 7 days, the last 30, and all time. The average
price is cost divided by energy rather than a mean of prices, which is what
tells you whether charging at cheap hours is actually working.

Costs only appear if an electricity price entity is configured; energy and
timings are recorded either way.

## Link quality

Each charger's card shows the round-trip time to it and how many checks got no
answer, with a small graph of recent measurements. A charger on marginal Wi-Fi
turns into something visible rather than charging behaviour that seems
inexplicable.

This is a TCP connect to the charger's MQTT port, not an ICMP ping. ICMP would
need a privilege the add-on does not have, and the TCP handshake tests the path
charging actually depends on — a charger can answer pings while its broker is
unreachable.

## How it decides

```
baseline = house_current - car_current      (per phase)
headroom = main_fuse - safety_margin - baseline
setpoint = the lowest headroom across the phases the car uses
```

The result is snapped to what the charger will accept: **0 A, or 6–16 A**.
There is no value between 1 and 5 A — below the 6 A floor an EV must stop
rather than charge slower, so the add-on commands 0 and pauses.

### Safety behaviour

- **Cold start.** Opens at 6 A and only raises once it has valid readings,
  matching what the real adapter does.
- **Sheds fast, restores slowly.** Overload throttles within a second. Raising
  waits `raise_delay` so the setpoint cannot oscillate.
- **Ignores its own wake.** For a few seconds after each change the car is
  still ramping and the meter still reports its previous draw, which briefly
  inflates the computed baseline. The add-on holds instead of throttling
  against that transient. Pausing is exempt, so a real overload is never
  delayed.
- **Falls back when blind.** If the current entities go stale or unavailable
  for longer than `stale_timeout`, it drops to `fallback_current` rather than
  guessing.
- **Never counts the car against itself, and never over-counts it either.**
  The car's own draw is subtracted from the meter reading so it does not
  consume its own allowance. But if the charger stops reporting, none of the
  reading is attributed to the car: over-subtracting would understate the
  house load and hand out current that is not there.

## Options

| Option | Default | What it does |
|---|---|---|
| `charger_host` | — | The charger's IP. It *is* the MQTT broker. |
| `charger_port` | `1883` | Plain MQTT, no TLS. |
| `charger_username` / `charger_password` | empty | Not needed; the broker is anonymous. |
| `charger_serial` | empty | Auto-discovered when blank. |
| `adapter_serial` | empty | The serial we announce as. Any stable string works. |
| `main_fuse` | `25` | **Your main breaker, per phase.** The hard ceiling. |
| `max_charge_current` | `16` | Your own cap. The charger's own 16 A rating also applies. |
| `safety_margin` | `1.0` | Amps held back from the fuse. |
| `phase_rotation` | `RST` | Overridden by the charger's own setting. |
| `current_entities` | — | **Required.** Three entity IDs, amps, one per phase. |
| `voltage_entities` | — | Optional. Defaults to 230 V per phase. |
| `power_in_entity` / `power_out_entity` | — | Optional. Derived if unset. |
| `stale_timeout` | `30` | Seconds before readings count as stale. |
| `fallback_current` | `6` | Commanded when flying blind. |
| `raise_delay` | `30` | Seconds of sustained headroom before raising. |
| `settle_window` | `10` | Seconds after a change when the baseline is untrusted. |
| `settle_tolerance` | `1.5` | Amps of command-vs-actual mismatch counted as "still ramping". |
| `control_interval` | `15` | Setpoint heartbeat, seconds. |
| `meter_interval` | `10` | Meter data cadence, seconds. |
| `charge_enable_entity` | — | Optional. Charging is permitted while this is on. Blank, unavailable or unrecognised all mean permitted. |
| `price_entity` | — | Optional. Price per kWh; unit read from the entity. |
| `currency` | `SEK` | Label shown beside costs. Cosmetic. |
| `ping_interval` | `30` | Seconds between link checks. |
| `dry_run` | `true` | Log decisions without sending them. |
| `log_level` | `info` | Use `debug` for per-tick detail. |

## Troubleshooting

**`Configuration error: current_entities must list exactly 3 entity_ids`**
You have fewer or more than three. Blank entries are stripped, so remove any
empty rows.

**`These entities are not present in Home Assistant: ...`**
The entity IDs are wrong. Check them in Developer Tools → States.

**Log shows `FALLBACK: house data NNs old` while the meter looks fine**
Fixed in 0.10.1. Home Assistant sends no event when a sensor re-reports an
unchanged value, and freshness used to be judged by the last event - so a
steady house looked like a dead meter. Update the add-on.

**Log shows `FALLBACK: no house data`**
The add-on connected to the charger but has no meter readings. Either the
entity IDs are wrong, or they report non-numeric states such as `unavailable`.

**No `Bound to charger ...` line**
It reached the broker but saw no retained configuration topic. Confirm the IP,
and that the charger is powered and on the same network.

**Charging keeps starting and stopping**
Raise **Meter lag** to match how often your meter really reports - ten seconds
or more for a P1. The house baseline is worked out by subtracting the car from
the meter reading, and if the meter has not caught up with the last change that
subtraction credits the car's own current to the house. **Restart hold** also
sets a floor on how often a car can be started again after a pause; cars fault
after several quick cycles.

**The setpoint flaps between values**
Increase `settle_window` if your car ramps slowly, or `raise_delay` if the
household load itself is swinging.

## Limitations

The failsafe behaviour when the controller goes silent is **not yet
characterised** — it is not known whether the charger holds the last setpoint,
decays to 6 A, or stops. Until that is established, treat a crash of this
add-on as unsafe rather than assuming the charger fails safe.

Only `State: 2` (charging) has been observed, so other states are unmapped.
