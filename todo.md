# Todo

## Done

- **Charge-enable input** — an entity Home Assistant sets on or off to permit
  charging, for price-based automation. Unset, unavailable or unrecognised all
  mean *charge*; only an explicit off stops it. The gate can only withhold
  current, never raise an allocation, so load balancing still applies
  underneath it.
- **Electricity price input** — per-session energy and cost, cost per hour per
  charger, and the totals on the dashboard. Cost accumulates as it goes, so a
  price change mid-session is priced correctly. The unit is read from the
  entity, so öre and cents are handled as well as whole units.
- **Link monitoring** — round-trip time and loss to each charger, with a
  sparkline on its card. A TCP connect to the MQTT port rather than ICMP: no
  extra privileges, and it tests the path charging actually uses.
- **Charger cards** — one card each, with session energy and cost, cost per
  hour, power, lifetime energy, whether the car is limiting itself, and link
  quality.

## Open

### Characterise the failsafe

The one genuine unknown, and it needs hardware. If the controller stops
publishing, does the charger hold its last setpoint, decay to
`MinAllowedCurrent`, or stop? Until this is known, a crash of this add-on has
to be treated as unsafe rather than assumed to fail safe.

To find out: with a car charging, take the controller off the network and watch
what the charger does over a few minutes.

### Fill in the `State` enum

Only `2` (charging) has ever been observed. Idle, connected, finished and fault
are unknown, which is why demand is inferred from current draw instead. Values
would come from watching a full plug-in to unplug cycle.

Knowing them would simplify the demand detection and let the cards say
something more useful than "no car".

### Confirm `0` pauses rather than faults

The balancer commands 0 A to pause a car, and the mock accepts it, but no real
charger has been sent a 0 while a car was plugged in. Worth confirming the car
resumes cleanly rather than needing a replug.

### Smaller things

- Per-charger schedules, so one car can charge on a cheap-rate window and
  another not. The gate is currently global.
- Cost history beyond the current session — daily and monthly totals would need
  persisting alongside the chart history.
- Solar: `activePowerOut` is already read and has stayed at zero here. Charging
  from surplus only would be a natural extension of the price gate.
