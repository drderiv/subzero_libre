"""DataUpdateCoordinator for Sub-Zero Libre integration."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import (
    SubZeroAppliance,
    SubZeroAsyncClient,
    SubZeroAuthError,
    SubZeroError,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


type SubZeroConfigEntry = ConfigEntry[SubZeroDataUpdateCoordinator]


class SubZeroDataUpdateCoordinator(DataUpdateCoordinator[dict[str, SubZeroAppliance]]):
    """Class to manage fetching Sub-Zero appliance data and real-time streaming."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: SubZeroAsyncClient,
        poll_interval: int,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=poll_interval),
        )
        self.client = client
        self._initial_telemetry_event: asyncio.Event = asyncio.Event()

    async def _async_update_data(self) -> dict[str, SubZeroAppliance]:
        """Fetch latest device list, configurations, and fault status via REST with outage recovery."""
        try:
            appliances = await self.client.async_get_devices()
        except SubZeroAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except (SubZeroError, TimeoutError) as err:
            # The base class logs the first failure and the recovery once each; entities
            # report unavailable only if the SignalR stream is also down.
            raise UpdateFailed(f"Error communicating with Sub-Zero cloud: {err}") from err

        # The client owns persistent appliance objects whose telemetry is kept
        # up to date by SignalR, so a REST refresh only needs to index them.
        return {app.serial_number: app for app in appliances}

    async def async_setup(self) -> None:
        """Set up the coordinator and start real-time SignalR telemetry listener."""
        # Initial data fetch
        await self.async_config_entry_first_refresh()

        # Register SignalR and connection callbacks
        self.client.register_signalr_callback(self._handle_signalr_telemetry)
        self.client.register_connection_callback(self._handle_connection_change)

        # Launch the persistent background SignalR WebSocket task
        await self.client.async_start_signalr()

        # Await the initial real-time telemetry snapshot from the physical appliance
        # (open_cloud_async prompts an instant telemetry push, typically received in 1-2s)
        try:
            await asyncio.wait_for(self._initial_telemetry_event.wait(), timeout=6.0)
            _LOGGER.debug("Initial Sub-Zero telemetry snapshot received before platform setup.")
        except asyncio.TimeoutError:
            _LOGGER.debug("Timed out waiting for initial Sub-Zero telemetry snapshot; proceeding with setup.")

    async def async_unload(self) -> None:
        """Stop background tasks and unregister callbacks."""
        self.client.unregister_signalr_callback(self._handle_signalr_telemetry)
        self.client.unregister_connection_callback(self._handle_connection_change)
        await self.client.async_stop_signalr()

    @callback
    def _handle_connection_change(self, connected: bool) -> None:
        """Handle SignalR cloud connection state changes."""
        _LOGGER.debug("Sub-Zero SignalR cloud connection changed: connected=%s", connected)
        if self.data:
            for appliance in self.data.values():
                appliance.is_connected = connected
            self.async_update_listeners()

    async def async_set_property(self, serial_number: str, key: str, value: Any) -> None:
        """Set one property on a Sub-Zero appliance."""
        await self.async_set_properties(serial_number, {key: value})

    async def async_set_properties(self, serial_number: str, params: dict[str, Any]) -> None:
        """Set properties on a Sub-Zero appliance; raises HomeAssistantError on failure."""
        if not self.data or serial_number not in self.data:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="appliance_not_found"
            )
        try:
            await self.client.async_set_properties(self.data[serial_number], params)
        except SubZeroAuthError as err:
            raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
        except (SubZeroError, TimeoutError) as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={"setting": ", ".join(params)},
            ) from err
        self.async_update_listeners()

    @callback
    def _handle_signalr_telemetry(self, serial_number: str, props: dict[str, Any]) -> None:
        """Handle real-time telemetry pushed over the SignalR stream.

        The client has already resolved the appliance and merged ``props`` into it.
        """
        _LOGGER.debug("SignalR telemetry received for '%s'", serial_number)

        # Signal that initial telemetry snapshot has been received
        self._initial_telemetry_event.set()

        # Inform Home Assistant of updated state immediately. Not async_set_updated_data:
        # that would mark the last REST poll successful and reset the poll timer.
        self.async_update_listeners()
