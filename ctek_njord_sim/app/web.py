"""
Ingress web UI.

Home Assistant proxies this behind its own authentication, so the server binds
to the container only and does no auth of its own. Requests arrive with the
ingress prefix already stripped, which is why every URL the page uses is
relative.
"""
from __future__ import annotations

import logging
import os

from aiohttp import web

from . import optionspec, supervisor

_LOG = logging.getLogger(__name__)

WWW = os.path.join(os.path.dirname(__file__), "www")

# Stands in for a stored secret so it never leaves the process. Posted back
# unchanged by the form, and dropped rather than written.
KEEP_SECRET = "•" * 8


# Home Assistant proxies Ingress traffic from the Supervisor itself, which sits
# at a fixed address on the internal network. Other add-ons share that network
# but have their own addresses, so this does distinguish "the authenticated user
# via Ingress" from "some other container on the same host".
SUPERVISOR_IP = "172.30.32.2"
LOOPBACK = ("127.0.0.1", "::1")

# Mutating calls must carry a header that a cross-origin form cannot set, so a
# malicious page cannot ride the user's Ingress session.
CSRF_HEADER = "X-Ctek-UI"


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


class WebUI:
    def __init__(self, service, port: int = 8099):
        self.service = service
        self.port = port
        self._runner: web.AppRunner | None = None

    # ---------- handlers ----------

    async def index(self, request):
        # Revalidate every load. The page is a single file that changes with
        # each add-on update, and a cached copy against a newer API is worse
        # than a slow load: the mismatch shows up as sections silently failing
        # to render rather than as an obvious error.
        return web.FileResponse(
            os.path.join(WWW, "index.html"),
            headers={"Cache-Control": "no-cache, must-revalidate"},
        )

    async def state(self, request):
        return web.json_response(self.service.snapshot())

    async def history(self, request):
        try:
            minutes = max(1, min(int(request.query.get("minutes", 30)), 7 * 24 * 60))
        except ValueError:
            minutes = 30
        return web.json_response(self.service.history_series(minutes))

    async def sessions(self, request):
        try:
            limit = max(1, min(int(request.query.get("limit", 200)), 500))
        except ValueError:
            limit = 200
        charger = request.query.get("charger") or None
        log = self.service.session_log
        return web.json_response({
            "sessions": log.list(limit, charger),
            "summary": log.summary(),
            "currency": self.service.opts.currency,
            "chargers": [{"id": c.id, "name": c.name} for c in self.service.clients],
        })

    async def get_settings(self, request):
        values = {s.key: getattr(self.service.opts, s.key) for s in optionspec.SPECS}
        # Never serve secrets back out. The form posts the placeholder straight
        # back when the field is untouched, which we treat as "leave it alone".
        for spec in optionspec.SPECS:
            if spec.type == "password" and values.get(spec.key):
                values[spec.key] = KEEP_SECRET
        return web.json_response({
            "specs": optionspec.as_json(),
            "values": values,
            "supervisor": supervisor.available(),
        })

    async def post_settings(self, request):
        try:
            body = await request.json()
        except Exception:
            return _json_error("malformed request body")

        raw = body.get("values") or {}
        changes, invalid = {}, []
        for key, value in raw.items():
            if key not in optionspec.BY_KEY:
                continue
            if optionspec.BY_KEY[key].type == "password" and value == KEEP_SECRET:
                continue  # untouched - keep whatever is already stored
            try:
                changes[key] = optionspec.coerce(key, value)
            except ValueError as e:
                invalid.append(str(e))
        if invalid:
            return _json_error("; ".join(invalid))

        # Cross-field checks, the same ones the service enforces at startup.
        merged = {s.key: getattr(self.service.opts, s.key) for s in optionspec.SPECS}
        merged.update(changes)
        if merged["max_charge_current"] > merged["main_fuse"]:
            return _json_error(
                f"Max charge current ({merged['max_charge_current']}A) cannot "
                f"exceed the main fuse ({merged['main_fuse']}A)"
            )
        if merged["fallback_current"] > merged["max_charge_current"]:
            return _json_error(
                f"Fallback current ({merged['fallback_current']}A) cannot exceed "
                f"max charge current ({merged['max_charge_current']}A)"
            )

        # Apply what can take effect now, so the fuse and limits respond
        # immediately even while a car is charging.
        applied_live = self.service.apply_live_settings(changes)

        persisted, warning = False, None
        if supervisor.available():
            try:
                await supervisor.set_options(changes)
                persisted = True
            except Exception as e:
                warning = f"Applied, but could not persist: {e}"
                _LOG.error("Persisting options failed: %s", e)
        else:
            warning = "Running outside the Supervisor - changes are not persisted."

        needs_restart = sorted(set(changes) - optionspec.LIVE_KEYS)
        return web.json_response({
            "ok": True,
            "applied_live": sorted(applied_live),
            "needs_restart": needs_restart,
            "persisted": persisted,
            "warning": warning,
        })

    async def entities(self, request):
        return web.json_response(await supervisor.list_entities())

    async def restart(self, request):
        if not supervisor.available():
            return _json_error("Not running under the Supervisor", 503)
        try:
            await supervisor.restart_addon()
        except Exception as e:
            return _json_error(str(e), 500)
        return web.json_response({"ok": True})

    # ---------- lifecycle ----------

    @web.middleware
    async def guard(self, request, handler):
        """
        Keep the API to the Ingress path.

        The UI does no authentication of its own because Home Assistant does it
        first - but that only holds for traffic that actually came through
        Ingress. Anything else on the Supervisor's Docker network could
        otherwise change the charging limits or restart the add-on unasked.

        Escape hatch: `restrict_api` can be turned off from the add-on's
        Configuration tab in Home Assistant, which keeps working even if this
        check is what is stopping the UI from loading.
        """
        if not request.path.startswith("/api/") or not self.service.opts.restrict_api:
            return await handler(request)

        peer = request.remote
        if peer not in LOOPBACK and peer != SUPERVISOR_IP:
            _LOG.warning(
                "Refused %s %s from %s - not the Ingress proxy. If this is your "
                "own access, add it or set restrict_api off in the add-on "
                "configuration.", request.method, request.path, peer,
            )
            return _json_error("Only reachable through Home Assistant Ingress", 403)

        if request.method != "GET" and request.headers.get(CSRF_HEADER) != "1":
            _LOG.warning("Refused %s %s: missing %s header",
                         request.method, request.path, CSRF_HEADER)
            return _json_error("Missing UI header", 403)

        return await handler(request)

    def build(self) -> web.Application:
        app = web.Application(middlewares=[self.guard])
        app.router.add_get("/", self.index)
        app.router.add_get("/api/state", self.state)
        app.router.add_get("/api/history", self.history)
        app.router.add_get("/api/sessions", self.sessions)
        app.router.add_get("/api/settings", self.get_settings)
        app.router.add_post("/api/settings", self.post_settings)
        app.router.add_get("/api/entities", self.entities)
        app.router.add_post("/api/restart", self.restart)
        return app

    async def start(self) -> None:
        self._runner = web.AppRunner(self.build(), access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await site.start()
        _LOG.info("Web UI listening on port %s", self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
