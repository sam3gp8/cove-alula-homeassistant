"""Alarm control panel for Cove (Alula)."""

from __future__ import annotations

import logging

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_PIN,
    CONF_PIN_MODE,
    DOMAIN,
    PIN_MODE_PROMPT,
    PIN_MODE_READONLY,
    PIN_MODE_STORED,
)
from .covealula import (
    LEVEL_AWAY,
    LEVEL_DISARM,
    LEVEL_NIGHT,
    LEVEL_STAY,
    PanelState,
)
from .coordinator import CoveAlulaCoordinator

_ARM_FEATURES = (
    AlarmControlPanelEntityFeature.ARM_HOME
    | AlarmControlPanelEntityFeature.ARM_AWAY
    | AlarmControlPanelEntityFeature.ARM_NIGHT
)

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

    def __init__(
        self,
        coordinator: CoveAlulaCoordinator,
        entry: ConfigEntry,
        device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._pin_mode = entry.data.get(CONF_PIN_MODE, PIN_MODE_STORED)
        self._pin = (entry.data.get(CONF_PIN) or "") or None
        self._attr_unique_id = f"{entry.entry_id}_{device_id}"

        if self._pin_mode == PIN_MODE_READONLY:
            # Monitor only: expose panel state for automations, but no arm/disarm in the UI.
            self._attr_supported_features = AlarmControlPanelEntityFeature(0)
            self._attr_code_arm_required = False
            self._attr_code_format = None
        elif self._pin_mode == PIN_MODE_PROMPT:
            # No stored PIN: Home Assistant prompts for the native Cove/Alula PIN on each
            # arm/disarm and passes it through as the code.
            self._attr_supported_features = _ARM_FEATURES
            self._attr_code_arm_required = True
            self._attr_code_format = CodeFormat.NUMBER
        else:  # PIN_MODE_STORED
            # Stored PIN is applied automatically, so automations can arm/disarm without
            # entering a code in the UI.
            self._attr_supported_features = _ARM_FEATURES
            self._attr_code_arm_required = False
            self._attr_code_format = None

    @property
    def _panel(self) -> PanelState | None:
        return self.coordinator.data.get(self._device_id) if self.coordinator.data else None

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
            return AlarmControlPanelState.TRIGGERED
        if p.in_entry_delay:
            return AlarmControlPanelState.PENDING
        if p.in_exit_delay:
            return AlarmControlPanelState.ARMING
        return {
            LEVEL_DISARM: AlarmControlPanelState.DISARMED,
            LEVEL_STAY: AlarmControlPanelState.ARMED_HOME,
            LEVEL_AWAY: AlarmControlPanelState.ARMED_AWAY,
            LEVEL_NIGHT: AlarmControlPanelState.ARMED_NIGHT,
        }.get(p.arming_level, None)

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

    # ---- commands -------------------------------------------------------

    def _resolve_pin(self, code: str | None) -> str:
        """Pick the PIN to send, honoring the configured PIN mode.

        - readonly: arm/disarm are disabled entirely.
        - prompt:   the code typed in the UI is the PIN (nothing is stored).
        - stored:   the stored PIN is used; a UI code, if given, takes precedence.
        """
        if self._pin_mode == PIN_MODE_READONLY:
            raise HomeAssistantError(
                "Cove (Alula) is configured in read-only mode: no PIN is stored, so "
                "arming and disarming from Home Assistant are disabled."
            )
        pin = code or self._pin
        if not pin:
            raise HomeAssistantError(
                "No PIN available for this command. Enter your Cove/Alula PIN in the "
                "dialog, or reconfigure the integration to store a PIN."
            )
        return pin

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self.coordinator.client.async_disarm(self._device_id, self._resolve_pin(code))
        await self.coordinator.async_request_refresh()

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self.coordinator.client.async_arm_stay(self._device_id, self._resolve_pin(code))
        await self.coordinator.async_request_refresh()

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self.coordinator.client.async_arm_away(self._device_id, self._resolve_pin(code))
        await self.coordinator.async_request_refresh()

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        await self.coordinator.client.async_arm_night(self._device_id, self._resolve_pin(code))
        await self.coordinator.async_request_refresh()
