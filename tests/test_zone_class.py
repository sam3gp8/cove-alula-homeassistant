"""Unit tests for the pure ui_type -> device class map (no Home Assistant needed)."""

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "zone_class", Path(__file__).parents[1] / "custom_components/cove_alula/zone_class.py"
)
zc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(zc)


def test_every_cove_app_type_maps():
    expected = {
        "Door": "door", "Window": "window", "Glassbreak": "sound", "Fire": "smoke",
        "Tilt": "garage_door", "Motion": "motion", "Panic": "safety", "High Temp": "heat",
        "Low Temp": "cold", "Water": "moisture", "CO": "carbon_monoxide", "Medical": "safety",
        "Tamper": "tamper", "Gate": "opening", "Safe": "opening",
    }
    for ui, cls in expected.items():
        assert zc.exact_class(ui) == cls, ui


def test_spelling_variants_and_unknowns():
    assert zc.exact_class("Glass Break") == "sound"
    assert zc.exact_class("  low_temp ") == "cold"
    assert zc.exact_class("DWS") is None  # door/window sensor: keep the name-based refinement
    assert zc.exact_class("Arming (keyswitch)") is None
    assert zc.exact_class(None) is None
    assert zc.exact_class("") is None


def test_freeze_name_fallback():
    assert zc.name_is_cold("Water Heater Low Temp")
    assert zc.name_is_cold("Kitchen freeze sensor")
    assert not zc.name_is_cold("Water Heater")
    assert not zc.name_is_cold(None)


def test_device_classes_are_real_ha_values():
    # Every value of homeassistant.components.binary_sensor.BinarySensorDeviceClass (2026.11).
    ha_values = {
        "battery", "battery_charging", "carbon_monoxide", "cold", "connectivity", "door",
        "garage_door", "gas", "heat", "light", "lock", "moisture", "motion", "moving",
        "occupancy", "opening", "plug", "power", "presence", "problem", "running", "safety",
        "smoke", "sound", "tamper", "update", "vibration", "window",
    }
    assert set(zc._EXACT.values()) <= ha_values
