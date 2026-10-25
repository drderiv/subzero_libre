"""Sign-in helpers shared by the integration and the developer scripts.

Standard library only (no Home Assistant, no aiohttp); shared by the integration, the
email/password login module and the developer scripts.
"""

from __future__ import annotations

import base64
import binascii
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import json
import logging
import os
import secrets
from typing import Any, Literal
from urllib.parse import parse_qs, urlencode, urlparse

from .const import (
    APIM_SUBSCRIPTION_KEY,
    B2C_AUTHORIZE_URL,
    CLIENT_ID,
    REDIRECT_URI,
    SCOPES,
)

_LOGGER = logging.getLogger(__name__)


def generate_pkce_pair() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_authorization_url(code_challenge: str, state: str) -> str:
    """Build the Azure AD B2C login URL for browser authentication."""
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "response_mode": "query",
        "scope": SCOPES,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{B2C_AUTHORIZE_URL}?{urlencode(params)}"


def apim_headers(id_token: str | None, user_id: str | None, has_body: bool) -> dict[str, str]:
    """Build the request headers the mobile app sends to the Azure APIM gateway."""
    headers = {
        "ocp-apim-subscription-key": APIM_SUBSCRIPTION_KEY,
        "user-agent": "Dart/3.11 (dart:io)",
        "app_platform": "android",
        "app_version": "4.6.2",
        "accept": "application/json",
        "userid": user_id or "",
        "authorization": f"Bearer {id_token}",
    }
    if has_body:
        headers["content-type"] = "application/json; charset=utf-8"
    return headers


def write_private_json(path: str, data: Any) -> None:
    """Atomically write JSON readable only by the current user (tokens, dumps)."""
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=2)
    os.replace(tmp, path)


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode unverified JWT payload claims."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return {}
        payload_b64 = parts[1]
        payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8"))
    except (ValueError, binascii.Error) as err:  # UnicodeDecodeError/JSONDecodeError are ValueErrors
        _LOGGER.debug("Failed to decode JWT payload: %s", err)
        return {}


@dataclass(frozen=True)
class ParsedInput:
    """The result of classifying what the user pasted into the form."""

    kind: Literal["code", "tokens_json", "refresh_token"]
    code: str | None = None
    refresh_token: str | None = None
    id_token: str | None = None
    state: str | None = None


def parse_auth_input(raw: str) -> ParsedInput:
    """Classify pasted input: redirect URL / query string, tokens JSON, or a raw refresh token."""
    raw = raw.strip()
    if raw.startswith("{") and "refresh_token" in raw:
        with suppress(json.JSONDecodeError):
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("refresh_token"):
                return ParsedInput(
                    "tokens_json",
                    refresh_token=data["refresh_token"],
                    id_token=data.get("id_token"),
                )
    if "code=" in raw or raw.startswith("com.szg.") or "oauth/redirect" in raw:
        query = parse_qs(urlparse(raw if "://" in raw else f"x://x?{raw}").query)
        return ParsedInput(
            "code",
            code=query.get("code", [""])[0],
            state=query.get("state", [None])[0],
        )
    return ParsedInput("refresh_token", refresh_token=raw)
