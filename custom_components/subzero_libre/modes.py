"""Appliance mode definitions shared by the select platform and the developer CLI.

Pure data and functions (no Home Assistant), derived from observed telemetry. The
appliance applies its own side effects: vacation modes turn the ice maker off and
High Usage turns Max Ice on with its own timer, restoring both on exit.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MODE_FLAGS = ("sabbath_on", "high_use_on", "short_vacation_on", "long_vacation_on")


def _mode_setting(active: str | None) -> dict[str, bool]:
    return {flag: flag == f"{active}_on" for flag in MODE_FLAGS}


# Option -> property values that produce it
MODE_SETTINGS: dict[str, dict[str, Any]] = {
    "normal": _mode_setting(None),
    "high_use": _mode_setting("high_use"),
    "short_vacation": _mode_setting("short_vacation"),
    "long_vacation": _mode_setting("long_vacation"),
    "sabbath": _mode_setting("sabbath"),
}

ICE_MAKER_SETTINGS: dict[str, dict[str, Any]] = {
    "normal": {"ice_maker_on": True, "max_ice_on": False, "night_ice_on": False},
    "max_ice": {"ice_maker_on": True, "max_ice_on": True, "night_ice_on": False},
    "night_ice": {"ice_maker_on": True, "max_ice_on": False, "night_ice_on": True},
    "off": {"ice_maker_on": False},
}

HUMIDITY_SETTINGS: dict[str, dict[str, Any]] = {
    "normal": {"humidity_control": 1},
    "enhanced": {"humidity_control": 2},
}


def current_mode(props: Mapping[str, Any]) -> str:
    """Return the active mode option from telemetry."""
    for flag in MODE_FLAGS:
        if props.get(flag):
            return flag.removesuffix("_on")
    return "normal"


def current_ice_maker(props: Mapping[str, Any]) -> str:
    """Return the active ice maker option from telemetry."""
    if not props.get("ice_maker_on"):
        return "off"
    if props.get("max_ice_on"):
        return "max_ice"
    if props.get("night_ice_on"):
        return "night_ice"
    return "normal"


def current_humidity(props: Mapping[str, Any]) -> str | None:
    """Return the active humidity option, or None for an unknown value."""
    return {1: "normal", 2: "enhanced"}.get(props.get("humidity_control"))


def changed_settings(props: Mapping[str, Any], setting: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the values in ``setting`` that differ from current telemetry.

    A property the appliance has not reported yet counts as off/False.
    """
    return {key: value for key, value in setting.items() if props.get(key, False) != value}
