"""
The Service's dashboard snapshot.

`snapshot()` runs on every web UI poll, concurrently with the control loop, so
it must be a pure read: anything it mutates races the loop that actually
controls the chargers.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ctek_njord_sim"))

from app.config import Options  # noqa: E402
from app.main import Service  # noqa: E402


def service(tmp_path, monkeypatch):
    monkeypatch.setenv("CTEK_DATA", str(tmp_path))
    opts = Options()
    opts.chargers = [{"name": "Garage", "host": "127.0.0.1", "port": 1883,
                      "serial": "", "enabled": True}]
    return Service(opts)


def test_a_snapshot_before_the_first_control_step_does_not_fail(tmp_path, monkeypatch):
    # /api/state used to raise until the regulator had stepped once, which it
    # cannot do before a charger is bound and the meter has reported.
    snap = service(tmp_path, monkeypatch).snapshot()
    assert snap["meter"]["last_step_age"] is None


def test_the_snapshot_does_not_judge_demand(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    calls = []
    svc.demand.wants_current = lambda *a, **k: calls.append(a) or True
    svc.snapshot()
    assert calls == [], "judging demand from the UI can swallow a car arriving"
