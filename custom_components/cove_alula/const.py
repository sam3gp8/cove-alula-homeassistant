"""Constants for the Cove (Alula) Alarm integration."""

DOMAIN = "cove_alula"
PLATFORMS = ["alarm_control_panel", "binary_sensor"]

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_PIN = "pin"
CONF_PIN_MODE = "pin_mode"
CONF_TOKEN = "token"  # persisted CoveToken dict (so restarts don't re-login)

# How the arm/disarm PIN is handled:
#   stored   - a PIN is saved in the config entry and applied automatically (default;
#              arm/disarm works unattended, e.g. from automations).
#   prompt   - no PIN is stored; the native Cove/Alula PIN is typed into the Home Assistant
#              dialog on each arm/disarm, so each household member can use their own PIN.
#   readonly - no PIN is stored and arm/disarm are disabled; the panel is monitored only.
PIN_MODE_STORED = "stored"
PIN_MODE_PROMPT = "prompt"
PIN_MODE_READONLY = "readonly"
PIN_MODES = (PIN_MODE_STORED, PIN_MODE_PROMPT, PIN_MODE_READONLY)

# Fallback REST poll interval; live updates arrive over the websocket.
POLL_INTERVAL_SECONDS = 30

# Grace period before an entity is reported unavailable after contact is lost.
# Cove/Alula has no always-on connection guarantee, and we intentionally recycle the
# socket on token refresh, so short lapses are normal. Without a grace period every brief
# reconnect flips entities to "unavailable" for a second or two and spams the HA history
# (observed: 2-second Unavailable blips every few minutes). We keep the last known state
# and only surface "unavailable" once contact has been lost for longer than this. Sits in
# the 30-60s range: comfortably rides one missed 30s poll, trips on a sustained outage.
AVAILABILITY_GRACE_SECONDS = 45

# Services
SERVICE_CANCEL_ALARM = "cancel_alarm"
SERVICE_CONFIRM_ALARM = "confirm_alarm"
SERVICE_BYPASS_ZONE = "bypass_zone"
SERVICE_FORCE_ARM = "force_arm"
SERVICE_ARM = "arm"
SERVICE_DISARM = "disarm"
ATTR_ZONE = "zone"
ATTR_BYPASS = "bypass"
ATTR_MODE = "mode"
ATTR_METHOD = "method"
ATTR_SILENT = "silent"
ATTR_NO_ENTRY_DELAY = "no_entry_delay"
