"""Tests for the email/password sign-in against a scripted fake B2C service."""

import base64
import json

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from custom_components.subzero_libre import auth, client as client_mod, login as login_mod
from custom_components.subzero_libre.const import REDIRECT_URI
from custom_components.subzero_libre.login import (
    LoginInvalidAuth,
    LoginInvalidCaptcha,
    LoginInvalidCode,
    LoginRateLimited,
    LoginStep,
    LoginUnsupported,
    SubZeroLogin,
)

PNG = base64.b64encode(b"\x89PNG fake image bytes").decode()


class FakeB2C:
    """Just enough of Sub-Zero's sign-in service to drive SubZeroLogin."""

    def __init__(self) -> None:
        self.scenario = "plain"
        self.password = "right-password"
        self.mfa_code = "123456"
        self.captcha_answer = "ABCD"
        self.state = ""
        self.requests: list[tuple[str, dict]] = []

    def page(self, request: web.Request, api: str, extra: str = "") -> web.Response:
        base = str(request.url.origin()) + "/tenant"
        settings = {
            "csrf": "CSRF",
            "transId": "StateProperties=tx",
            "pageViewId": "pv",
            "api": api,
            "hosts": {"tenant": base, "policy": "B2C_1A"},
            "config": {"enableCaptchaChallenge": self.scenario == "captcha" and api == "PhoneFactor"},
        }
        return web.Response(
            text=f"<script>var SETTINGS = {json.dumps(settings)};{extra}</script>",
            content_type="text/html",
        )

    def redirect(self, state: str | None = None) -> web.Response:
        state = self.state if state is None else state
        return web.Response(
            status=302,
            headers={"Location": f"{REDIRECT_URI}?code=AUTHCODE&state={state}"},
        )

    async def authorize(self, request: web.Request) -> web.Response:
        self.state = request.query["state"]
        return self.page(request, "CombinedSigninAndSignup")

    async def self_asserted(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        self.requests.append(("SelfAsserted", form))
        assert request.headers["X-CSRF-TOKEN"] == "CSRF"
        if self.scenario == "ratelimit":
            return web.json_response({"status": "429"}, status=429)
        if form["password"] != self.password:
            return web.json_response({"status": "400", "message": "wrong"}, status=400)
        return web.json_response({"status": "200"})

    async def confirmed(self, request: web.Request) -> web.Response:
        if self.scenario == "unsupported":
            return self.page(request, "SelfAsserted")
        if self.scenario in ("sms", "captcha"):
            return self.page(request, "PhoneFactor", 'var UV_PHONE = {"PhoneNumbers":[{"Id":"7"}]};')
        return self.redirect("other-state" if self.scenario == "badstate" else None)

    async def phone_confirmed(self, request: web.Request) -> web.Response:
        return self.redirect()

    async def get_challenge(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"challengeId": "cid", "challengeString": f"data:image/png;base64,{PNG}", "azureRegion": "r"}
        )

    async def verify_challenge(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        ok = form["captchaEntered"] == self.captcha_answer
        return web.json_response(
            {"status": "200", "isCaptchaSolved": "true"} if ok else {"status": "400", "isCaptchaSolved": "false"},
            status=200 if ok else 400,
        )

    async def verify(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        self.requests.append(("verify", form))
        if form["request_type"] == "VERIFICATION_REQUEST":
            return web.json_response({"status": "200"})
        ok = form["verification_code"] == self.mfa_code
        return web.json_response({"status": "200" if ok else "449"})

    async def token(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        assert form["grant_type"] == "authorization_code"
        assert form["code"] == "AUTHCODE" and form["code_verifier"]
        return web.json_response({"id_token": "ID", "refresh_token": "RT", "token_type": "Bearer"})


@pytest.fixture
async def b2c(monkeypatch, socket_enabled):
    fake = FakeB2C()
    app = web.Application()
    app.router.add_get("/tenant/oauth2/v2.0/authorize", fake.authorize)
    app.router.add_post("/tenant/SelfAsserted", fake.self_asserted)
    app.router.add_get("/tenant/api/CombinedSigninAndSignup/confirmed", fake.confirmed)
    app.router.add_get("/tenant/api/PhoneFactor/confirmed", fake.phone_confirmed)
    app.router.add_get(f"/tenant{login_mod._CAPTCHA_PATH}/GetChallenge", fake.get_challenge)
    app.router.add_post(f"/tenant{login_mod._CAPTCHA_PATH}/VerifyChallenge", fake.verify_challenge)
    app.router.add_post("/tenant/PhoneFactor/verify", fake.verify)
    app.router.add_post("/tenant/oauth2/v2.0/token", fake.token)
    server = TestServer(app)
    await server.start_server()
    base = f"http://127.0.0.1:{server.port}"
    monkeypatch.setattr(login_mod, "LOGIN_HOST", "127.0.0.1")
    monkeypatch.setattr(auth, "B2C_AUTHORIZE_URL", f"{base}/tenant/oauth2/v2.0/authorize")
    monkeypatch.setattr(client_mod, "B2C_TOKEN_URL", f"{base}/tenant/oauth2/v2.0/token")
    yield fake
    await server.close()


@pytest.fixture
async def login(b2c):
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.CookieJar(unsafe=True, quote_cookie=False)
    ) as login_session, aiohttp.ClientSession() as token_session:
        yield SubZeroLogin(login_session, token_session)


async def test_plain_sign_in_returns_tokens(b2c, login):
    tokens = await login.async_login("me@example.com", "right-password")
    assert tokens["refresh_token"] == "RT" and tokens["id_token"] == "ID"
    assert b2c.requests[0][1]["signInName"] == "me@example.com"


async def test_wrong_password(b2c, login):
    with pytest.raises(LoginInvalidAuth):
        await login.async_login("me@example.com", "nope")


async def test_rate_limited(b2c, login):
    b2c.scenario = "ratelimit"
    with pytest.raises(LoginRateLimited):
        await login.async_login("me@example.com", "right-password")


async def test_unsupported_step_is_reported(b2c, login):
    b2c.scenario = "unsupported"
    with pytest.raises(LoginUnsupported):
        await login.async_login("me@example.com", "right-password")


async def test_state_mismatch_is_rejected(b2c, login):
    b2c.scenario = "badstate"
    with pytest.raises(LoginUnsupported):
        await login.async_login("me@example.com", "right-password")


async def test_sms_code_flow(b2c, login):
    b2c.scenario = "sms"
    step = await login.async_login("me@example.com", "right-password")
    assert step == LoginStep("sms_code")
    assert any(f.get("request_type") == "VERIFICATION_REQUEST" and f["id"] == "7" for _, f in b2c.requests)
    with pytest.raises(LoginInvalidCode):
        await login.async_submit_code("000000")
    tokens = await login.async_submit_code("123456")
    assert tokens["refresh_token"] == "RT"


async def test_image_challenge_then_sms(b2c, login):
    b2c.scenario = "captcha"
    step = await login.async_login("me@example.com", "right-password")
    assert step.kind == "captcha" and step.captcha_image.startswith("data:image/png;base64,")
    with pytest.raises(LoginInvalidCaptcha):
        await login.async_submit_captcha("WRONG")
    assert (await login.async_new_captcha()).kind == "captcha"
    assert await login.async_submit_captcha("ABCD") == LoginStep("sms_code")
    assert (await login.async_submit_code("123456"))["refresh_token"] == "RT"


async def test_other_hosts_are_refused(b2c, login):
    with pytest.raises(LoginUnsupported):
        await login._request("GET", "https://evil.example/")
