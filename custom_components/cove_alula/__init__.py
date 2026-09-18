"""Cove (Alula) Alarm integration setup."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_BYPASS,
    ATTR_METHOD,
    ATTR_MODE,
    ATTR_ZONE,
    CONF_PIN,
    DOMAIN,
    PLATFORMS,
    SERVICE_BYPASS_ZONE,
    SERVICE_CANCEL_ALARM,
    SERVICE_CONFIRM_ALARM,
    SERVICE_FORCE_ARM,
)
from .coordinator import CoveAlulaCoordinator
from .covealula import LEVEL_AWAY, LEVEL_NIGHT, LEVEL_STAY

# arm mode -> armingLevelValue. Sourced from the client constants so the away/night
# assignment can never drift out of sync with the arm/read mapping there.
_MODE_LEVEL = {"home": LEVEL_STAY, "stay": LEVEL_STAY, "away": LEVEL_AWAY, "night": LEVEL_NIGHT}


def _coordinators(hass: HomeAssistant) -> list[CoveAlulaCoordinator]:
    return list(hass.data.get(DOMAIN, {}).values())


async def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_CANCEL_ALARM):
        return

    async def _for_each_panel(handler) -> None:
        for coord in _coordinators(hass):
            for device_id in (coord.data or {}):
                await handler(coord, device_id)

    async def _cancel(call: ServiceCall) -> None:
        async def h(coord, device_id):
            await coord.client.async_cancel_alarm(device_id)
        await _for_each_panel(h)

    async def _confirm(call: ServiceCall) -> None:
        async def h(coord, device_id):
            await coord.client.async_confirm_alarm(device_id)
        await _for_each_panel(h)

    async def _bypass(call: ServiceCall) -> None:
        zone = int(call.data[ATTR_ZONE])
        bypass = bool(call.data.get(ATTR_BYPASS, True))

        async def h(coord, device_id):
            await coord.client.async_set_zone_bypass(device_id, zone, bypass)
            await coord.client.request_zone_statuses(device_id, 0, 63)
        await _for_each_panel(h)
        for coord in _coordinators(hass):
            await coord.async_request_refresh()

    async def _force_arm(call: ServiceCall) -> None:
        mode = str(call.data[ATTR_MODE]).lower()
        method = str(call.data.get(ATTR_METHOD, "bypass")).lower()
        level = _MODE_LEVEL.get(mode)
        if level is None:
            return

        async def h(coord, device_id):
            pin = coord.entry.data.get(CONF_PIN)
            if method == "native":
                # panel's native forceArm (no PIN, attributed to user 0)
                await coord.client.async_force_arm(device_id, level)
            else:
                # bypass any open zones, then arm with the stored PIN (most reliable)
                await coord.client.async_arm_bypassing_open(device_id, level, pin)
        await _for_each_panel(h)
        for coord in _coordinators(hass):
            await coord.async_request_refresh()

    hass.services.async_register(DOMAIN, SERVICE_CANCEL_ALARM, _cancel)
    hass.services.async_register(DOMAIN, SERVICE_CONFIRM_ALARM, _confirm)
    hass.services.async_register(
        DOMAIN,
        SERVICE_BYPASS_ZONE,
        _bypass,
        schema=vol.Schema(
            {
                vol.Required(ATTR_ZONE): vol.Coerce(int),
                vol.Optional(ATTR_BYPASS, default=True): cv.boolean,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FORCE_ARM,
        _force_arm,
        schema=vol.Schema(
            {
                vol.Required(ATTR_MODE): vol.In(["home", "stay", "away", "night"]),
                vol.Optional(ATTR_METHOD, default="bypass"): vol.In(["bypass", "native"]),
            }
        ),
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Cove (Alula) from a config entry."""
    coordinator = CoveAlulaCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await _async_register_services(hass)

    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: CoveAlulaCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
        # remove services when the last entry goes away
        if not hass.data.get(DOMAIN):
            for svc in (SERVICE_CANCEL_ALARM, SERVICE_CONFIRM_ALARM, SERVICE_BYPASS_ZONE,
                        SERVICE_FORCE_ARM):
                if hass.services.has_service(DOMAIN, svc):
                    hass.services.async_remove(DOMAIN, svc)
    return unload_ok


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload only when a reload-worthy field changed (credentials / PIN).

    The access token is refreshed and written back to the entry about every 14 minutes.
    That write fires this update listener, and reloading the whole integration each time
    tore down and rebuilt every entity -- surfacing as recurring 2-3s "unavailable" blips
    in the history. Skip the reload for those token-only writes.
    """
    coordinator: CoveAlulaCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None and not coordinator.config_changed(entry):
        return
    await hass.config_entries.async_reload(entry.entry_id)
