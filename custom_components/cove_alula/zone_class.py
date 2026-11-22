"""Map Cove/Alula zone ui_type strings to Home Assistant binary_sensor device classes.

Pure module (no Home Assistant imports) so it can be unit-tested on its own.
The keys are the exact ui_type strings the Cove Android app (4.3.x) uses for zones:
Door, Window, Glassbreak, Fire, Tilt, Motion, Panic, High Temp, Low Temp, Water, CO,
Medical, Arming (keyswitch), Tamper, Gate, Safe. A few spellings seen from the API
("Glass Break", "DWS") are handled too. Values are BinarySensorDeviceClass values.
"""

from __future__ import annotations

_EXACT: dict[str, str] = {
    "door": "door",
    "window": "window",
    "glassbreak": "sound",
    "glass break": "sound",
    "fire": "smoke",
    "smoke": "smoke",
    "tilt": "garage_door",
    "garage door": "garage_door",
    "motion": "motion",
    "panic": "safety",
    "medical": "safety",
    "high temp": "heat",
    "low temp": "cold",
    "freeze": "cold",
    "water": "moisture",
    "flood": "moisture",
    "co": "carbon_monoxide",
    "tamper": "tamper",
    "gate": "opening",
    "safe": "opening",
}


def normalize(ui_type: str | None) -> str:
    """Lower-case, underscores to spaces, collapse whitespace."""
    return " ".join((ui_type or "").replace("_", " ").lower().split())


def exact_class(ui_type: str | None) -> str | None:
    """Device class for a known Cove ui_type, or None to fall back to heuristics."""
    return _EXACT.get(normalize(ui_type))


def name_is_cold(name: str | None) -> bool:
    """Zone-name fallback for freeze sensors, checked before the water/leak fallback."""
    n = (name or "").lower()
    return "low temp" in n or "freeze" in n or "freezing" in n
