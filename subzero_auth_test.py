#!/usr/bin/env python3
"""
Sub-Zero Cloud Authentication & Discovery Tool
==============================================
Signs in to Sub-Zero (Azure AD B2C, PKCE), saves subzero_tokens.json, and lists the
appliances on the account. Standard library only; no packages to install.

Constants and sign-in helpers come from custom_components/subzero_libre so this tool
and the Home Assistant integration cannot drift apart.

Usage:
  python3 subzero_auth_test.py               # reuse/refresh cached tokens, or sign in
  python3 subzero_auth_test.py --login       # force a new interactive sign-in
  python3 subzero_auth_test.py --no-browser  # headless/SSH: only print the sign-in URL
  python3 subzero_auth_test.py --verbose  # also print the full device JSON
"""

import argparse
import contextlib
import json
import os
import secrets
import sys
import time
import types
import urllib.error
import urllib.request
import webbrowser
from typing import Any
from urllib.parse import urlencode

# Load shared stdlib-only modules without importing Home Assistant via the package.
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_pkg = types.ModuleType("subzero_libre")
_pkg.__path__ = [os.path.join(CURRENT_DIR, "custom_components", "subzero_libre")]
sys.modules["subzero_libre"] = _pkg

from subzero_libre.auth import (  # noqa: E402
    apim_headers,
    build_authorization_url,
    decode_jwt_payload,
    generate_pkce_pair,
    parse_auth_input,
    write_private_json,
)
from subzero_libre.const import (  # noqa: E402
    APIM_BASE_URL,
    B2C_TOKEN_URL,
    CLIENT_ID,
    MODEL_MAPPING,
    REDIRECT_URI,
)

TOKEN_FILE = os.path.join(CURRENT_DIR, "subzero_tokens.json")
TOKEN_HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": "okhttp/4.9.2",
}


def http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
    json_body: Any = None,
) -> tuple[int, str]:
    """Perform an HTTP request with urllib; returns (status, body). Status 0 = network error."""
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
    elif form is not None:
        data = urlencode(form).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8") if err.fp else ""
    except (urllib.error.URLError, TimeoutError) as err:
        return 0, str(getattr(err, "reason", err))


def token_request(form: dict[str, str]) -> dict[str, Any] | None:
    """POST to the B2C token endpoint; returns tokens or None (after printing why)."""
    status, text = http_request(B2C_TOKEN_URL, "POST", TOKEN_HEADERS, form=form)
    if status != 200:
        print(f"\n[!] Token request failed (HTTP {status}): {text}")
        return None
    tokens = json.loads(text)
    tokens["obtained_at"] = int(time.time())
    return tokens


def refresh_tokens(refresh_token: str) -> dict[str, Any] | None:
    tokens = token_request(
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
            "redirect_uri": REDIRECT_URI,
        }
    )
    if tokens:
        tokens.setdefault("refresh_token", refresh_token)
    return tokens


def save_tokens(tokens: dict[str, Any]) -> None:
    write_private_json(TOKEN_FILE, tokens)
    print(f"[*] Tokens saved to {TOKEN_FILE}")


def load_tokens() -> dict[str, Any] | None:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as err:
        print(f"[-] Could not read cached tokens: {err}")
        return None


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


def interactive_login(open_browser: bool = True) -> dict[str, Any]:
    print("\n" + "=" * 65 + "\nSUB-ZERO OAUTH2 PKCE AUTHENTICATION\n" + "=" * 65)
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

    parsed = parse_auth_input(input("\nEnter Redirect URL: "))
    if parsed.kind != "code" or not parsed.code:
        sys.exit("[!] No authorization code found in input!")
    if parsed.state and parsed.state != state:
        sys.exit("[!] The 'state' in that URL does not match this sign-in attempt. Start again.")

    print("\n[*] Exchanging authorization code for tokens...")
    tokens = token_request(
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": parsed.code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        }
    )
    if tokens is None:
        sys.exit(1)
    save_tokens(tokens)
    return tokens


