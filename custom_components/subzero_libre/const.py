"""Constants for the Sub-Zero Libre integration."""

from collections.abc import Mapping
from enum import IntEnum
import logging
from types import MappingProxyType
from typing import Final

DOMAIN: Final = "subzero_libre"
LOGGER = logging.getLogger(__package__)

# Configuration keys
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_ID_TOKEN: Final = "id_token"
CONF_USER_ID: Final = "user_id"
CONF_USER_EMAIL: Final = "user_email"
CONF_USER_NAME: Final = "user_name"
CONF_POLL_INTERVAL: Final = "poll_interval"

DEFAULT_POLL_INTERVAL: Final = 300
MIN_POLL_INTERVAL: Final = 15
MAX_POLL_INTERVAL: Final = 900

# Azure AD B2C OAuth2 endpoints
B2C_TENANT: Final = "SubZeroB2CPrd.onmicrosoft.com"
B2C_POLICY: Final = "B2C_1A_SIGNUP_SIGNIN"
B2C_AUTHORIZE_URL: Final = f"https://login.subzero-wolf.com/{B2C_TENANT}/{B2C_POLICY}/oauth2/v2.0/authorize"
B2C_TOKEN_URL: Final = f"https://login.subzero-wolf.com/{B2C_TENANT}/{B2C_POLICY}/oauth2/v2.0/token"

CLIENT_ID: Final = "6eefabd0-49a3-4b92-b329-81b9f638e940"
REDIRECT_URI: Final = "com.szg.szgdigitalproductexperience://oauth/redirect"
SCOPES: Final = "openid offline_access"

# Azure APIM Gateway
APIM_BASE_URL: Final = "https://prod.iot.subzero.com"
# Static application-level key used by the official mobile app. It identifies the
# app (not the user), is required on every request, and is not a user credential.
# Obfuscating it would only add false security, so it is kept as a plain constant
# and intentionally not repeated in the documentation.
APIM_SUBSCRIPTION_KEY: Final = "e88bf0b60baf441583f822fa9ba9c895"

# Platforms supported
# Plain strings (equal to homeassistant.const.Platform members) so this module and the
# client stay importable without Home Assistant, e.g. by the dev scripts.
PLATFORMS: Final = ["binary_sensor", "number", "select", "sensor", "switch"]

# Model mapping (Internal Sub-Zero appliance ID to model designation)
MODEL_MAPPING: Final[Mapping[str, str]] = MappingProxyType({
    "17.11.3.3": "BI-42SID/S/TH (42\" Built-in Side-by-Side with Dispenser)",
    "17.11.2.0": "BI-42S/S/TH (42\" Built-in Side-by-Side)",
    "17.7.1.1": "BI-36U (36\" Built-in Over-and-Under)",
    "17.7.2.1": "BI-36UID (36\" Built-in Over-and-Under with Dispenser)",
    "17.10.0": "BI-48S (48\" Built-in Side-by-Side)",
    "17.3.0": "BI-36R (36\" Built-in All Refrigerator)",
})


class SignalRMessage(IntEnum):
    """SignalR JSON hub protocol message types."""

    INVOCATION = 1
    PING = 6
    CLOSE = 7


# Setpoint ranges (min, max) by temperature unit. The cloud does not expose limits,
# so these are defaults observed on the app, with per-model overrides keyed by
# appliance ID (see MODEL_MAPPING) taking precedence.
type SetpointRange = dict[str, tuple[float, float]]

DEFAULT_SETPOINT_RANGES: dict[str, SetpointRange] = {
    "ref_set_temp": {"F": (34.0, 42.0), "C": (1.0, 6.0)},
    "frz_set_temp": {"F": (-5.0, 5.0), "C": (-21.0, -15.0)},
    "crisp_set_temp": {"F": (34.0, 42.0), "C": (1.0, 6.0)},
}

MODEL_SETPOINT_RANGES: dict[str, dict[str, SetpointRange]] = {}


def get_setpoint_range(appliance_id: str, key: str, unit: str) -> tuple[float, float]:
    """Return the (min, max) setpoint for a model, falling back to the defaults."""
    ranges = MODEL_SETPOINT_RANGES.get(appliance_id, {}).get(key) or DEFAULT_SETPOINT_RANGES[key]
    return ranges["C" if unit == "C" else "F"]
