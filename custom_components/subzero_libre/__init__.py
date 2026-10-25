"""The Sub-Zero Libre Home Assistant integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import SubZeroAsyncClient
from .const import (
    CONF_ID_TOKEN,
    CONF_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DEFAULT_POLL_INTERVAL,
    PLATFORMS,
)
from .coordinator import SubZeroConfigEntry, SubZeroDataUpdateCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: SubZeroConfigEntry) -> bool:
    """Set up Sub-Zero Libre from a config entry."""
    session = async_get_clientsession(hass)
    refresh_token = entry.data[CONF_REFRESH_TOKEN]
    id_token = entry.data.get(CONF_ID_TOKEN)
    user_id = entry.data.get(CONF_USER_ID)

    poll_interval = entry.options.get(
        CONF_POLL_INTERVAL, entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
    )

    @callback
    def _persist_tokens(new_refresh_token: str, _id_token: str | None) -> None:
        """Store the rotated refresh token so it survives restarts."""
        if new_refresh_token and new_refresh_token != entry.data.get(CONF_REFRESH_TOKEN):
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: new_refresh_token}
            )

    client = SubZeroAsyncClient(
        session=session,
        refresh_token=refresh_token,
        id_token=id_token,
        user_id=user_id,
        # Tie the long-lived SignalR/keepalive tasks to the config entry lifecycle
        task_factory=lambda coro, name: entry.async_create_background_task(
            hass, coro, name
        ),
        on_tokens_updated=_persist_tokens,
    )

    coordinator = SubZeroDataUpdateCoordinator(
        hass=hass,
        client=client,
        poll_interval=poll_interval,
    )

    # Fetch initial data and start SignalR real-time stream. If anything after
    # this point fails, stop the background tasks so a retry doesn't duplicate them.
    try:
        await coordinator.async_setup()

        entry.runtime_data = coordinator

        # Forward entry setups to platforms
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        await coordinator.async_unload()
        raise

    # Apply option changes (poll interval) in place; token persistence also fires
    # update listeners, so a full reload here would loop.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: SubZeroConfigEntry) -> bool:
    """Unload a Sub-Zero Libre config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        await entry.runtime_data.async_unload()

    return unload_ok


async def _async_options_updated(hass: HomeAssistant, entry: SubZeroConfigEntry) -> None:
    """Apply the configured poll interval without reloading the entry."""
    poll_interval = entry.options.get(
        CONF_POLL_INTERVAL, entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)
    )
    entry.runtime_data.update_interval = timedelta(seconds=poll_interval)
