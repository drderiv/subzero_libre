#!/usr/bin/env python3
"""
Live Sub-Zero Telemetry Monitor & Setter CLI
============================================
Connects to the Sub-Zero cloud with the same client the Home Assistant integration
uses (custom_components/subzero_libre), so behavior matches the integration.

Run subzero_auth_test.py first to create subzero_tokens.json.

Usage:
  python3 live_signalr_test.py                                # monitor live telemetry (Ctrl+C to stop)
  python3 live_signalr_test.py --list-devices

  # Setpoints (integer degrees, in the appliance's unit)
  python3 live_signalr_test.py --set-fridge 38 --set-freezer 0 --set-crisper 36

  # Modes (same choices as the mobile app / Home Assistant selects)
  python3 live_signalr_test.py --set-mode short_vacation
  python3 live_signalr_test.py --set-ice-maker max_ice
  python3 live_signalr_test.py --set-humidity enhanced
  python3 live_signalr_test.py --set-night-mode off --set-air-purification on
  python3 live_signalr_test.py --set-crisper-auto off --set-crisper 37

  python3 live_signalr_test.py --set-fridge 38 --no-monitor   # send and exit
  python3 live_signalr_test.py --wake-up                      # cloud wake-up handshake
  python3 live_signalr_test.py --serial 1234567 --verbose     # pick appliance, debug log
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import types
from typing import Any

import aiohttp

# Load the integration's client directly. The package __init__ imports Home Assistant,
# so register a bare namespace package instead; the modules used here need only aiohttp.
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_pkg = types.ModuleType("subzero_libre")
_pkg.__path__ = [os.path.join(CURRENT_DIR, "custom_components", "subzero_libre")]
sys.modules["subzero_libre"] = _pkg

from subzero_libre.auth import write_private_json  # noqa: E402
from subzero_libre.client import SubZeroAsyncClient, SubZeroError  # noqa: E402
from subzero_libre.modes import (  # noqa: E402
    HUMIDITY_SETTINGS,
    ICE_MAKER_SETTINGS,
    MODE_SETTINGS,
)

TOKEN_FILE = os.path.join(CURRENT_DIR, "subzero_tokens.json")


def load_tokens() -> dict[str, Any]:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        sys.exit(
            f"[!] Tokens file not found at {TOKEN_FILE}\n"
            "    Please run subzero_auth_test.py first to authenticate."
        )
    except (OSError, json.JSONDecodeError) as err:
        sys.exit(f"[!] Could not read {TOKEN_FILE}: {err}")


def parse_bool(val: str) -> bool:
    v = val.strip().lower()
    if v in ("on", "true", "1", "yes", "enable"):
        return True
    if v in ("off", "false", "0", "no", "disable"):
        return False
    raise argparse.ArgumentTypeError(f"Invalid toggle: '{val}'. Expected 'on' or 'off'.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Sub-Zero cloud telemetry monitor & setter CLI",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    for flag, name in (("fridge", "refrigerator"), ("freezer", "freezer"), ("crisper", "crisper")):
        parser.add_argument(f"--set-{flag}", type=int, metavar="TEMP", help=f"Set {name} setpoint")
    parser.add_argument("--set-mode", choices=list(MODE_SETTINGS), help="Set appliance mode")
    parser.add_argument("--set-ice-maker", choices=list(ICE_MAKER_SETTINGS), help="Set ice maker mode")
    parser.add_argument("--set-humidity", choices=list(HUMIDITY_SETTINGS), help="Set humidity control")
    parser.add_argument("--set-night-mode", type=parse_bool, metavar="STATE", help="Night Mode on/off")
    parser.add_argument("--set-crisper-auto", type=parse_bool, metavar="STATE", help="Automatic crisper temperature (refrigerator - 2 degF) on/off")
    parser.add_argument("--set-air-purification", type=parse_bool, metavar="STATE", help="Air purification on/off")
    parser.add_argument("--serial", help="Target this appliance serial (default: first on the account)")
    parser.add_argument("--wake-up", "-w", action="store_true", help="Send the cloud wake-up handshake")
    parser.add_argument("--open-async", "-a", action="store_true", help="Send open_cloud_async to open the reporting channel")
    parser.add_argument("--no-monitor", action="store_true", help="Exit after sending commands")
    parser.add_argument("--list-devices", action="store_true", help="List appliances and exit")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG logging from the client")
    return parser


def collect_commands(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Translate CLI flags into `set` parameter dicts (same values the integration sends)."""
    commands: list[dict[str, Any]] = []
    for flag, key in (("fridge", "ref_set_temp"), ("freezer", "frz_set_temp"), ("crisper", "crisp_set_temp")):
        if (value := getattr(args, f"set_{flag}")) is not None:
            commands.append({key: value})
    if args.set_mode:
        commands.append(MODE_SETTINGS[args.set_mode])
    if args.set_ice_maker:
        commands.append(ICE_MAKER_SETTINGS[args.set_ice_maker])
    if args.set_humidity:
        commands.append(HUMIDITY_SETTINGS[args.set_humidity])
    if args.set_night_mode is not None:
        commands.append({"night_mode": int(args.set_night_mode)})  # 0/1 integer, not boolean
    if args.set_crisper_auto is not None:
        commands.append({"crisp_temp_mode": int(args.set_crisper_auto)})  # 0/1 integer
    if args.set_air_purification is not None:
        commands.append({"air_filter_on": args.set_air_purification})
    return commands


