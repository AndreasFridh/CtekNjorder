"""
Trust-boundary tests.

Two things reach this add-on from outside and must not be trusted:

1. The charger's MQTT broker. It accepts anonymous publishes from anywhere on
   the network (verified with tools/test_write.py), so anyone on the LAN can
   retain a topic containing whatever they like. The charger serial is parsed
   out of a topic name and then rendered in the web UI.
2. The web UI's own API, which is reachable by anything on the Supervisor's
   Docker network, not only by the authenticated user going through Ingress.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ctek_njord_sim"))

from app.ctek import SERIAL_RE, CtekClient  # noqa: E402
from app.optionspec import BY_KEY, coerce  # noqa: E402
from app.web import KEEP_SECRET  # noqa: E402


def client():
    # port 1 so nothing ever actually connects during the test
    return CtekClient(host="127.0.0.1", port=1, dry_run=True)


# ---------- the serial arrives inside an attacker-writable topic name ----------

@pytest.mark.parametrize("serial", [
    "40000A00X0000001",
    "40000B00Y0000002",
    "abc1",
    "A-b_c.1",
])
def test_plausible_serials_are_accepted(serial):
    assert SERIAL_RE.match(serial)


@pytest.mark.parametrize("payload", [
    "<img src=x onerror=alert(1)>",
    '"><script>alert(1)</script>',
    "../../etc/passwd",
    "a/b",                      # would forge a different topic level
    "with space",
    "",
    "abc",                      # too short to be a real serial
    "x" * 200,
])
def test_hostile_serials_are_rejected(payload):
    assert not SERIAL_RE.match(payload)


def test_hostile_serial_is_not_adopted_even_if_it_arrives_first():
    """
    A retained hostile topic wins the race against the real charger, because
    retained messages are delivered the instant we subscribe. Losing that race
    must not mean adopting the attacker's value.
    """
    c = client()
    c._bind("<img src=x onerror=alert(document.domain)>")
    assert c.topics is None, "hostile serial was adopted"

    c._bind("40000A00X0000001")
    assert c.topics is not None
    assert c.topics.charger == "40000A00X0000001"


def test_a_bound_serial_cannot_be_hijacked_afterwards():
    c = client()
    c._bind("40000A00X0000001")
    c._bind("../../evil")
    assert c.topics.charger == "40000A00X0000001"


def test_topics_built_from_a_valid_serial_stay_on_their_own_prefix():
    c = client()
    c._bind("40000A00X0000001")
    for topic in c.topics.subscriptions() + [c.topics.control_current]:
        assert topic.startswith("ctek/")
        assert ".." not in topic
        assert "//" not in topic


# ---------- stored secrets must not come back out of the API ----------

def test_the_password_placeholder_is_not_a_usable_password():
    """
    The UI is served the placeholder instead of the stored secret. If it were
    ever accepted as a real value it would silently become the password.
    """
    assert KEEP_SECRET
    assert "•" in KEEP_SECRET
    assert BY_KEY["charger_password"].type == "password"


def test_coerce_still_accepts_a_genuine_new_password():
    """Redaction must not stop the user actually setting one."""
    assert coerce("charger_password", "hunter2") == "hunter2"


# ---------- option coercion is the API's input validation ----------

@pytest.mark.parametrize("key,value", [
    ("main_fuse", 0),          # below the schema minimum
    ("main_fuse", 999),        # above it
    ("main_fuse", ""),         # empty
    ("safety_margin", 99),
    ("phase_rotation", "'; DROP TABLE"),
    ("current_entities", ["only", "two"]),
])
def test_out_of_range_and_junk_values_are_refused(key, value):
    with pytest.raises(ValueError):
        coerce(key, value)


def test_unknown_option_keys_are_refused():
    with pytest.raises(KeyError):
        coerce("definitely_not_an_option", 1)


def test_numeric_options_cannot_smuggle_a_string_through():
    with pytest.raises(ValueError):
        coerce("main_fuse", "twenty-five")


# ---------- a reconnect has to re-subscribe ----------

def test_binding_twice_resubscribes():
    """
    We connect with a clean session, so the broker forgets our subscriptions
    the instant the link drops. A reconnect that skipped re-subscribing left
    the charger permanently silent while still looking bound - and still
    holding an allocation it could no longer justify.
    """
    class Recorder:
        def __init__(self):
            self.subscribed = []

        def subscribe(self, topic, qos=0):
            self.subscribed.append(topic)

        def publish(self, *a, **kw):
            pass

    c = client()
    c._c = Recorder()

    c._bind("40000A00X0000001")
    first = len(c._c.subscribed)
    assert first > 0

    c._announced = False          # as a real disconnect resets it
    c._bind("40000A00X0000001")   # the reconnect
    assert len(c._c.subscribed) == first * 2, "reconnect did not re-subscribe"


def test_a_second_charger_cannot_hijack_a_bound_client():
    c = client()
    c._bind("40000A00X0000001")
    c._bind("40353I37W4009999")
    assert c.topics.charger == "40000A00X0000001"
