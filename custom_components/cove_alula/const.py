"""Constants for the Cove (Alula) Alarm integration."""

DOMAIN = "cove_alula"
PLATFORMS = ["alarm_control_panel", "binary_sensor"]

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_PIN = "pin"
CONF_TOKEN = "token"  # persisted CoveToken dict (so restarts don't re-login)

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
ATTR_ZONE = "zone"
ATTR_BYPASS = "bypass"
ATTR_MODE = "mode"
ATTR_METHOD = "method"
