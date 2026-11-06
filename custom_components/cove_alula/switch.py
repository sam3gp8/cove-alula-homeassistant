"""Switches for Cove (Alula): live arm/disarm modifiers.

Home Assistant's alarm_control_panel more-info dialog has no room for extra options, so the
"silent" and "no entry delay" flags the panel supports are exposed as switches on the panel
device. The alarm entity reads them when arming/disarming from the panel's own buttons or the
standard alarm services, so you can arm or disarm silently without the YAML services.

These are a live, in-session override. Their starting value comes from the persistent
"Arm silently" / "No entry delay" options (Configure screen), and they reset to that default
on restart. Set the option to make this panel silent by default; flip the switch for one-off
changes. The option alone works even if you never touch these switches.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_PIN_MODE, DOMAIN, PIN_MODE_READONLY, PIN_MODE_STORED, get_setting
from .covealula import PanelState
from .coordinator import CoveAlulaCoordinator

# attribute on the coordinator <-> (name, icon) for each modifier switch
_SWITCHES = [
    ("arm_silent", "Silent arming", "mdi:volume-off"),
    ("arm_no_entry_delay", "No entry delay", "mdi:timer-off-outline"),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CoveAlulaCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Read-only mode has no arm/disarm, so these modifiers would do nothing.
    if get_setting(entry, CONF_PIN_MODE, PIN_MODE_STORED) == PIN_MODE_READONLY:
        return

    entities: list[SwitchEntity] = []
    for device_id in coordinator.client.panels:
        for attr, name, icon in _SWITCHES:
            entities.append(
                CoveAlulaModifierSwitch(coordinator, entry, device_id, attr, name, icon)
            )
    async_add_entities(entities)


class CoveAlulaModifierSwitch(SwitchEntity):
    """A live arm/disarm modifier (e.g. Silent arming) stored on the coordinator.

    Seeded from the matching persistent option, and resets to it on restart.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: CoveAlulaCoordinator,
        entry: ConfigEntry,
        device_id: str,
        attr: str,
        name: str,
        icon: str,
    ) -> None:
        self._coordinator = coordinator
        self._device_id = device_id
        self._attr_name = name
        self._attr = attr
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{attr}"
        # Seed from the persistent option so the switch starts at the configured default.
        self._store(bool(get_setting(entry, attr, False)))

    def _store(self, value: bool) -> None:
        getattr(self._coordinator, self._attr)[self._device_id] = value

    @property
    def _panel(self) -> PanelState | None:
        data = self._coordinator.data
        return data.get(self._device_id) if data else None

    @property
    def is_on(self) -> bool:
        return bool(getattr(self._coordinator, self._attr).get(self._device_id, False))

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

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._store(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._store(False)
        self.async_write_ha_state()
