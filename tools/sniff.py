#!/usr/bin/env python3
"""
Phase 2: capture everything on the CTEK Njord's MQTT broker.

Subscribes to '#' and '$SYS/#' and writes newline-delimited JSON to
captures/. Leave the real Nanogrid Air powered on while this runs - the whole
point is to observe the live conversation between it and the charger.

Uses a randomised client id so the broker will NOT disconnect the real
Nanogrid Air (brokers evict an existing session that reconnects with the same id).

  python tools/sniff.py --host 192.168.5.40 --duration 300
"""
import argparse
import binascii
import json
import os
import signal
import sys
import time
import uuid
from collections import Counter, defaultdict

import paho.mqtt.client as mqtt

stats = Counter()
first_seen = {}
last_payload = {}
retained = set()
start = time.time()
fh = None


def decode(payload: bytes):
    """Return (kind, value) - JSON if we can, else text, else hex."""
    if payload == b"":
        return "empty", ""
    try:
        return "json", json.loads(payload.decode("utf-8"))
    except Exception:
        pass
    try:
        text = payload.decode("utf-8")
        if text.isprintable() or "\n" in text:
            return "text", text
    except UnicodeDecodeError:
        pass
    return "hex", binascii.hexlify(payload).decode()


def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code != 0:
        print(f"[!] Connect failed: {reason_code}")
        return
    print("[*] Connected. Subscribing to '#' and '$SYS/#' ...")
    client.subscribe([("#", 0), ("$SYS/#", 0)])
    print("[*] Listening. Retained messages arrive first - they map the topic tree.\n")


def on_message(client, userdata, msg):
    ts = time.time()
    kind, value = decode(msg.payload)
    stats[msg.topic] += 1
    if msg.topic not in first_seen:
        first_seen[msg.topic] = ts
    last_payload[msg.topic] = (kind, value)
    if msg.retain:
        retained.add(msg.topic)

    rec = {
        "ts": round(ts, 3),
        "t_rel": round(ts - start, 3),
        "topic": msg.topic,
        "qos": msg.qos,
        "retain": bool(msg.retain),
        "kind": kind,
        "payload": value,
        "bytes": len(msg.payload),
    }
    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    fh.flush()

    n = stats[msg.topic]
    # Print every message for a new topic, then taper off so the console stays readable.
    if n <= 2 or n % 25 == 0:
        tag = "RET" if msg.retain else "   "
        shown = value if kind != "json" else json.dumps(value, ensure_ascii=False)
        if len(str(shown)) > 400:
            shown = str(shown)[:400] + f"... (+{len(str(shown)) - 400} chars)"
        print(f"[{rec['t_rel']:7.2f}s] {tag} {msg.topic}  (#{n})\n        {shown}")


def summarise():
    print("\n" + "=" * 78)
    print(f"CAPTURE SUMMARY - {sum(stats.values())} messages across {len(stats)} topics "
          f"in {time.time() - start:.0f}s")
    print("=" * 78)
    if not stats:
        print("No messages received.")
        print("The broker may only forward traffic once the Nanogrid Air is talking,")
        print("or the charger publishes nothing until a car/meter is present.")
        return
    groups = defaultdict(list)
    for topic in stats:
        groups[topic.split("/")[0]].append(topic)
    for root in sorted(groups):
        print(f"\n  {root}/")
        for topic in sorted(groups[root]):
            count = stats[topic]
            elapsed = max(time.time() - first_seen[topic], 1e-9)
            rate = f"{count / elapsed:.2f}/s" if count > 1 else "once"
            flag = " [retained]" if topic in retained else ""
            print(f"    {topic}  x{count} ({rate}){flag}")
            kind, value = last_payload[topic]
            s = json.dumps(value, ensure_ascii=False) if kind == "json" else str(value)
            print(f"        last: {s[:300]}")


def main():
    global fh, start
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--username", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--duration", type=int, default=300, help="seconds (0 = until Ctrl-C)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out = args.out or f"captures/capture-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    fh = open(out, "w", encoding="utf-8")
    print(f"[*] Writing to {out}")

    cid = f"ha-sniffer-{uuid.uuid4().hex[:8]}"
    print(f"[*] Client id: {cid}  (distinct, so your real Nanogrid Air stays connected)")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid, clean_session=True)
    if args.username:
        client.username_pw_set(args.username, args.password)
    client.on_connect = on_connect
    client.on_message = on_message

    def bail(*_):
        summarise()
        fh.close()
        print(f"\n[*] Saved: {out}")
        sys.exit(0)

    signal.signal(signal.SIGINT, bail)

    try:
        client.connect(args.host, args.port, keepalive=30)
    except Exception as e:
        print(f"[!] Could not connect to {args.host}:{args.port} - {e}")
        sys.exit(1)

    start = time.time()
    client.loop_start()
    try:
        if args.duration:
            time.sleep(args.duration)
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    client.loop_stop()
    bail()


if __name__ == "__main__":
    main()
