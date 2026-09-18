"""Binary sensors for Cove (Alula): per-zone open/closed + system troubles.

Zone entities are created from whatever zones the panel reports. Each zone maps to
a door/window/motion/smoke device class based on its device_type / ui_type, falling
back to a generic opening. System-level troubles (battery, power, tamper, connectivity)
are exposed as diagnostic binary sensors on the panel device.
"""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .covealula import PanelState, Zone
from .coordinator import CoveAlulaCoordinator

_LOGGER = logging.getLogger(__name__)


# Map a zone's reported type hint to a Home Assistant device class. The panel's own
# uiType is the most reliable signal (observed values: DWS, Glass Break, CO,
# Home Disaster, Motion, Smoke); the zone name is a good secondary hint.
def _zone_device_class(zone: Zone) -> BinarySensorDeviceClass:
    ui = (zone.ui_type or "").strip().lower()
    name = (zone.name or "").lower()

    # 1) exact-ish uiType classification
    if ui:
        if "motion" in ui or "pir" in ui or "occup" in ui:
            return BinarySensorDeviceClass.MOTION
        if "glass" in ui:
            return BinarySensorDeviceClass.WINDOW
        if ui == "co" or "carbon" in ui:
            return BinarySensorDeviceClass.CO
        if "smoke" in ui or "fire" in ui or "heat" in ui:
            return BinarySensorDeviceClass.SMOKE
        if "flood" in ui or "water" in ui or "leak" in ui or "disaster" in ui:
            return BinarySensorDeviceClass.MOISTURE
        if "garage" in ui:
            return BinarySensorDeviceClass.GARAGE_DOOR
        if "dws" in ui or "door" in ui or "window" in ui or "contact" in ui:
            # door/window sensor: refine using the zone name
            if "window" in name or "glass" in name:
                return BinarySensorDeviceClass.WINDOW
            return BinarySensorDeviceClass.DOOR

    # 2) fall back to the zone name
    if any(k in name for k in ("motion", "pir")):
        return BinarySensorDeviceClass.MOTION
    if "glass" in name or "window" in name:
        return BinarySensorDeviceClass.WINDOW
    if "smoke" in name or "fire" in name:
        return BinarySensorDeviceClass.SMOKE
    if name.endswith(" co") or "carbon" in name or "co detector" in name:
        return BinarySensorDeviceClass.CO
    if "flood" in name or "water" in name or "leak" in name:
        return BinarySensorDeviceClass.MOISTURE
    if "garage" in name:
        return BinarySensorDeviceClass.GARAGE_DOOR
    if "door" in name:
        return BinarySensorDeviceClass.DOOR
    return BinarySensorDeviceClass.OPENING


