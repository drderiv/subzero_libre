"""Diagnostics support for Sub-Zero Libre."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_USER_EMAIL,
    CONF_USER_ID,
    CONF_USER_NAME,
)
from .coordinator import SubZeroConfigEntry

TO_REDACT = {
    CONF_REFRESH_TOKEN,
    CONF_ID_TOKEN,
    CONF_USER_ID,
    CONF_USER_EMAIL,
    CONF_USER_NAME,
    "unique_id",
    "title",
    "serialNumber",
    "serial_number",
    "appliance_serial",
    "macId",
    "bda",
    "id",
    "deviceId",
    "DeviceId",
    "ap_ssid",
    "ipv4_addr",
    "device_wlan_id",
    "remote_svc_reg_token",
    "Payload",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SubZeroConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "signalr_connected": coordinator.client.is_signalr_connected,
        "last_update_success": coordinator.last_update_success,
        "appliances": {
            f"appliance_{index}": async_redact_data(
                {
                    "model": appliance.model_description,
                    "temp_unit": appliance.temp_unit,
                    "properties": appliance.properties,
                    "faults": appliance.faults,
                },
                TO_REDACT,
            )
            for index, appliance in enumerate((coordinator.data or {}).values())
        },
    }