def banner(title: str) -> None:
    print("\n" + "=" * 65 + f"\n{title}\n" + "=" * 65)


async def monitor(client: SubZeroAsyncClient) -> None:
    """Stream telemetry through the client's real SignalR loop until interrupted."""

    def on_telemetry(serial: str, props: dict[str, Any]) -> None:
        print(f"\n[{time.strftime('%H:%M:%S')}] {serial}:\n{json.dumps(props, indent=2, sort_keys=True)}", flush=True)

    def on_connection(connected: bool) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] SignalR {'connected' if connected else 'disconnected'}", flush=True)

    client.register_signalr_callback(on_telemetry)
    client.register_connection_callback(on_connection)
    banner("MONITORING REAL-TIME TELEMETRY (press Ctrl+C to exit)")
    await client.async_start_signalr()
    try:
        await asyncio.Event().wait()
    finally:
        await client.async_stop_signalr()


async def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    tokens = load_tokens()

    def persist(refresh_token: str, id_token: str | None) -> None:
        """Save the rotated refresh token immediately so it is never lost."""
        tokens["refresh_token"] = refresh_token
        if id_token:
            tokens["id_token"] = id_token
        write_private_json(TOKEN_FILE, tokens)

    async with aiohttp.ClientSession() as session:
        client = SubZeroAsyncClient(
            session=session,
            refresh_token=tokens["refresh_token"],
            id_token=tokens.get("id_token"),
            on_tokens_updated=persist,
        )

        print("[*] Refreshing tokens with Sub-Zero Azure AD B2C...")
        try:
            await client.async_refresh_tokens()
            print(f"[+] Authenticated successfully! User ID: {client.user_id}")
            print("[*] Fetching registered Sub-Zero appliances...")
            devices = await client.async_get_devices()
        except SubZeroError as err:
            sys.exit(f"[!] {err}")

        if not devices:
            sys.exit("[!] No Sub-Zero appliances registered to this account.")

        print(f"[+] Found {len(devices)} appliance(s):")
        for idx, d in enumerate(devices, 1):
            print(f"    [{idx}] {d.name} (Model: {d.model_description})")
            print(f"        Serial: {d.serial_number}")
            print(f"        Temperature Unit: °{d.temp_unit}")
        if args.list_devices:
            return

        appliance = next((d for d in devices if d.serial_number == args.serial), None) if args.serial else devices[0]
        if appliance is None:
            sys.exit(f"[!] No appliance with serial {args.serial}")

        commands = collect_commands(args)

        if args.wake_up:
            banner(f"CLOUD WAKE-UP HANDSHAKE: {appliance.name}")
            ok = await client.async_wake_appliance(appliance)
            print("[+] Wake-up complete." if ok else "[!] Wake-up completed with warnings.")
            await asyncio.sleep(2)

        if args.open_async:
            banner(f"OPEN CLOUD ASYNC CHANNEL: {appliance.name}")
            ok = await client.async_open_cloud_async(appliance)
            print("[+] Channel opened." if ok else "[!] open_cloud_async completed with warnings.")

        if commands:
            banner("SETTER COMMANDS")
            for params in commands:
                print(f"[*] Sending {params} to {appliance.name}...")
                try:
                    await client.async_set_properties(appliance, params)
                    print("[+] Command accepted by the cloud.")
                except SubZeroError as err:
                    print(f"[!] Command failed: {err}")

        if args.no_monitor:
            return
        await monitor(client)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[*] Stopped.")
