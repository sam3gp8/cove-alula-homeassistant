"""
covealula.py — minimal async client for the Cove Connect / Alula cloud API.

Recovered from Cove_Connect 4.3.121.333. Cloud-only (no LAN/BLE). Lets you log in
with your own account, read your panel, and arm/disarm with your own PIN — the same
things the app does. See cove_alula_protocol.md for the full reverse-engineering notes.

Dependency: aiohttp (already present in Home Assistant OS).

Standalone test (username may be your Cove account number, e.g. C123456):
    pip install aiohttp
    python covealula.py login    <user> '<password>'
    python covealula.py status   <user> '<password>'           # subscribe + live state + raw frames
    python covealula.py diag     <user> '<password>'           # status + arming-level names
    python covealula.py names    <user> '<password>'           # which number == stay/away/night
    python covealula.py disarm   <user> '<password>' 1234
    python covealula.py arm_away <user> '<password>' 1234
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)

API_BASE = "https://api.alula.net"
WS_URL = "wss://api.alula.net/ws/v1"
TOKEN_URL = f"{API_BASE}/oauth/token"

# Mobile-client credentials embedded in the app and shared by every install.
# Not a per-user secret; you still need your own username/password to log in.
CLIENT_ID = "4ce837c4-08e2-11e7-aa3b-605718912297"
CLIENT_SECRET = "Uzka3sgLNDTaH3cQ"

USER_AGENT = "CoveConnect/4.3.121 (HomeAssistant integration)"

# armingLevelValue == ArmingLevel.getByteCode(). Byte codes verified from the decompiled
# enum:
#   LEVEL_0 byte 0  -> "unknown"  (placeholder/unknown state, NEVER a command target)
#   LEVEL_1 byte 1  -> "disarmed" / can_disarm     <- this is DISARM
#   LEVEL_2 byte 2  -> first armed level  ("stay"/"home")
#   LEVEL_3 byte 3  -> armed level        ("night" on this panel family)
#   LEVEL_4 byte 4  -> armed level        ("away" on this panel family)
# requestDisarmWithPin() in the app sends armingLevelValue = LEVEL_1.getByteCode() = 1,
# so disarm is 1 (NOT 0). The *semantic label* of each armed level (stay/away/night) is
# configured per panel and the enum's own naming did not match real behavior here:
# on-device testing (arming each mode and reading it back on the panel) showed byte 3 =
# NIGHT and byte 4 = AWAY, which is the reverse of the enum's nominal ordering. The numbers
# below are the single source of truth for both arming commands and state read-back, so
# they stay consistent. If a different panel maps these the other way, swap these two.
LEVEL_UNKNOWN = 0
LEVEL_DISARM = 1
LEVEL_STAY = 2   # home / first armed level
LEVEL_NIGHT = 3  # corrected from on-device testing (enum nominally called byte 3 "away")
LEVEL_AWAY = 4   # corrected from on-device testing (enum nominally called byte 4 "night")

# Arming differs by Alula panel family (see PanelState.supports_partition_arming):
#   Helix        -> changeArmingLevelUsingCode  (numeric armingLevelValue + PIN)
#   ConnectFlex  -> partitionArmingLevelChange  (string level + partitions + userNumber,
#                                                PIN only for disarm)
CMD_CHANGE_ARMING_LEVEL_CODE = "changeArmingLevelUsingCode"
CMD_CHANGE_ARMING_LEVEL_PARTITION = "partitionArmingLevelChange"
# ConnectFlex-family panel ids that use the partition arming command.
_PARTITION_PANEL_FAMILIES = frozenset({
    "connectflx", "connectflx_z", "connectflx_dual", "connectflx_dual_z",
})
# level number -> string name used by the ConnectFlex partition command
_PARTITION_LEVEL_NAME = {
    LEVEL_DISARM: "disarm", LEVEL_STAY: "stay",
    LEVEL_NIGHT: "night", LEVEL_AWAY: "away",
}
CMD_REQUEST_MFD = "requestMfd"
CMD_WRITE_MFD = "writeMfd"
CHANNEL_HELIX = "device.helix"
CHANNEL_STATUS = "device.status"

# How early (seconds) before expiry we proactively refresh the access token.
_REFRESH_SKEW = 120


class CoveAlulaError(Exception):
    """Base error."""


class CoveAlulaAuthError(CoveAlulaError):
    """Login / token failure (bad credentials, invalid_client, etc.)."""


def _pin_to_array(pin: str) -> list[str]:
    """'1234' -> ['1','2','3','4'] as the panel expects."""
    return list(str(pin).strip())


def _is_unsupported_command_nak(resp: Optional[dict]) -> bool:
    """True if a helix command response is a NAK whose reason is that the command is not
    supported — i.e. we sent the wrong panel family's arming command. Used to fall back
    to the other command. Any other response (ack, a different NAK, or None) returns False."""
    if not isinstance(resp, dict):
        return False
    event = resp.get("event") if isinstance(resp.get("event"), dict) else None
    data = event.get("data") if (event and isinstance(event.get("data"), dict)) else resp
    if not isinstance(data, dict) or data.get("cmdrsp") != "nak":
        return False
    reasons = (data.get("payload") or {}).get("nakReasons") or []
    text = " ".join(str(r.get("reason", "")) for r in reasons).lower()
    return "unsupported command" in text


@dataclass
class CoveToken:
    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    expires_at: float = 0.0  # epoch seconds

    @property
    def is_expired(self) -> bool:
        return (not self.access_token) or (time.time() >= self.expires_at - _REFRESH_SKEW)

    @property
    def is_refreshable(self) -> bool:
        return bool(self.refresh_token)

    def update_from_response(self, data: dict) -> None:
        self.access_token = data.get("access_token") or self.access_token
        # Some refresh responses omit a new refresh_token; keep the old one.
        self.refresh_token = data.get("refresh_token") or self.refresh_token
        expires_in = data.get("expires_in")
        if expires_in is not None:
            self.expires_at = time.time() + float(expires_in)

    def as_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CoveToken":
        return cls(
            access_token=d.get("access_token"),
            refresh_token=d.get("refresh_token"),
            expires_at=float(d.get("expires_at", 0.0)),
        )


@dataclass
class Zone:
    """A single panel zone / sensor (door, window, motion, etc.).

    Field names mirror the helix_zones model in the app. `open` is the live
    open/closed state; `device_type`/`ui_type` describe the sensor kind.
    """

    index: int
    name: Optional[str] = None
    open: Optional[bool] = None
    bypassed: Optional[bool] = None
    alarm: Optional[bool] = None
    tamper: Optional[bool] = None
    low_battery: Optional[bool] = None
    trouble: Optional[bool] = None
    installed: Optional[bool] = None
    inactive: Optional[bool] = None
    signal_level: Optional[int] = None
    device_type: Optional[str] = None   # e.g. "door", "window", "motion", "smoke"
    ui_type: Optional[str] = None       # UI hint the app uses to pick an icon/class
    raw: dict = field(default_factory=dict)

    def apply(self, attrs: dict) -> None:
        self.raw.update(attrs)
        if "name" in attrs and attrs["name"] not in (None, ""):
            self.name = attrs["name"]
        for src in ("open", "bypassed", "alarm", "tamper", "low_battery",
                    "trouble", "installed", "inactive"):
            if src in attrs:
                setattr(self, src, _as_bool(attrs[src]))
        if "signal_level" in attrs:
            try:
                self.signal_level = int(attrs["signal_level"])
            except (TypeError, ValueError):
                pass
        for src in ("device_type", "ui_type"):
            if src in attrs and attrs[src] not in (None, ""):
                setattr(self, src, attrs[src])


@dataclass
class PanelState:
    """Live panel state assembled from REST + WebSocket pushes."""

    device_id: str
    name: Optional[str] = None
    panel_name: Optional[str] = None   # friendly system name, e.g. "My Home"
    connected_panel: str = ""          # Alula panel family id, e.g. "helix" / "connectflx"
    online: Optional[bool] = None
    serial_number: Optional[str] = None
    firmware_version: Optional[str] = None
    arming_level: Optional[int] = None
    arming_level_names: Optional[dict] = None  # per-panel numeric->label map
    ready_to_arm: Optional[bool] = None
    in_exit_delay: Optional[bool] = None
    in_entry_delay: Optional[bool] = None
    alarm: Optional[bool] = None
    alarm_type: Optional[str] = None
    open_zones: Optional[int] = None
    bypassed_zones: Optional[int] = None
    # system troubles
    low_battery: Optional[bool] = None
    ac_failure: Optional[bool] = None
    tamper: Optional[bool] = None
    cs_comm_fail: Optional[bool] = None
    server_comm_fail: Optional[bool] = None
    siren_trouble: Optional[bool] = None
    fire_trouble: Optional[bool] = None
    highest_zone_index: Optional[int] = None
    zones: dict = field(default_factory=dict)  # index -> Zone
    raw: dict = field(default_factory=dict)

    def apply(self, attrs: dict) -> None:
        """Merge an attributes dict (REST attributes or a socket status payload)."""
        self.raw.update(attrs)
        if "connectedPanel" in attrs:
            self.connected_panel = attrs["connectedPanel"] or ""
        if "name" in attrs:
            self.name = attrs["name"]
        if "panel_name" in attrs and attrs["panel_name"] not in (None, ""):
            self.panel_name = attrs["panel_name"]
        if "online" in attrs:
            self.online = _as_bool(attrs["online"])
        if "serial_number" in attrs:
            self.serial_number = attrs["serial_number"]
        if "firmware_version" in attrs and attrs["firmware_version"] not in (None, ""):
            self.firmware_version = attrs["firmware_version"]
        if "arming_level" in attrs:
            self.arming_level = _as_arming_level(attrs["arming_level"])
        if "arming_level_names" in attrs and isinstance(attrs["arming_level_names"], dict):
            self.arming_level_names = attrs["arming_level_names"]
        for src in (
            "ready_to_arm", "in_exit_delay", "in_entry_delay", "alarm",
            "low_battery", "ac_failure", "tamper", "cs_comm_fail",
            "server_comm_fail", "siren_trouble", "fire_trouble",
        ):
            if src in attrs:
                setattr(self, src, _as_bool(attrs[src]))
        for k in ("alarm_type", "open_zones", "bypassed_zones", "highest_zone_index"):
            if k in attrs:
                setattr(self, k, attrs[k])

    def zone(self, index: int) -> Zone:
        z = self.zones.get(index)
        if z is None:
            z = Zone(index=index)
            self.zones[index] = z
        return z

    def _configured_zones(self) -> list:
        """Zones that physically exist (index within the panel's highest used index)."""
        out = []
        for idx, z in self.zones.items():
            if self.highest_zone_index is not None and idx > self.highest_zone_index:
                continue
            out.append(z)
        return out

    @property
    def not_ready_zones(self) -> list:
        """Configured zones that would block arming: open and not bypassed."""
        return [z for z in self._configured_zones() if z.open and not z.bypassed]

    @property
    def ready(self) -> Optional[bool]:
        """Whether the panel can arm. Prefers the panel's own ready_to_arm flag; if the
        panel doesn't report it (some models only expose it via systemStatus, which they
        may NAK), derive it: ready when no configured zone is open-and-unbypassed."""
        if self.ready_to_arm is not None:
            return self.ready_to_arm
        zones = self._configured_zones()
        if not any(z.open is not None for z in zones):
            return None  # no zone status yet -> unknown
        return len(self.not_ready_zones) == 0

    @property
    def supports_partition_arming(self) -> bool:
        """True for ConnectFlex-family panels, which arm via partitionArmingLevelChange
        rather than the Helix changeArmingLevelUsingCode command."""
        return self.connected_panel in _PARTITION_PANEL_FAMILIES





def _as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


_LEVEL_STR = {
    "unknown": 0, "level0": 0,
    "disarm": 1, "disarmed": 1, "off": 1, "level1": 1,
    "stay": 2, "arm_stay": 2, "armed_stay": 2, "home": 2, "level2": 2,
    "away": 3, "arm_away": 3, "armed_away": 3, "level3": 3,
    "night": 4, "armed_night": 4, "level4": 4,
    "level5": 5, "level6": 6, "level7": 7, "level8": 8,
    "any": 255,
}


def _as_arming_level(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s.isdigit():
            return int(s)
        return _LEVEL_STR.get(s)
    return None


class CoveAlulaClient:
    """
    Async cloud client.

    Typical use:
        client = CoveAlulaClient(token_updated=save_cb)
        await client.async_login(email, password)        # or load a saved token
        panels = await client.async_get_panels()
        await client.async_connect_ws()                  # live state + commands
        await client.async_set_arming_level(panel_id, LEVEL_AWAY, pin="1234")
        ...
        await client.async_close()
    """

    def __init__(
        self,
        session: Optional[aiohttp.ClientSession] = None,
        token: Optional[CoveToken] = None,
        token_updated: Optional[Callable[[CoveToken], Awaitable[None] | None]] = None,
        update_callback: Optional[Callable[[PanelState], Awaitable[None] | None]] = None,
        raw_callback: Optional[Callable[[dict], None]] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        self._session = session
        self._own_session = session is None
        self._token = token or CoveToken()
        self._token_updated = token_updated
        self._update_callback = update_callback
        # Stored so we can silently re-login when the refresh token is rejected
        # (expired/revoked/rotated). Without these, a dead refresh token is fatal.
        self._username = username
        self._password = password
        # Optional hook that receives every decoded inbound WS frame (for debugging /
        # protocol discovery). Synchronous; keep it cheap.
        self._raw_callback = raw_callback

        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._auth_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self.panels: dict[str, PanelState] = {}
        # Set once the server sends its connect-success / "ready" frame. The app waits
        # for this before subscribing to channels, so we mirror that.
        self._connected = asyncio.Event()
        self._subscribed: set[str] = set()
        # diagnostics: map requestId -> MFD read name, and read name -> result string
        self._req_names: dict[str, str] = {}
        self._read_results: dict[str, str] = {}
        # device_id -> "code" | "partition": the arming command family confirmed to work on
        # this panel (learned when a command is accepted, so we skip the failing one after).
        self._arming_command_kind: dict[str, str] = {}

    # ---- lifecycle -------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        return self._session

    async def async_close(self) -> None:
        for task in (self._watchdog_task, self._ws_task):
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._ws_task = None
        self._watchdog_task = None
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()

    # ---- auth ------------------------------------------------------------

    @property
    def token(self) -> CoveToken:
        return self._token

    async def _notify_token(self) -> None:
        if self._token_updated:
            res = self._token_updated(self._token)
            if asyncio.iscoroutine(res):
                await res

    async def async_login(self, username: str, password: str) -> None:
        await self._token_request({
            "grant_type": "password",
            "username": username,
            "password": password,
        })

    async def async_refresh(self) -> None:
        if not self._token.is_refreshable:
            raise CoveAlulaAuthError("no refresh_token available; full login required")
        await self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": self._token.refresh_token,
        })

    async def _refresh_or_login(self) -> None:
        """Obtain a fresh access token. Prefer the refresh token; if it is rejected
        (expired/revoked/single-use-rotated), fall back to a full login with the stored
        credentials. Caller must hold `_auth_lock` (token rotation is single-use, so
        concurrent refreshes must be serialized). Either success persists the new token."""
        if self._token.is_refreshable:
            try:
                await self.async_refresh()
                return
            except CoveAlulaAuthError as err:
                if not (self._username and self._password):
                    raise
                _LOGGER.warning(
                    "Cove/Alula token refresh rejected (%s); re-logging in with stored "
                    "credentials", err,
                )
        if self._username and self._password:
            await self.async_login(self._username, self._password)
        else:
            raise CoveAlulaAuthError("token expired and no stored credentials to re-login")

    async def async_ensure_authenticated(self) -> None:
        """Public, locked entrypoint: make sure we hold a usable access token, refreshing
        or re-logging in as needed. Used at setup so a dead refresh token self-heals."""
        async with self._auth_lock:
            if self._token.is_expired or not self._token.access_token:
                await self._refresh_or_login()

    async def _token_request(self, extra: dict) -> None:
        session = await self._ensure_session()
        data = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, **extra}
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
        }
        async with session.post(TOKEN_URL, data=data, headers=headers) as resp:
            text = await resp.text()
            if resp.status == 429:
                retry = resp.headers.get("Retry-After", "?")
                raise CoveAlulaAuthError(f"rate limited (HTTP 429), retry after {retry}s")
            if resp.status >= 400:
                raise CoveAlulaAuthError(f"token request failed HTTP {resp.status}: {text[:300]}")
            try:
                payload = json.loads(text)
            except ValueError as err:
                raise CoveAlulaAuthError(f"bad token response: {text[:200]}") from err
        if "access_token" not in payload:
            raise CoveAlulaAuthError(f"no access_token in response: {payload}")
        self._token.update_from_response(payload)
        await self._notify_token()

    async def _valid_access_token(self) -> str:
        async with self._auth_lock:
            if self._token.is_expired or not self._token.access_token:
                await self._refresh_or_login()
            assert self._token.access_token
            return self._token.access_token

    # ---- REST ------------------------------------------------------------

    async def _rest(self, method: str, path: str, *, json_body: Any = None,
                    retry_auth: bool = True) -> Any:
        session = await self._ensure_session()
        token = await self._valid_access_token()
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {token}",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        async with session.request(method, url, headers=headers, json=json_body) as resp:
            text = await resp.text()
            if resp.status == 401 and retry_auth:
                async with self._auth_lock:
                    await self._refresh_or_login()
                return await self._rest(method, path, json_body=json_body, retry_auth=False)
            if resp.status >= 400:
                raise CoveAlulaError(f"{method} {path} -> HTTP {resp.status}: {text[:300]}")
            if not text:
                return None
            try:
                return json.loads(text)
            except ValueError:
                return text

    async def async_get_self(self) -> dict:
        return await self._rest("GET", "/rest/v1/self")

    async def async_get_devices(self) -> list[dict]:
        """Return the raw JSON:API 'data' list of all devices on the account."""
        payload = await self._rest("GET", "/rest/v1/devices")
        if isinstance(payload, dict):
            data = payload.get("data", [])
            return data if isinstance(data, list) else [data]
        return payload or []

    async def async_get_panels(self) -> list[PanelState]:
        """Filter devices to alarm panels and seed PanelState objects."""
        out: list[PanelState] = []
        for dev in await self.async_get_devices():
            attrs = dev.get("attributes", dev) if isinstance(dev, dict) else {}
            dev_id = str(dev.get("id") or attrs.get("device_id") or "")
            if not dev_id:
                continue
            # Panel-vs-not filter. The REST API uses camelCase (isPanel / isCamera); the
            # earlier snake_case-only check never matched, so panels were actually included
            # by the "not a camera" fallback below. Now: trust an explicit panel flag when
            # present (this is what includes ConnectFlex panels and excludes flagged
            # non-panels); if the account sends no panel flag at all, fall back to
            # "include unless it's a camera" so a panel that omits the flag isn't dropped.
            raw_is_panel = attrs.get("is_panel", attrs.get("isPanel"))
            if raw_is_panel is not None:
                if not _as_bool(raw_is_panel):
                    continue
            elif _as_bool(attrs.get("is_camera", attrs.get("isCamera", False))):
                continue
            ps = self.panels.get(dev_id) or PanelState(device_id=dev_id)
            ps.apply(attrs)
            self.panels[dev_id] = ps
            out.append(ps)
        return out

    # ---- alarm ack (RPC) -------------------------------------------------

    async def async_cancel_alarm(self, device_id: str, partition: int = 0) -> Any:
        body = {"id": str(uuid.uuid4()),
                "params": {"deviceId": device_id, "partition": partition}}
        return await self._rest("POST", "/rpc/v1/alarm/cancel", json_body=body)

    async def async_confirm_alarm(self, device_id: str, partition: int = 0) -> Any:
        body = {"id": str(uuid.uuid4()),
                "params": {"deviceId": device_id, "partition": partition}}
        return await self._rest("POST", "/rpc/v1/alarm/confirm", json_body=body)

    # ---- WebSocket -------------------------------------------------------

    async def async_connect_ws(self) -> None:
        """Open the live socket and start the background receive loop."""
        if self._ws_task and not self._ws_task.done():
            return
        await self._open_ws()
        self._ws_task = asyncio.ensure_future(self._ws_loop())
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.ensure_future(self._token_watchdog())

    async def _open_ws(self) -> None:
        session = await self._ensure_session()
        token = await self._valid_access_token()
        headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}
        self._connected.clear()
        self._ws = await session.ws_connect(WS_URL, headers=headers, heartbeat=30)
        _LOGGER.debug("Cove/Alula websocket connected (awaiting server ready frame)")

    async def _ws_loop(self) -> None:
        backoff = 1
        while True:
            try:
                if self._ws is None or self._ws.closed:
                    await self._open_ws()
                assert self._ws is not None
                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._handle_ws_text(msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                backoff = 1  # clean exit of the async-for => server (or we) closed; reconnect fast
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Cove/Alula websocket error: %s; reconnecting in %ss", err, backoff)
            # connection dropped: forget ready-state and re-subscribe after reconnect
            self._connected.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            self._ws = None

    async def _token_watchdog(self) -> None:
        """Refresh the access token and proactively recycle the socket shortly BEFORE the
        token expires. The websocket is authenticated with the bearer token at connect
        time, so when it expires the server silently drops us and aiohttp only notices via
        the ~30s heartbeat -> a long gap. By cycling on our own schedule we reconnect in
        ~1-2s (and _ws_loop re-subscribes + reconciles), so live state stays in sync."""
        while True:
            try:
                ttl = self._token.expires_at - time.time()
                # wake ~75s before expiry; clamp so we always make forward progress
                await asyncio.sleep(max(5.0, min(ttl - 75.0, 600.0)))
                if not self._token.is_refreshable:
                    continue
                if self._token.expires_at - time.time() > 90.0:
                    continue  # not close enough to expiry yet
                async with self._auth_lock:
                    await self._refresh_or_login()
                # Avoid recycling the socket while a command is waiting on a response --
                # yanking the connection mid-request is what surfaces as "no response
                # within Ns" on arm/disarm calls (_helix_command's CoveAlulaError). Give
                # in-flight requests a bounded window to finish first; if they don't
                # drain in time, proceed anyway so the recycle is never deferred
                # indefinitely (the token was already refreshed above regardless).
                for _ in range(6):  # ~3s max, comfortably inside _helix_command's timeout
                    if not self._pending:
                        break
                    await asyncio.sleep(0.5)
                ws = self._ws
                if ws is not None and not ws.closed:
                    # closing makes _ws_loop fall through and reopen with the fresh token
                    await ws.close()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Cove/Alula token watchdog: %s", err)
                await asyncio.sleep(60)

    def _handle_ws_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        try:
            msg = json.loads(text)
        except ValueError:
            _LOGGER.debug("non-JSON ws frame: %s", text[:120])
            return

        # Hand the raw frame to any debug hook first (protocol discovery).
        if self._raw_callback is not None:
            try:
                self._raw_callback(msg)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("raw_callback raised", exc_info=True)

        if not isinstance(msg, dict):
            return

        # Connect-success / "ready" handshake. The server sends a frame on channel "*"
        # carrying a sessionId and message == "ready". Until then, subscriptions are not
        # guaranteed to take, so the app waits for this. Mirror that.
        if not self._connected.is_set():
            ch = msg.get("channel")
            msg_field = str(msg.get("message", "")).lower()
            if ch == "*" or "sessionId" in msg or msg_field == "ready":
                self._connected.set()
                _LOGGER.debug("Cove/Alula websocket ready; resubscribing + reconciling")
                for dev_id in list(self._subscribed):
                    asyncio.ensure_future(self._resubscribe_and_reconcile(dev_id))

        # The real response shape: payload/cmdrsp/requestId live under event.data
        # (confirmed from HelixResponse.processEvent + live captures). Older code looked
        # under "send", which is only the delivery ACK, not the data.
        event = msg.get("event") if isinstance(msg.get("event"), dict) else None
        data = event.get("data") if (event and isinstance(event.get("data"), dict)) else None

        req_id = None
        cmdrsp = None
        if data:
            req_id = data.get("requestId")
            cmdrsp = data.get("cmdrsp")
        if not req_id:
            # fall back to the ACK/legacy shapes
            ack = msg.get("send") if isinstance(msg.get("send"), dict) else msg
            if isinstance(ack, dict):
                req_id = ack.get("requestId") or ack.get("requestID")

        # Diagnostics: map this response back to the read we sent, and surface NAKs.
        read_name = self._req_names.pop(req_id, None) if req_id else None
        if cmdrsp == "nak":
            reasons = self._nak_reasons(data)
            if read_name:
                self._read_results[read_name] = f"NAK: {reasons}"
            _LOGGER.debug("panel NAK for read %s (req %s): %s", read_name, req_id, reasons)
        elif cmdrsp in ("sendMfd", "sendMfdEnhanced", "ack") and read_name:
            # 'ack' is the success reply for commands/writes (arm, bypass, forceArm)
            self._read_results[read_name] = "ok"

        # Resolve any pending future waiting on this requestId.
        if req_id and req_id in self._pending:
            fut = self._pending.pop(req_id)
            if not fut.done():
                fut.set_result(msg)

        # Apply panel/zone state from this frame (NAKs simply yield nothing).
        if cmdrsp != "nak":
            self._maybe_apply_status(msg)

    @staticmethod
    def _nak_reasons(data: Optional[dict]) -> str:
        try:
            arr = (data or {}).get("payload", {}).get("nakReasons", [])
            return ", ".join(str(r.get("reason", r)) for r in arr) or "(unspecified)"
        except Exception:  # noqa: BLE001
            return "(unparseable)"

    @staticmethod
    def _first_string(node: Any) -> Optional[str]:
        """Find the first non-empty string inside a value (handles {'name': 'X'} etc.)."""
        if isinstance(node, str):
            return node or None
        if isinstance(node, dict):
            for k in ("name", "value", "text", "label"):
                v = node.get(k)
                if isinstance(v, str) and v:
                    return v
            for v in node.values():
                if isinstance(v, str) and v:
                    return v
        if isinstance(node, list):
            for v in node:
                s = CoveAlulaClient._first_string(v)
                if s:
                    return s
        return None

    # Inbound field names we understand, mapped to PanelState attribute names. Covers both
    # the snake_case helix status fields and the camelCase MFD field names seen in the app.
    _FIELD_ALIASES = {
        "arming_level": "arming_level", "armingLevel": "arming_level",
        "armingLevelValue": "arming_level", "arming_level_value": "arming_level",
        "arming_level_names": "arming_level_names", "armingLevelNames": "arming_level_names",
        "armingLevelName": "arming_level_names",
        "ready_to_arm": "ready_to_arm", "readyToArm": "ready_to_arm",
        "in_exit_delay": "in_exit_delay", "inExitDelay": "in_exit_delay",
        "in_entry_delay": "in_entry_delay", "inEntryDelay": "in_entry_delay",
        "alarm": "alarm", "alarm_type": "alarm_type", "alarmType": "alarm_type",
        "open_zones": "open_zones", "openZones": "open_zones",
        "bypassed_zones": "bypassed_zones", "bypassedZones": "bypassed_zones",
        "low_battery": "low_battery", "lowBattery": "low_battery",
        "ac_failure": "ac_failure", "acFailure": "ac_failure",
        "tamper": "tamper",
        "cs_comm_fail": "cs_comm_fail", "csCommFail": "cs_comm_fail",
        "server_comm_fail": "server_comm_fail", "serverCommFail": "server_comm_fail",
        "siren_trouble": "siren_trouble", "sirenTrouble": "siren_trouble",
        "sirenTroubleCondition": "siren_trouble",
        "fire_trouble": "fire_trouble", "fireTrouble": "fire_trouble",
        "highest_index_zone": "highest_zone_index", "highestIndexZone": "highest_zone_index",
        "firmware_version": "firmware_version", "firmwareVersion": "firmware_version",
        "gatewayVersions": "firmware_version",
        "panel_name": "panel_name", "panelName": "panel_name",
        "name": "name",
        "online": "online", "serial_number": "serial_number", "serialNumber": "serial_number",
    }

    # MFD read field names whose value is a per-zone array (or index-keyed dict).
    _ZONE_MFD_NAMES = {
        "zoneStatus", "zoneName", "zoneNames", "zoneInfo",
        "zoneConfiguration", "zoneConfigurations", "zoneOptions",
    }
    # Per-zone field names -> Zone attribute names.
    _ZONE_ALIASES = {
        "name": "name", "zoneName": "name", "name_sort": None,
        "open": "open", "zoneOpen": "open",
        "bypassed": "bypassed", "zoneBypassed": "bypassed", "zoneBypass": "bypassed",
        "alarm": "alarm",
        "tamper": "tamper", "zoneTampered": "tamper", "tamper_alarm": "tamper",
        "low_battery": "low_battery", "low_battery_trouble": "low_battery",
        "zoneLowBattery": "low_battery", "lowBattery": "low_battery",
        "trouble": "trouble", "general_trouble": "trouble",
        "supervisory_trouble": "trouble", "sensor_malfunction_trouble": "trouble",
        "installed": "installed",
        "inactive": "inactive", "zone_inactive": "inactive", "zoneInactive": "inactive",
        "signal_level": "signal_level", "signalLevel": "signal_level",
        "device_type": "device_type", "deviceType": "device_type",
        "ui_type": "ui_type", "uiType": "ui_type",
        "ui_type_user": "ui_type", "uiTypeUser": "ui_type",
    }
    _ZONE_INDEX_KEYS = ("zoneIndex", "zone_index", "index", "zoneNumber", "_id")

    # MFD field names whose response value carries panel-level fields.
    _PANEL_MFD_NAMES = {
        "panelStatus", "systemStatus", "partitionStatus",
        "entryDelay", "exitDelay", "statusVersion",
    }

    def _collect_panel(self, obj: Any, attrs: dict) -> None:
        """Map a flat value object's known keys into panel attrs via _FIELD_ALIASES.

        Also derives a single `tamper` flag by OR-ing the panel's several tamper sources
        (cover/wall/zone), since panelStatus has no single 'tamper' field.
        """
        if not isinstance(obj, dict):
            return
        tamper_seen = False
        tamper_val = False
        for k, v in obj.items():
            dst = self._FIELD_ALIASES.get(k)
            if dst and not isinstance(v, (dict, list)):
                attrs[dst] = v
            if k in ("tamper", "alarmPanelCoverTamper", "alarmPanelWallTamper",
                     "tamperZones", "keystrokeTamper", "busModuleTamper"):
                tamper_seen = True
                tamper_val = tamper_val or _as_bool(v)
        if tamper_seen:
            attrs["tamper"] = tamper_val

    def _collect_zones(self, array: Any, zones: dict) -> None:
        """Parse an indexed zone array: items like {"index": N, "value": {...zone...}}.

        Also tolerates entries that carry zone fields directly, index-keyed dicts, and
        positional plain-string name arrays.
        """
        if isinstance(array, dict):
            pairs = list(array.items())
        elif isinstance(array, list):
            pairs = list(enumerate(array))
        else:
            return
        for key, entry in pairs:
            idx: Optional[int] = None
            value: Any = entry
            if isinstance(entry, dict):
                for ik in self._ZONE_INDEX_KEYS:
                    if ik in entry:
                        try:
                            idx = int(entry[ik])
                            break
                        except (TypeError, ValueError):
                            pass
                # zoneName entries carry a plain string: {"index": N, "value": "BACK DOOR"}
                if isinstance(entry.get("value"), str):
                    if idx is None:
                        try:
                            idx = int(key)
                        except (TypeError, ValueError):
                            continue
                    zones.setdefault(idx, {})["name"] = entry["value"]
                    continue
                # zoneStatus/zoneConfiguration: per-zone fields nested under "value"
                if isinstance(entry.get("value"), dict):
                    value = entry["value"]
            if idx is None:
                try:
                    idx = int(key)
                except (TypeError, ValueError):
                    continue
            zattrs = zones.setdefault(idx, {})
            if isinstance(value, dict):
                for k, v in value.items():
                    dst = self._ZONE_ALIASES.get(k)
                    if dst and not isinstance(v, (dict, list)):
                        zattrs[dst] = v
                # zone-name reads: a {"name": "..."} label
                if "name" not in zattrs and isinstance(value.get("name"), str):
                    zattrs["name"] = value["name"]
            elif isinstance(value, str):
                zattrs["name"] = value

    def _extract_mfd_entry(self, entry: dict, attrs: dict, zones: dict) -> None:
        """Handle one payload entry {"name": <field>, "value"/"items": ...}."""
        nm = entry.get("name")
        val = entry.get("value")
        items = entry.get("items")
        if items is None and isinstance(val, list):
            items = val

        if nm in self._ZONE_MFD_NAMES:
            self._collect_zones(items if items is not None else val, zones)
            return
        if nm == "panelName":
            s = self._first_string(val) or self._first_string(items)
            if s:
                attrs["panel_name"] = s
            return
        if nm == "gatewayVersions":
            if isinstance(val, dict):
                # observed shape: {"major":1,"mid":1,"minor":32,"build":0,"hardware":16}
                parts = [val.get(k) for k in ("major", "mid", "minor", "build")]
                if any(p is not None for p in parts):
                    attrs["firmware_version"] = ".".join(
                        str(int(p)) for p in parts if p is not None
                    )
                    return
                for key in ("version", "gatewayVersion", "firmwareVersion",
                            "firmware", "panelVersion", "app"):
                    if isinstance(val.get(key), str):
                        attrs["firmware_version"] = val[key]
                        return
                s = self._first_string(val)
                if s:
                    attrs["firmware_version"] = s
            return
        if nm == "highestUsedIndexes":
            if isinstance(val, dict):
                for key in ("zoneIndex", "zone", "highestIndexZone",
                            "highest_index_zone", "zones", "highestZone"):
                    if key in val:
                        try:
                            attrs["highest_zone_index"] = int(val[key])
                            return
                        except (TypeError, ValueError):
                            pass
            return
        if nm == "armingLevelName":
            names: dict[int, str] = {}
            src = items if items is not None else val
            if isinstance(src, list):
                for it in src:
                    if not isinstance(it, dict):
                        continue
                    try:
                        i = int(it.get("index"))
                    except (TypeError, ValueError):
                        continue
                    lab = self._first_string(it.get("value")) or self._first_string(it)
                    if lab:
                        names[i] = lab
            if names:
                attrs["arming_level_names"] = names
            return

        # default: panelStatus / systemStatus / partitionStatus / *Delay -> field objects
        if isinstance(val, dict):
            self._collect_panel(val, attrs)
        if isinstance(items, list):
            for it in items:
                if isinstance(it, dict) and isinstance(it.get("value"), dict):
                    self._collect_panel(it["value"], attrs)

    def _maybe_apply_status(self, msg: dict) -> None:
        # Applies both solicited read responses and UNSOLICITED live pushes: when a zone
        # opens/closes or the arming level changes, the panel pushes a sendMfd/sendMfdEnhanced
        # frame in this same shape, so handling it here gives real-time updates between polls.
        attrs: dict = {}
        zones: dict = {}
        device_id: Optional[str] = None

        event = msg.get("event") if isinstance(msg.get("event"), dict) else None
        if event:
            device_id = event.get("deviceId") or device_id
            # device.status push: {"status": {"online": true, "lastEvent": ...}}
            st = event.get("status")
            if isinstance(st, dict) and "online" in st:
                attrs["online"] = st["online"]
            data = event.get("data") if isinstance(event.get("data"), dict) else None
            if data:
                payload = data.get("payload")
                entries: list = []
                if isinstance(payload, list):
                    entries = payload
                elif isinstance(payload, dict):
                    entries = [payload]
                for e in entries:
                    if isinstance(e, dict):
                        self._extract_mfd_entry(e, attrs, zones)
        else:
            # legacy / unknown shape: best-effort top-level deviceId + flat fields
            device_id = msg.get("deviceId") or msg.get("device_id")
            for k, v in msg.items():
                dst = self._FIELD_ALIASES.get(k)
                if dst and not isinstance(v, (dict, list)):
                    attrs[dst] = v

        if not device_id or (not attrs and not zones):
            return
        ps = self.panels.get(str(device_id)) or PanelState(device_id=str(device_id))
        if attrs:
            ps.apply(attrs)
        for idx, zattrs in zones.items():
            ps.zone(idx).apply(zattrs)
        self.panels[str(device_id)] = ps
        if self._update_callback:
            res = self._update_callback(ps)
            if asyncio.iscoroutine(res):
                asyncio.ensure_future(res)

    async def _ws_send(self, channel: str, verb: str, payload: dict) -> None:
        if self._ws is None or self._ws.closed:
            await self.async_connect_ws()
        assert self._ws is not None
        envelope = {"channel": channel, "id": str(uuid.uuid4()), verb: payload}
        await self._ws.send_str(json.dumps(envelope) + "\r\n")

    async def _helix_command(self, device_id: str, cmdrsp: str, payload: dict,
                             *, wait: bool = False, timeout: float = 15.0) -> Optional[dict]:
        request_id = str(uuid.uuid4())
        inner = {
            "deviceId": device_id,
            "cmdrsp": cmdrsp,
            "payload": payload,
            "requestId": request_id,
        }
        fut: Optional[asyncio.Future] = None
        if wait:
            fut = asyncio.get_event_loop().create_future()
            self._pending[request_id] = fut
        await self._ws_send(CHANNEL_HELIX, "send", inner)
        if fut is None:
            return None
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise CoveAlulaError(f"no response to {cmdrsp} within {timeout}s")

    # ---- subscriptions + state reads ------------------------------------
    #
    # Nothing pushes panel state until you subscribe. The app's subscribeToHelix() sends
    # a subscribe frame on BOTH device.status and device.helix for the device, then pulls
    # a current snapshot. We do the same.

    async def _send_subscribe(self, channel: str, device_id: str) -> None:
        await self._ws_send(channel, "subscribe", {"deviceId": device_id})

    async def _send_subscriptions(self, device_id: str) -> None:
        await self._send_subscribe(CHANNEL_STATUS, device_id)
        await self._send_subscribe(CHANNEL_HELIX, device_id)

    async def _resubscribe_and_reconcile(self, device_id: str) -> None:
        """Run after the (re)connect ready handshake: re-subscribe for future pushes, then
        re-read live state. Subscribing alone does NOT replay current values, so without the
        reconcile any zone open/close that happened while the socket was down would stay
        stale in Home Assistant until the next physical change."""
        try:
            await self._send_subscriptions(device_id)
            await self.async_reconcile(device_id)
        except CoveAlulaError as err:
            _LOGGER.debug("resubscribe/reconcile failed for %s: %s", device_id, err)

    async def async_reconcile(self, device_id: str) -> None:
        """Re-read live panel + zone status so cached state matches the panel. Lighter than
        async_refresh_state (skips names/config, which don't change). Safe to call any time;
        waits briefly for the connect-ready handshake so reads aren't sent too early."""
        if self._ws_task is None or self._ws_task.done():
            await self.async_connect_ws()
        try:
            await asyncio.wait_for(self._connected.wait(), 5.0)
        except asyncio.TimeoutError:
            _LOGGER.debug("reconcile: no ready frame within 5s; reading anyway")
        ps = self.panels.get(device_id)
        if ps is None or ps.highest_zone_index is None:
            try:
                # Was hardcoded to 5s, well under this panel/cloud combination's real
                # round-trip time -- observed live to fail 100% of the time, every single
                # reconcile cycle, never once succeeding at 5s. _read_mfd's own default
                # (15.0) is what every other MFD read in this file already relies on;
                # there was no reason for this one call to override it down.
                await self.request_highest_indices(device_id, wait=True, timeout=15.0)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("reconcile: highestUsedIndexes unavailable (%s)", err)
        await self.request_panel_status(device_id)
        await asyncio.sleep(0.15)
        ps = self.panels.get(device_id)
        if ps is None or ps.highest_zone_index is None:
            # Real zone count still unknown (e.g. this reconcile raced the initial
            # setup's own highestUsedIndexes read, or the request above also failed/
            # timed out). Previously this fell back to `last = 63`, scanning the full
            # protocol-supported zone range and creating a phantom entity for every
            # response -- observed live as a burst of ~58 bogus "Zone N" entities that
            # don't correspond to any real panel zone. Skip this reconcile's zone-status
            # refresh instead of guessing; the explicit setup flow (or a later reconcile,
            # once the index is known) fills in real zone state shortly after.
            _LOGGER.debug(
                "reconcile: highest zone index still unknown for %s; skipping zone-status "
                "refresh rather than scanning the full 0-63 range", device_id,
            )
            return
        await self.request_zone_statuses(device_id, 0, int(ps.highest_zone_index))

    async def async_subscribe_device(self, device_id: str, *, ready_timeout: float = 8.0) -> None:
        """Subscribe to live status + helix channels for a device.

        Waits (briefly) for the server's connect-ready frame first, like the app does,
        then sends the subscribe frames. The device is remembered so we automatically
        re-subscribe after any reconnect.
        """
        if self._ws_task is None or self._ws_task.done():
            await self.async_connect_ws()
        self._subscribed.add(str(device_id))
        try:
            await asyncio.wait_for(self._connected.wait(), ready_timeout)
        except asyncio.TimeoutError:
            _LOGGER.debug("no ready frame within %ss; subscribing anyway", ready_timeout)
        await self._send_subscriptions(str(device_id))

    async def _read_mfd(self, device_id: str, field_specs: list[dict],
                        *, wait: bool = False, timeout: float = 15.0) -> Optional[dict]:
        """Send a requestMfd read (payload is an array of field-spec objects).

        Mirrors HelixRequest.sendSocketRequestInArray: cmdrsp 'requestMfd', payload is the
        array, plus bypassCache:true so we get a fresh read rather than a cached value.
        """
        request_id = str(uuid.uuid4())
        inner = {
            "deviceId": device_id,
            "cmdrsp": CMD_REQUEST_MFD,
            "bypassCache": True,
            "payload": field_specs,
            "requestId": request_id,
        }
        # remember what we asked for so the receive loop can report ok/NAK per read
        try:
            name = field_specs[0].get("name")
            if name:
                self._req_names[request_id] = name
                self._read_results.setdefault(name, "pending")
        except (IndexError, AttributeError):
            pass
        fut: Optional[asyncio.Future] = None
        if wait:
            fut = asyncio.get_event_loop().create_future()
            self._pending[request_id] = fut
        await self._ws_send(CHANNEL_HELIX, "send", inner)
        if fut is None:
            return None
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise CoveAlulaError(f"no MFD response within {timeout}s")

    async def request_panel_status(self, device_id: str, **kw) -> Optional[dict]:
        return await self._read_mfd(device_id, [{"name": "panelStatus"}], **kw)

    async def request_system_status(self, device_id: str, **kw) -> Optional[dict]:
        return await self._read_mfd(device_id, [{"name": "systemStatus"}], **kw)

    async def request_partition_status(self, device_id: str, first: int = 0,
                                       last: int = 0, **kw) -> Optional[dict]:
        return await self._read_mfd(
            device_id,
            [{"name": "partitionStatus", "indexFirst": first, "indexLast": last}],
            **kw,
        )

    async def request_arming_level_names(self, device_id: str, **kw) -> Optional[dict]:
        """Read the per-panel arming-level labels (indices 0-7). Use this to confirm which
        numeric level means stay/away/night on your panel."""
        return await self._read_mfd(
            device_id,
            [{"name": "armingLevelName", "indexFirst": 0, "indexLast": 7}],
            **kw,
        )

    async def request_panel_name(self, device_id: str, **kw) -> Optional[dict]:
        """Read the friendly system name (e.g. 'My Home')."""
        return await self._read_mfd(device_id, [{"name": "panelName"}], **kw)

    async def request_firmware_version(self, device_id: str, **kw) -> Optional[dict]:
        return await self._read_mfd(device_id, [{"name": "gatewayVersions"}], **kw)

    async def request_highest_indices(self, device_id: str, **kw) -> Optional[dict]:
        """Read highestUsedIndexes; tells us how many zones/users/etc. exist."""
        return await self._read_mfd(device_id, [{"name": "highestUsedIndexes"}], **kw)

    async def request_zone_names(self, device_id: str, first: int = 0,
                                 last: int = 63, **kw) -> Optional[dict]:
        return await self._read_mfd(
            device_id, [{"name": "zoneName", "indexFirst": first, "indexLast": last}], **kw
        )

    async def request_zone_statuses(self, device_id: str, first: int = 0,
                                    last: int = 63, **kw) -> Optional[dict]:
        return await self._read_mfd(
            device_id, [{"name": "zoneStatus", "indexFirst": first, "indexLast": last}], **kw
        )

    async def request_zone_configurations(self, device_id: str, first: int = 0,
                                          last: int = 63, **kw) -> Optional[dict]:
        return await self._read_mfd(
            device_id,
            [{"name": "zoneConfiguration", "indexFirst": first, "indexLast": last}],
            **kw,
        )

    async def async_load_zones(self, device_id: str, *, last: Optional[int] = None) -> None:
        """Pull zone names, configurations, and live statuses for the panel. Responses
        arrive on the receive loop and populate PanelState.zones. Capped to the panel's
        highest used zone index when known so we don't create phantom zones.

        The `last=63` default this used to carry was itself the bug: if the caller didn't
        pass an explicit value and `highest_zone_index` wasn't cached yet (e.g. the
        `request_highest_indices` call in async_refresh_state's budgeted sequence didn't
        complete in time -- a real, observed failure mode, not hypothetical), this silently
        fell through to scanning the full protocol-supported 0-63 range, creating a phantom
        entity for every response. Observed live, twice, from two different callers before
        this one was found. No caller in this codebase passes `last` explicitly, so the
        only way to get a real value here is the cache lookup below -- if that's empty,
        skip instead of guessing.
        """
        await self.async_subscribe_device(device_id)
        ps = self.panels.get(device_id)
        if ps and ps.highest_zone_index is not None:
            last = max(0, int(ps.highest_zone_index))
        if last is None:
            _LOGGER.debug(
                "async_load_zones: zone count unknown for %s; skipping zone load rather "
                "than scanning the full 0-63 range -- a later refresh/reconcile will pick "
                "it up once the index is known", device_id,
            )
            return
        await self.request_zone_names(device_id, 0, last)
        await asyncio.sleep(0.2)
        await self.request_zone_configurations(device_id, 0, last)
        await asyncio.sleep(0.2)
        await self.request_zone_statuses(device_id, 0, last)

    async def _write_mfd(self, device_id: str, field: dict, *, wait: bool = False,
                         timeout: float = 15.0) -> Optional[dict]:
        """Send a writeMfd command (the verb the app uses for zoneBypass/forceArm/etc.)."""
        request_id = str(uuid.uuid4())
        inner = {
            "deviceId": device_id,
            "cmdrsp": CMD_WRITE_MFD,
            "bypassCache": True,
            "payload": [field],
            "requestId": request_id,
        }
        try:
            if field.get("name"):
                self._req_names[request_id] = field["name"]
        except AttributeError:
            pass
        fut: Optional[asyncio.Future] = None
        if wait:
            fut = asyncio.get_event_loop().create_future()
            self._pending[request_id] = fut
        await self._ws_send(CHANNEL_HELIX, "send", inner)
        if fut is None:
            return None
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise CoveAlulaError(f"no writeMfd response within {timeout}s")

    async def async_set_zone_bypass(self, device_id: str, zone_index: int,
                                    bypass: bool, *, user_number: int = 0,
                                    wait: bool = False, timeout: float = 15.0) -> Optional[dict]:
        """Bypass / unbypass a zone via writeMfd (zoneBypass / zoneUnbypass)."""
        field = {
            "name": "zoneBypass" if bypass else "zoneUnbypass",
            "indexFirst": zone_index,
            "indexLast": zone_index,
            "items": [{"index": zone_index, "value": {"userNumber": user_number}}],
        }
        return await self._write_mfd(device_id, field, wait=wait, timeout=timeout)

    async def async_force_arm(self, device_id: str, level: int, *,
                              user_number: int = 0, wait: bool = False) -> Optional[dict]:
        """Arm past open zones using the panel's native forceArm (writeMfd, user-number
        based, no PIN). Use async_arm_bypassing_open for a PIN-based alternative."""
        field = {"name": "forceArm",
                 "value": {"armingLevelValue": int(level), "userNumber": int(user_number)}}
        return await self._write_mfd(device_id, field, wait=wait)

    async def async_arm_bypassing_open(self, device_id: str, level: int, pin: str, *,
                                       user_number: int = 0, wait: bool = False) -> Optional[dict]:
        """Bypass any currently-open zones, then arm with the proven PIN command.

        This is the most reliable "force arm" on panels where forceArm/partition commands
        are uncertain: it only uses zoneBypass + changeArmingLevelUsingCode, both verified.
        Bypasses clear when the panel is next disarmed.
        """
        # make sure we have fresh zone status to know what's open. Use the real,
        # already-known zone count rather than unconditionally scanning 0-63 -- that
        # scan is what created a phantom entity for every response, observed live as a
        # burst of ~58-64 bogus "Zone N" entities (see async_reconcile's equivalent fix).
        # highest_zone_index is learned during setup, well before any arm attempt, so the
        # unknown branch below should be rare in practice.
        ps = self.panels.get(device_id)
        zone_last = int(ps.highest_zone_index) if (ps and ps.highest_zone_index is not None) else None
        try:
            if zone_last is not None:
                await self.request_zone_statuses(device_id, 0, zone_last)
            else:
                _LOGGER.debug(
                    "async_arm_bypassing_open: zone count unknown for %s; skipping "
                    "zone-status refresh rather than scanning the full 0-63 range",
                    device_id,
                )
            await asyncio.sleep(1.2)
        except CoveAlulaError:
            pass
        ps = self.panels.get(device_id)
        open_zones = ps.not_ready_zones if ps else []
        for z in open_zones:
            try:
                await self.async_set_zone_bypass(device_id, z.index, True,
                                                 user_number=user_number)
            except CoveAlulaError as err:
                _LOGGER.warning("bypass of zone %s failed: %s", z.index, err)
        if open_zones:
            await asyncio.sleep(1.0)
        return await self.async_set_arming_level(device_id, level, pin, wait=wait)

    async def async_refresh_state(self, device_id: str, *, budget: float = 30.0) -> None:
        """Subscribe, then pull a fresh snapshot. Reads are issued sequentially and the
        critical ones are awaited, because firing many reads back-to-back makes the cloud
        drop some responses (observed: panelName/panelStatus occasionally missing). We wait
        for highestUsedIndexes before loading zones so we read only the real zone range.

        `budget` caps the TOTAL time spent here. Without it a slow or unresponsive cloud
        could park this for ~45s, and Home Assistant cancels config-entry setup that blocks
        that long (which surfaced as a CancelledError mid-read). Anything not read within
        the budget simply arrives later via the coordinator poll or a live push.
        """
        await self.async_subscribe_device(device_id)
        deadline = time.monotonic() + budget

        async def _try(coro_fn, *, timeout: float = 8.0) -> None:
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                return  # out of budget; leave the rest to the poll/pushes
            try:
                await coro_fn(device_id, wait=True, timeout=min(timeout, remaining))
            except asyncio.CancelledError:
                raise  # never swallow cancellation (HA shutdown / entry reload)
            except Exception as err:  # noqa: BLE001 - NAK, timeout, transient socket error
                _LOGGER.debug("refresh read failed (%s); continuing", err)
            await asyncio.sleep(0.2)

        await _try(self.request_panel_name)
        await _try(self.request_firmware_version)
        await _try(self.request_highest_indices)   # sets highest_zone_index for zone load
        await _try(self.request_panel_status)       # arming level + troubles
        # these are unsupported on some panels (NAK quickly); harmless to try
        await _try(self.request_system_status, timeout=5.0)
        await _try(self.request_partition_status, timeout=5.0)
        # now that the zone count is known, load only the real zones
        if time.monotonic() < deadline:
            try:
                await self.async_load_zones(device_id)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("zone load failed (%s); poll/pushes will fill in", err)

    async def async_probe_reads(self, device_id: str) -> dict[str, str]:
        """Fire every candidate MFD read once and return {read_name: 'ok'|'NAK: ...'}.

        Used to discover which panel-data codes a specific panel firmware supports, since
        some models NAK panelStatus/systemStatus and expose state via partitionStatus etc.
        """
        await self.async_subscribe_device(device_id)
        self._read_results.clear()
        reads = [
            ("armingLevelName", lambda: self.request_arming_level_names(device_id)),
            ("panelName", lambda: self.request_panel_name(device_id)),
            ("gatewayVersions", lambda: self.request_firmware_version(device_id)),
            ("highestUsedIndexes", lambda: self.request_highest_indices(device_id)),
            ("panelStatus", lambda: self.request_panel_status(device_id)),
            ("systemStatus", lambda: self.request_system_status(device_id)),
            ("partitionStatus", lambda: self.request_partition_status(device_id, 0, 1)),
            ("entryDelay", lambda: self._read_mfd(device_id, [{"name": "entryDelay"}])),
            ("exitDelay", lambda: self._read_mfd(device_id, [{"name": "exitDelay"}])),
            ("zoneName", lambda: self.request_zone_names(device_id, 0, 31)),
            ("zoneStatus", lambda: self.request_zone_statuses(device_id, 0, 31)),
            ("zoneConfiguration", lambda: self.request_zone_configurations(device_id, 0, 31)),
        ]
        for _, fn in reads:
            await fn()
            await asyncio.sleep(0.4)
        # give late responses time to arrive
        await asyncio.sleep(4)
        return dict(self._read_results)

    # ---- high level arm/disarm ------------------------------------------

    async def async_set_arming_level(
        self,
        device_id: str,
        level: int,
        pin: str,
        *,
        silent: bool = False,
        no_entry_delay: bool = False,
        wait: bool = False,
        max_retries: int = 2,
        retry_delay: float = 3.0,
    ) -> Optional[dict]:
        """Set the arming level using `pin`. 1=disarm, 2=stay, 3=night, 4=away, … (the
        meaning of each armed level is per-panel; confirm with request_arming_level_names).

        The command differs by Alula panel family: Helix uses changeArmingLevelUsingCode
        (numeric level + PIN); ConnectFlex uses partitionArmingLevelChange (string level +
        partitions, armed by user number, PIN only for disarm). We pick the command the
        panel most likely wants, and if the panel rejects it as an *unsupported command* we
        automatically retry with the other family's command and remember which one worked —
        so this is correct even when we can't identify the panel family up front.

        Retries on a plain timeout (CoveAlulaError from _helix_command), up to
        `max_retries` additional attempts with `retry_delay` between them -- observed live
        that a single WS round-trip occasionally doesn't get a response within 12s (most
        likely contention with the coordinator's own periodic reconcile traffic on the same
        connection), with no retry previously in place, so a single dashboard tap could
        require the user to press it again by hand. Arming/disarming an already-armed or
        already-disarmed panel is a safe no-op on this hardware, so retrying the whole
        command (including re-running the family-detection fallback below) is safe -- this
        never risks a double physical action, only a repeated one."""
        order = (["partition", "code"]
                 if self._preferred_arming_kind(device_id) == "partition"
                 else ["code", "partition"])
        last: Optional[dict] = None
        for attempt in range(max_retries + 1):
            try:
                for i, kind in enumerate(order):
                    command, payload = self._build_arming_command(
                        kind, level, pin, silent=silent, no_entry_delay=no_entry_delay
                    )
                    # wait on all but the final attempt so we can detect an
                    # unsupported-command NAK and fall back; honor the caller's `wait`
                    # on the last attempt
                    want = True if i < len(order) - 1 else wait
                    # Was 12.0s. Observed live: all 3 attempts (the original call plus
                    # both retries added above) failed at exactly this mark on a real
                    # arm attempt -- the same signature as the highestUsedIndexes 5s
                    # timeout that turned out to just be too tight for this install's
                    # real round-trip time, not a genuine unsupported-command case
                    # (those NAK immediately rather than timing out). Bumped to 20.0s;
                    # the retry loop above remains as a safety net for genuine blips.
                    resp = await self._helix_command(
                        device_id, command, payload, wait=want, timeout=20.0
                    )
                    last = resp
                    if not _is_unsupported_command_nak(resp):
                        self._arming_command_kind[device_id] = kind  # this family works
                        return resp
                    _LOGGER.info(
                        "panel %s rejected %s as unsupported; retrying with the other "
                        "arming command", device_id, command,
                    )
                return last
            except CoveAlulaError as err:
                if attempt < max_retries:
                    _LOGGER.warning(
                        "arm/disarm command for %s timed out (attempt %d/%d): %s; "
                        "retrying in %.1fs", device_id, attempt + 1, max_retries + 1,
                        err, retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
                    continue
                raise
        return last

    def _preferred_arming_kind(self, device_id: str) -> str:
        """Which arming command to try first: 'partition' for ConnectFlex-family panels,
        else 'code' (Helix). Uses, in order: a previously-learned result, an explicit
        connectedPanel family id, or the panel's read capabilities (a partition panel
        answers partitionStatus but NAKs panelStatus)."""
        cached = self._arming_command_kind.get(device_id)
        if cached:
            return cached
        panel = self.panels.get(device_id)
        if panel is not None and panel.supports_partition_arming:
            return "partition"
        rr = self._read_results
        if rr.get("partitionStatus") == "ok" and str(rr.get("panelStatus", "")).startswith("NAK"):
            return "partition"
        return "code"

    def _build_arming_command(
        self, kind: str, level: int, pin: str, *, silent: bool, no_entry_delay: bool
    ) -> tuple[str, dict]:
        """Build the (command, payload) pair for one arming-command family."""
        if kind == "partition":
            name = _PARTITION_LEVEL_NAME.get(int(level))
            if name is None:
                raise CoveAlulaError(
                    f"partition arming does not support arming level {level}"
                )
            payload = {
                "armingLevel": name,
                "partitions": [True, False, False, False, False, False, False, False],
                "armSilent": bool(silent),
                "noEntryDelay": bool(no_entry_delay),
                # ConnectFlex arms by user number; disarm authenticates with the PIN
                "authType": "pin" if level == LEVEL_DISARM else "user",
                "userNumber": 0,
                "forceArm": False,
            }
            if level == LEVEL_DISARM:
                payload["pin"] = _pin_to_array(pin)
            return CMD_CHANGE_ARMING_LEVEL_PARTITION, payload
        payload = {
            "armingLevelValue": int(level),
            "armSilent": bool(silent),
            "noEntryDelay": bool(no_entry_delay),
            "pin": _pin_to_array(pin),
        }
        return CMD_CHANGE_ARMING_LEVEL_CODE, payload

    async def async_disarm(self, device_id: str, pin: str, **kw) -> Optional[dict]:
        return await self.async_set_arming_level(device_id, LEVEL_DISARM, pin, **kw)

    async def async_arm_stay(self, device_id: str, pin: str, **kw) -> Optional[dict]:
        return await self.async_set_arming_level(device_id, LEVEL_STAY, pin, **kw)

    async def async_arm_away(self, device_id: str, pin: str, **kw) -> Optional[dict]:
        return await self.async_set_arming_level(device_id, LEVEL_AWAY, pin, **kw)

    async def async_arm_night(self, device_id: str, pin: str, **kw) -> Optional[dict]:
        return await self.async_set_arming_level(device_id, LEVEL_NIGHT, pin, **kw)


# --------------------------------------------------------------------------
# tiny CLI for standalone testing (no Home Assistant needed)
# --------------------------------------------------------------------------

async def _cli() -> None:
    import sys

    logging.basicConfig(level=logging.INFO)
    args = sys.argv[1:]
    if len(args) < 3:
        print(__doc__)
        return
    cmd, email, password = args[0], args[1], args[2]
    pin = args[3] if len(args) > 3 else None

    # Print every inbound frame so the actual panel/state JSON is visible. Invaluable for
    # confirming the per-panel arming-level numbers and the exact push shape.
    def _dump(frame: dict) -> None:
        try:
            print("  << ", json.dumps(frame)[:1000])
        except Exception:
            print("  << ", frame)

    verbose = cmd != "login"
    client = CoveAlulaClient(raw_callback=_dump if verbose else None)
    try:
        await client.async_login(email, password)
        print("login OK; refresh_token saved in memory")

        if cmd == "login":
            return

        panels = await client.async_get_panels()
        if not panels:
            print("no panels found on this account")
            return
        panel = panels[0]
        print(f"panel: id={panel.device_id} name={panel.name!r} "
              f"online={panel.online} arming_level={panel.arming_level} "
              f"serial={panel.serial_number}")

        def _print_state(tag: str = "live state") -> None:
            p = client.panels[panel.device_id]
            print(f"{tag}:", json.dumps({
                "panel_name": p.panel_name,
                "firmware": p.firmware_version,
                "arming_level": p.arming_level,
                "ready": p.ready,
                "not_ready_zones": [z.name or f"zone {z.index}" for z in p.not_ready_zones],
                "alarm": p.alarm,
                "in_exit_delay": p.in_exit_delay,
                "in_entry_delay": p.in_entry_delay,
                "low_battery": p.low_battery,
                "ac_failure": p.ac_failure,
                "tamper": p.tamper,
            }, indent=2))

        def _print_zones() -> None:
            p = client.panels[panel.device_id]
            if not p.zones:
                print("zones: (none parsed yet — check raw frames above for the shape)")
                return
            print(f"zones ({len(p.zones)}):")
            for idx in sorted(p.zones):
                z = p.zones[idx]
                state = "open" if z.open else ("closed" if z.open is not None else "?")
                flags = []
                if z.bypassed:
                    flags.append("bypassed")
                if z.alarm:
                    flags.append("ALARM")
                if z.tamper:
                    flags.append("tamper")
                if z.low_battery:
                    flags.append("low-batt")
                if z.trouble:
                    flags.append("trouble")
                extra = (" [" + ",".join(flags) + "]") if flags else ""
                typ = f" type={z.device_type or z.ui_type}" if (z.device_type or z.ui_type) else ""
                print(f"  #{idx:<3} {z.name or '(unnamed)':<24} {state}{extra}{typ}")

        if cmd in ("status", "diag"):
            # subscribe + pull a fresh snapshot, then listen so pushes + responses land
            await client.async_refresh_state(panel.device_id)
            if cmd == "diag":
                await client.request_arming_level_names(panel.device_id)
            print("subscribed + requested snapshot; listening 25s (raw frames above)…")
            for _ in range(25):
                await asyncio.sleep(1)
            _print_state()
            _print_zones()
            return

        if cmd == "zones":
            await client.async_subscribe_device(panel.device_id)
            await client.request_highest_indices(panel.device_id)
            await asyncio.sleep(2)
            await client.async_load_zones(panel.device_id)
            print("requested zone names/config/status; listening 20s (raw frames above)…")
            for _ in range(20):
                await asyncio.sleep(1)
            _print_zones()
            return

        if cmd == "watch":
            # Subscribe, snapshot, then print changes as the panel pushes them live.
            await client.async_refresh_state(panel.device_id)
            await asyncio.sleep(2)
            _print_state("initial")
            print("\nwatching for live changes — open/close a zone or arm/disarm "
                  "(listening 180s)…")
            last = None
            for _ in range(90):
                await asyncio.sleep(2)
                p = client.panels[panel.device_id]
                snap = json.dumps({
                    "level": p.arming_level, "ready": p.ready,
                    "entry": p.in_entry_delay, "exit": p.in_exit_delay, "alarm": p.alarm,
                    "open": sorted(z.index for z in p._configured_zones() if z.open),
                }, sort_keys=True)
                if snap != last:
                    p2 = client.panels[panel.device_id]
                    print("  change @%s:" % time.strftime("%H:%M:%S"), snap,
                          "open:", [z.name or f"#{z.index}" for z in p2.not_ready_zones])
                    last = snap
            return

        if cmd == "probe":
            results = await client.async_probe_reads(panel.device_id)
            print("\n=== MFD read support on this panel ===")
            for name in sorted(results):
                print(f"  {name:<24} {results[name]}")
            print("\n(ok = panel returned data; NAK = unsupported on this panel)")
            _print_state()
            _print_zones()
            return

        if cmd == "names":
            # just read the per-panel arming level labels (which number == stay/away/night)
            await client.async_subscribe_device(panel.device_id)
            await client.request_arming_level_names(panel.device_id)
            print("requested arming-level names; listening 12s (look for armingLevelName)…")
            for _ in range(12):
                await asyncio.sleep(1)
            return

        if cmd == "bypass" or cmd == "unbypass":
            # usage: bypass <user> <pass> <zoneIndex>
            if pin is None:
                print("usage: (un)bypass <user> <pass> <zoneIndex>")
                return
            zone_index = int(pin)
            await client.async_subscribe_device(panel.device_id)
            print(f"sending {cmd} for zone {zone_index}…")
            await client.async_set_zone_bypass(
                panel.device_id, zone_index, bypass=(cmd == "bypass")
            )
            await client.request_zone_statuses(panel.device_id, 0, 63)
            for _ in range(10):
                await asyncio.sleep(1)
            _print_zones()
            return

        if cmd in ("cancel", "confirm"):
            # alarm cancel / confirm via the REST RPC
            print(f"sending alarm {cmd}…")
            res = await (client.async_cancel_alarm(panel.device_id)
                         if cmd == "cancel" else client.async_confirm_alarm(panel.device_id))
            print("response:", json.dumps(res)[:500] if res is not None else "(none)")
            return

        level_map = {
            "disarm": LEVEL_DISARM, "arm_stay": LEVEL_STAY,
            "arm_home": LEVEL_STAY, "arm_away": LEVEL_AWAY, "arm_night": LEVEL_NIGHT,
        }
        # force_arm_* = bypass any open zones, then arm with PIN (reliable)
        force_map = {
            "force_arm_stay": LEVEL_STAY, "force_arm_home": LEVEL_STAY,
            "force_arm_away": LEVEL_AWAY, "force_arm_night": LEVEL_NIGHT,
        }
        if cmd in force_map:
            if not pin:
                print("this command needs a PIN: ... <cmd> <email> <pass> <pin>")
                return
            await client.async_refresh_state(panel.device_id)
            await asyncio.sleep(2)
            _print_state("state before")
            print(f"force-arming ({cmd}); bypassing open zones then arming…")
            await client.async_arm_bypassing_open(panel.device_id, force_map[cmd], pin)
            for _ in range(15):
                await asyncio.sleep(1)
            _print_state("state after")
            _print_zones()
            return

        if cmd in level_map:
            if not pin:
                print("this command needs a PIN: ... <cmd> <email> <pass> <pin>")
                return
            # subscribe first so we can observe the resulting state change
            await client.async_subscribe_device(panel.device_id)
            await client.request_panel_status(panel.device_id)
            await asyncio.sleep(1)
            _print_state("state before")
            print(f"sending {cmd} (armingLevelValue {level_map[cmd]})…")
            await client.async_set_arming_level(panel.device_id, level_map[cmd], pin)
            print("command sent; watching state for 15s…")
            for _ in range(15):
                await asyncio.sleep(1)
            _print_state("state after")
            return

        print(f"unknown command {cmd!r}")
        print("commands: login | status | diag | probe | watch | names | zones | "
              "bypass | unbypass | cancel | confirm | disarm | arm_stay | arm_away | "
              "arm_night | force_arm_stay | force_arm_away | force_arm_night")
    finally:
        await client.async_close()


if __name__ == "__main__":
    asyncio.run(_cli())
