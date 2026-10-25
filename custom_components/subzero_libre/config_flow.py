"""Config flow for Sub-Zero Libre integration."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
import secrets
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import (
    async_create_clientsession,
    async_get_clientsession,
)
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
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
from .login import (
    LoginInvalidAuth,
    LoginInvalidCaptcha,
    LoginInvalidCode,
    LoginRateLimited,
    LoginStep,
    LoginUnsupported,
    SubZeroLogin,
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


_MAX_CHALLENGE_ATTEMPTS = 3


class SubZeroConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Sub-Zero Libre."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize flow."""
        self._code_verifier: str | None = None
        self._auth_url: str | None = None
        self._state: str | None = None
        self._login: SubZeroLogin | None = None
        self._login_session: aiohttp.ClientSession | None = None
        self._email = ""
        self._captcha_image = ""
        self._failed_attempts = {"captcha": 0, "code": 0}

    @callback
    def async_remove(self) -> None:
        """Release the dedicated sign-in session when the flow ends.

        Home Assistant requires detach() (not close()) for sessions created with
        auto_cleanup=False.
        """
        if self._login_session is not None:
            self._login_session.detach()
            self._login_session = None

    # ------------------------------------------------------------------ menus

    def _menu(self, step_id: str) -> ConfigFlowResult:
        return self.async_show_menu(step_id=step_id, menu_options=["password", "browser"])

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose how to sign in."""
        return self._menu("user")

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start re-authentication when the stored refresh token is rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask how to sign in again."""
        return self._menu("reauth_confirm")

    # --------------------------------------------------- email and password path

    def _password_form(self, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="password",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL, default=self._email): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
                    ),
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD, autocomplete="current-password"
                        )
                    ),
                }
            ),
            errors=errors or {},
        )

    def _fallback_to_browser(self) -> ConfigFlowResult:
        """Send the user to the browser sign-in after something unexpected."""
        self._login = None
        return self._browser_form({"base": "password_unavailable"})

    @staticmethod
    def _login_error(err: Exception) -> str | None:
        """Map a sign-in exception to an error key (None = use the browser instead)."""
        if isinstance(err, LoginInvalidAuth):
            return "invalid_auth"
        if isinstance(err, LoginRateLimited):
            return "rate_limited"
        if isinstance(err, LoginInvalidCode):
            return "invalid_code"
        if isinstance(err, LoginInvalidCaptcha):
            return "invalid_captcha"
        if isinstance(err, SubZeroConnectionError):
            return "cannot_connect"
        if isinstance(err, LoginUnsupported):
            LOGGER.warning("Password sign-in not possible: %s", redact_error(err))
        elif isinstance(err, SubZeroError):
            LOGGER.warning("Password sign-in failed: %s", redact_error(err))
        else:
            LOGGER.exception("Unexpected error during password sign-in")
        return None

    async def async_step_password(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in with the Sub-Zero account email and password."""
        if user_input is None:
            return self._password_form()

        self._email = user_input[CONF_EMAIL].strip()
        if self._login_session is None:
            # Its own cookie jar: never share sign-in cookies with other integrations
            self._login_session = async_create_clientsession(
                self.hass,
                auto_cleanup=False,
                cookie_jar=aiohttp.CookieJar(quote_cookie=False),
            )
        self._login = SubZeroLogin(self._login_session, async_get_clientsession(self.hass))
        self._failed_attempts = {"captcha": 0, "code": 0}
        try:
            result = await self._login.async_login(self._email, user_input[CONF_PASSWORD])
        except Exception as err:  # noqa: BLE001 - every failure is mapped below
            if (error := self._login_error(err)) is None:
                return self._fallback_to_browser()
            return self._password_form({"base": error})
        return await self._async_handle_login_result(result)

    async def _async_handle_login_result(
        self, result: dict[str, Any] | LoginStep
    ) -> ConfigFlowResult:
        if isinstance(result, LoginStep):
            self._captcha_image = result.captcha_image or ""
            return await (
                self.async_step_captcha() if result.kind == "captcha" else self.async_step_sms()
            )
        return await self._async_finish_tokens(result, self._password_form)

    async def async_step_captcha(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show Sub-Zero's image challenge and collect the typed characters."""
        errors: dict[str, str] = {}
        if user_input is not None and self._login is not None:
            try:
                result = await self._login.async_submit_captcha(user_input["captcha"])
            except Exception as err:  # noqa: BLE001
                error = self._login_error(err)
                if error is None:
                    return self._fallback_to_browser()
                errors["base"] = error
                self._failed_attempts["captcha"] += 1
                if self._failed_attempts["captcha"] >= _MAX_CHALLENGE_ATTEMPTS:
                    return self._fallback_to_browser()
                if error == "invalid_captcha":
                    try:
                        self._captcha_image = (await self._login.async_new_captcha()).captcha_image or ""
                    except Exception:  # noqa: BLE001
                        return self._fallback_to_browser()
            else:
                return await self._async_handle_login_result(result)
        if not self._captcha_image:
            return self._fallback_to_browser()
        return self.async_show_form(
            step_id="captcha",
            data_schema=vol.Schema({vol.Required("captcha"): str}),
            errors=errors,
            description_placeholders={"captcha_image": f"![Image challenge]({self._captcha_image})"},
        )

    async def async_step_sms(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the verification code Sub-Zero sent by text message."""
        errors: dict[str, str] = {}
        if user_input is not None and self._login is not None:
            try:
                result = await self._login.async_submit_code(user_input["code"])
            except Exception as err:  # noqa: BLE001
                error = self._login_error(err)
                if error is None:
                    return self._fallback_to_browser()
                errors["base"] = error
                self._failed_attempts["code"] += 1
                if self._failed_attempts["code"] >= _MAX_CHALLENGE_ATTEMPTS:
                    return self._fallback_to_browser()
            else:
                return await self._async_handle_login_result(result)
        return self.async_show_form(
            step_id="sms",
            data_schema=vol.Schema({vol.Required("code"): str}),
            errors=errors,
        )

    # ------------------------------------------------------------ browser path

    def _ensure_pkce(self) -> None:
        """Create the PKCE verifier and login URL once per flow."""
        if self._code_verifier is None:
            verifier, challenge = generate_pkce_pair()
            self._state = secrets.token_urlsafe(16)
            self._code_verifier = verifier
            self._auth_url = build_authorization_url(challenge, self._state)

    def _schema(self, default: str = "") -> vol.Schema:
        """Return the single-field schema used by the browser step."""
        return vol.Schema(
            {
                vol.Required(
                    CONF_REFRESH_TOKEN,
                    default=default,
                ): TextSelector(TextSelectorConfig(multiline=True)),
            }
        )

    def _browser_form(
        self, errors: dict[str, str] | None = None, default: str = ""
    ) -> ConfigFlowResult:
        self._ensure_pkce()
        return self.async_show_form(
            step_id="browser",
            data_schema=self._schema(default),
            errors=errors or {},
            description_placeholders={"auth_url": self._auth_url or ""},
        )

    async def async_step_browser(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in with the web browser and paste the redirect URL, code or token."""
        self._ensure_pkce()
        if user_input is None:
            # Developer convenience: pre-fill from <config>/subzero_tokens.json
            cached_token = (
                await self.hass.async_add_executor_job(
                    find_cached_token, self.hass.config.config_dir
                )
                or ""
            )
            return self._browser_form(default=cached_token)

        client, _devices, errors = await self._async_validate_input(
            user_input[CONF_REFRESH_TOKEN]
        )
        if client is None or errors:
            return self._browser_form(errors)
        return await self._async_complete(client)

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
        return await self._async_verify_tokens(refresh_token, id_token)

    # ------------------------------------------------------------- both paths

    async def _async_verify_tokens(
        self, refresh_token: str, id_token: str | None
    ) -> tuple[SubZeroAsyncClient | None, list[Any], dict[str, str]]:
        """Use the tokens once to confirm the account works and has appliances."""
        errors: dict[str, str] = {}
        client = SubZeroAsyncClient(
            session=async_get_clientsession(self.hass),
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

    async def _async_finish_tokens(
        self, tokens: dict[str, Any], show_form: Any
    ) -> ConfigFlowResult:
        """Verify tokens from the password path, then create or update the entry."""
        client, _devices, errors = await self._async_verify_tokens(
            tokens["refresh_token"], tokens.get("id_token")
        )
        if client is None or errors:
            return show_form(errors)
        return await self._async_complete(client)

    async def _async_complete(self, client: SubZeroAsyncClient) -> ConfigFlowResult:
        """Create the entry (or update it on re-authentication)."""
        await self.async_set_unique_id(client.user_id or "subzero_account")
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates={
                    CONF_REFRESH_TOKEN: client.refresh_token,
                    CONF_ID_TOKEN: client.id_token,
                },
            )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            # The entry represents the account, not any one appliance
            title=client.user_name or client.user_email or "Sub-Zero",
            data={
                CONF_REFRESH_TOKEN: client.refresh_token,
                CONF_ID_TOKEN: client.id_token,
                CONF_USER_ID: client.user_id,
                CONF_USER_EMAIL: client.user_email,
                CONF_USER_NAME: client.user_name,
                CONF_POLL_INTERVAL: DEFAULT_POLL_INTERVAL,
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
