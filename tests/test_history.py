"""
Chart history: two tiers, one of which outlives the process.

The live tier is memory-only at full resolution. The long tier is downsampled
to minute buckets and persisted, which is what makes the charts survive an
add-on restart. Both the downsampling and the restart path have failure modes
that are invisible until someone actually looks at a chart, so they are pinned
here.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ctek_njord_sim"))

from app.history import LIVE_SECONDS, History  # noqa: E402


@pytest.fixture
def data_dir(tmp_path):
    return str(tmp_path)


def fill(h, seconds, start=None, house=None, car=None, setpoint=10):
    """Feed `seconds` of 1 Hz samples ending now."""
    start = start if start is not None else time.time() - seconds
    for i in range(seconds):
        h.add(start + i, list(house or [4.0, 4.0, 4.0]),
              list(car or [10.0, 10.0, 10.0]), setpoint)
    return h


# ---------- the live tier ----------

def test_live_tier_is_bounded(data_dir):
    h = History(data_dir)
    fill(h, LIVE_SECONDS + 500)
    assert len(h.live) == LIVE_SECONDS, "live tier must not grow without bound"


# ---------- downsampling must not hide the interesting moments ----------

def test_a_brief_spike_survives_downsampling(data_dir):
    """
    Averaging would erase exactly the events worth charting. Buckets keep the
    worst value seen, so a one-second peak is still visible a week later.
    """
    h = History(data_dir)
    now = time.time() - 3600
    for i in range(3600):
        house = [24.0, 4.0, 4.0] if i == 1234 else [4.0, 4.0, 4.0]
        h.add(now + i, house, [10.0, 10.0, 10.0], 10)
    h.close()
    assert max(r[1][0] for r in h.long if r[1]) == 24.0


def test_buckets_keep_the_most_restrictive_setpoint(data_dir):
    """A momentary pause matters more than the value either side of it."""
    h = History(data_dir)
    now = time.time() - 300
    for i in range(300):
        h.add(now + i, [4.0, 4.0, 4.0], [10.0, 10.0, 10.0], 0 if i == 42 else 16)
    h.close()
    assert min(r[3] for r in h.long if r[3] is not None) == 0


def test_one_bucket_per_minute(data_dir):
    h = History(data_dir)
    fill(h, 600)
    h.close()
    assert 9 <= len(h.long) <= 11, f"expected ~10 buckets for 10 minutes, got {len(h.long)}"


# ---------- surviving a restart ----------

def test_history_survives_a_restart(data_dir):
    h = History(data_dir)
    fill(h, 3600)
    h.close()
    before = len(h.long)

    reopened = History(data_dir)
    assert len(reopened.long) == before
    assert reopened.live == reopened.live.__class__(), "live tier is not persisted"


def test_the_default_view_is_not_blank_right_after_a_restart(data_dir):
    """
    The live tier starts empty, so a 30-minute view would show nothing for the
    first half hour - precisely when someone restarting wants to see what
    happened. It must fall back to the persisted buckets.
    """
    h = History(data_dir)
    fill(h, 3600)
    h.close()

    fresh = History(data_dir)
    series = fresh.series(30)
    assert series["t"], "default view was blank after a restart"
    assert series["resolution"] == "1m"


def test_the_live_tier_takes_over_once_it_covers_the_window(data_dir):
    h = History(data_dir)
    fill(h, 3600)
    h.close()

    fresh = History(data_dir)
    fill(fresh, LIVE_SECONDS)
    series = fresh.series(30)
    assert series["resolution"] == "1s", "should prefer full resolution once available"


# ---------- the file is not a database, and may be damaged ----------

def test_corrupt_lines_are_skipped_not_fatal(data_dir):
    h = History(data_dir)
    fill(h, 300)
    h.close()

    with open(h.path, "a", encoding="utf-8") as f:
        f.write("this is not json\n")
        f.write('{"half": \n')

    reopened = History(data_dir)
    assert reopened.long, "a damaged line should not lose the whole history"


def test_old_rows_are_dropped_on_load(data_dir):
    path = os.path.join(data_dir, "history.jsonl")
    ancient = time.time() - 30 * 24 * 3600
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps([ancient, [4, 4, 4], [10, 10, 10], 16]) + "\n")
        f.write(json.dumps([time.time() - 60, [4, 4, 4], [10, 10, 10], 16]) + "\n")

    reopened = History(data_dir)
    assert len(reopened.long) == 1, "rows past the retention window should be dropped"


def test_an_unwritable_data_dir_does_not_take_the_balancer_down(data_dir):
    """Losing charts is acceptable. Crashing the load balancer is not."""
    h = History(data_dir)
    h.path = os.path.join(data_dir, "nope", "\0invalid", "history.jsonl")
    fill(h, 120)
    h.close()          # must not raise
    assert len(h.long) > 0, "in-memory history should still work"


def test_no_data_dir_at_all_is_tolerated(tmp_path):
    """Running outside the Supervisor, /data does not exist."""
    h = History(str(tmp_path / "does-not-exist"))
    fill(h, 120)
    h.close()
    assert h.series(30)["t"]
