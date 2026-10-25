"""Config flow tests: menu, email/password path, challenges, browser path and reauth."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.subzero_libre.const import (
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DOMAIN,
)
from custom_components.subzero_libre.login import (
    LoginInvalidAuth,
    LoginInvalidCaptcha,
    LoginInvalidCode,
    LoginStep,
    LoginUnsupported,
)

TOKENS = {"refresh_token": "RT-new", "id_token": "ID-new"}
CREDENTIALS = {CONF_EMAIL: "me@example.com", CONF_PASSWORD: "secret"}


def make_client(user_id: str = "user-1") -> SimpleNamespace:
    return SimpleNamespace(
        refresh_token="RT-new",
        id_token="ID-new",
        user_id=user_id,
        user_email="me@example.com",
        user_name="Me",
    )


def patch_verify(user_id: str = "user-1", errors: dict | None = None):
    return patch(
        "custom_components.subzero_libre.config_flow.SubZeroConfigFlow._async_verify_tokens",
        AsyncMock(return_value=(make_client(user_id), [object()], errors or {})),
    )


def patch_login():
    """Patch SubZeroLogin; returns (patcher, mocked instance)."""
    instance = MagicMock()
    for name in ("async_login", "async_submit_code", "async_submit_captcha", "async_new_captcha"):
        setattr(instance, name, AsyncMock())
    patcher = patch("custom_components.subzero_libre.config_flow.SubZeroLogin", return_value=instance)
    return patcher, instance


def patch_setup():
    """Avoid real setup/unload (and any network) once a flow creates or updates an entry."""
    return patch.multiple(
        "custom_components.subzero_libre",
        async_setup_entry=AsyncMock(return_value=True),
        async_unload_entry=AsyncMock(return_value=True),
    )


async def start(hass, choice: str):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    assert set(result["menu_options"]) == {"password", "browser"}
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": choice})


async def test_password_sign_in_creates_entry_without_password(hass):
    patcher, login = patch_login()
    login.async_login.return_value = TOKENS
    with patcher, patch_verify(), patch_setup():
        result = await start(hass, "password")
        assert result["step_id"] == "password"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Me"
    assert result["data"][CONF_REFRESH_TOKEN] == "RT-new"
    assert "secret" not in str(result["data"]) and CONF_PASSWORD not in result["data"]
    login.async_login.assert_awaited_once_with("me@example.com", "secret")


async def test_sign_in_session_is_detached_without_warnings(hass, caplog):
    """The dedicated session must be released with detach(), as Home Assistant requires."""
    from homeassistant.helpers import aiohttp_client

    created = []
    real = aiohttp_client.async_create_clientsession

    def recording(*args, **kwargs):
        session = real(*args, **kwargs)
        created.append(session)
        return session

    patcher, login = patch_login()
    login.async_login.return_value = TOKENS
    with (
        patcher,
        patch_verify(),
        patch_setup(),
        patch("custom_components.subzero_libre.config_flow.async_create_clientsession", recording),
    ):
        result = await start(hass, "password")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(created) == 1 and created[0].closed
    assert "closes the Home Assistant aiohttp session" not in caplog.text


async def test_abandoned_flow_releases_the_session(hass, caplog):
    patcher, login = patch_login()
    login.async_login.side_effect = LoginInvalidAuth("no")
    with patcher:
        result = await start(hass, "password")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        hass.config_entries.flow.async_abort(result["flow_id"])
        await hass.async_block_till_done()
    assert "closes the Home Assistant aiohttp session" not in caplog.text


async def test_wrong_password_shows_error(hass):
    patcher, login = patch_login()
    login.async_login.side_effect = LoginInvalidAuth("no")
    with patcher:
        result = await start(hass, "password")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "password"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_unsupported_step_falls_back_to_browser(hass):
    patcher, login = patch_login()
    login.async_login.side_effect = LoginUnsupported("page changed")
    with patcher:
        result = await start(hass, "password")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "browser"
    assert result["errors"] == {"base": "password_unavailable"}
    assert "auth_url" in result["description_placeholders"]


async def test_unexpected_exception_falls_back_to_browser(hass):
    patcher, login = patch_login()
    login.async_login.side_effect = RuntimeError("boom")
    with patcher:
        result = await start(hass, "password")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["step_id"] == "browser" and result["errors"] == {"base": "password_unavailable"}


async def test_sms_code_step(hass):
    patcher, login = patch_login()
    login.async_login.return_value = LoginStep("sms_code")
    login.async_submit_code.side_effect = [LoginInvalidCode("bad"), TOKENS]
    with patcher, patch_verify(), patch_setup():
        result = await start(hass, "password")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["step_id"] == "sms"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "000000"})
        assert result["step_id"] == "sms" and result["errors"] == {"base": "invalid_code"}
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_three_wrong_codes_fall_back_to_browser(hass):
    patcher, login = patch_login()
    login.async_login.return_value = LoginStep("sms_code")
    login.async_submit_code.side_effect = LoginInvalidCode("bad")
    with patcher:
        result = await start(hass, "password")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        for _ in range(3):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "1"})
    assert result["step_id"] == "browser" and result["errors"] == {"base": "password_unavailable"}


async def test_image_challenge_then_sms(hass):
    patcher, login = patch_login()
    login.async_login.return_value = LoginStep("captcha", captcha_image="data:image/png;base64,AAAA")
    login.async_submit_captcha.side_effect = [
        LoginInvalidCaptcha("bad"),
        LoginStep("sms_code"),
    ]
    login.async_new_captcha.return_value = LoginStep("captcha", captcha_image="data:image/png;base64,BBBB")
    login.async_submit_code.return_value = TOKENS
    with patcher, patch_verify(), patch_setup():
        result = await start(hass, "password")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        assert result["step_id"] == "captcha"
        assert "data:image/png;base64,AAAA" in result["description_placeholders"]["captcha_image"]
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"captcha": "x"})
        assert result["errors"] == {"base": "invalid_captcha"}
        assert "BBBB" in result["description_placeholders"]["captcha_image"]
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"captcha": "ABCD"})
        assert result["step_id"] == "sms"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_image_challenge_failure_falls_back_to_browser(hass):
    patcher, login = patch_login()
    login.async_login.return_value = LoginStep("captcha", captcha_image="data:image/png;base64,AAAA")
    login.async_submit_captcha.side_effect = LoginUnsupported("odd")
    with patcher:
        result = await start(hass, "password")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"captcha": "x"})
    assert result["step_id"] == "browser" and result["errors"] == {"base": "password_unavailable"}


async def test_browser_path_creates_entry(hass):
    exchange = AsyncMock(return_value={"refresh_token": "RT-new", "id_token": "ID-new"})
    with (
        patch("custom_components.subzero_libre.config_flow.SubZeroAsyncClient.async_exchange_code_for_tokens", exchange),
        patch_verify(),
        patch_setup(),
    ):
        result = await start(hass, "browser")
        assert result["step_id"] == "browser"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_REFRESH_TOKEN: "com.szg.szgdigitalproductexperience://oauth/redirect?code=abc"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    exchange.assert_awaited_once()


async def test_browser_path_rejects_foreign_state(hass):
    result = await start(hass, "browser")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_REFRESH_TOKEN: "com.szg.szgdigitalproductexperience://oauth/redirect?code=abc&state=other"},
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_duplicate_account_is_aborted(hass):
    MockConfigEntry(domain=DOMAIN, unique_id="user-1", data={CONF_REFRESH_TOKEN: "x"}).add_to_hass(hass)
    patcher, login = patch_login()
    login.async_login.return_value = TOKENS
    with patcher, patch_verify("user-1"):
        result = await start(hass, "password")
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_reauth_updates_tokens(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="user-1",
        data={CONF_REFRESH_TOKEN: "old", CONF_ID_TOKEN: "old", CONF_USER_ID: "user-1"},
    )
    entry.add_to_hass(hass)
    patcher, login = patch_login()
    login.async_login.return_value = TOKENS
    with patcher, patch_verify("user-1"), patch_setup():
        result = await entry.start_reauth_flow(hass)
        assert result["type"] is FlowResultType.MENU and result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "password"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data[CONF_REFRESH_TOKEN] == "RT-new" and entry.data[CONF_USER_ID] == "user-1"


async def test_reauth_rejects_a_different_account(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id="user-1", data={CONF_REFRESH_TOKEN: "old"})
    entry.add_to_hass(hass)
    patcher, login = patch_login()
    login.async_login.return_value = TOKENS
    with patcher, patch_verify("someone-else"), patch_setup():
        result = await entry.start_reauth_flow(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "password"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], CREDENTIALS)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "wrong_account"
    assert entry.data[CONF_REFRESH_TOKEN] == "old"
