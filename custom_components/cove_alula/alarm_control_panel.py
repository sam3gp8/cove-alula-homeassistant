"""Alarm control panel for Cove (Alula)."""

from __future__ import annotations

import logging

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_PIN, DOMAIN
from .covealula import (
    LEVEL_AWAY,
    LEVEL_DISARM,
    LEVEL_NIGHT,
    LEVEL_STAY,
    LEVEL_UNKNOWN,
    PanelState,
)
from .coordinator import CoveAlulaCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: CoveAlulaCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        CoveAlulaAlarmPanel(coordinator, entry, device_id)
        for device_id in coordinator.client.panels
    ]
    async_add_entities(entities)


class CoveAlulaAlarmPanel(CoordinatorEntity[CoveAlulaCoordinator], AlarmControlPanelEntity):
    """A Cove/Alula Helix panel as a Home Assistant alarm control panel."""

    _attr_has_entity_name = True
    _attr_name = None  # use the device name
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_NIGHT
    )
    # PIN is stored in config and applied automatically, so automations can
    # arm/disarm without entering a code in the UI.
    _attr_code_arm_required = False
    _attr_code_format = None

    def __init__(
        self,
        coordinator: CoveAlulaCoordinator,
        entry: ConfigEntry,
        device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._pin = entry.data[CONF_PIN]
        self._attr_unique_id = f"{entry.entry_id}_{device_id}"
        # Held across brief transitional reads (see alarm_state) so the entity never
        # blanks to "unknown" mid-command, which hides the Lovelace card's action buttons.
        self._last_known_state: AlarmControlPanelState | None = None

    @property
    def _panel(self) -> PanelState | None:
        return self.coordinator.data.get(self._device_id) if self.coordinator.data else None

    @property
    def available(self) -> bool:
        p = self._panel
        return super().available and p is not None and (p.online is not False)

    @property
    def device_info(self) -> DeviceInfo:
        p = self._panel
        friendly = None
        if p:
            friendly = p.panel_name or p.name
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            manufacturer="Alula (Cove)",
            name=(friendly or "Cove Alarm"),
            model="Helix",
            serial_number=(p.serial_number if p else None),
            sw_version=(p.firmware_version if p else None),
        )

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        p = self._panel
        if p is None:
            return None
        if p.alarm:
            self._last_known_state = AlarmControlPanelState.TRIGGERED
            return self._last_known_state
        if p.in_entry_delay:
            self._last_known_state = AlarmControlPanelState.PENDING
            return self._last_known_state
        if p.in_exit_delay:
            self._last_known_state = AlarmControlPanelState.ARMING
            return self._last_known_state
        mapped = {
            LEVEL_DISARM: AlarmControlPanelState.DISARMED,
            LEVEL_STAY: AlarmControlPanelState.ARMED_HOME,
            LEVEL_AWAY: AlarmControlPanelState.ARMED_AWAY,
            LEVEL_NIGHT: AlarmControlPanelState.ARMED_NIGHT,
        }.get(p.arming_level)
        if mapped is not None:
            self._last_known_state = mapped
            return mapped
        # arming_level is LEVEL_UNKNOWN (0) or some other unmapped value: the panel
        # reports this as a real transitional reading (e.g. right after exit delay
        # ends, before the settled arm/disarm level lands), not a "no data" case.
        # Keep showing the last real state instead of blanking to `unknown`, which
        # would otherwise hide the Lovelace alarm card's action buttons -- including
        # Disarm -- for as long as the transitional value persists.
        if p.arming_level == LEVEL_UNKNOWN:
            _LOGGER.debug(
                "device %s: arming_level is LEVEL_UNKNOWN, holding last known state %s",
                self._device_id, self._last_known_state,
            )
        return self._last_known_state

    @property
    def extra_state_attributes(self) -> dict:
        p = self._panel
        if p is None:
            return {}
        return {
            "arming_level": p.arming_level,
            "ready_to_arm": p.ready,
            "open_zones": [z.name or f"zone {z.index}" for z in p.not_ready_zones],
            "bypassed_zones": p.bypassed_zones,
            "alarm_type": p.alarm_type,
            "low_battery": p.low_battery,
            "ac_failure": p.ac_failure,
        }

    # ---- commands (PIN supplied from config) ----------------------------

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self.coordinator.client.async_disarm(self._device_id, code or self._pin)
        await self.coordinator.async_request_refresh()

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self.coordinator.client.async_arm_stay(self._device_id, code or self._pin)
        await self.coordinator.async_request_refresh()

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self.coordinator.client.async_arm_away(self._device_id, code or self._pin)
        await self.coordinator.async_request_refresh()

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        await self.coordinator.client.async_arm_night(self._device_id, code or self._pin)
        await self.coordinator.async_request_refresh()
