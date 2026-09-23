"""Sorting Home Assistant entities into the settings pickers."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ctek_njord_sim"))

from app.supervisor import classify_entities  # noqa: E402


def _st(eid, unit=None, state="1"):
    attrs = {"unit_of_measurement": unit} if unit else {}
    return {"entity_id": eid, "state": state, "attributes": attrs}


def test_booleans_are_offered_for_the_charge_enable_gate():
    # The bug: an input_boolean has no unit, so the unit-only filter dropped it
    # and the gate could only be pointed at numeric sensors.
    out = classify_entities([
        _st("input_boolean.forbrukning_tillaten_p_g_a_pris", state="on"),
        _st("switch.garage", state="off"),
        _st("binary_sensor.cheap_hour", state="on"),
        _st("sensor.l1_current", "A"),
    ])
    ids = [e["entity_id"] for e in out["switch"]]
    assert ids[0] == "input_boolean.forbrukning_tillaten_p_g_a_pris"
    assert set(ids) == {"input_boolean.forbrukning_tillaten_p_g_a_pris",
                        "switch.garage", "binary_sensor.cheap_hour"}
    assert [e["entity_id"] for e in out["current"]] == ["sensor.l1_current"]


def test_price_sensors_get_their_own_list():
    out = classify_entities([
        _st("sensor.nordpool", "SEK/kWh"),
        _st("sensor.tibber", "öre/kWh"),
        _st("sensor.power", "kW"),
    ])
    assert {e["entity_id"] for e in out["price"]} == {"sensor.nordpool", "sensor.tibber"}
    assert [e["entity_id"] for e in out["power"]] == ["sensor.power"]
