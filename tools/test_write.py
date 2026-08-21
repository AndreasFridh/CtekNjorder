#!/usr/bin/env python3
"""
Does the Njord's broker let an anonymous client PUBLISH, or only subscribe?

Some brokers allow anonymous reads but restrict writes via ACL. We need write
access to the control topic, so prove it before building on that assumption.

Publishes to harmless scratch topics only - it never touches the control topic.
Write access is confirmed by subscribing to the same topic and seeing our own
message come back from the broker.

  python -u tools/test_write.py --host 192.168.5.40
  python -u tools/test_write.py --host 192.168.5.40 --username admin --password xxx
"""
import argparse
import sys
import threading
import time
import uuid

import paho.mqtt.client as mqtt


def log(msg):
    print(msg, flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--username", default=None)
    ap.add_argument("--password", default=None)
    args = ap.parse_args()

    nonce = uuid.uuid4().hex[:8]
    # One topic outside the vendor tree, one inside it - ACLs are usually
    # written per-prefix, so the two can behave differently.
    probes = [f"hasim/test/{nonce}", f"ctek/hasim/test/{nonce}"]

    received = set()
    connack = threading.Event()
    state = {"rc": None, "disconnected": None, "expected_disconnect": False}

    def on_connect(c, u, flags, rc, props=None):
        state["rc"] = rc
        if rc == 0:
            for t in probes:
                c.subscribe(t, qos=0)
        connack.set()

    def on_message(c, u, msg):
        received.add((msg.topic, msg.payload.decode("utf-8", "replace")))

    def on_disconnect(c, u, flags, rc, props=None):
        if not state["expected_disconnect"]:
            state["disconnected"] = rc

    label = "anonymous (no credentials)" if not args.username else f"user={args.username}"
    log(f"[*] Connecting to {args.host}:{args.port} as {label}")

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                    client_id=f"hasim-wtest-{nonce}", clean_session=True)
    if args.username:
        c.username_pw_set(args.username, args.password)
    c.on_connect = on_connect
    c.on_message = on_message
    c.on_disconnect = on_disconnect
    # Do not let paho silently reconnect in a loop if the broker kicks us.
    c.reconnect_delay_set(min_delay=99999, max_delay=99999)

    try:
        c.connect(args.host, args.port, keepalive=30)
    except Exception as e:
        log(f"[!] Connect failed: {e}")
        sys.exit(1)

    c.loop_start()

    if not connack.wait(timeout=10):
        log("[!] No CONNACK within 10s")
        c.loop_stop()
        sys.exit(1)
    if state["rc"] != 0:
        log(f"[!] Broker refused the connection: {state['rc']}")
        c.loop_stop()
        sys.exit(1)
    log("[+] Connected, subscribed to scratch topics.")

    time.sleep(1.0)  # let SUBACKs settle

    results = {}
    for topic in probes:
        payload = f"probe-{nonce}"
        log(f"[*] Publishing to {topic} ...")
        c.publish(topic, payload, qos=0)
        deadline = time.time() + 5
        while time.time() < deadline:
            if (topic, payload) in received:
                break
            time.sleep(0.1)
        results[topic] = (topic, payload) in received

    state["expected_disconnect"] = True
    c.loop_stop()
    try:
        c.disconnect()
    except Exception:
        pass

    log("")
    for topic, ok in results.items():
        log(f"    {'WRITE OK    ' if ok else 'NO ECHO     '} {topic}")
    if state["disconnected"]:
        log(f"[!] Broker disconnected us (rc={state['disconnected']}) - often an ACL rejection")

    log("")
    if all(results.values()):
        log(f"=> Publish works as {label}. Full write access to the control topic.")
    elif any(results.values()):
        log("=> Partial: some prefixes writable. See per-topic results above.")
    else:
        log("=> No writes echoed back. Re-run with --username/--password.")


if __name__ == "__main__":
    main()
