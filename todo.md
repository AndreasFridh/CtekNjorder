# Todo

Planned work, none of it built yet. Notes under each item are the decisions
that will need making, recorded now so they are not rediscovered later.

---

## 1. Charge-enable input from Home Assistant

An entity Home Assistant sets to `1` / `true` to permit charging, so charging
can be automated against the electricity price.

**If the option is left blank, every charger charges as normal.** Not
configured must mean "no gate", never "gate closed" — an empty setting must not
silently stop the cars.

Open questions:

- **What happens to a car mid-charge when it flips off?** Commanding 0 A pauses
  it, which is the obvious reading. Worth confirming the car resumes cleanly
  when it flips back on rather than needing a replug.
- **Global or per charger?** Global reads as intended ("enable charging in our
  app"). Per-charger entities would allow one car on a cheap-rate schedule and
  another not — probably a later addition rather than part of this.
- **Interaction with the safety path.** The gate may only ever *withhold*
  current. It must never raise an allocation, and load balancing must keep
  applying underneath it: enabled does not mean unlimited.
- **Stale gate entity.** If the entity goes unavailable, treat it as its last
  known value or as enabled? Leaning enabled, since the failure mode of
  "unavailable sensor silently stops charging overnight" is worse than the
  alternative and this gate is about cost, not safety.

## 2. Electricity price input from Home Assistant

A float entity carrying the current price. Used to work out:

- cost of each charging cycle
- current cost per hour, per charger
- the same totalled across all chargers, on the dashboard

Open questions:

- **Units and currency.** Nordic price sensors publish variously as SEK/kWh and
  öre/kWh, so the unit needs reading from the entity rather than assumed, and a
  currency label needs to come from somewhere for display.
- **Where energy comes from.** The charger already reports `info.energy` in Wh
  as a lifetime, monotonic counter — differencing it gives energy per cycle
  without any new plumbing. Cost per hour is simply current power × price.
- **What defines a "cycle".** Probably from a car starting to draw to it
  stopping. `State` is the natural signal but only the charging value has ever
  been confirmed on real hardware, so this likely has to key off draw the same
  way demand detection already does.
- **Where cycle history lives.** `History` already persists to `/data`; cycle
  records are a natural fit, and are small enough not to change its footprint.
- **Price changes mid-cycle** are the normal case on an hourly tariff, so cost
  has to accumulate incrementally rather than be computed once at the end.

## 3. Network monitoring for each charger

Track and plot reachability and latency per charger, so a flaky WiFi link shows
up as a graph rather than as unexplained charging behaviour.

Open questions:

- **ICMP ping needs `NET_RAW`**, which the add-on does not have and should not
  ask for. A TCP connect to the charger's MQTT port measures the same thing for
  our purposes — it is the path that actually matters — and needs no extra
  privileges. Worth doing that unless there is a reason not to.
- Reuse the existing two-tier `History` (fine-grained recent, minute buckets
  persisted) rather than inventing a second storage scheme.
- Also worth recording: disconnects, reconnect attempts, and how long each
  charger has been unreachable. The reconnect path already logs these.

## 4. Nicer per-charger cards

The current cards are functional but plain. Make each charger a proper card
with its own stats.

Candidates, once items 1–3 land:

- session so far: energy, cost, elapsed, average current
- allocated vs actually drawn, and *why* — capped by its own limit, waiting for
  capacity, or unrestricted
- link quality from item 3
- lifetime energy, and cost per hour right now
- a clearer state line than the current text

Worth keeping in mind: the dashboard's job is to answer "what is it doing and
why", so any new number should earn its place against that.
