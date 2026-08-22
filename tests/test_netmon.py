"""
Link quality tracking.

The number that predicts trouble is loss, not the average round trip: a charger
that answers in 4 ms nine times out of ten is a worse link than one that
answers in 40 ms every time.
"""
import os
import socket
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ctek_njord_sim"))

from app.netmon import LinkMonitor  # noqa: E402


def test_a_reachable_port_is_measured():
    """Probe a real listening socket rather than mocking the measurement."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    host, port = server.getsockname()
    threading.Thread(target=lambda: server.accept(), daemon=True).start()

    m = LinkMonitor()
    ms = m.measure("a", host, port)
    server.close()

    assert ms is not None and ms >= 0
    assert m.stats("a")["loss"] == 0.0
    assert m.stats("a")["samples"] == 1


def test_a_closed_port_counts_as_loss_not_as_zero():
    """
    Recording an unreachable charger as 0 ms would make a dead link look like
    the fastest one on the dashboard.
    """
    m = LinkMonitor()
    # Port 1 on loopback: nothing listens there, and it fails fast.
    assert m.measure("a", "127.0.0.1", 1) is None
    stats = m.stats("a")
    assert stats["latest"] is None
    assert stats["loss"] == 100.0


def test_stats_are_empty_before_anything_is_measured():
    m = LinkMonitor()
    stats = m.stats("never-probed")
    assert stats["samples"] == 0
    assert stats["latest"] is None
    assert stats["loss"] is None


def test_loss_is_the_share_of_probes_that_got_no_answer():
    m = LinkMonitor()
    for ms in (10.0, None, 20.0, None):
        m.record("a", ms)
    stats = m.stats("a")
    assert stats["loss"] == 50.0
    assert stats["avg"] == 15.0
    assert stats["worst"] == 20.0


def test_history_is_bounded():
    m = LinkMonitor(samples=10)
    for i in range(50):
        m.record("a", float(i))
    assert m.stats("a")["samples"] == 10
    assert m.series("a")[-1] == 49.0


def test_series_keeps_gaps_so_a_sparkline_can_show_them():
    m = LinkMonitor()
    m.record("a", 10.0)
    m.record("a", None)
    m.record("a", 12.0)
    assert m.series("a") == [10.0, None, 12.0]
