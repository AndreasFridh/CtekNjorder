# Changelog

## 0.11.1

- No functional change. Cut the README from 239 lines to 90: setup, options and
  troubleshooting live in DOCS.md, which is what Home Assistant shows on the
  add-on page, and duplicating them was how they drifted apart.

## 0.11.0

- **Charging session log.** Every completed session is recorded and kept across
  restarts: charger, day, start and end time, how long it ran, how long it
  actually charged, energy, cost, average price paid, and peak current.
- **A Sessions tab** to browse them, with totals for 7 days, 30 days and all
  time, and a filter per charger.
- Elapsed time and charging time are shown separately, because load balancing
  pauses a car through a household peak - a session that ran ten minutes and
  charged for eight should say so rather than claim ten.
- Average price is cost divided by energy, not a mean of prices: a short
  expensive session and a long cheap one do not average to the middle.
- **Renamed to CtekNjorder** throughout the add-on and its documentation. The
  charger keeps its own name - it is still a CTEK Njord GO.

## 0.10.2

- **Fixed "waiting for capacity" with capacity plainly available.** A charger
  with no car takes none of the current it is offered, and that was recorded as
  a car limited to about 1 A. One amp is below the 6 A minimum a car can
  charge at, so it could never be served - and a paused charger cannot
  demonstrate demand, so nothing ever revisited it. Drawing nothing is now read
  as absence rather than as a limit, and no cap can fall below the minimum that
  can actually be commanded.
- **Draw now outranks `State`.** Current offered and not taken is measured;
  `State` has only ever been confirmed as "2 happens while charging". A charger
  reporting 2 with nothing plugged in used to hold an allocation indefinitely
  on the strength of a flag we cannot read.
- An empty charger is re-offered current every few minutes, and immediately
  whenever its `State` changes, so a car plugged in while it was paused is
  still noticed.
- **Clearer status.** A charger with no car now reads
  `ready - Not connected - Available capacity 16 A` instead of implying
  something is wrong.

## 0.10.1

Two fixes, both reported as "house data NNs old" while the meter was fine.

- **A steady house was mistaken for a dead meter.** Home Assistant does not
  send an event when a sensor re-reports the value it already had, so a house
  holding a constant load produces no events at all - and freshness was being
  judged by the last event. The best-behaved possible meter was the one most
  likely to be declared stale. Freshness now follows the connection and whether
  each entity holds a usable value, which is what actually determines whether
  our picture is current.
- **The fallback current never reached a charger.** Falling back reported a
  setpoint but no headroom, and the allocator divides headroom - so every
  charger got 0 A and `fallback_current` looked set while doing nothing.
- The dashboard now says "meter last changed" rather than "meter age", since a
  reading that has not changed in an hour is not necessarily an old one.

## 0.10.0

Everything that was on the todo list, plus a scrub of the repository.

- **Charge enable.** Point it at an entity your price automation switches and
  charging is held at 0 A while it is off. Unset, unavailable, or a state we do
  not recognise all mean permitted - this gate saves money, it does not keep
  anyone safe, and a dropped sensor must not silently leave a car uncharged.
  It can only withhold current, never raise an allowance.
- **Electricity price.** Each session is costed as it goes, at the price in
  force at the time, so an hourly tariff changing mid-session is priced
  correctly. Cards show session energy and cost, cost per hour, and the
  dashboard totals both. The unit is read from the entity, so ore and cents
  work as well as whole units. Energy comes from the charger's own meter.
- **Link monitoring.** Round-trip time and loss to each charger, with a
  sparkline on its card. A TCP connect to the MQTT port rather than ICMP: no
  extra privilege needed, and it tests the path charging actually uses.
- **Reworked charger cards.** One card each, with session, cost, power,
  lifetime energy, whether the car is limiting itself, and link quality.
- **No installation-specific details left in the repository.** Addresses and
  serial numbers are examples, no address ships as a default, and the setup
  steps no longer assume any particular router.

## 0.9.4

- No functional change. Records planned work in `todo.md`: a charge-enable
  input for price automation, an electricity price input for cost tracking,
  per-charger network monitoring, and a nicer set of charger cards.

## 0.9.3

- Added the add-on icon and logo, so it no longer shows a generic placeholder
  in the add-on store.

## 0.9.2

- **Fixed: a charger never recovered from losing its connection.** We connect
  with a clean session, so the broker forgets our subscriptions when the link
  drops - and the reconnect path skipped re-subscribing. The charger stayed
  silent afterwards while still counting as connected and still holding an
  allocation.
- **Only chargers that answer take part.** One that is switched off, unplugged,
  or simply does not exist is left out of the split entirely, and its current
  goes to the cars that are actually there.
- Anything not answering is retried every 15 seconds, so a charger that comes
  back - or is switched on for the first time - joins on its own.
- An unreachable charger is no longer reported as a failure. "Never reached"
  and "went offline" are shown differently, because the first usually means an
  address for a charger you do not have.
- Empty rows in the charger list no longer appear ticked. Showing them enabled
  made it look like six chargers were configured, and unticking one appeared to
  do nothing because a row with no address is ignored anyway.
- An offline charger shows "draw unknown" rather than its last reading.

## 0.9.1

- No functional change. Records the project's release habit: every change gets
  a version bump, a changelog entry, and a push to `main` — including
  documentation-only changes, so the add-on's version always moves when
  anything in the repository does.

## 0.9.0

**Up to six chargers.** The Nanogrid Air controls exactly one; this is the
reason the project exists.

- Each charger is configured with its own address, because each one hosts its
  own MQTT broker. Existing single-charger installs are migrated automatically
  and need no changes.
- The meter sees every car at once, so the house baseline is now the reading
  minus *all* of them, and the remaining headroom is shared out.
- **Sharing** setting: `optimal` notices a car that is not taking everything it
  was offered - an onboard limit, or tapering near full - and gives the surplus
  to a car that can use it. `even` always splits equally.
- A charger with no car is given nothing rather than a share, so it cannot
  strand current a waiting car could use. Demand is inferred from behaviour,
  because only the charging value of `State` has ever been confirmed on real
  hardware.
- When there is not enough for everyone, fewer cars charge properly rather than
  all of them charging illegally below the 6 A floor. Cars already charging
  keep priority, so the set does not churn.
- Per-charger cards on the dashboard: allocation, actual draw, and whether a
  car is capped, waiting, or absent.
- The UI is no longer cached, so it cannot be left stale against a newer API
  after an update.

## 0.8.0

- **Charts now survive a restart.** History is kept in two tiers: one sample a
  second for the last 30 minutes in memory, and one bucket a minute for the
  last 7 days written to `/data`. Each bucket keeps the worst value it saw, so
  a one-second spike is still visible a week later instead of being averaged
  away.
- Ranges extended to 5m / 30m / 6h / 24h / 7d.
- Straight after a restart the view falls back to the persisted buckets rather
  than showing an empty chart until the live tier refills.
- The charger serial gets its own full-width row instead of being squeezed
  into a narrow grid column.

About 500 KB on disk for a full week. Written append-only, roughly 60 bytes
once a minute, so it is not meaningful wear on an SD card.

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
