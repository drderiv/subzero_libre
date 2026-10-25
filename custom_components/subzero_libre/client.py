"""Asynchronous Python API client for Sub-Zero cloud and appliances."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import uuid
from collections.abc import Callable, Coroutine
from typing import Any, Final

import aiohttp

from .auth import apim_headers, decode_jwt_payload
from .const import (
    APIM_BASE_URL,
    B2C_TOKEN_URL,
    CLIENT_ID,
    MODEL_MAPPING,
    REDIRECT_URI,
    SignalRMessage,
)

_LOGGER = logging.getLogger(__name__)

RECORD_SEPARATOR = "\x1e"

# Cloud keepalive (open_cloud_async) every ~25 min, jittered +/- 2 min
KEEPALIVE_INTERVAL: Final = 1500
KEEPALIVE_JITTER: Final = 120

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
_SIGNALR_HANDSHAKE_TIMEOUT = 10
_SIGNALR_RETRY_MIN = 5
_SIGNALR_RETRY_MAX = 60
_STALE_WINDOW = 60  # seconds a newer property value outranks an older seq

_SECRET_PARAM_RE = re.compile(
    r"\b(access_token|id_token|refresh_token|code|code_verifier)=[^&\s'\"]+"
)
_BEARER_RE = re.compile(r"(Bearer\s+)[\w.\-~+/]+=*", re.IGNORECASE)


def redact_error(err: BaseException | str) -> str:
    """Return str(err) with bearer tokens / token query parameters masked.

    aiohttp exceptions embed the full request URL in their string form. For the
    SignalR hub that URL carries ?access_token=<JWT>, so any aiohttp exception
    text must pass through this helper before being logged or re-raised.
    """
    text = _SECRET_PARAM_RE.sub(r"\1=***", str(err))
    return _BEARER_RE.sub(r"\1***", text)


class SubZeroError(Exception):
    """Base exception for Sub-Zero errors."""


class SubZeroAuthError(SubZeroError):
    """Authentication or token error."""


class SubZeroConnectionError(SubZeroError):
    """Connection or network error."""


def _maybe_json(value: Any) -> Any:
    """Decode a JSON-encoded string; return any other value (or bad JSON) unchanged."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as err:
            _LOGGER.debug("Value is not valid JSON: %s", err)
    return value


def extract_telemetry(arg: dict[str, Any]) -> tuple[str | None, str | None, dict[str, Any]]:
    """Pull (device_id, serial, properties) out of one SignalR invocation argument."""
    device_id = arg.get("DeviceId") or arg.get("deviceId") or arg.get("id")
    serial: str | None = arg.get("serialNumber") or arg.get("serial")
    props: dict[str, Any] = {}

    payload = _maybe_json(arg.get("Payload"))
    if isinstance(payload, dict):
        async_ch = _maybe_json(payload.get("api.async_channel"))
        pload = async_ch.get("pload") if isinstance(async_ch, dict) else None
        if isinstance(pload, dict):
            serial = pload.get("appliance_serial") or serial
            # Full snapshot state tree (type 1)
            props = {k: v for k, v in pload.items() if k not in ("notifs", "props")}
            # Delta updates (type 2)
            if isinstance(diff_props := pload.get("props"), dict):
                props.update(diff_props)

    # Fallback to twin-style {"properties": {"reported": {...}}} if not wrapped in async_channel
    if not props and isinstance(wrapper := arg.get("properties"), dict):
        if isinstance(reported := wrapper.get("reported"), dict):
            props = dict(reported)

    return device_id, serial, props


