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


def _st(drawn):
    return {"current": [drawn] * 3, "state": 2, "max_allowed_current": 16}


def test_a_car_drawing_through_its_setpoint_is_flagged(tmp_path, monkeypatch):
    # Field chart: allowed 7 A, car spiking to 13-16 A.
    from types import SimpleNamespace
    svc = service(tmp_path, monkeypatch)
    c = SimpleNamespace(id="a", name="Garage")
    svc.allocation = {"a": 7}
    svc._alloc_changed_at = {"a": 0.0}
    svc._check_overdraw(100.0, 100.0, [c], {"a": _st(15.0)})
    assert "a" not in svc.overdraw, "a moment is not an override"
    svc._check_overdraw(103.0, 103.0, [c], {"a": _st(15.0)})
    assert svc.overdraw["a"] == 103.0


def test_a_car_still_following_a_cut_is_not_overdraw(tmp_path, monkeypatch):
    # Cut from 16 to 7 A: the car has up to 5 s to follow.
    from types import SimpleNamespace
    svc = service(tmp_path, monkeypatch)
    c = SimpleNamespace(id="a", name="Garage")
    svc.allocation = {"a": 7}
    svc._alloc_changed_at = {"a": 100.0}
    for t in (100.0, 102.0, 104.5):
        svc._check_overdraw(t, t, [c], {"a": _st(15.0)})
    assert "a" not in svc.overdraw


def test_jitter_and_ramp_downs_are_not_overdraw(tmp_path, monkeypatch):
    from types import SimpleNamespace
    svc = service(tmp_path, monkeypatch)
    c = SimpleNamespace(id="a", name="Garage")
    svc.allocation = {"a": 7}
    svc._alloc_changed_at = {"a": 0.0}
    for t, drawn in ((100.0, 8.5), (102.0, 15.0), (103.0, 7.2), (104.0, 15.0)):
        svc._check_overdraw(t, t, [c], {"a": _st(drawn)})
    assert "a" not in svc.overdraw


# ---------- entities published to Home Assistant ----------

def test_power_and_energy_are_published_per_charger(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    client = svc.clients[0]
    client.topics = object()               # bound
    client.connected.set()
    client.state.power, client.state.energy = 7360, 7_824_262
    ents = svc.ha_entities()

    power, pattrs = ents["sensor.ctek_njorder_garage_power"]
    assert power == "7360" and pattrs["unit_of_measurement"] == "W"
    assert pattrs["device_class"] == "power"

    energy, eattrs = ents["sensor.ctek_njorder_garage_energy"]
    assert energy == "7824.262" and eattrs["unit_of_measurement"] == "kWh"
    # What the Energy dashboard needs to accept it.
    assert eattrs["device_class"] == "energy"
    assert eattrs["state_class"] == "total_increasing"


def test_an_offline_charger_reports_unavailable_not_a_stale_value(tmp_path, monkeypatch):
    svc = service(tmp_path, monkeypatch)
    svc.clients[0].state.power, svc.clients[0].state.energy = 7360, 1000
    ents = svc.ha_entities()
    assert ents["sensor.ctek_njorder_garage_power"][0] == "unavailable"
    assert ents["sensor.ctek_njorder_garage_energy"][0] == "unavailable"


def test_entity_ids_come_from_the_charger_name():
    from app.main import Service
    assert Service._slug("Garage") == "garage"
    assert Service._slug("Gården Väst #2") == "garden_vast_2"
