"""DataUpdateCoordinator that owns the Cove/Alula client and live websocket."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    AVAILABILITY_GRACE_SECONDS,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_PIN,
    CONF_TOKEN,
    DOMAIN,
    POLL_INTERVAL_SECONDS,
)
from .covealula import (
    CoveAlulaAuthError,
    CoveAlulaClient,
    CoveAlulaError,
    CoveToken,
    PanelState,
)

_LOGGER = logging.getLogger(__name__)


class CoveAlulaCoordinator(DataUpdateCoordinator[dict[str, PanelState]]):
    """Keeps panel state fresh from REST polling + websocket pushes."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLL_INTERVAL_SECONDS),
        )
        self.entry = entry
        session = async_get_clientsession(hass)

        # Snapshot of the config fields that actually warrant a reload. The token is
        # refreshed and persisted to the entry every ~14 minutes; persisting fires the
        # entry's update listener, and reloading on every token write tore down and
        # rebuilt every entity -- the real cause of the recurring 2-3s "unavailable"
        # blips in the history. config_changed() lets the listener skip token-only writes.
        self._reload_signature = self._config_signature(entry)

        # device_id -> monotonic time of last HEALTHY contact (a live push or a genuinely
        # successful poll where the panel reported itself online). Entity availability is
        # derived from this with a grace period, so brief socket recycles don't flap.
        self._last_healthy: dict[str, float] = {}

        token = None
        if entry.data.get(CONF_TOKEN):
            token = CoveToken.from_dict(entry.data[CONF_TOKEN])

        self.client = CoveAlulaClient(
            session=session,
            token=token,
            token_updated=self._persist_token,
            update_callback=self._on_push,
            username=entry.data[CONF_EMAIL],
            password=entry.data[CONF_PASSWORD],
        )

    async def _persist_token(self, token: CoveToken) -> None:
        """Save the refreshed token into the config entry so restarts don't re-login.
        This fires the entry update listener; config_changed() below makes that listener
        skip the reload for these token-only writes."""
        data = {**self.entry.data, CONF_TOKEN: token.as_dict()}
        self.hass.config_entries.async_update_entry(self.entry, data=data)

    @staticmethod
    def _config_signature(entry: ConfigEntry) -> tuple:
        """The parts of the entry whose change should trigger a reload -- credentials and
        PIN. Deliberately excludes CONF_TOKEN, which changes on every refresh."""
        return (
            entry.data.get(CONF_EMAIL),
            entry.data.get(CONF_PASSWORD),
            entry.data.get(CONF_PIN),
        )

    def config_changed(self, entry: ConfigEntry) -> bool:
        """True only if a reload-worthy field (email/password/PIN) changed since the last
        reload. Returns False for token-only writes, so the update listener can skip the
        reload that would otherwise flap every entity to 'unavailable'."""
        sig = self._config_signature(entry)
        if sig == self._reload_signature:
            return False
        self._reload_signature = sig
        return True

    def _on_push(self, panel: PanelState) -> None:
        """Websocket pushed new state -> notify entities immediately."""
        self._mark_healthy(self.client.panels)
        self.async_set_updated_data(dict(self.client.panels))

    def _mark_healthy(self, panels: dict[str, PanelState]) -> None:
        """Record 'now' as the last healthy contact for each panel that isn't reporting
        itself offline. Called only on genuine contact (a live push or a successful poll) --
        never on the keep-last-known-state fallbacks below, so a real outage still ages out."""
        now = time.monotonic()
        for device_id, panel in panels.items():
            if panel.online is not False:
                self._last_healthy[device_id] = now

    def panel_is_fresh(self, device_id: str) -> bool:
        """Whether we've had healthy contact for this panel within the grace window. Entity
        `available` uses this instead of the momentary poll/socket state, so a brief lapse
        (Cove/Alula offers no persistent connection; we also recycle the socket on token
        refresh) keeps the last known state instead of flapping to 'unavailable'."""
        ts = self._last_healthy.get(device_id, 0.0)
        if not ts:
            return False  # never had a healthy view yet -> unavailable until first data
        return (time.monotonic() - ts) < AVAILABILITY_GRACE_SECONDS

    async def async_setup(self) -> None:
        """Authenticate, discover panels, open the socket, and subscribe.

        Deliberately FAST. Home Assistant cancels config-entry setup that blocks too long,
        and pulling the full snapshot here (panel name, firmware, zone names/config/status)
        could take ~45s against the cloud -- that is what raised CancelledError mid-read.
        We now only do the quick work, then fetch the snapshot in a background task; the
        entities fill in as responses and live pushes arrive.
        """
        try:
            async with asyncio.timeout(30):
                # refresh if we have a token, else log in; if the stored refresh token is
                # rejected this transparently re-logs in with the stored credentials
                await self.client.async_ensure_authenticated()
                panels = await self.client.async_get_panels()
                await self.client.async_connect_ws()
                for panel in panels:
                    await self.client.async_subscribe_device(panel.device_id)
        except CoveAlulaAuthError as err:
            # Genuinely bad credentials (re-login also failed): ask the user to re-auth
            raise ConfigEntryAuthFailed(f"Cove/Alula authentication failed: {err}") from err
        except (CoveAlulaError, TimeoutError) as err:
            # ConfigEntryNotReady makes HA retry setup with backoff instead of giving up
            raise ConfigEntryNotReady(f"Cove/Alula setup failed: {err}") from err

        self.entry.async_create_background_task(
            self.hass,
            self._async_bootstrap([p.device_id for p in panels]),
            "cove_alula_bootstrap",
        )

    async def _async_bootstrap(self, device_ids: list[str]) -> None:
        """Pull the first full snapshot AFTER setup has returned, so a slow cloud can never
        stall (or get cancelled by) config-entry setup."""
        for device_id in device_ids:
            try:
                await self.client.async_refresh_state(device_id, budget=45.0)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("initial snapshot for %s incomplete: %s", device_id, err)
        await asyncio.sleep(1)  # let the last responses land
        if self.client.panels:
            self._mark_healthy(self.client.panels)
            self.async_set_updated_data(dict(self.client.panels))

    async def _async_update_data(self) -> dict[str, PanelState]:
        """Backstop refresh. The websocket keeps state live and now reconciles on every
        reconnect, so this just ensures the socket is up and re-reads zone/panel status in
        case a push was ever missed. Transient failures keep the last known state rather
        than flapping every entity to 'unavailable'."""
        try:
            async with asyncio.timeout(45):
                await self.client.async_connect_ws()  # reconnect if the socket dropped
                device_ids = list(self.client.panels)
                if not device_ids:
                    # first run, or panels lost: (re)discover via REST
                    panels = await self.client.async_get_panels()
                    device_ids = [p.device_id for p in panels]
                for device_id in device_ids:
                    await self.client.async_reconcile(device_id)
        except TimeoutError as err:
            if self.client.panels:
                _LOGGER.debug("poll timed out; keeping last known state")
                return dict(self.client.panels)
            raise UpdateFailed("timed out talking to the Cove/Alula cloud") from err
        except CoveAlulaAuthError:
            # token died and couldn't refresh -> try a full re-login once
            try:
                await self.client.async_login(
                    self.entry.data[CONF_EMAIL], self.entry.data[CONF_PASSWORD]
                )
                await self.client.async_connect_ws()
            except CoveAlulaError as err2:
                if self.client.panels:
                    _LOGGER.warning("re-auth failed, keeping last known state: %s", err2)
                    return dict(self.client.panels)
                raise UpdateFailed(f"re-auth failed: {err2}") from err2
        except CoveAlulaError as err:
            if self.client.panels:
                _LOGGER.debug("poll reconcile failed, keeping last known state: %s", err)
                return dict(self.client.panels)
            raise UpdateFailed(f"poll failed: {err}") from err
        # reached only when the reconcile above completed without error == genuine contact
        self._mark_healthy(self.client.panels)
        return dict(self.client.panels)

    async def async_shutdown(self) -> None:
        await self.client.async_close()