# System-level trouble sensors: (PanelState attr, name suffix, device class, enabled-by-default)
_SYSTEM_SENSORS = [
    ("alarm", "Alarm", BinarySensorDeviceClass.SAFETY, True),
    ("ready_to_arm", "Ready to arm", None, True),
    ("low_battery", "Low battery", BinarySensorDeviceClass.BATTERY, True),
    ("ac_failure", "AC power", BinarySensorDeviceClass.POWER, True),
    ("tamper", "Tamper", BinarySensorDeviceClass.TAMPER, True),
    ("cs_comm_fail", "Central station comms", BinarySensorDeviceClass.CONNECTIVITY, False),
    ("server_comm_fail", "Server comms", BinarySensorDeviceClass.CONNECTIVITY, False),
    ("siren_trouble", "Siren trouble", BinarySensorDeviceClass.PROBLEM, False),
    ("fire_trouble", "Fire trouble", BinarySensorDeviceClass.PROBLEM, False),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CoveAlulaCoordinator = hass.data[DOMAIN][entry.entry_id]

    known_zones: set[tuple[str, int]] = set()

    def _build_new() -> list[BinarySensorEntity]:
        new: list[BinarySensorEntity] = []
        data = coordinator.data or {}
        for device_id, panel in data.items():
            # the panel reports the highest configured zone index; anything above that is
            # an unused slot (no real sensor) so we don't create phantom entities for it.
            top = panel.highest_zone_index
            for idx, zone in panel.zones.items():
                if top is not None and idx > top:
                    continue
                # skip zones the panel reports as not installed / inactive
                if zone.installed is False or zone.inactive is True:
                    continue
                key = (device_id, idx)
                if key in known_zones:
                    continue
                known_zones.add(key)
                new.append(CoveAlulaZoneSensor(coordinator, entry, device_id, idx))
        return new

    # System trouble sensors (one set per panel)
    entities: list[BinarySensorEntity] = []
    for device_id in (coordinator.data or {}):
        for attr, suffix, devclass, default_on in _SYSTEM_SENSORS:
            entities.append(
                CoveAlulaSystemSensor(
                    coordinator, entry, device_id, attr, suffix, devclass, default_on
                )
            )
    entities.extend(_build_new())
    async_add_entities(entities)

    # Zones can appear after the first refresh; add them as they show up.
    @callback
    def _on_update() -> None:
        new = _build_new()
        if new:
            async_add_entities(new)

    entry.async_on_unload(coordinator.async_add_listener(_on_update))


class _BasePanelEntity(CoordinatorEntity[CoveAlulaCoordinator], BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: CoveAlulaCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id

    @property
    def _panel(self) -> PanelState | None:
        return self.coordinator.data.get(self._device_id) if self.coordinator.data else None

    @property
    def device_info(self) -> DeviceInfo:
        p = self._panel
        friendly = (p.panel_name or p.name) if p else None
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer="Alula (Cove)",
            name=(friendly or "Cove Alarm"),
            model="Helix",
            serial_number=(p.serial_number if p else None),
            sw_version=(p.firmware_version if p else None),
        )

    @property
    def available(self) -> bool:
        # Ride over brief connection lapses. Once we've seen this panel, stay available as
        # long as the coordinator has had healthy contact within the grace window, even if
        # the current poll/socket momentarily dropped. This prevents the 2-second
        # "unavailable" blips (and the resulting history/activity noise) on every socket
        # recycle. A sustained outage ages out of the grace window and correctly reports
        # unavailable. Panel-reported-offline is folded into the grace via _mark_healthy,
        # which only stamps contact while the panel is online.
        return self._panel is not None and self.coordinator.panel_is_fresh(self._device_id)


class CoveAlulaZoneSensor(_BasePanelEntity):
    """A single zone/sensor as an open/closed binary sensor."""

    def __init__(self, coordinator, entry, device_id: str, index: int) -> None:
        super().__init__(coordinator, device_id)
        self._index = index
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_zone_{index}"

    @property
    def _zone(self) -> Zone | None:
        p = self._panel
        return p.zones.get(self._index) if p else None

    @property
    def name(self) -> str | None:
        z = self._zone
        if z and z.name:
            return z.name
        return f"Zone {self._index}"

    @property
    def device_class(self) -> BinarySensorDeviceClass | None:
        z = self._zone
        return _zone_device_class(z) if z else None

    @property
    def is_on(self) -> bool | None:
        z = self._zone
        if z is None or z.open is None:
            return None
        return bool(z.open)  # on == open/detected

    @property
    def extra_state_attributes(self) -> dict:
        z = self._zone
        if z is None:
            return {}
        return {
            "zone_index": z.index,
            "bypassed": z.bypassed,
            "alarm": z.alarm,
            "tamper": z.tamper,
            "low_battery": z.low_battery,
            "trouble": z.trouble,
            "signal_level": z.signal_level,
            "device_type": z.device_type,
        }


class CoveAlulaSystemSensor(_BasePanelEntity):
    """A panel-level status/trouble flag."""

    def __init__(self, coordinator, entry, device_id: str, attr: str, suffix: str,
                 devclass, default_on: bool) -> None:
        super().__init__(coordinator, device_id)
        self._attr = attr
        self._attr_name = suffix
        self._attr_device_class = devclass
        self._attr_entity_registry_enabled_default = default_on
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{attr}"
        # Troubles are diagnostics; "ready to arm" and "alarm" are operational.
        if attr not in ("ready_to_arm", "alarm"):
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool | None:
        p = self._panel
        if p is None:
            return None
        # "ready_to_arm" uses the derived/preferred ready value (on = ready); the panel
        # may not report it directly, so PanelState.ready falls back to zone state.
        if self._attr == "ready_to_arm":
            return p.ready
        val = getattr(p, self._attr, None)
        if val is None:
            return None
        return bool(val)

    @property
    def extra_state_attributes(self) -> dict:
        if self._attr != "ready_to_arm":
            return {}
        p = self._panel
        if p is None:
            return {}
        return {"open_zones": [z.name or f"zone {z.index}" for z in p.not_ready_zones]}
