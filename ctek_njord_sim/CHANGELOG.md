# Changelog

## 0.7.1

- Licensed under AGPL-3.0-or-later. The web UI links to its own source, which
  is what the license requires of software people interact with over a network.

## 0.7.0

Closes the remaining items from the security and UX review.

- **The API now only answers the Ingress proxy.** Previously anything on the
  Supervisor's Docker network could change the charging limits or restart the
  add-on without authenticating. Mutating calls additionally require a header
  that a cross-origin form cannot set, so a malicious page cannot ride an
  Ingress session. `restrict_api` turns this off from the add-on's
  Configuration tab, which keeps working even if the check itself is what is
  stopping the UI loading.
- Settings warn before you switch tabs with unsaved changes.
- The allowed current is shown as the integer it is, not `6.0 A`.
- Banners announce themselves to screen readers.
- Added a favicon.

## 0.6.0

Security and UX review.

- **Fixed a stored XSS.** The charger serial is parsed out of an MQTT topic
  name, and the charger's broker accepts anonymous publishes from anywhere on
  the LAN — so the serial was attacker-controlled, and it was rendered into
  the dashboard unescaped. Serials are now validated before being adopted, and
  every value the page interpolates is escaped at the point of use.
- **Stopped serving the MQTT password back out.** `GET /api/settings` returned
  `charger_password` in cleartext. It now returns a placeholder, and posting
  the placeholder back leaves the stored secret alone.
- **The dashboard now says when it has lost contact with the add-on.** It used
  to freeze on the last reading, which looks identical to a healthy system —
  the worst failure mode for a page whose job is showing live current.
- **Stopped inventing charger state names.** Only `State: 2` has ever been
  observed, so the other labels were guesses shown as fact. Unknown states now
  display their number.
- Pinned aiohttp forward to 3.10.11.
- Added `SECURITY.md` and `tests/test_security.py`.

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
