"""Email and password sign-in for Sub-Zero's Azure AD B2C sign-in page.

Replays the requests the hosted sign-in page makes, so Home Assistant can ask for an
email and password instead of a pasted redirect URL. Home Assistant-free: the caller
provides the aiohttp sessions. The password is only held in memory for the duration of
one call and is sent only to Sub-Zero's sign-in host.

The caller must give ``login_session`` its own cookie jar
(``aiohttp.CookieJar(quote_cookie=False)``); never use a session shared with other code.

Any step this module does not recognise raises :class:`LoginUnsupported`, so callers can
fall back to the browser sign-in.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import json
import logging
import re
import secrets
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import aiohttp

from .auth import build_authorization_url, generate_pkce_pair
from .client import (
    SubZeroAsyncClient,
    SubZeroAuthError,
    SubZeroConnectionError,
    SubZeroError,
    redact_error,
)
from .const import B2C_TOKEN_URL, REDIRECT_URI

_LOGGER = logging.getLogger(__name__)

LOGIN_HOST = urlparse(B2C_TOKEN_URL).hostname
_MAX_REDIRECTS = 10
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)
_CAPTCHA_PATH = "/SelfAsserted/DisplayControlAction/vbeta/captchaControlChallengeCode"


class LoginInvalidAuth(SubZeroAuthError):
    """The email or password was rejected."""


class LoginRateLimited(SubZeroError):
    """Sub-Zero asked us to slow down; do not retry automatically."""


class LoginInvalidCode(SubZeroError):
    """The text-message verification code was rejected."""


class LoginInvalidCaptcha(SubZeroError):
    """The image challenge answer was rejected."""


class LoginUnsupported(SubZeroError):
    """Sub-Zero asked for something this sign-in cannot handle; use the browser."""


@dataclass(frozen=True)
class LoginStep:
    """What the user must do next to finish signing in."""

    kind: Literal["captcha", "sms_code"]
    captcha_image: str | None = None  # data: URL, for kind == "captcha"


def page_variable(html: str, name: str) -> Any:
    """Decode the JSON value of a ``NAME = {...}`` assignment embedded in a B2C page."""
    match = re.search(rf"\b{re.escape(name)}\s*=\s*", html)
    if not match:
        return None
    try:
        return json.JSONDecoder().raw_decode(html[match.end() :])[0]
    except json.JSONDecodeError:
        return None


class SubZeroLogin:
    """One email/password sign-in attempt, possibly spanning several user steps."""

    def __init__(
        self,
        login_session: aiohttp.ClientSession,
        token_session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize; ``token_session`` (cookie-free) is used for the code exchange."""
        self._session = login_session
        self._token_session = token_session or login_session
        self._verifier, challenge = generate_pkce_pair()
        self._state = secrets.token_urlsafe(16)
        self._auth_url = build_authorization_url(challenge, self._state)
        self._page_url = ""
        self._html = ""
        self._settings: dict[str, Any] = {}
        self._captcha_solved = False
        self._challenge: dict[str, Any] = {}

    # ------------------------------------------------------------------ HTTP

    async def _request(
        self,
        method: str,
        url: str,
        *,
        form: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any, str]:
        """Send one request to the sign-in host without following redirects."""
        if urlparse(url).hostname != LOGIN_HOST:
            raise LoginUnsupported("Unexpected sign-in host")
        try:
            async with self._session.request(
                method,
                url,
                data=form,
                headers={"User-Agent": _USER_AGENT, **(headers or {})},
                allow_redirects=False,
                timeout=_REQUEST_TIMEOUT,
            ) as resp:
                return resp.status, resp.headers, await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SubZeroConnectionError(
                f"Network error during sign-in: {redact_error(err) or type(err).__name__}"
            ) from err

    async def _navigate(self, url: str) -> tuple[str, Any]:
        """Follow redirects to the app redirect (code) or to an HTML page."""
        for _ in range(_MAX_REDIRECTS):
            status, headers, body = await self._request("GET", url)
            if status in (301, 302, 303, 307, 308):
                location = urljoin(url, headers.get("Location", ""))
                if location.startswith(REDIRECT_URI):
                    query = parse_qs(urlparse(location).query)
                    if "error" in query:
                        raise LoginUnsupported("Sign-in returned an error redirect")
                    return "code", (query.get("code", [""])[0], query.get("state", [None])[0])
                url = location
                continue
            if status == 200:
                return "page", (url, body)
            raise LoginUnsupported(f"Unexpected HTTP {status} from the sign-in service")
        raise LoginUnsupported("Too many redirects")

    def _set_page(self, page_url: str, html: str) -> None:
        settings = page_variable(html, "SETTINGS")
        if not isinstance(settings, dict) or not all(
            key in settings for key in ("csrf", "transId", "api", "hosts")
        ):
            raise LoginUnsupported("Unrecognised sign-in page")
        self._page_url, self._html, self._settings = page_url, html, settings

    def _b2c_url(self, path: str, **extra: str) -> str:
        base = self._settings["hosts"]["tenant"]
        if not base.startswith("http"):
            base = f"https://{LOGIN_HOST}{base}"
        query = {**extra, "tx": self._settings["transId"], "p": self._settings["hosts"]["policy"]}
        return f"{base}{path}?{urlencode(query)}"

    def _ajax_headers(self) -> dict[str, str]:
        headers = {
            "X-CSRF-TOKEN": self._settings["csrf"],
            "X-Requested-With": "XMLHttpRequest",
            "Referer": self._page_url,
            "Origin": f"https://{LOGIN_HOST}",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        if self._settings.get("pageViewId") and self._settings.get("isPageViewIdSentWithHeader"):
            headers["x-ms-cpim-pageviewid"] = self._settings["pageViewId"]
        return headers

    async def _post_json(self, path: str, form: dict[str, str]) -> dict[str, Any]:
        """POST a form the way the page's JavaScript does; return the JSON reply."""
        status, _, body = await self._request(
            "POST", self._b2c_url(path), form=form, headers=self._ajax_headers()
        )
        if status == 429:
            raise LoginRateLimited("Too many sign-in attempts")
        try:
            reply = json.loads(body)
        except json.JSONDecodeError:
            raise LoginUnsupported(f"Unexpected reply (HTTP {status}) from {path}") from None
        if not isinstance(reply, dict):
            raise LoginUnsupported(f"Unexpected reply from {path}")
        if str(reply.get("status")) == "429":
            raise LoginRateLimited("Too many sign-in attempts")
        return reply

    # ------------------------------------------------------------- the journey

    async def async_login(self, email: str, password: str) -> dict[str, Any] | LoginStep:
        """Submit credentials. Returns tokens, or the next step the user must complete."""
        kind, value = await self._navigate(self._auth_url)
        if kind != "page":
            raise LoginUnsupported("Unexpected redirect before sign-in")
        self._set_page(*value)
        if self._settings["api"] != "CombinedSigninAndSignup":
            raise LoginUnsupported(f"Unexpected sign-in page type '{self._settings['api']}'")

        reply = await self._post_json(
            "/SelfAsserted",
            {"request_type": "RESPONSE", "signInName": email, "password": password},
        )
        if str(reply.get("status")) != "200":
            raise LoginInvalidAuth("The email or password was not accepted")
        return await self._after_confirm()

    async def _after_confirm(self) -> dict[str, Any] | LoginStep:
        """Confirm the current page's journey step and see where it leads."""
        confirm = self._b2c_url(
            f"/api/{self._settings['api']}/confirmed",
            rememberMe="false",
            csrf_token=self._settings["csrf"],
        )
        kind, value = await self._navigate(confirm)
        if kind == "code":
            return await self._exchange(*value)

        self._set_page(*value)
        if "phone" not in self._settings["api"].lower():
            raise LoginUnsupported(f"Unsupported sign-in step '{self._settings['api']}'")
        if (self._settings.get("config") or {}).get("enableCaptchaChallenge") and not self._captcha_solved:
            return await self.async_new_captcha()
        await self._send_sms()
        return LoginStep("sms_code")

    async def async_new_captcha(self) -> LoginStep:
        """Fetch a fresh image challenge."""
        status, _, body = await self._request(
            "GET",
            self._b2c_url(f"{_CAPTCHA_PATH}/GetChallenge", challengeType="Visual"),
            headers=self._ajax_headers(),
        )
        if status == 429:
            raise LoginRateLimited("Too many sign-in attempts")
        try:
            challenge = json.loads(body)
            image = str(challenge["challengeString"])
            raw = image.split(",", 1)[-1]
            base64.b64decode(raw, validate=True)
            self._challenge = challenge
        except (json.JSONDecodeError, KeyError, TypeError, binascii.Error):
            raise LoginUnsupported("Unrecognised image challenge") from None
        data_url = image if image.startswith("data:") else f"data:image/png;base64,{raw}"
        return LoginStep("captcha", captcha_image=data_url)

    async def async_submit_captcha(self, answer: str) -> LoginStep:
        """Check the typed characters, then request the text message."""
        reply = await self._post_json(
            f"{_CAPTCHA_PATH}/VerifyChallenge",
            {
                "challengeId": str(self._challenge.get("challengeId", "")),
                "captchaEntered": answer.strip(),
                "challengeType": "Visual",
                "azureRegion": str(self._challenge.get("azureRegion", "")),
            },
        )
        solved = str(reply.get("status")) == "200" and (
            reply.get("isCaptchaSolved") in (True, "true")
            or reply.get("reason") == "usermessageifbypass"
        )
        if not solved:
            raise LoginInvalidCaptcha("The image challenge answer was not accepted")
        self._captcha_solved = True
        await self._send_sms()
        return LoginStep("sms_code")

    async def _send_sms(self) -> None:
        phones = (page_variable(self._html, "UV_PHONE") or {}).get("PhoneNumbers") or []
        phone_id = str((phones[0].get("Id") if phones else None) or 1)
        reply = await self._post_json(
            f"/{self._settings['api']}/verify",
            {"request_type": "VERIFICATION_REQUEST", "auth_type": "onewaysms", "id": phone_id},
        )
        if str(reply.get("status")) != "200":
            raise LoginUnsupported("Could not send the verification text message")

    async def async_submit_code(self, code: str) -> dict[str, Any] | LoginStep:
        """Check the text-message code and finish the sign-in."""
        reply = await self._post_json(
            f"/{self._settings['api']}/verify",
            {"request_type": "VALIDATION_REQUEST", "verification_code": code.strip()},
        )
        if str(reply.get("status")) != "200":
            raise LoginInvalidCode("The verification code was not accepted")
        return await self._after_confirm()

    async def _exchange(self, code: str, state: str | None) -> dict[str, Any]:
        if not code or state != self._state:
            raise LoginUnsupported("Sign-in returned a mismatched state or no code")
        return await SubZeroAsyncClient.async_exchange_code_for_tokens(
            self._token_session, code, self._verifier
        )
