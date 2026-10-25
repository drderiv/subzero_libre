"""Config flow for Sub-Zero Libre integration."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
import secrets
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .auth import (
    build_authorization_url,
    generate_pkce_pair,
    parse_auth_input,
)
from .client import (
    SubZeroAsyncClient,
    SubZeroAuthError,
    SubZeroConnectionError,
    SubZeroError,
    redact_error,
)
from .const import (
    CONF_ID_TOKEN,
    CONF_POLL_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_USER_EMAIL,
    CONF_USER_ID,
    CONF_USER_NAME,
    DEFAULT_POLL_INTERVAL,
    DOMAIN,
    LOGGER,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)

def find_cached_token(config_dir: str) -> str | None:
    """Read the refresh token from {config_dir}/subzero_tokens.json if present.

    Developer convenience only; deliberately limited to the HA config directory.
    """
    path = os.path.join(config_dir, "subzero_tokens.json")
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as err:
        LOGGER.debug("Ignoring unreadable %s: %s", path, type(err).__name__)
        return None
    return data.get("refresh_token") if isinstance(data, dict) else None


class SubZeroConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sub-Zero Libre."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow."""
        self._auth_data: dict[str, Any] = {}
        self._code_verifier: str | None = None
        self._auth_url: str | None = None
        self._state: str | None = None

    def _ensure_pkce(self) -> None:
        """Create the PKCE verifier and login URL once per flow."""
        if self._code_verifier is None:
            verifier, challenge = generate_pkce_pair()
            self._state = secrets.token_urlsafe(16)
            self._code_verifier = verifier
            self._auth_url = build_authorization_url(challenge, self._state)

    def _schema(self, default: str = "") -> vol.Schema:
        """Return the single-field schema used by the user and reauth steps."""
        return vol.Schema(
            {
                vol.Required(
                    CONF_REFRESH_TOKEN,
                    default=default,
                ): TextSelector(TextSelectorConfig(multiline=True)),
            }
        )

    async def _async_validate_input(
        self, raw_input: str
    ) -> tuple[SubZeroAsyncClient | None, list[Any], dict[str, str]]:
        """Turn pasted input (redirect URL, code, JSON or refresh token) into a verified client."""
        errors: dict[str, str] = {}
        session = async_get_clientsession(self.hass)
        parsed = parse_auth_input(raw_input)
        refresh_token = parsed.refresh_token
        id_token = parsed.id_token

        if parsed.kind == "code":
            if not parsed.code or (parsed.state and parsed.state != self._state):
                # Missing code, or a redirect from a different login attempt (CSRF check)
                errors["base"] = "invalid_auth"
            else:
                try:
                    tokens = await SubZeroAsyncClient.async_exchange_code_for_tokens(
                        session=session,
                        code=parsed.code,
                        code_verifier=self._code_verifier or "",
                    )
                    refresh_token = tokens.get("refresh_token")
                    id_token = tokens.get("id_token")
                except SubZeroAuthError:
                    errors["base"] = "invalid_auth"
                except SubZeroConnectionError:
                    errors["base"] = "cannot_connect"
                except Exception:
                    LOGGER.exception("Unexpected error exchanging code")
                    errors["base"] = "unknown"

        if errors or not refresh_token:
            return None, [], errors

        client = SubZeroAsyncClient(
            session=session,
            refresh_token=refresh_token,
            id_token=id_token,
        )
        devices: list[Any] = []
        try:
            await client.async_refresh_tokens()
            devices = await client.async_get_devices()
            if not devices:
                errors["base"] = "no_devices"
        except SubZeroAuthError:
            errors["base"] = "invalid_auth"
        except SubZeroConnectionError:
            errors["base"] = "cannot_connect"
        except SubZeroError as err:
            LOGGER.error("Sub-Zero config error: %s", redact_error(err))
            errors["base"] = "unknown"
        except Exception:
            LOGGER.exception("Unexpected error in config flow")
            errors["base"] = "unknown"

        return client, devices, errors

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: User provides authorization redirect URL, code, or refresh token."""
        errors: dict[str, str] = {}
        self._ensure_pkce()

        # Safely read cached token in executor if available
        cached_token = (
            await self.hass.async_add_executor_job(
                find_cached_token, self.hass.config.config_dir
            )
            or ""
        )

        if user_input is not None:
            client, devices, errors = await self._async_validate_input(
                user_input[CONF_REFRESH_TOKEN]
            )

            if client is not None and not errors:
                user_id = client.user_id or "subzero_account"
                await self.async_set_unique_id(user_id)
                self._abort_if_unique_id_configured()

                # The entry represents the account, not any one appliance
                title = client.user_name or client.user_email or "Sub-Zero"

                return self.async_create_entry(
                    title=title,
                    data={
                        CONF_REFRESH_TOKEN: client.refresh_token,
                        CONF_ID_TOKEN: client.id_token,
                        CONF_USER_ID: client.user_id,
                        CONF_USER_EMAIL: client.user_email,
                        CONF_USER_NAME: client.user_name,
                        CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self._schema(cached_token),
            errors=errors,
            description_placeholders={
                "auth_url": self._auth_url or "",
            },
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication when the stored refresh token is rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to sign in again and replace the stored tokens."""
        errors: dict[str, str] = {}
        self._ensure_pkce()

        if user_input is not None:
            client, _devices, errors = await self._async_validate_input(
                user_input[CONF_REFRESH_TOKEN]
            )
            if client is not None and not errors:
                await self.async_set_unique_id(client.user_id or "subzero_account")
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    self._get_reauth_entry(),
                    data_updates={
                        CONF_REFRESH_TOKEN: client.refresh_token,
                        CONF_ID_TOKEN: client.id_token,
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self._schema(),
            errors=errors,
            description_placeholders={
                "auth_url": self._auth_url or "",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> SubZeroOptionsFlow:
        """Get the options flow handler."""
        return SubZeroOptionsFlow()


class SubZeroOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Sub-Zero Libre."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_poll = self.config_entry.options.get(
            CONF_POLL_INTERVAL,
            self.config_entry.data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
        )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=current_poll,
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_POLL_INTERVAL,
                        max=MAX_POLL_INTERVAL,
                        step=5,
                        mode=NumberSelectorMode.SLIDER,
                        unit_of_measurement="seconds",
                    )
                ),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)
