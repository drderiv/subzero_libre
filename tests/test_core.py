"""Tests for the pure logic: modes, telemetry ordering, input parsing, discovery, safety checks."""

from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.subzero_libre.auth import decode_jwt_payload, parse_auth_input
from custom_components.subzero_libre.binary_sensor import _discover_doors
from custom_components.subzero_libre.client import SubZeroAppliance, extract_telemetry, redact_error
from custom_components.subzero_libre.const import get_setpoint_range
from custom_components.subzero_libre.modes import (
    ICE_MAKER_SETTINGS,
    MODE_SETTINGS,
    changed_settings,
    current_humidity,
    current_ice_maker,
    current_mode,
)
from custom_components.subzero_libre import number as number_mod
from custom_components.subzero_libre import select as select_mod


def appliance(**props) -> SubZeroAppliance:
    app = SubZeroAppliance({"serialNumber": "S1", "id": "D1"})
    app.update_properties({"seq": 1, **props})
    return app


# ---------------------------------------------------------------- modes


def test_current_mode_and_ice_maker():
    assert current_mode({}) == "normal"
    assert current_mode({"short_vacation_on": True}) == "short_vacation"
    assert current_ice_maker({"ice_maker_on": False}) == "off"
    assert current_ice_maker({"ice_maker_on": True, "max_ice_on": True}) == "max_ice"
    assert current_ice_maker({"ice_maker_on": True, "night_ice_on": True}) == "night_ice"
    assert current_ice_maker({"ice_maker_on": True}) == "normal"
    assert current_humidity({"humidity_control": 2}) == "enhanced"
    assert current_humidity({"humidity_control": 9}) is None


def test_changed_settings_sends_only_differences():
    props = {"ice_maker_on": True, "max_ice_on": False, "night_ice_on": False}
    assert changed_settings(props, ICE_MAKER_SETTINGS["max_ice"]) == {"max_ice_on": True}
    assert changed_settings(props, ICE_MAKER_SETTINGS["normal"]) == {}
    assert changed_settings({}, MODE_SETTINGS["normal"]) == {}  # unreported flags count as off
    assert changed_settings({"sabbath_on": True}, MODE_SETTINGS["normal"]) == {"sabbath_on": False}
    assert changed_settings({"short_vacation_on": True}, MODE_SETTINGS["normal"]) == {"short_vacation_on": False}


async def test_select_sends_only_changed_properties():
    app = appliance(ice_maker_on=True, max_ice_on=False, night_ice_on=False)
    entity = select_mod.SubZeroSelect.__new__(select_mod.SubZeroSelect)
    entity.entity_description = next(d for d in select_mod.SELECT_TYPES if d.key == "ice_maker_mode")
    entity.coordinator = MagicMock(async_set_properties=AsyncMock())
    entity._serial_number = "S1"
    type(entity).appliance = property(lambda self: app)
    await entity.async_select_option("max_ice")
    entity.coordinator.async_set_properties.assert_awaited_once_with("S1", {"max_ice_on": True})
    entity.coordinator.async_set_properties.reset_mock()
    await entity.async_select_option("normal")
    entity.coordinator.async_set_properties.assert_not_awaited()


# ---------------------------------------------------- telemetry ordering


def test_older_sequence_does_not_override_newer_value():
    app = appliance(night_mode=1)
    app.update_properties({"seq": 551, "night_mode": 1})
    app.update_properties({"seq": 550, "night_mode": 0})  # arrived late
    assert app.properties["night_mode"] == 1


def test_optimistic_write_is_not_overridden_by_older_echo_but_new_echo_applies():
    app = appliance(night_mode=1)
    app.update_properties({"night_mode": 0}, from_device=False)
    assert app.properties["night_mode"] == 0
    app.update_properties({"seq": 2, "night_mode": 0})  # echo of our write
    assert app.properties["night_mode"] == 0


def test_metadata_refresh_keeps_telemetry():
    app = appliance(ref_set_temp=38)
    app.update_metadata({"serialNumber": "S1", "id": "D1", "name": "Renamed"})
    assert app.name == "Renamed" and app.properties["ref_set_temp"] == 38


def test_extract_telemetry_snapshot_and_delta():
    inner = {"pload": {"appliance_serial": "S1", "ref_set_temp": 38, "notifs": [], "props": {"frz_door_ajar": True}}}
    arg = {"DeviceId": "D1", "Payload": {"api.async_channel": inner}}
    device_id, serial, props = extract_telemetry(arg)
    assert (device_id, serial) == ("D1", "S1")
    assert props["ref_set_temp"] == 38 and props["frz_door_ajar"] is True and "notifs" not in props
    assert extract_telemetry({"DeviceId": "D1", "Payload": {"api.notifications": "x"}})[2] == {}


# ------------------------------------------------------- input parsing


def test_parse_auth_input_variants():
    parsed = parse_auth_input("com.szg.x://oauth/redirect?code=ABC&state=ST")
    assert (parsed.kind, parsed.code, parsed.state) == ("code", "ABC", "ST")
    assert parse_auth_input('{"refresh_token": "r", "id_token": "i"}').kind == "tokens_json"
    assert parse_auth_input("just-a-refresh-token").kind == "refresh_token"
    assert parse_auth_input("com.szg.x://oauth/redirect").code == ""


def test_decode_jwt_payload_tolerates_garbage():
    assert decode_jwt_payload("not-a-jwt") == {}
    assert decode_jwt_payload("a.%%%.c") == {}


def test_redact_error_masks_secrets():
    text = redact_error("GET wss://x/?hub=y&access_token=SECRET123 failed; password=hunter2 Bearer abc.def")
    assert "SECRET123" not in text and "hunter2" not in text and "abc.def" not in text


# ------------------------------------------------- discovery and numbers


def test_door_discovery_by_pattern():
    app = appliance(ref_door_ajar=False, frz_door_ajar=True, wine_door_ajar=False, door_ajar_timeout=5)
    doors = {d.key: d.translation_placeholders["door"] for d in _discover_doors(app)}
    assert doors == {"ref_door_ajar": "Refrigerator", "frz_door_ajar": "Freezer", "wine_door_ajar": "Wine"}


def test_setpoint_ranges():
    assert get_setpoint_range("17.11.3.3", "ref_set_temp", "F") == (34.0, 42.0)
    assert get_setpoint_range("17.11.3.3", "frz_set_temp", "C") == (-21.0, -15.0)


async def test_crisper_rejects_writes_in_automatic_mode_but_allows_manual():
    desc = next(d for d in number_mod.NUMBER_TYPES if d.key == "crisp_set_temp")
    app = appliance(crisp_set_temp=36, crisp_temp_mode=1)
    entity = number_mod.SubZeroNumberEntity.__new__(number_mod.SubZeroNumberEntity)
    entity.entity_description = desc
    entity.coordinator = MagicMock(async_set_property=AsyncMock())
    entity._serial_number = "S1"
    type(entity).appliance = property(lambda self: app)
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(37)
    app.update_properties({"seq": 2, "crisp_temp_mode": 0})
    await entity.async_set_native_value(37)
    entity.coordinator.async_set_property.assert_awaited_once_with("S1", "crisp_set_temp", 37)
