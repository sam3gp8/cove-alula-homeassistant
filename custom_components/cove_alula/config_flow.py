"""Config flow (initial setup) and options flow (post-setup) for Cove (Alula) Alarm."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    AVAILABILITY_GRACE_MAX,
    AVAILABILITY_GRACE_MIN,
    AVAILABILITY_GRACE_SECONDS,
    CONF_ARM_NO_ENTRY_DELAY,
    CONF_ARM_SILENT,
    CONF_AVAILABILITY_GRACE,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_PIN,
    CONF_PIN_MODE,
    CONF_POLL_INTERVAL,
    CONF_TOKEN,
    DOMAIN,
    POLL_INTERVAL_MAX,
    POLL_INTERVAL_MIN,
    POLL_INTERVAL_SECONDS,
    PIN_MODE_STORED,
    PIN_MODES,
    get_setting,
)
from .covealula import CoveAlulaAuthError, CoveAlulaClient, CoveAlulaError


def _pin_mode_selector() -> SelectSelector:
    """Dropdown of the PIN handling modes, labelled via the shared `pin_mode` translation
    key so the initial-setup and options flows read identically."""
    return SelectSelector(
        SelectSelectorConfig(
            options=list(PIN_MODES),
            mode=SelectSelectorMode.DROPDOWN,
            translation_key="pin_mode",
        )
    )


def _seconds_selector(minimum: int, maximum: int) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=maximum,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="seconds",
        )
    )


STEP_USER = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_PIN_MODE, default=PIN_MODE_STORED): _pin_mode_selector(),
        # Only used (and only stored) when the PIN mode is "stored"; leave blank for the
        # "prompt" and "readonly" modes.
        vol.Optional(CONF_PIN, default=""): str,
    }
)


class CoveAlulaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return CoveAlulaOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            pin_mode = user_input.get(CONF_PIN_MODE, PIN_MODE_STORED)
            pin = (user_input.get(CONF_PIN) or "").strip()

            if pin_mode == PIN_MODE_STORED and not pin:
                # A stored PIN is the whole point of this mode; don't silently create an
                # entry that can never arm/disarm.
                errors["base"] = "pin_required"
            else:
                session = async_get_clientsession(self.hass)
                client = CoveAlulaClient(session=session)
                try:
                    await client.async_login(
                        user_input[CONF_EMAIL], user_input[CONF_PASSWORD]
                    )
                    await client.async_get_self()
                except CoveAlulaAuthError:
                    errors["base"] = "invalid_auth"
                except CoveAlulaError:
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=user_input[CONF_EMAIL],
                        data={
                            CONF_EMAIL: user_input[CONF_EMAIL],
                            CONF_PASSWORD: user_input[CONF_PASSWORD],
                            CONF_PIN_MODE: pin_mode,
                            # Store the PIN only in "stored" mode; the other modes keep
                            # nothing in the backend.
                            CONF_PIN: pin if pin_mode == PIN_MODE_STORED else "",
                            CONF_TOKEN: client.token.as_dict(),
                        },
                    )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER, errors=errors
        )


class CoveAlulaOptionsFlow(OptionsFlow):
    """Let the user change the tunable settings after setup, from the integration's
    Configure screen, without removing and re-adding the integration.

    Credentials stay in the config entry's data (changed via re-authentication); everything
    adjustable lives in options and takes effect on the reload that saving triggers.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        errors: dict[str, str] = {}

        if user_input is not None:
            pin_mode = user_input.get(CONF_PIN_MODE, PIN_MODE_STORED)
            pin = (user_input.get(CONF_PIN) or "").strip()

            if pin_mode == PIN_MODE_STORED and not pin:
                errors["base"] = "pin_required"
            else:
                return self.async_create_entry(
                    data={
                        CONF_PIN_MODE: pin_mode,
                        # Keep a PIN only in "stored" mode.
                        CONF_PIN: pin if pin_mode == PIN_MODE_STORED else "",
                        CONF_POLL_INTERVAL: int(user_input[CONF_POLL_INTERVAL]),
                        CONF_AVAILABILITY_GRACE: int(user_input[CONF_AVAILABILITY_GRACE]),
                        CONF_ARM_SILENT: bool(user_input.get(CONF_ARM_SILENT, False)),
                        CONF_ARM_NO_ENTRY_DELAY: bool(
                            user_input.get(CONF_ARM_NO_ENTRY_DELAY, False)
                        ),
                    },
                )

        # Pre-fill with the values currently in effect (options over data over defaults).
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_PIN_MODE,
                    default=get_setting(entry, CONF_PIN_MODE, PIN_MODE_STORED),
                ): _pin_mode_selector(),
                vol.Optional(
                    CONF_PIN, default=get_setting(entry, CONF_PIN, "")
                ): str,
                vol.Optional(
                    CONF_POLL_INTERVAL,
                    default=int(
                        get_setting(entry, CONF_POLL_INTERVAL, POLL_INTERVAL_SECONDS)
                    ),
                ): _seconds_selector(POLL_INTERVAL_MIN, POLL_INTERVAL_MAX),
                vol.Optional(
                    CONF_AVAILABILITY_GRACE,
                    default=int(
                        get_setting(
                            entry, CONF_AVAILABILITY_GRACE, AVAILABILITY_GRACE_SECONDS
                        )
                    ),
                ): _seconds_selector(AVAILABILITY_GRACE_MIN, AVAILABILITY_GRACE_MAX),
                vol.Optional(
                    CONF_ARM_SILENT,
                    default=bool(get_setting(entry, CONF_ARM_SILENT, False)),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_ARM_NO_ENTRY_DELAY,
                    default=bool(get_setting(entry, CONF_ARM_NO_ENTRY_DELAY, False)),
                ): BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )
