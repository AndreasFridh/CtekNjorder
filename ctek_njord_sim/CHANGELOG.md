# Changelog

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