class SubZeroAppliance:
    """Represents a Sub-Zero smart appliance."""

    def __init__(self, raw_data: dict[str, Any]) -> None:
        """Initialize appliance representation."""
        # Dynamic state updated via SignalR or local updates; never reset by metadata refreshes
        self.properties: dict[str, Any] = {}
        self._property_seq: dict[str, tuple[int, float]] = {}
        self._max_seq = 0
        self.faults: list[dict[str, Any]] = []
        self.is_connected: bool = False
        self.update_metadata(raw_data)

    def update_metadata(self, raw_data: dict[str, Any]) -> None:
        """Refresh account-level metadata in place, leaving telemetry untouched."""
        self.device_id: str = raw_data.get("id") or raw_data.get("deviceId", "")
        self.serial_number: str = raw_data.get("serialNumber", "")
        self.name: str = raw_data.get("name") or "Sub-Zero Refrigerator"
        self.appliance_id: str = raw_data.get("applianceId", "")
        self.temp_unit: str = raw_data.get("temperatureUnitForAppliance", "F")

        self.model_description: str = MODEL_MAPPING.get(
            self.appliance_id, f"Sub-Zero Model {self.appliance_id}"
        )

    def update_properties(self, props: dict[str, Any], *, from_device: bool = True) -> None:
        """Merge telemetry (``from_device``) or an optimistic local change into the state.

        SignalR delivers messages out of order (e.g. seq 551 before 550), so a device
        message is ignored for any property already set by a newer ``seq`` in the last
        few seconds. Optimistic writes are stamped with the newest seq seen so far, so
        the device's echo of them still applies.
        """
        now = time.monotonic()
        seq = props.get("seq") if from_device else self._max_seq
        if not isinstance(seq, int):
            seq = None
        for key, value in props.items():
            if seq is not None:
                last = self._property_seq.get(key)
                if last and last[0] > seq and now - last[1] < _STALE_WINDOW:
                    continue
                self._property_seq[key] = (seq, now)
            self.properties[key] = value
        if from_device and seq is not None:
            self._max_seq = max(self._max_seq, seq)