def get_valid_tokens(force_login: bool, open_browser: bool = True) -> dict[str, Any]:
    """Return fresh tokens: cached if still valid, else refreshed, else a new sign-in."""
    tokens = None if force_login else load_tokens()
    if tokens and tokens.get("refresh_token"):
        lifetime = tokens.get("id_token_expires_in") or tokens.get("expires_in", 3600)
        if time.time() - tokens.get("obtained_at", 0) < lifetime - 300:
            print("[*] Reusing cached valid token.")
            return tokens
        print("[*] Cached token is expired or expiring soon, refreshing...")
        if refreshed := refresh_tokens(tokens["refresh_token"]):
            save_tokens(refreshed)
            return refreshed
        print("[-] Refresh failed, falling back to interactive login.")
    return interactive_login(open_browser)


def api_get(endpoint: str, id_token: str, user_id: str, method: str = "GET") -> tuple[int, Any]:
    """Call the APIM gateway with the same headers the integration sends."""
    status, text = http_request(
        f"{APIM_BASE_URL}{endpoint}", method, apim_headers(id_token, user_id, False)
    )
    try:
        return status, json.loads(text) if text else None
    except json.JSONDecodeError:
        return status, text


def list_appliances(tokens: dict[str, Any], verbose: bool) -> None:
    id_token = tokens.get("id_token")
    if not id_token:
        sys.exit("[!] No id_token available; run with --login.")
    claims = decode_jwt_payload(id_token)
    user_id = claims.get("sub", "")
    name = f"{claims.get('given_name', '')} {claims.get('family_name', '')}".strip() or "User"

    print("\n" + "=" * 65)
    print(f"AUTHENTICATED USER: {name} ({claims.get('email', 'unknown email')})")
    print(f"User ID (sub claim): {user_id}")
    print("=" * 65)

    status, data = api_get("/consumerapp/user/devices", id_token, user_id)
    if status != 200:
        sys.exit(f"[!] /consumerapp/user/devices returned HTTP {status}: {data}")
    devices = data.get("devices", []) if isinstance(data, dict) else data
    print(f"\nDISCOVERED {len(devices)} REGISTERED APPLIANCE(S)")

    for dev in devices:
        serial = dev.get("serialNumber", "unknown")
        appliance_id = dev.get("applianceId", "")
        print(f"\n--- Appliance: {dev.get('name') or 'Sub-Zero Refrigerator'} ---")
        print(f"    - Serial Number : {serial}")
        print(f"    - Model         : {MODEL_MAPPING.get(appliance_id, f'Sub-Zero Model {appliance_id}')}")
        print(f"    - Temp Unit     : {dev.get('temperatureUnitForAppliance') or 'F'}")

        fault_status, faults = api_get(f"/fault-notifications/v1/notifications/device/{serial}", id_token, user_id)
        if fault_status == 200:
            print(f"    - Active Faults : {faults or 'None (Operating Normally)'}")

        dump_file = os.path.join(CURRENT_DIR, f"appliance_{serial}.json")
        write_private_json(dump_file, {"metadata": dev, "faults": faults if fault_status == 200 else []})
        print(f"[*] Appliance data saved to {dump_file}")
        if verbose:
            print(json.dumps(dev, indent=2))

    status, hub = api_get("/signal-r/negotiateUser", id_token, user_id, method="POST")
    if status == 200:
        print(f"\n[+] SignalR negotiation succeeded (hub: {hub.get('url')})")
        print("    Run live_signalr_test.py to monitor real-time telemetry.")
    else:
        print(f"\n[-] SignalR negotiate returned HTTP {status}: {hub}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--login", action="store_true", help="Force interactive login (ignore cached tokens)")
    parser.add_argument("--no-browser", action="store_true", help="Never try to launch a browser; just print the URL")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print full device JSON")
    args = parser.parse_args()
    list_appliances(get_valid_tokens(args.login, not args.no_browser), args.verbose)


if __name__ == "__main__":
    main()
