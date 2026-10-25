#!/usr/bin/env python3
"""
Sub-Zero Cloud Sign-in & Discovery Tool
=======================================
Signs in to Sub-Zero (Azure AD B2C), saves subzero_tokens.json, and lists the appliances
on the account. Uses the same login and client code as the Home Assistant integration
(custom_components/subzero_libre), so it exercises the real thing. Needs only `aiohttp`.

Usage:
  python3 subzero_auth_test.py             # reuse/refresh cached tokens, or ask how to sign in
  python3 subzero_auth_test.py --password  # sign in with email and password (no browser)
  python3 subzero_auth_test.py --browser   # sign in with a browser and paste the redirect URL
  python3 subzero_auth_test.py --no-browser  # never try to launch a browser, only print the URL
  python3 subzero_auth_test.py --verbose   # DEBUG logging from the client
"""

import argparse
import asyncio
import base64
import contextlib
import getpass
import json
import logging
import os
import secrets
import sys
import time
import types
import webbrowser
from typing import Any
from urllib.parse import urlparse

import aiohttp

# Load the integration's modules without importing Home Assistant (the package __init__ does).
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_pkg = types.ModuleType("subzero_libre")
_pkg.__path__ = [os.path.join(CURRENT_DIR, "custom_components", "subzero_libre")]
sys.modules["subzero_libre"] = _pkg

from subzero_libre.auth import (  # noqa: E402
    build_authorization_url,
    decode_jwt_payload,
    generate_pkce_pair,
    parse_auth_input,
    write_private_json,
)
from subzero_libre.client import (  # noqa: E402
    SubZeroAsyncClient,
    SubZeroAuthError,
    SubZeroError,
)
from subzero_libre.login import (  # noqa: E402
    LoginInvalidAuth,
    LoginInvalidCaptcha,
    LoginInvalidCode,
    LoginRateLimited,
    LoginStep,
    LoginUnsupported,
    SubZeroLogin,
)

TOKEN_FILE = os.path.join(CURRENT_DIR, "subzero_tokens.json")
MAX_CHALLENGE_ATTEMPTS = 3


def load_tokens() -> dict[str, Any] | None:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as err:
        print(f"[-] Could not read cached tokens: {err}")
        return None


def save_tokens(tokens: dict[str, Any]) -> None:
    write_private_json(TOKEN_FILE, {**tokens, "obtained_at": int(time.time())})


def try_open_browser(url: str) -> bool:
    """Best-effort browser launch; stays silent on headless or SSH-only sessions."""
    has_gui = (
        sys.platform in ("darwin", "win32")
        or bool(os.environ.get("DISPLAY"))
        or bool(os.environ.get("WAYLAND_DISPLAY"))
    )
    if not has_gui:
        return False
    # Browser helpers (xdg-open, IDE launchers) print their own errors; keep them quiet.
    with open(os.devnull, "w") as devnull, contextlib.redirect_stderr(devnull):
        saved = os.dup(2)
        try:
            os.dup2(devnull.fileno(), 2)
            return bool(webbrowser.open(url))
        except Exception:  # any launcher failure just means "paste the URL manually"
            return False
        finally:
            os.dup2(saved, 2)
            os.close(saved)


async def prompt(text: str, secret: bool = False) -> str:
    """Read a line without blocking the event loop."""
    reader = getpass.getpass if secret else input
    return (await asyncio.to_thread(reader, text)).strip()


# ------------------------------------------------------------------ sign-in


async def password_signin(session: aiohttp.ClientSession) -> dict[str, Any]:
    """Email and password sign-in, including SMS and image challenges."""
    print("\n" + "=" * 65 + "\nEMAIL + PASSWORD SIGN-IN\n" + "=" * 65)
    print("Your password is sent only to Sub-Zero's sign-in service and is never stored.")
    email = await prompt("Sub-Zero email: ")
    password = await prompt("Password (hidden): ", secret=True)

    # A separate session with its own cookie jar, exactly as the integration does
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(quote_cookie=False)) as login_session:
        login = SubZeroLogin(login_session, session)
        result: dict[str, Any] | LoginStep = await login.async_login(email, password)
        del password
        attempts = 0
        while isinstance(result, LoginStep):
            try:
                if result.kind == "captcha":
                    image_file = os.path.join(CURRENT_DIR, "captcha.png")
                    with open(image_file, "wb") as file:
                        file.write(base64.b64decode((result.captcha_image or "").split(",", 1)[-1]))
                    print(f"\n[?] Sub-Zero asks for an image check. Open {image_file} and type the characters.")
                    answer = await prompt("    Characters: ")
                    os.remove(image_file)
                    result = await login.async_submit_captcha(answer)
                else:
                    print("\n[?] Sub-Zero sent a verification code by text message.")
                    result = await login.async_submit_code(await prompt("    Code: "))
            except (LoginInvalidCaptcha, LoginInvalidCode) as err:
                attempts += 1
                if attempts >= MAX_CHALLENGE_ATTEMPTS:
                    raise LoginUnsupported("Too many failed attempts") from err
                print(f"[!] {err}. Try again.")
                if isinstance(err, LoginInvalidCaptcha):
                    result = await login.async_new_captcha()
        return result


