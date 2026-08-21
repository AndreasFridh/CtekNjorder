#!/usr/bin/env python3
"""
Phase 3: turn a capture into a protocol description.

Reports, per topic: publish interval, retained flag, the JSON shape, and the
observed range of every numeric field. That range is what tells us which
fields are live telemetry and which are static config.

  python tools/analyze.py captures/capture-long.jsonl
"""
import argparse
import json
import statistics
from collections import defaultdict


def walk(obj, prefix=""):
    """Flatten nested JSON into dotted paths; lists become [i] slots."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{prefix}[{i}]")
    else:
        yield prefix, obj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    args = ap.parse_args()

    times = defaultdict(list)
    fields = defaultdict(lambda: defaultdict(list))
    retained = set()
    scalars = defaultdict(list)
    total = 0

    with open(args.capture, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            total += 1
            topic = rec["topic"]
            times[topic].append(rec["t_rel"])
            if rec["retain"]:
                retained.add(topic)
            payload = rec["payload"]
            if rec["kind"] == "json" and isinstance(payload, (dict, list)):
                for path, val in walk(payload):
                    fields[topic][path].append(val)
            else:
                scalars[topic].append(payload)

    print(f"{total} messages / {len(times)} topics\n")
    for topic in sorted(times):
        ts = times[topic]
        line = f"### {topic}\n  count={len(ts)}"
        if len(ts) > 1:
            gaps = [b - a for a, b in zip(ts, ts[1:])]
            line += (f"  interval: median={statistics.median(gaps):.2f}s "
                     f"min={min(gaps):.2f}s max={max(gaps):.2f}s")
        if topic in retained:
            line += "  [RETAINED]"
        print(line)

        if scalars[topic]:
            vals = scalars[topic]
            uniq = sorted(set(map(str, vals)))
            print(f"  raw scalar payload, distinct values: {uniq[:12]}")

        for path, vals in fields[topic].items():
            uniq = {json.dumps(v, sort_keys=True) for v in vals}
            nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if len(uniq) == 1:
                desc = f"CONST {vals[0]!r}"
            elif nums:
                desc = (f"range {min(nums)} .. {max(nums)}  "
                        f"mean={statistics.mean(nums):.2f}  ({len(uniq)} distinct)")
            else:
                sample = sorted(uniq)[:6]
                desc = f"{len(uniq)} distinct: {sample}"
            print(f"    {path:<28} {desc}")
        print()


if __name__ == "__main__":
    main()
