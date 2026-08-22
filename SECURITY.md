# Security notes

This add-on controls how much current a car draws through a domestic main
fuse, and it talks to a device with no authentication. Both are worth being
explicit about.

## Trust boundaries

| Input | Trusted? | Why |
|---|---|---|
| Add-on options | Yes | Set by the owner through Home Assistant. |
| Charger MQTT broker | **No** | Accepts anonymous publish from anywhere on the LAN. |
| Home Assistant entity states | Partly | Values are trusted; names and IDs are escaped before display. |
| Web UI requests | Partly | Ingress authenticates users, but see *Network exposure*. |

## The charger's broker is unauthenticated

Verified with `tools/test_write.py`: the Njord's broker at port 1883 accepts
anonymous **connect, subscribe and publish**. Supplying credentials also works,
which suggests it ignores them entirely.

Anyone who can reach the charger can therefore read its telemetry, publish
retained topics, and **command a charging current**. That is a property of the
charger's firmware, not of this add-on, and it is equally true with the real
Nanogrid Air installed.

Practical consequence: **keep the charger off any untrusted network segment.**
A guest VLAN with access to it is enough for someone to control your charging.
This add-on cannot fix that; only network segmentation can.

### What follows from it

Because any LAN device can retain a topic, the charger serial — which we parse
out of a topic *name* — is attacker-controlled. It is validated against
`SERIAL_RE` before being adopted, used to build topics, or displayed. A hostile
value is ignored even when its retained message wins the race against the real
charger, and a serial already bound cannot be replaced. See
`tests/test_security.py`.

## Web UI

The UI does **no authentication of its own**. It relies on Home Assistant
Ingress, which authenticates the user before proxying.

### Network exposure

`config.yaml` declares no `ports:`, so the UI is not reachable from your LAN.

It is reachable from the Supervisor's internal Docker network, so `/api/*`
accepts requests only from the Ingress proxy (the Supervisor's own address) or
from inside this container. Another add-on on the same network is refused, and
the refusal is logged with the address that was rejected.

`restrict_api` disables the check. It is set from the add-on's **Configuration**
tab in Home Assistant rather than from this UI, so it still works if the check
is itself what is preventing the UI from loading.

### Cross-site request forgery

Mutating endpoints require the `X-Ctek-UI` header, which a cross-origin form
cannot set and which triggers a CORS preflight this server does not answer.
Ingress URLs also carry a per-session token.

### Output escaping

Every value interpolated into the page is escaped at the point of use
(`esc()` in `index.html`), rather than relying on remembering which values are
safe. This matters because two sources are outside our control: the charger
serial from MQTT, and entity names from Home Assistant.

### Secrets

`charger_password` is never returned by `GET /api/settings`; a placeholder is
sent instead, and posting the placeholder back is treated as "unchanged" rather
than being written. Option values are not logged — only their keys.

## What this add-on can do to your system

- **Charging current.** It sets it. A wrong `main_fuse` can trip your main
  breaker. It ships with `dry_run: true` so nothing is commanded until you
  opt in.
- **Supervisor API** (`hassio_api: true`), scoped to `/addons/self/*`: read and
  write its own options, and restart itself.
- **Home Assistant API** (`homeassistant_api: true`): read entity states. It
  never calls services or writes state.

## Not yet characterised

**What the charger does when the controller goes silent** is unknown — whether
it holds the last setpoint, decays to the 6 A minimum, or stops. Until that is
established, treat a crash of this add-on as unsafe rather than assuming the
charger fails safe. See the open questions in [PROTOCOL.md](PROTOCOL.md).

## Reporting

Open an issue at
<https://github.com/AndreasFridh/CtekNjorder/issues>.