async def browser_signin(session: aiohttp.ClientSession, open_browser: bool) -> dict[str, Any]:
    """Browser sign-in: open the link, sign in, paste the redirect URL."""
    print("\n" + "=" * 65 + "\nWEB BROWSER SIGN-IN\n" + "=" * 65)
    verifier, challenge = generate_pkce_pair()
    state = secrets.token_urlsafe(16)
    auth_url = build_authorization_url(challenge, state)

    print("\nStep 1: Open this URL in a desktop browser (any machine works; this terminal")
    print("        does not need a browser) and WAIT until the sign-in page has fully loaded:")
    print("-" * 65 + f"\n{auth_url}\n" + "-" * 65)
    if open_browser and try_open_browser(auth_url):
        print("[*] Also launched the URL in the default browser.")
    print("\nStep 2: Only after the page has loaded, open Developer Tools (F12 or Ctrl+Shift+I)")
    print("        and switch to the Network tab. Then sign in.")
    print("Step 3: The page stays on the login screen. Depending on your setup you may see")
    print('        "Page Not Found", "Unable to open address", or a box asking to open another')
    print("        application; ignore any of these. In the Network tab, right-click the last")
    print("        (red) row and choose Copy > Copy URL. It starts with")
    print("        com.szg.szgdigitalproductexperience://oauth/redirect?code=...")
    print("        Paste it below.")

    parsed = parse_auth_input(await prompt("\nEnter Redirect URL: "))
    if parsed.kind != "code" or not parsed.code:
        sys.exit("[!] No authorization code found in input!")
    if parsed.state and parsed.state != state:
        sys.exit("[!] The 'state' in that URL does not match this sign-in attempt. Start again.")
    print("\n[*] Exchanging authorization code for tokens...")
    return await SubZeroAsyncClient.async_exchange_code_for_tokens(session, parsed.code, verifier)


async def sign_in(session: aiohttp.ClientSession, args: argparse.Namespace) -> dict[str, Any]:
    method = "password" if args.password else "browser" if args.browser else ""
    while not method:
        choice = await prompt("Sign in with [1] email and password or [2] a web browser? ")
        method = {"1": "password", "2": "browser"}.get(choice, "")
    if method == "browser":
        return await browser_signin(session, not args.no_browser)
    try:
        return await password_signin(session)
    except LoginInvalidAuth:
        sys.exit("[!] The email or password was not accepted.")
    except LoginRateLimited:
        sys.exit("[!] Sub-Zero is limiting sign-in attempts. Wait a while, then try again.")
    except LoginUnsupported as err:
        sys.exit(f"[!] Email/password sign-in cannot be used here ({err}).\n    Use --browser instead.")


# ---------------------------------------------------------------- discovery


async def list_appliances(client: SubZeroAsyncClient) -> None:
    claims = decode_jwt_payload(client.id_token or "")
    name = f"{claims.get('given_name', '')} {claims.get('family_name', '')}".strip() or "User"
    print("\n" + "=" * 65)
    print(f"AUTHENTICATED USER: {name} ({claims.get('email', 'unknown email')})")
    print(f"User ID (sub claim): {client.user_id}")
    print("=" * 65)

    devices = await client.async_get_devices()
    print(f"\nDISCOVERED {len(devices)} REGISTERED APPLIANCE(S)")
    for dev in devices:
        print(f"\n--- Appliance: {dev.name} ---")
        print(f"    - Serial Number : {dev.serial_number}")
        print(f"    - Model         : {dev.model_description}")
        print(f"    - Temp Unit     : {dev.temp_unit}")
        print(f"    - Active Faults : {dev.faults or 'None (Operating Normally)'}")

    hub = await client.async_negotiate_signalr()
    print(f"\n[+] SignalR negotiation succeeded (hub: {urlparse(hub.get('url', '')).hostname})")
    print("    Run live_signalr_test.py to monitor real-time telemetry.")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--password", action="store_true", help="Sign in with email and password")
    parser.add_argument("--browser", "--login", dest="browser", action="store_true", help="Sign in with a web browser")
    parser.add_argument("--no-browser", action="store_true", help="Never try to launch a browser; just print the URL")
    parser.add_argument("--verbose", "-v", action="store_true", help="DEBUG logging")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    async with aiohttp.ClientSession() as session:
        tokens = None if (args.password or args.browser) else load_tokens()
        client: SubZeroAsyncClient | None = None
        if tokens and tokens.get("refresh_token"):
            client = SubZeroAsyncClient(
                session, tokens["refresh_token"], tokens.get("id_token"),
                on_tokens_updated=lambda rt, idt: save_tokens({**tokens, "refresh_token": rt, "id_token": idt}),
            )
            try:
                await client.async_refresh_tokens()
                print("[*] Cached sign-in refreshed.")
            except SubZeroAuthError:
                print("[-] Cached sign-in is no longer valid.")
                client = None
        if client is None:
            tokens = await sign_in(session, args)
            save_tokens(tokens)
            print(f"[*] Tokens saved to {TOKEN_FILE}")
            client = SubZeroAsyncClient(
                session, tokens["refresh_token"], tokens.get("id_token"),
                on_tokens_updated=lambda rt, idt: save_tokens({**tokens, "refresh_token": rt, "id_token": idt}),
            )
        try:
            await list_appliances(client)
        except SubZeroError as err:
            sys.exit(f"[!] {err}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] Stopped.")
