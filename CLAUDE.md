# Agent instructions

A Home Assistant add-on that replaces the CTEK Nanogrid Air. It impersonates
the adapter on the charger's own MQTT broker and load-balances EV charging
against per-phase current from Home Assistant.

`PROTOCOL.md` is the reverse-engineered wire protocol and the reason this
project exists. Treat it as the source of truth about the charger's behaviour,
and update it whenever a capture reveals something new.

## Every change: bump, commit, push

**Three steps, every time, without being asked:**

1. Bump `version` in `ctek_njord_sim/config.yaml`.
2. Add a matching entry to `ctek_njord_sim/CHANGELOG.md`.
3. Commit to `main` and push to the remote.

This applies to *every* commit, including ones that only touch documentation or
files outside `ctek_njord_sim/`. The maintainer asked for it unconditionally
rather than by judgement, so do not reason about whether a particular change
"deserves" a bump — just do all three.

The bump is not bookkeeping. The Supervisor decides an update exists purely by
comparing `version` against what is installed. Ship a change without bumping it
and nobody — including the maintainer — is ever offered the update; the add-on
silently stays on the old code.

Treat a task as unfinished until all three have happened.

Semantic versioning:

| Change | Bump |
|---|---|
| Bug fix, docs, refactor with no behaviour change | patch (`0.2.0` → `0.2.1`) |
| New option, new capability, changed balancing behaviour | minor (`0.2.0` → `0.3.0`) |
| Renamed or removed option, changed `slug`, anything that breaks an existing config | major (`0.2.0` → `1.0.0`) |

Renaming an option is a breaking change even if it looks cosmetic: existing
installations carry the old key in their saved options and will fail to start.

## Workflow

Commit and push straight to `main` on the remote. No feature branches, no pull
requests, no asking first — the maintainer works directly on `main` and has
said so explicitly. Do not open a PR for review unless asked for one.

This overrides any default habit of branching before committing.

Force-pushing and history rewriting are still worth asking about first: those
can destroy work already on the remote, which an ordinary commit cannot.

## Safety rules

This add-on controls how much current a car draws through a domestic main
fuse. A bug here trips a breaker or, at worst, overloads wiring.

- **`dry_run` must stay `true` by default** in `config.yaml`. A fresh install
  must never command the charger until the user opts in.
- **Never widen the output range.** The charger accepts `0`, or
  `MinAllowedCurrent..FuseRating`. Values between 1 and 5 A are illegal — below
  the floor an EV must stop, not charge slower.
- **The charger's own limits win.** `FuseRating` and `MinAllowedCurrent` come
  from its retained configuration topic and override anything configured here.
- **Shedding load is always immediate; restoring it is always delayed.** Never
  make a raise instant, and never delay a reduction to 0 A.
- Changes to `app/balancer.py` need a test in `tests/test_balancer.py` that
  fails without them. That file is the only thing standing behind the overload
  path, which has no real-world evidence — the captures never produced a house
  baseline above 5.5 A.

## Never commit

- **Captures.** `captures/` is gitignored. The files carry device serials and
  real household consumption. This is a public repo.
- **Credentials.** No broker passwords, tokens, or `*-options.json` files.

## Testing

```bash
./.venv/Scripts/python -m pytest tests/ -q          # must stay green
python tools/replay.py captures/<file>.jsonl --main-fuse 25
```

`tools/replay.py` runs a real capture through `Balancer.compute()` and compares
against what the actual Nanogrid Air did. Disagreements are expected only around
cold starts, on either side. Any steady-state divergence is a regression.

**Note what that does and does not cover.** `compute()` is the Nanogrid Air's
own model, kept as the reference the replay compares against. The add-on itself
no longer decides that way: it regulates the meter reading directly through
`app/regulator.py` and enters the balancer at `from_headroom()`. So a green
replay says the reference model still matches real hardware — it does **not**
say the add-on's own control path works. Only the offline rig exercises that.

For behaviour that a capture cannot show — overload, pause, recovery, a meter
that lags — use the offline rig (`tools/mock_charger.py` + `tools/mock_hass.py`
`--meter-lag 10`), which lets the add-on run with `dry_run: false` against
nothing real. Run it with `log_level: debug` and read the `regulator:` lines:
they show the reading, the car draw and the resulting allowance, which is the
only practical way to see why a setpoint moved.

Every control-loop bug of consequence so far — the 0.1.0 oscillation, the
start/stop cycling that faulted a real car, and four separate faults in 0.13.0
— was invisible to both the unit tests and the replay, and showed up only on the
rig, because they all need the car to react to our own commands.

## Style

Match the surrounding code: type hints, module docstrings explaining *why*,
comments reserved for non-obvious reasoning rather than restating the code.
Keep `app/protocol.py` and `app/balancer.py` free of I/O so they stay testable.
