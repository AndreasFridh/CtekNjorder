#!/usr/bin/env python3
"""
Phase 1 recon for the CTEK Njord charger / Nanogrid Air.

Finds which TCP ports are open (looking for the MQTT broker) and pokes the
known HTTP API endpoints, which per CTEK's own docs can report the MQTT port.

Stdlib only - no install required.

  python tools/probe.py --host 192.168.1.50
  python tools/probe.py --host ctek-ng-air.local --password mysecret
"""
import argparse
import base64
import json
import socket
import ssl
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# Ports worth checking. MQTT usually 1883 (plain) / 8883 (TLS) / 9001 (websocket),
# but CTEK is known to move the broker, hence the wider sweep.
PORTS = [80, 443, 502, 1883, 1884, 8000, 8080, 8081, 8083, 8443, 8880, 8883, 9001, 9883]

# Endpoints seen in CTEK docs / the community integration, plus likely siblings.
ENDPOINTS = [
    "/status/", "/get/evse/", "/get/meter/", "/get/info/", "/get/config/",
    "/get/mqtt/", "/get/network/", "/get/version/", "/get/wifi/",
    "/meter/", "/evse/", "/info/", "/api/", "/",
]

DEFAULT_USER = "ctek"


def check_port(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return port, True
    except OSError:
        return port, False


def banner(host, port, timeout=2.0):
    """Grab a few bytes so we can tell an MQTT broker from a web server."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            # Minimal MQTT 3.1.1 CONNECT; a real broker answers with CONNACK (0x20).
            pkt = bytearray([0x10, 0x00, 0x00, 0x04])
            pkt += b"MQTT"
            pkt += bytes([0x04, 0x02, 0x00, 0x3c])
            cid = b"ctek-probe"
            pkt += len(cid).to_bytes(2, "big") + cid
            pkt[1] = len(pkt) - 2
            s.sendall(bytes(pkt))
            s.settimeout(timeout)
            data = s.recv(64)
            if data[:1] == b"\x20":
                code = data[3] if len(data) > 3 else -1
                meaning = {
                    0: "ACCEPTED (anonymous connect allowed!)",
                    1: "refused: unacceptable protocol version",
                    2: "refused: client id rejected",
                    3: "refused: server unavailable",
                    4: "refused: BAD USERNAME/PASSWORD (auth required)",
                    5: "refused: NOT AUTHORIZED (auth required)",
                }.get(code, f"refused: code {code}")
                return f"MQTT BROKER -> CONNACK {meaning}"
            return f"raw: {data[:32]!r}"
    except Exception as e:
        return f"(no banner: {type(e).__name__})"


def http_get(host, port, path, user=None, password=None, timeout=4.0, tls=False):
    scheme = "https" if tls else "http"
    netloc = host if port in (80, 443) else f"{host}:{port}"
    url = f"{scheme}://{netloc}{path}"
    req = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": "ctek-probe/1.0"})
    if password is not None:
        tok = base64.b64encode(f"{user or DEFAULT_USER}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {tok}")
    ctx = ssl._create_unverified_context() if tls else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, r.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(4000).decode("utf-8", "replace")
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def main():
    ap = argparse.ArgumentParser(description="Recon a CTEK Njord / Nanogrid Air")
    ap.add_argument("--host", required=True, help="IP or hostname (e.g. ctek-ng-air.local)")
    ap.add_argument("--user", default=DEFAULT_USER)
    ap.add_argument("--password", default=None, help="if omitted, tries unauthenticated first")
    ap.add_argument("--http-port", type=int, default=80)
    ap.add_argument("--out", default="captures/probe-report.json")
    args = ap.parse_args()

    report = {"host": args.host, "open_ports": [], "endpoints": {}}

    try:
        ip = socket.gethostbyname(args.host)
        print(f"[*] {args.host} resolves to {ip}")
        report["resolved_ip"] = ip
    except OSError as e:
        print(f"[!] Cannot resolve {args.host}: {e}")
        print("    If using .local, mDNS may not work from this machine - use the raw IP.")
        sys.exit(1)

    print(f"\n[*] Scanning {len(PORTS)} ports ...")
    with ThreadPoolExecutor(max_workers=len(PORTS)) as ex:
        results = list(ex.map(lambda p: check_port(args.host, p), PORTS))
    for port, is_open in sorted(results):
        if is_open:
            b = banner(args.host, port)
            print(f"    OPEN  {port:<5}  {b}")
            report["open_ports"].append({"port": port, "banner": b})
    if not report["open_ports"]:
        print("    (none open - wrong IP, or a firewall/VLAN is in the way)")

    print(f"\n[*] Probing HTTP endpoints on :{args.http_port} ...")
    for path in ENDPOINTS:
        status, body = http_get(args.host, args.http_port, path, args.user, args.password)
        if status is None:
            continue
        preview = " ".join(body.split())[:220]
        flag = "OK " if status == 200 else f"{status}"
        print(f"    {flag:<4} {path:<16} {preview}")
        entry = {"status": status, "body": body[:20000]}
        try:
            entry["json"] = json.loads(body)
        except Exception:
            pass
        report["endpoints"][path] = entry
        # Surface anything that smells like broker config.
        low = body.lower()
        for kw in ("mqtt", "broker", "1883", "port"):
            if kw in low and status == 200:
                report.setdefault("mqtt_hints", []).append({"path": path, "keyword": kw})
                break

    if args.password is None and any(
        e.get("status") == 401 for e in report["endpoints"].values()
    ):
        print("\n[!] Got 401s - the API wants credentials. Re-run with "
              f"--user {DEFAULT_USER} --password <pw from the CTEK app / device label>")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\n[*] Full report written to {args.out}")

    hints = report.get("mqtt_hints")
    if hints:
        print(f"[*] Possible MQTT config found in: {', '.join(h['path'] for h in hints)}")


if __name__ == "__main__":
    main()
