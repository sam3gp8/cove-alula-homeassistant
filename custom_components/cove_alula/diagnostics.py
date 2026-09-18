"""Diagnostics support for the Cove (Alula) Alarm integration.

Adds a "Download diagnostics" button on the integration's config-entry and device pages.

This is a security system, so the file is redacted with two goals: never leak credentials,
and never map the home. Redacted: account number / email / password / PIN / token, the
panel's device id and friendly name, and zone names. Everything needed to debug the
integration is kept -- connection and token state, which panel-data reads the panel
answered vs NAKed vs never returned, arming level, troubles, and per-zone structure and
state -- keyed by zone index instead of name.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.loader import async_get_integration

from .const import CONF_EMAIL, CONF_PASSWORD, CONF_PIN, CONF_TOKEN, DOMAIN
from .covealula import LEVEL_AWAY, LEVEL_DISARM, LEVEL_NIGHT, LEVEL_STAY, PanelState

REDACTED = "**REDACTED**"

# config-entry data/options keys scrubbed recursively by key name. CONF_TOKEN redacts the
# whole persisted token dict (access_token, refresh_token, expiry) in one shot.
TO_REDACT = {
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_PIN,
    CONF_TOKEN,
    "access_token",
    "refresh_token",
    "username",
    "account",
    "account_number",
    "serial",
    "serial_number",
}

_ARMING_LABEL = {
    LEVEL_DISARM: "disarmed",
    LEVEL_STAY: "armed_home",
    LEVEL_NIGHT: "armed_night",
    LEVEL_AWAY: "armed_away",
}


def _zone_dict(zone: Any) -> dict[str, Any]:
    """One zone, by index. Name is intentionally omitted (reveals home layout);
    device_type + ui_type are what drive sensor classification, so they stay."""
    return {
        "index": zone.index,
        "device_type": zone.device_type,
        "ui_type": zone.ui_type,
        "open": zone.open,
        "bypassed": zone.bypassed,
        "alarm": zone.alarm,
        "tamper": zone.tamper,
        "low_battery": zone.low_battery,
        "trouble": zone.trouble,
        "installed": zone.installed,
        "inactive": zone.inactive,
        "signal_level": zone.signal_level,
    }


def _panel_dict(ps: PanelState) -> dict[str, Any]:
    """One panel, with id and friendly name redacted, state and troubles kept."""
    configured = ps._configured_zones()
    return {
        "device_id": REDACTED,
        "name": REDACTED,  # user-chosen friendly name may contain personal info
        "connected_panel": ps.connected_panel,  # Alula panel family id (not sensitive)
        "supports_partition_arming": ps.supports_partition_arming,
        "online": ps.online,
        "firmware_version": ps.firmware_version,
        "arming_level": ps.arming_level,
        "arming_state": _ARMING_LABEL.get(ps.arming_level),
        "ready_to_arm": ps.ready,
        "in_entry_delay": ps.in_entry_delay,
        "in_exit_delay": ps.in_exit_delay,
        "alarm": ps.alarm,
        "alarm_type": ps.alarm_type,
        "troubles": {
            "low_battery": ps.low_battery,
            "ac_failure": ps.ac_failure,
            "tamper": ps.tamper,
            "cs_comm_fail": ps.cs_comm_fail,
            "server_comm_fail": ps.server_comm_fail,
            "siren_trouble": ps.siren_trouble,
            "fire_trouble": ps.fire_trouble,
        },
        "highest_zone_index": ps.highest_zone_index,
        "configured_zone_count": len(configured),
        "not_ready_zone_count": len(ps.not_ready_zones),
        "zones": [_zone_dict(z) for z in sorted(configured, key=lambda z: z.index)],
    }


def _base(hass: HomeAssistant, entry: ConfigEntry, version: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "integration": {"domain": DOMAIN, "version": version},
        "note": (
            "Credentials, the panel device id, and all panel/zone names are redacted. "
            "Zones are identified by index; state and troubles are preserved for debugging."
        ),
        "entry": {
            "title": REDACTED,  # entry title is the account number
            "version": entry.version,
            "state": str(entry.state),
            # setup error message, if the entry failed to load (helps debug auth/setup)
            "error_reason": (str(entry.reason)[:300] if entry.reason else None),
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
    }

    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is None:
        # entry failed to set up: no live client yet. Return what we can so the report is
        # still useful for diagnosing the failure (state + error_reason above).
        data["connection"] = {"status": "not set up (integration failed to load)"}
        return data

    client = coordinator.client
    token = client.token
    data["connection"] = {
        "ws_connected": client._connected.is_set(),
        "ws_loop_running": bool(client._ws_task and not client._ws_task.done()),
        "watchdog_running": bool(
            client._watchdog_task and not client._watchdog_task.done()
        ),
        "subscribed_device_count": len(client._subscribed),
        "token_valid": not token.is_expired,
        "token_refreshable": token.is_refreshable,
        # which panel-data reads the panel answered ("ok"), NAKed, or never returned
        "panel_data_reads": dict(client._read_results),
    }
    data["coordinator"] = {
        "last_update_success": coordinator.last_update_success,
        "update_interval_seconds": (
            coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None
        ),
    }
    return data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Diagnostics for the whole config entry (all panels)."""
    integration = await async_get_integration(hass, DOMAIN)
    data = _base(hass, entry, str(integration.version))
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        data["panels"] = [_panel_dict(ps) for ps in coordinator.client.panels.values()]
    return data


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Diagnostics scoped to one device/panel."""
    integration = await async_get_integration(hass, DOMAIN)
    data = _base(hass, entry, str(integration.version))
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        client = coordinator.client
        ids = {i[1] for i in device.identifiers if i[0] == DOMAIN}
        panels = [ps for did, ps in client.panels.items() if did in ids]
        data["panels"] = [_panel_dict(ps) for ps in (panels or client.panels.values())]
    return data