class SubZeroAsyncClient:
    """Async API Client for Sub-Zero Cloud Services."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str,
        id_token: str | None = None,
        user_id: str | None = None,
        task_factory: Callable[[Coroutine[Any, Any, Any], str], asyncio.Task] | None = None,
        on_tokens_updated: Callable[[str, str | None], None] | None = None,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self.refresh_token = refresh_token
        self.id_token = id_token
        self.user_id = user_id
        self.user_email: str | None = None
        self.user_name: str | None = None
        self.token_expiry: float = 0
        # Called with (refresh_token, id_token) after every successful refresh so the
        # host can persist the rotated refresh token.
        self._on_tokens_updated = on_tokens_updated
        self._refresh_lock = asyncio.Lock()

        self.appliances: dict[str, SubZeroAppliance] = {}
        self._device_id_map: dict[str, str] = {}
        self._signalr_ws: aiohttp.ClientWebSocketResponse | None = None
        self._signalr_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._signalr_callbacks: list[Callable[[str, dict[str, Any]], None]] = []
        self._connection_callbacks: list[Callable[[bool], None]] = []
        self._stop_signalr = False
        self._signalr_failures = 0
        self._signalr_established = False
        self._background_tasks: set[asyncio.Task] = set()
        # Lets the host (Home Assistant) own task lifetimes; defaults to plain asyncio.
        self._task_factory = task_factory or (
            lambda coro, name: asyncio.create_task(coro, name=name)
        )

        if id_token:
            self._extract_claims(id_token)

    def _extract_claims(self, id_token: str) -> None:
        """Extract user claims from id_token."""
        claims = decode_jwt_payload(id_token)
        if "sub" in claims:
            self.user_id = claims["sub"]
        if "email" in claims:
            self.user_email = claims["email"]
        given = claims.get("given_name", "")
        family = claims.get("family_name", "")
        full = f"{given} {family}".strip()
        if full:
            self.user_name = full
        exp = claims.get("exp")
        if exp:
            self.token_expiry = float(exp)

    @staticmethod
    async def async_exchange_code_for_tokens(
        session: aiohttp.ClientSession,
        code: str,
        code_verifier: str,
    ) -> dict[str, Any]:
        """Exchange authorization code and PKCE verifier for tokens with Azure AD B2C."""
        data = {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": code_verifier,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "okhttp/4.9.2",
        }
        try:
            async with session.post(B2C_TOKEN_URL, data=data, headers=headers) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    _LOGGER.error("Token exchange failed (HTTP %s): %s", resp.status, text)
                    raise SubZeroAuthError(f"Failed to exchange code: HTTP {resp.status} - {text}")
                return await resp.json()
        except aiohttp.ClientError as err:
            raise SubZeroConnectionError(f"Network error exchanging code: {redact_error(err)}") from err

    async def async_refresh_tokens(
        self,
        *,
        if_needed: bool = False,
        stale_id_token: str | None = None,
    ) -> dict[str, Any]:
        """Exchange refresh token for updated tokens with Azure AD B2C.

        Refreshes are serialized. Callers that queued behind another refresh can
        pass ``if_needed`` (skip if the token is now fresh) or ``stale_id_token``
        (skip if the id_token already changed) to avoid re-using a rotated token.
        """
        async with self._refresh_lock:
            if if_needed and not self._token_needs_refresh():
                return {}
            if stale_id_token is not None and self.id_token != stale_id_token:
                return {}
            return await self._async_do_refresh()

    async def _async_do_refresh(self) -> dict[str, Any]:
        """Perform the refresh_token grant (caller must hold the refresh lock)."""
        data = {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": self.refresh_token,
            "redirect_uri": REDIRECT_URI,
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "okhttp/4.9.2",
        }

        try:
            async with self._session.post(
                B2C_TOKEN_URL, data=data, headers=headers, timeout=_REQUEST_TIMEOUT
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    _LOGGER.error("Token refresh failed (HTTP %s): %s", resp.status, text)
                    if resp.status in (400, 401):
                        # invalid_grant / expired or revoked refresh token
                        raise SubZeroAuthError(f"Failed to refresh token: HTTP {resp.status}")
                    raise SubZeroConnectionError(f"Token endpoint unavailable: HTTP {resp.status}")

                tokens = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SubZeroConnectionError(
                f"Network error refreshing tokens: {redact_error(err) or type(err).__name__}"
            ) from err

        self.id_token = tokens.get("id_token")
        if new_refresh := tokens.get("refresh_token"):
            self.refresh_token = new_refresh

        if self.id_token:
            self._extract_claims(self.id_token)

        if self._on_tokens_updated:
            try:
                self._on_tokens_updated(self.refresh_token, self.id_token)
            except Exception as err:  # persistence must never break a valid refresh
                _LOGGER.warning("Token persistence callback failed: %s", type(err).__name__)

        return tokens

    def _token_needs_refresh(self) -> bool:
        """Return True if the id_token is missing or expires in under 5 minutes."""
        return bool(
            not self.id_token
            or not self.user_id
            or (self.token_expiry and time.time() >= (self.token_expiry - 300))
        )

    async def _ensure_token_valid(self) -> None:
        """Ensure id_token is present and valid, refreshing if needed."""
        if self._token_needs_refresh():
            await self.async_refresh_tokens(if_needed=True)

    def _apim_headers(self, has_body: bool) -> dict[str, str]:
        """Build the APIM request headers using the current id_token."""
        return apim_headers(self.id_token, self.user_id, has_body)

    async def _send_apim_request(
        self,
        method: str,
        url: str,
        json_body: dict[str, Any] | None,
        endpoint: str,
    ) -> tuple[int, Any]:
        """Send one APIM request and return (status, payload).

        The connection is released before any retry logic runs. A single
        DNS/connect glitch is retried once after one second.
        """
        for attempt in range(2):
            try:
                async with self._session.request(
                    method,
                    url,
                    headers=self._apim_headers(json_body is not None),
                    json=json_body,
                    timeout=_REQUEST_TIMEOUT,
                ) as resp:
                    if resp.status in (200, 201):
                        return resp.status, await resp.json()
                    if resp.status == 204:
                        return resp.status, None
                    return resp.status, await resp.text()
            except aiohttp.ClientConnectorError as err:
                if attempt == 0:
                    _LOGGER.debug(
                        "Transient DNS/connection glitch on %s (%s). Retrying in 1s...",
                        endpoint,
                        redact_error(err),
                    )
                    await asyncio.sleep(1)
                    continue
                raise SubZeroConnectionError(
                    f"Connection error requesting {endpoint}: {redact_error(err)}"
                ) from err
            except (aiohttp.ClientError, TimeoutError) as err:
                raise SubZeroConnectionError(
                    f"Connection error requesting {endpoint}: "
                    f"{redact_error(err) or type(err).__name__}"
                ) from err
        raise SubZeroConnectionError(f"Connection error requesting {endpoint}")

    async def _make_apim_request(
        self,
        endpoint: str,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
        retry_on_401: bool = True,
    ) -> Any:
        """Make an authenticated request to Sub-Zero Azure APIM."""
        await self._ensure_token_valid()
        used_id_token = self.id_token

        url = f"{APIM_BASE_URL}{endpoint}" if not endpoint.startswith("http") else endpoint
        status, payload = await self._send_apim_request(method, url, json_body, endpoint)

        if status == 401:
            if not retry_on_401:
                raise SubZeroAuthError(f"{endpoint}: HTTP 401 after token refresh")
            _LOGGER.warning("Received HTTP 401 from APIM, forcing token refresh...")
            await self.async_refresh_tokens(stale_id_token=used_id_token)
            return await self._make_apim_request(
                endpoint, method, json_body, retry_on_401=False
            )

        if status not in (200, 201, 204):
            raise SubZeroError(f"API request to {endpoint} failed (HTTP {status}): {payload}")
        return payload

    async def async_get_devices(self) -> list[SubZeroAppliance]:
        """Fetch all registered appliances for the authenticated user."""
        devices_data = await self._make_apim_request("/consumerapp/user/devices")

        devices_list = []
        if isinstance(devices_data, dict):
            devices_list = devices_data.get("devices", [])
        elif isinstance(devices_data, list):
            devices_list = devices_data

        appliances: list[SubZeroAppliance] = []
        for dev in devices_list:
            serial = dev.get("serialNumber")
            if not serial:
                continue

            # Keep one persistent object per appliance so SignalR telemetry (which
            # mutates .properties) is never replaced or overwritten by a REST refresh.
            appliance = self.appliances.get(serial)
            if appliance is None:
                appliance = SubZeroAppliance(dev)
                self.appliances[serial] = appliance
            else:
                appliance.update_metadata(dev)
            appliance.is_connected = self.is_signalr_connected

            # Fetch active faults for this appliance
            try:
                appliance.faults = await self.async_get_faults(serial)
            except SubZeroError as err:
                _LOGGER.debug("Could not fetch faults for appliance %s: %s", serial, err)

            if appliance.device_id:
                self._device_id_map[appliance.device_id] = serial
            appliances.append(appliance)

        # Forget appliances that are no longer on the account
        current = {app.serial_number for app in appliances}
        for serial in [s for s in self.appliances if s not in current]:
            removed = self.appliances.pop(serial)
            self._device_id_map.pop(removed.device_id, None)

        return appliances

    async def async_get_faults(self, serial_number: str) -> list[dict[str, Any]]:
        """Fetch active fault notifications for an appliance serial number."""
        resp = await self._make_apim_request(f"/fault-notifications/v1/notifications/device/{serial_number}")
        if isinstance(resp, list):
            return resp
        return []

    async def async_open_cloud_async(self, appliance: SubZeroAppliance) -> bool:
        """Open appliance cloud async reporting channel via Azure IoT Hub Direct Method."""
        if not appliance.device_id:
            _LOGGER.warning("Appliance %s has no device ID", appliance.serial_number)
            return False

        endpoint = f"/consumerapp/device/{appliance.device_id}/directmethod/executeAPICmd"
        payload = {
            "req_id": str(uuid.uuid4()),
            "pload": {"cmd": "open_cloud_async"},
        }
        _LOGGER.debug("Sending open_cloud_async direct method to %s (%s)...", appliance.name, appliance.device_id)
        try:
            await self._make_apim_request(endpoint, method="POST", json_body=payload)
            _LOGGER.debug("Successfully opened cloud async channel for %s", appliance.name)
            return True
        except SubZeroError as err:
            _LOGGER.warning("open_cloud_async direct method encountered: %s", err)
            return False

    async def async_set_properties(
        self, appliance: SubZeroAppliance, params: dict[str, Any]
    ) -> None:
        """Send a set command for appliance properties via Azure IoT Hub Direct Method.

        Raises SubZeroError (or a subclass) on failure; callers decide how to report it.
        """
        if not appliance.device_id:
            raise SubZeroError(
                f"Cannot set {', '.join(params)}: Appliance {appliance.serial_number} has no device ID"
            )

        endpoint = f"/consumerapp/device/{appliance.device_id}/directmethod/executeAPICmd"
        payload = {
            "req_id": str(uuid.uuid4()),
            "pload": {
                "cmd": "set",
                "params": params,
            },
        }
        _LOGGER.debug(
            "Sending Direct Method set command to %s (%s): %s",
            appliance.name,
            appliance.device_id,
            payload,
        )
        await self._make_apim_request(endpoint, method="POST", json_body=payload)
        appliance.update_properties(params, from_device=False)

    async def async_set_property(self, appliance: SubZeroAppliance, key: str, value: Any) -> None:
        """Set a single appliance property."""
        await self.async_set_properties(appliance, {key: value})

    async def async_wake_appliance(self, appliance: SubZeroAppliance | None = None) -> bool:
        """Send wake-up requests to refresh device cloud session and Azure IoT Hub state."""
        _LOGGER.debug("Sending cloud wake-up handshake for Sub-Zero appliance...")
        try:
            devices = await self.async_get_devices()
            target = appliance or (devices[0] if devices else None)
            if self.user_id:
                await self._make_apim_request(f"/users/v1/users/{self.user_id}")
            if target:
                if target.serial_number:
                    await self.async_get_faults(target.serial_number)
                await self.async_open_cloud_async(target)
            await self.async_negotiate_signalr()
            return True
        except SubZeroError as err:
            _LOGGER.warning("Cloud wake-up encountered warning: %s", err)
            return False

    async def async_negotiate_signalr(self) -> dict[str, Any]:
        """Negotiate a SignalR connection URL and access token."""
        return await self._make_apim_request("/signal-r/negotiateUser", method="POST")

    @property
    def is_signalr_connected(self) -> bool:
        """Return True if the SignalR WebSocket is currently connected and active."""
        return self._signalr_ws is not None and not self._signalr_ws.closed

    def register_signalr_callback(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        """Register a callback for incoming appliance telemetry updates."""
        if callback not in self._signalr_callbacks:
            self._signalr_callbacks.append(callback)

    def unregister_signalr_callback(self, callback: Callable[[str, dict[str, Any]], None]) -> None:
        """Unregister a telemetry callback."""
        if callback in self._signalr_callbacks:
            self._signalr_callbacks.remove(callback)

    def register_connection_callback(self, callback: Callable[[bool], None]) -> None:
        """Register a callback for cloud connection state changes."""
        if callback not in self._connection_callbacks:
            self._connection_callbacks.append(callback)

    def unregister_connection_callback(self, callback: Callable[[bool], None]) -> None:
        """Unregister a cloud connection state callback."""
        if callback in self._connection_callbacks:
            self._connection_callbacks.remove(callback)

    def _set_connected_state(self, connected: bool) -> None:
        """Update appliance connection state and notify callbacks."""
        for appliance in self.appliances.values():
            appliance.is_connected = connected
        for cb in list(self._connection_callbacks):
            try:
                cb(connected)
            except Exception:  # isolate third-party callbacks
                _LOGGER.exception("Error in connection callback")

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
        """Create a task through the host-provided factory."""
        return self._task_factory(coro, name)

    def _spawn_tracked(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task:
        """Create a task and keep a reference so it is not garbage collected."""
        task = self._spawn(coro, name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def async_start_signalr(self) -> None:
        """Start the persistent SignalR streaming task and periodic keepalive."""
        if self._signalr_task and not self._signalr_task.done():
            return
        self._stop_signalr = False
        self._signalr_task = self._spawn(self._signalr_stream_loop(), "subzero_signalr")
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = self._spawn(self._cloud_keepalive_loop(), "subzero_keepalive")

    async def async_stop_signalr(self) -> None:
        """Stop the persistent SignalR streaming task, keepalive and helper tasks."""
        self._stop_signalr = True
        if self._signalr_ws and not self._signalr_ws.closed:
            await self._signalr_ws.close()
        self._signalr_ws = None
        self._set_connected_state(False)
        tasks = [
            task
            for task in (self._signalr_task, self._keepalive_task, *self._background_tasks)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _cloud_keepalive_loop(self) -> None:
        """Maintain active appliance cloud session by sending open_cloud_async periodically."""
        _LOGGER.debug("Starting Sub-Zero cloud keepalive loop (~25 min interval with jitter).")
        while not self._stop_signalr:
            try:
                # Jitter prevents thundering herd and emulates human mobile app usage
                delay = random.uniform(
                    KEEPALIVE_INTERVAL - KEEPALIVE_JITTER, KEEPALIVE_INTERVAL + KEEPALIVE_JITTER
                )
                await asyncio.sleep(delay)

                if self._stop_signalr:
                    break

                if self.appliances:
                    _LOGGER.debug(
                        "Dispatching periodic cloud keepalive (open_cloud_async) for %d appliance(s)",
                        len(self.appliances),
                    )
                    for appliance in list(self.appliances.values()):
                        if self._stop_signalr:
                            break
                        try:
                            await self.async_open_cloud_async(appliance)
                        except SubZeroError as err:
                            _LOGGER.debug(
                                "Keepalive error for appliance %s: %s",
                                appliance.serial_number,
                                err,
                            )
            except asyncio.CancelledError:
                break
            except Exception as err:
                _LOGGER.debug("Error in cloud keepalive loop: %s", err)
                await asyncio.sleep(60)

    async def _signalr_stream_loop(self) -> None:
        """Maintain persistent SignalR connection with back-off between every attempt."""
        retry_delay = _SIGNALR_RETRY_MIN

        while not self._stop_signalr:
            self._signalr_established = False
            reason: str
            try:
                await self._run_signalr_session()
                reason = "connection closed by server"
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as err:
                # Never log str(err): it contains the URL and therefore the access token.
                reason = f"HTTP {err.status} from SignalR hub"
            except (aiohttp.ClientError, TimeoutError, SubZeroError) as err:
                reason = redact_error(err) or type(err).__name__
            except Exception as err:  # keep the stream task alive no matter what
                reason = f"unexpected {type(err).__name__}: {redact_error(err)}"
            finally:
                self._signalr_ws = None
                self._set_connected_state(False)

            if self._stop_signalr:
                break

            if self._signalr_established:
                # A real session happened: restart the back-off from the minimum.
                retry_delay = _SIGNALR_RETRY_MIN
                _LOGGER.warning(
                    "Sub-Zero SignalR connection lost (%s). Reconnecting in ~%ss...",
                    reason,
                    retry_delay,
                )
            else:
                self._signalr_failures += 1
                if self._signalr_failures <= 3:
                    _LOGGER.warning(
                        "SignalR stream error (attempt %d): %s. Reconnecting in ~%ss...",
                        self._signalr_failures,
                        reason,
                        retry_delay,
                    )
                else:
                    _LOGGER.debug(
                        "SignalR stream outage ongoing (attempt %d): %s",
                        self._signalr_failures,
                        reason,
                    )

            await asyncio.sleep(retry_delay + random.uniform(0, 2))
            if not self._signalr_established:
                retry_delay = min(retry_delay * 2, _SIGNALR_RETRY_MAX)

    async def _run_signalr_session(self) -> None:
        """Negotiate, connect, handshake and stream until the socket closes.

        Raises on any failure; returns normally if the server closes the socket.
        """
        _LOGGER.debug("Negotiating SignalR streaming connection...")
        negotiation = await self.async_negotiate_signalr()
        hub_url = negotiation.get("url", "")
        access_token = negotiation.get("accessToken", "")

        ws_url = hub_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url += f"&access_token={access_token}"

        _LOGGER.debug("Connecting to Sub-Zero SignalR hub...")
        async with self._session.ws_connect(
            ws_url,
            heartbeat=30.0,
            headers={"User-Agent": "okhttp/4.9.2"},
        ) as ws:
            self._signalr_ws = ws

            # 1. Send SignalR Protocol Handshake
            handshake = json.dumps({"protocol": "json", "version": 1}) + RECORD_SEPARATOR
            await ws.send_str(handshake)

            # 2. Wait for Handshake ACK (bounded)
            ack = await asyncio.wait_for(ws.receive(), timeout=_SIGNALR_HANDSHAKE_TIMEOUT)
            if ack.type is not aiohttp.WSMsgType.TEXT or '"error"' in ack.data:
                raise SubZeroConnectionError("SignalR handshake rejected by hub")
            _LOGGER.debug("SignalR handshake response: %s", ack.data)

            self._signalr_established = True
            _LOGGER.info("Sub-Zero SignalR connection established and streaming active.")
            self._set_connected_state(True)
            if self._signalr_failures > 0:
                _LOGGER.info(
                    "Sub-Zero SignalR connection recovered after %d failed attempt(s).",
                    self._signalr_failures,
                )
                self._signalr_failures = 0

            # 3. Request appliance to open cloud async channel and push initial state
            for appliance in list(self.appliances.values()):
                self._spawn_tracked(
                    self.async_open_cloud_async(appliance), "subzero_open_cloud_async"
                )

            buffer = ""

            # 4. Read stream messages
            async for msg in ws:
                if self._stop_signalr:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    buffer += msg.data
                    while RECORD_SEPARATOR in buffer:
                        raw_record, buffer = buffer.split(RECORD_SEPARATOR, 1)
                        raw_record = raw_record.strip()
                        if not raw_record or raw_record == "{}":
                            continue
                        try:
                            parsed = json.loads(raw_record)
                        except json.JSONDecodeError as err:
                            _LOGGER.debug("Ignoring undecodable SignalR record (%s)", err)
                            continue
                        try:
                            await self._handle_signalr_message(parsed, ws)
                        except SubZeroError:
                            raise
                        except Exception:  # one bad message must not drop the stream
                            _LOGGER.exception("Error handling SignalR message")
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break

    async def _handle_signalr_message(
        self, msg: dict[str, Any], ws: aiohttp.ClientWebSocketResponse
    ) -> None:
        """Process an incoming SignalR protocol message."""
        match msg.get("type"):
            case SignalRMessage.PING:
                try:
                    await ws.send_str(json.dumps({"type": SignalRMessage.PING}) + RECORD_SEPARATOR)
                except (aiohttp.ClientError, OSError):
                    pass  # socket is going away; the read loop will notice
            case SignalRMessage.CLOSE:
                reason = redact_error(msg.get("error") or "no reason given")
                raise SubZeroConnectionError(f"Server closed connection: {reason}")
            case SignalRMessage.INVOCATION:
                self._dispatch_invocation(msg)

    def _dispatch_invocation(self, msg: dict[str, Any]) -> None:
        """Apply telemetry carried by an invocation message and notify callbacks."""
        arguments = msg.get("arguments", [])
        _LOGGER.debug(
            "SignalR invocation received: target='%s', arguments count=%d",
            msg.get("target"),
            len(arguments),
        )
        for arg_raw in arguments:
            arg = _maybe_json(arg_raw)
            if not isinstance(arg, dict):
                continue

            device_id, serial, props = extract_telemetry(arg)
            appliance = self._resolve_appliance(device_id, serial)
            if appliance is None or not props:
                continue

            changed = {k: v for k, v in props.items() if appliance.properties.get(k) != v}
            appliance.update_properties(props)
            _LOGGER.debug(
                "Updated Sub-Zero appliance %s with %d properties; changed: %s",
                appliance.serial_number,
                len(props),
                changed,
            )
            for cb in list(self._signalr_callbacks):
                try:
                    cb(appliance.serial_number, props)
                except Exception:
                    _LOGGER.exception("Error executing SignalR callback")

    def _resolve_appliance(
        self, device_id: str | None, serial: str | None
    ) -> SubZeroAppliance | None:
        """Find the appliance a telemetry message belongs to."""
        if device_id and serial:
            self._device_id_map[device_id] = serial
        elif device_id:
            serial = self._device_id_map.get(device_id)

        if serial and serial in self.appliances:
            return self.appliances[serial]
        if device_id:
            for appliance in self.appliances.values():
                if appliance.device_id == device_id:
                    return appliance
        if len(self.appliances) == 1:
            return next(iter(self.appliances.values()))
        return None
