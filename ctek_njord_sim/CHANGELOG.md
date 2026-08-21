# Changelog

## 0.5.0

- **Safety fix.** When the charger's telemetry went stale the balancer assumed
  the car was still drawing its last setpoint and subtracted that from the
  meter reading. If the car was actually idle, the whole reading was house
  load and the subtraction invented headroom that did not exist: a 20 A house
  could be granted a further 16 A on a 25 A fuse. It now attributes none of
  the meter reading to the car once the charger goes quiet, which can only
  under-estimate spare capacity.

## 0.4.0

- Split the single chart in two. **Meter current** plots L1/L2/L3 separately
  against the fuse line, so you can read the actual Home Assistant sensor
  history rather than a collapsed peak. **Charging** plots allowed against
  actually drawn, so it is obvious whether the car is following its
  allowance or has tapered off on its own.
- Range selector: 5 / 15 / 30 minutes.
- Live values in the chart legends.
- Main fuse, max charge current and **safety margin** are now editable on the
  dashboard itself, with a line spelling out the arithmetic they feed:
  `25 A fuse - 1 A margin - 14.0 A busiest phase = 10.0 A available`.
  They apply to the running balancer as soon as they are changed.

## 0.3.0

- Web UI on Home Assistant Ingress, with a "Show in sidebar" toggle.
- Dashboard: allowed current and the reason for it, a per-phase bar showing
  house load plus car draw against the main fuse, a 30-minute history chart,
  and charger state.
- **Every option is editable in the UI.** Limits and behaviour apply to the
  running balancer immediately; connection and entity settings are flagged as
  needing a restart, with a restart button.
- Entity pickers list your Home Assistant sensors, filtered by unit, so the
  meter entities no longer have to be typed from memory.
- A configuration error no longer stops the add-on. It is reported in the UI
  and the balancer holds the safe fallback current, because exiting would
  leave the charger with no controller at all.

## 0.2.0

- Installable as a Home Assistant add-on repository: added `repository.yaml`,
  add-on documentation and this changelog.
- Pinned `WORKDIR /` in the Dockerfile so `python3 -m app.main` cannot break if
  the base image changes its default working directory.
- Pointed the add-on `url` at the real repository.

## 0.1.0

First release.

- Impersonates the CTEK Nanogrid Air on the charger's own MQTT broker.
- Load-balances against per-phase current from Home Assistant.
- Auto-discovers the charger serial from the broker's retained topics.
- Ships with `dry_run` enabled: decisions are logged, nothing is sent to the
  charger until you turn it off.
