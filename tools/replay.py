#!/usr/bin/env python3
"""
Replay a capture through our Balancer and compare against the real adapter.

This is the honest test of the simulator: identical inputs, side-by-side
outputs. Any row marked DIFF is a place where we would have commanded something
the Nanogrid Air did not.

  python tools/replay.py captures/capture-long.jsonl --main-fuse 20
  python tools/replay.py captures/capture-long.jsonl --sweep
"""
from __future__ import annotations

import argparse
import bisect
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ctek_njord_sim"))

from app.balancer import Balancer, BalancerConfig  # noqa: E402


def load(path):
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    rows.sort(key=lambda r: r["t_rel"])
    meter = [r for r in rows if r["topic"].endswith("/sma/meterdata")]
    upd = [r for r in rows if r["topic"].endswith("/1/update")]
    ctrl = [r for r in rows if "controller" in r["topic"]]
    cfg = next((r for r in rows if r["topic"].endswith("/1/configuration")), None)
    return meter, upd, ctrl, cfg


def latest(seq, t):
    i = bisect.bisect_right([x["t_rel"] for x in seq], t) - 1
    return seq[i] if i >= 0 else None


def run(meter, upd, ctrl, cfg, main_fuse, margin, max_current, raise_delay, verbose):
    bcfg = BalancerConfig(
        main_fuse=main_fuse,
        max_charge_current=max_current,
        safety_margin=margin,
        raise_delay=raise_delay,
    )
    if cfg:
        p = cfg["payload"]
        bcfg.charger_fuse_rating = p.get("FuseRating", 16)
        bcfg.min_allowed_current = p.get("MinAllowedCurrent", 6)
    bal = Balancer(bcfg)

    rows = []
    for m in meter:
        t = m["t_rel"]
        house = m["payload"]["current"]
        u = latest(upd, t)
        car = u["payload"]["Current"] if u else [0.0, 0.0, 0.0]
        uses = u["payload"].get("EvUsesPhase") if u else [1, 1, 1]

        d = bal.compute(
            now=t, house_current=house, house_age=0.0,
            charger_current=car, ev_uses_phase=uses,
        )
        actual = latest(ctrl, t)
        actual_sp = actual["payload"] if actual else None
        rows.append((t, house, car, d, actual_sp))

    if verbose:
        print(f"{'t(s)':>8}  {'house A':<20} {'car A':<20} {'base A':<18} "
              f"{'ours':>5} {'NGA':>5}")
        print("-" * 88)
        for t, house, car, d, actual_sp in rows:
            base = [round(b, 1) for b in d.baseline] if d.baseline else []
            flag = ""
            if actual_sp is not None and actual_sp != d.setpoint:
                flag = "  DIFF"
            print(f"{t:8.1f}  {str([round(h,1) for h in house]):<20} "
                  f"{str([round(c,1) for c in car]):<20} {str(base):<18} "
                  f"{d.setpoint:>5} {str(actual_sp):>5}{flag}")

    compared = [(t, d, a) for t, _, _, d, a in rows if a is not None]
    agree = sum(1 for _, d, a in compared if d.setpoint == a)
    ceiling = min(max_current, bcfg.charger_fuse_rating)
    never_exceeds = all(d.setpoint <= ceiling for _, _, _, d, _ in rows)
    return rows, compared, agree, never_exceeds, bcfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--main-fuse", type=float, default=20.0)
    ap.add_argument("--safety-margin", type=float, default=1.0)
    ap.add_argument("--max-current", type=int, default=16)
    ap.add_argument("--raise-delay", type=float, default=30.0)
    ap.add_argument("--sweep", action="store_true",
                    help="try a range of main fuse sizes instead of one")
    args = ap.parse_args()

    meter, upd, ctrl, cfg = load(args.capture)
    print(f"Loaded {len(meter)} meter readings, {len(upd)} charger updates, "
          f"{len(ctrl)} real setpoints")
    if cfg:
        print(f"Charger config from capture: {cfg['payload']}")
    print()

    if args.sweep:
        print("How our balancer behaves at different main fuse sizes")
        print("(the capture's house baseline peaked around 5.5 A)\n")
        print(f"{'fuse':>6} {'min':>6} {'max':>6} {'pauses':>8}  setpoints seen")
        print("-" * 60)
        for fuse in (16, 20, 25, 32, 35):
            rows, *_ = run(meter, upd, ctrl, cfg, fuse, args.safety_margin,
                           args.max_current, args.raise_delay, verbose=False)
            sps = [d.setpoint for _, _, _, d, _ in rows]
            pauses = sum(1 for s in sps if s == 0)
            print(f"{fuse:>6} {min(sps):>6} {max(sps):>6} {pauses:>8}  "
                  f"{sorted(set(sps))}")
        return

    rows, compared, agree, never_exceeds, bcfg = run(
        meter, upd, ctrl, cfg, args.main_fuse, args.safety_margin,
        args.max_current, args.raise_delay, verbose=True
    )

    print()
    print("=" * 60)
    sps = [d.setpoint for _, _, _, d, _ in rows]
    print(f"main_fuse={args.main_fuse}A  margin={args.safety_margin}A  "
          f"ceiling={min(args.max_current, bcfg.charger_fuse_rating)}A")
    print(f"our setpoints: min={min(sps)}A max={max(sps)}A  distinct={sorted(set(sps))}")
    if compared:
        pct = 100.0 * agree / len(compared)
        print(f"agreement with the real Nanogrid Air: {agree}/{len(compared)} ({pct:.0f}%)")
    over = [s for s in sps if s > min(args.max_current, bcfg.charger_fuse_rating)]
    print(f"setpoints above the ceiling: {len(over)} (must be 0)")
    illegal = [s for s in sps if 0 < s < bcfg.min_allowed_current]
    print(f"illegal setpoints in 1..{bcfg.min_allowed_current - 1}A: {len(illegal)} (must be 0)")


if __name__ == "__main__":
    main()
