"""Switches for Cove (Alula): arm/disarm modifiers surfaced in the UI.

Home Assistant's alarm_control_panel more-info dialog has no room for extra options, so the
"silent" and "no entry delay" flags the panel supports are exposed here as config switches on
the panel device. The alarm entity reads their state when arming/disarming from the UI or the
standard alarm services, so you can arm or disarm silently without calling the YAML services.

State is remembered across restarts, so leaving "Silent arming" on keeps every arm/disarm
silent until you turn it back off.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

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


class CoveAlulaModifierSwitch(RestoreEntity, SwitchEntity):
    """A sticky arm/disarm modifier (e.g. Silent arming) stored on the coordinator."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

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
        self._store(False)

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

    async def async_added_to_hass(self) -> None:
        """Restore the last on/off state so a user's silent preference survives restarts."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            self._store(last.state == "on")
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._store(True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._store(False)
        self.async_write_ha_state()
