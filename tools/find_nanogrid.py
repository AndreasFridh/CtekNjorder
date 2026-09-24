#!/usr/bin/env python3
"""
Find anything on the network that could be commanding the charger.

A setpoint the add-on did not send kept appearing on the charger's control
topic with the Nanogrid Air supposedly unplugged. MQTT does not say who
published a message, so this looks from the other side: which devices on the
network look like a Nanogrid Air, or like anything else that talks MQTT.

For every host that answers on the given subnets it reports open ports of
interest, reverse DNS, MAC address (from the ARP table), and whatever an HTTP
server there says about itself. Hosts mentioning CTEK / Nanogrid / ngair are
flagged. Stdlib only.

With --charger it then listens on the charger's control topic and prints every
setpoint with a timestamp, so the foreign ones can be lined up against a
device being unplugged. That part needs paho-mqtt.

  python tools/find_nanogrid.py 192.168.1.0/24 192.168.5.0/24
  python tools/find_nanogrid.py 192.168.1.0/24 192.168.5.0/24 --charger 192.168.5.40

Run it from a computer on the same network. It only connects to hosts that
answer; nothing is sent to the charger.
"""
from __future__ import annotations

import argparse
import ipaddress
import platform
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

PORTS = {80: "http", 443: "https", 1883: "mqtt", 8883: "mqtt-tls", 8080: "http-alt"}
# Overridable for testing against the offline rig.
HTTP_PORTS = (80, 8080, 443)
HINTS = re.compile(r"ctek|nanogrid|ngair|ng-air|njord", re.I)
NGA_NAMES = ["ctek-ng-air.local", "ctek-ng-air"]
IS_WINDOWS = platform.system() == "Windows"


def ping(ip: str) -> bool:
    cmd = (["ping", "-n", "1", "-w", "700", ip] if IS_WINDOWS
           else ["ping", "-c", "1", "-W", "1", ip])
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=3).returncode == 0
    except Exception:
        return False


def port_open(ip: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def alive(ip: str) -> bool:
    # Many IoT devices ignore ping, so an open port counts as alive too.
    return ping(ip) or any(port_open(ip, p, 0.5) for p in PORTS)


def arp_table() -> dict[str, str]:
    """ip -> MAC, from the OS ARP cache (filled in by the sweep)."""
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True,
                             timeout=10).stdout
    except Exception:
        return {}
    table = {}
    for line in out.splitlines():
        ip = re.search(r"(\d+\.\d+\.\d+\.\d+)", line)
        mac = re.search(r"([0-9a-fA-F]{1,2}[:-]){5}[0-9a-fA-F]{1,2}", line)
        if ip and mac:
            table[ip.group(1)] = mac.group(0).replace("-", ":").lower()
    return table


def http_banner(ip: str, port: int) -> str:
    """Title, Server header and a hint of the body - enough to recognise a device."""
    scheme = "https" if port == 443 else "http"
    for path in ("/status/", "/"):
        try:
            ctx = ssl._create_unverified_context()
            with urllib.request.urlopen(f"{scheme}://{ip}:{port}{path}",
                                        timeout=2, context=ctx) as r:
                body = r.read(4000).decode("utf-8", "replace")
                server = r.headers.get("Server", "")
        except urllib.error.HTTPError as e:
            body, server = "", e.headers.get("Server", "") if e.headers else ""
            if e.code == 401:
                realm = e.headers.get("WWW-Authenticate", "") if e.headers else ""
                return f"401 {realm} {server}".strip()
            continue
        except Exception:
            continue
        title = re.search(r"<title>(.*?)</title>", body, re.I | re.S)
        text = " ".join(filter(None, [
            server, title.group(1).strip() if title else "",
            re.sub(r"\s+", " ", body[:160]) if not title else "",
        ]))
        return text[:200]
    return ""


def describe(ip: str) -> dict:
    try:
        name = socket.gethostbyaddr(ip)[0]
    except OSError:
        name = ""
    ports = [p for p in PORTS if port_open(ip, p)]
    banner = ""
    for p in HTTP_PORTS:
        if p in ports:
            banner = http_banner(ip, p)
            if banner:
                break
    return {"ip": ip, "name": name, "ports": ports, "banner": banner}


def scan(nets: list[str]) -> list[dict]:
    hosts = [str(h) for n in nets for h in ipaddress.ip_network(n, strict=False).hosts()]
    print(f"Sweeping {len(hosts)} addresses...", flush=True)
    with ThreadPoolExecutor(64) as pool:
        live = [ip for ip, ok in zip(hosts, pool.map(alive, hosts)) if ok]
    print(f"{len(live)} hosts answered. Looking closer...", flush=True)
    with ThreadPoolExecutor(32) as pool:
        found = list(pool.map(describe, live))
    macs = arp_table()
    for h in found:
        h["mac"] = macs.get(h["ip"], "")
    return found


def report(found: list[dict]) -> None:
    for name in NGA_NAMES:
        try:
            ip = socket.gethostbyname(name)
            print(f"\n!! {name} resolves to {ip} - that name is the Nanogrid Air's.")
        except OSError:
            pass

    print(f"\n{'IP':<16}{'MAC':<19}{'ports':<22}name / what it says")
    suspects = []
    for h in sorted(found, key=lambda h: tuple(int(x) for x in h["ip"].split("."))):
        ports = ",".join(PORTS[p] for p in h["ports"])
        what = " | ".join(filter(None, [h["name"], h["banner"]]))
        flag = HINTS.search(what) is not None
        if flag:
            suspects.append(h)
        print(f"{'!!' if flag else '  '}{h['ip']:<14}{h['mac']:<19}{ports:<22}{what[:90]}")

    brokers = [h for h in found if 1883 in h["ports"]]
    print("\nMQTT brokers (port 1883):", ", ".join(h["ip"] for h in brokers) or "none")
    if suspects:
        print("Looks like CTEK equipment:", ", ".join(h["ip"] for h in suspects))
        print("A Nanogrid Air answering here is powered and on the network.")
    else:
        print("Nothing identified itself as CTEK. A device can still hide behind a "
              "blank HTTP page - look for MACs you do not recognise.")


def listen(charger: str, port: int, seconds: int) -> None:
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("\n--charger needs paho-mqtt: pip install paho-mqtt")
        return
    t0 = time.time()

    def on_connect(c, u, f, rc, p=None):
        c.subscribe([("ctek/ng-v2/controller/#", 0), ("$SYS/broker/clients/#", 0)])
        print(f"\nListening on {charger}:{port} for {seconds}s. Every setpoint the "
              "charger receives - ours and anyone else's:")

    def on_message(c, u, m):
        print(f"  {time.strftime('%H:%M:%S')} (+{time.time() - t0:5.1f}s)  "
              f"{m.topic}  {m.payload.decode('utf-8', 'replace')}"
              f"{'  [retained]' if m.retain else ''}", flush=True)

    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                    client_id=f"ctek-find-{int(t0) % 100000}")
    c.on_connect, c.on_message = on_connect, on_message
    c.connect(charger, port, keepalive=30)
    c.loop_start()
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        pass
    c.loop_stop()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("subnets", nargs="+", help="e.g. 192.168.1.0/24")
    ap.add_argument("--charger", help="charger IP: also listen on its control topic")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--listen", type=int, default=180, help="seconds to listen")
    args = ap.parse_args()
    report(scan(args.subnets))
    if args.charger:
        listen(args.charger, args.port, args.listen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
