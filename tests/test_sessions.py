"""
The charging session log.

This is the part someone reads back weeks later, so it has to survive restarts
and to be honest about the difference between how long a session ran and how
long it actually charged - load balancing pauses a car through a household
peak, and a session that lasted four hours but charged for one should say so.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ctek_njord_sim"))

from app.costs import CostTracker  # noqa: E402
from app.sessions import KEEP, SessionLog  # noqa: E402


@pytest.fixture
def data_dir(tmp_path):
    return str(tmp_path)


def record(started, energy=10.0, cost=20.0, charger="a", name="Garage",
           duration=3600, charging=3600):
    return {
        "charger": charger, "name": name,
        "started": started, "ended": started + duration,
        "duration_s": duration, "charging_s": charging,
        "energy_kwh": energy, "cost": cost, "currency": "SEK",
        "peak_current": 16.0, "avg_current": 14.0,
        "avg_price": round(cost / energy, 3) if energy else None,
        "min_price": 1.0, "max_price": 3.0,
    }


# ---------- persistence ----------

def test_sessions_survive_a_restart(data_dir):
    log = SessionLog(data_dir)
    log.add(record(time.time() - 7200))
    log.add(record(time.time() - 3600))

    reopened = SessionLog(data_dir)
    assert len(reopened.sessions) == 2
    assert reopened.totals()["energy_kwh"] == pytest.approx(20.0)


def test_the_newest_session_is_listed_first(data_dir):
    """A log is read newest-first; oldest-first would bury what just happened."""
    log = SessionLog(data_dir)
    now = time.time()
    log.add(record(now - 7200, energy=1.0))
    log.add(record(now - 60, energy=2.0))
    assert log.list()[0]["energy_kwh"] == 2.0


def test_the_log_does_not_grow_without_bound(data_dir):
    log = SessionLog(data_dir)
    for i in range(KEEP + 40):
        log.add(record(time.time() - i))
    assert len(log.sessions) == KEEP


def test_a_damaged_line_does_not_lose_the_log(data_dir):
    log = SessionLog(data_dir)
    log.add(record(time.time()))
    with open(log.path, "a", encoding="utf-8") as f:
        f.write("not json at all\n")
        f.write(json.dumps({"no": "started field"}) + "\n")

    reopened = SessionLog(data_dir)
    assert len(reopened.sessions) == 1


def test_an_unwritable_location_does_not_raise(data_dir):
    """Losing the log is a nuisance; interrupting charging is not."""
    log = SessionLog(data_dir)
    log.path = os.path.join(data_dir, "nope", "\0bad", "sessions.jsonl")
    log.add(record(time.time()))            # must not raise
    assert len(log.sessions) == 1


# ---------- filtering and totals ----------

def test_sessions_can_be_filtered_to_one_charger(data_dir):
    log = SessionLog(data_dir)
    log.add(record(time.time() - 100, charger="a", name="Garage"))
    log.add(record(time.time() - 50, charger="b", name="Driveway"))
    assert len(log.list(charger="a")) == 1
    assert log.list(charger="a")[0]["name"] == "Garage"


def test_totals_respect_the_time_window(data_dir):
    log = SessionLog(data_dir)
    now = time.time()
    log.add(record(now - 40 * 24 * 3600, energy=5.0, cost=10.0))   # old
    log.add(record(now - 3600, energy=7.0, cost=14.0))             # recent
    summary = log.summary()
    assert summary["all"]["sessions"] == 2
    assert summary["month"]["sessions"] == 1
    assert summary["month"]["energy_kwh"] == pytest.approx(7.0)


def test_average_price_is_cost_over_energy_not_a_mean_of_prices(data_dir):
    """
    A short expensive session and a long cheap one do not average to the middle
    - what was paid per kWh overall is the number that matters.
    """
    log = SessionLog(data_dir)
    now = time.time()
    log.add(record(now - 200, energy=1.0, cost=5.0))     # 5.00/kWh
    log.add(record(now - 100, energy=9.0, cost=9.0))     # 1.00/kWh
    assert log.totals()["avg_price"] == pytest.approx(1.4)   # 14 / 10


def test_no_energy_means_no_average_price(data_dir):
    log = SessionLog(data_dir)
    log.add(record(time.time(), energy=0.0, cost=0.0))
    assert log.totals()["avg_price"] is None


# ---------- what a finished session records ----------

def test_a_completed_session_reaches_the_log(data_dir):
    log = SessionLog(data_dir)
    tracker = CostTracker(
        on_complete=lambda cid, s: log.add(s.as_record(cid, "Garage", "SEK")))

    tracker.update(0.0, "a", drawn=10.0, energy_counter=0, price=2.0)
    tracker.update(60.0, "a", drawn=10.0, energy_counter=2000, price=2.0)
    tracker.update(60.0 + tracker.IDLE_GRACE + 1, "a", drawn=0.0,
                   energy_counter=2000, price=2.0)

    assert len(log.sessions) == 1
    row = log.sessions[0]
    assert row["energy_kwh"] == pytest.approx(2.0)
    assert row["cost"] == pytest.approx(4.0)
    assert row["name"] == "Garage"
    assert row["started"] < row["ended"]


def test_charging_time_is_not_the_same_as_elapsed_time():
    """
    Load balancing pauses a car through a household peak. A session that ran
    for an hour but only charged for ten minutes must not claim an hour of
    charging.
    """
    t = CostTracker()
    step = 10.0
    now = 0.0

    def run(seconds, drawn, counter):
        nonlocal now
        for _ in range(int(seconds / step)):
            now += step
            t.update(now, "a", drawn=drawn, energy_counter=counter, price=1.0)

    run(300, 10.0, 500)      # five minutes charging
    run(60, 0.0, 500)        # paused by load balancing, inside the grace period
    run(300, 10.0, 1000)     # five more minutes charging
    run(t.IDLE_GRACE + 30, 0.0, 1000)   # then done

    done = t.last_completed("a")
    assert done is not None
    assert done.charging_s < done.duration, (
        "the pause in the middle should be visible in the record"
    )
    # `duration` runs to the moment it last drew, so the trailing idle time is
    # already excluded and the gap is exactly the pause in the middle.
    assert done.duration - done.charging_s == pytest.approx(60, abs=15)
    assert 550 < done.charging_s < 650, "about ten minutes of actual charging"


def test_a_session_records_its_peak_current():
    t = CostTracker()
    t.update(0.0, "a", drawn=6.0, energy_counter=0, price=1.0)
    t.update(10.0, "a", drawn=15.5, energy_counter=100, price=1.0)
    t.update(20.0, "a", drawn=9.0, energy_counter=200, price=1.0)
    assert t.session_for("a").peak_current == pytest.approx(15.5)


def test_a_session_records_the_range_of_prices_it_spanned():
    t = CostTracker()
    t.update(0.0, "a", drawn=10.0, energy_counter=0, price=2.0)
    t.update(10.0, "a", drawn=10.0, energy_counter=100, price=0.5)
    t.update(20.0, "a", drawn=10.0, energy_counter=200, price=3.5)
    s = t.session_for("a")
    assert s.min_price == pytest.approx(0.5)
    assert s.max_price == pytest.approx(3.5)


def test_a_failing_log_does_not_break_the_session_tracker():
    def explode(cid, session):
        raise RuntimeError("disk gone")

    t = CostTracker(on_complete=explode)
    t.update(0.0, "a", drawn=10.0, energy_counter=0, price=1.0)
    t.update(10.0, "a", drawn=10.0, energy_counter=100, price=1.0)
    t.update(10.0 + t.IDLE_GRACE + 1, "a", drawn=0.0, energy_counter=100, price=1.0)
    assert t.session_for("a") is None, "the session still closed cleanly"
