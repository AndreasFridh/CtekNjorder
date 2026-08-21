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


def _json_error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


class WebUI:
    def __init__(self, service, port: int = 8099):
        self.service = service
        self.port = port
        self._runner: web.AppRunner | None = None

    # ---------- handlers ----------

    async def index(self, request):
        return web.FileResponse(os.path.join(WWW, "index.html"))

    async def state(self, request):
        return web.json_response(self.service.snapshot())

    async def history(self, request):
        return web.json_response(self.service.history_series())

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

    def build(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.index)
        app.router.add_get("/api/state", self.state)
        app.router.add_get("/api/history", self.history)
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
