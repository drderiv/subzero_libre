"""Binary sensor platform for Sub-Zero Libre integration."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
import re
from typing import Any, Callable

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import SubZeroAppliance
from .coordinator import SubZeroConfigEntry, SubZeroDataUpdateCoordinator
from .entity import (
    SubZeroBaseEntity,
    SubZeroEntityDescription,
    always_supported,
    async_setup_discovery,
)


PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SubZeroBinarySensorEntityDescription(BinarySensorEntityDescription, SubZeroEntityDescription):
    """Describes a Sub-Zero binary sensor entity."""

    is_on_fn: Callable[[SubZeroAppliance], bool | None] = lambda app: None
    attrs_fn: Callable[[SubZeroAppliance], dict[str, Any] | None] = lambda app: None


# Sub-Zero names door telemetry "<door>_door_ajar" (ref, frz, and e.g. wine on wine
# units); every such property the appliance reports gets a door sensor.
_DOOR_KEY_RE = re.compile(r"^([a-z0-9]+(?:_[a-z0-9]+)*)_door_ajar$")
_DOOR_NAMES = {"ref": "Refrigerator", "frz": "Freezer"}


@cache
def _door_description(property_key: str, door: str) -> SubZeroBinarySensorEntityDescription:
    """Build the description for a pattern-discovered door sensor."""
    return SubZeroBinarySensorEntityDescription(
        key=property_key,
        translation_key="door_ajar",
        translation_placeholders={"door": _DOOR_NAMES.get(door) or door.replace("_", " ").title()},
        device_class=BinarySensorDeviceClass.DOOR,
        is_on_fn=lambda app: bool(app.properties.get(property_key)),
    )


def _discover_doors(
    appliance: SubZeroAppliance,
) -> list[SubZeroBinarySensorEntityDescription]:
    """Return door descriptions for every *_door_ajar property reported."""
    return [
        _door_description(key, match.group(1))
        for key in appliance.properties
        if (match := _DOOR_KEY_RE.match(key))
    ]


BINARY_SENSOR_TYPES: tuple[SubZeroBinarySensorEntityDescription, ...] = (
    SubZeroBinarySensorEntityDescription(
        key="service_required",
        supported_fn=always_supported,
        translation_key="service_required",
        is_on_fn=lambda app: bool(app.properties.get("service_required")),
    ),
    SubZeroBinarySensorEntityDescription(
        key="unit_on",
        supported_fn=always_supported,
        translation_key="unit_on",
        device_class=BinarySensorDeviceClass.POWER,
        is_on_fn=lambda app: bool(app.properties.get("unit_on", True)),
    ),
    SubZeroBinarySensorEntityDescription(
        key="ice_clean_required",
        translation_key="ice_clean_required",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda app: bool(app.properties.get("ice_clean_required")),
    ),
    SubZeroBinarySensorEntityDescription(
        key="faults",
        supported_fn=always_supported,
        translation_key="faults",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda app: len(app.faults) > 0,
        attrs_fn=lambda app: {
            "fault_count": len(app.faults),
            "faults": app.faults,
        },
    ),
    SubZeroBinarySensorEntityDescription(
        key="connected",
        supported_fn=always_supported,
        translation_key="connected",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        is_on_fn=lambda app: app.is_connected,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SubZeroConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Sub-Zero binary sensors from a config entry with dynamic capability detection."""
    async_setup_discovery(
        entry,
        async_add_entities,
        BINARY_SENSOR_TYPES,
        SubZeroBinarySensor,
        dynamic_descriptions=_discover_doors,
    )


class SubZeroBinarySensor(SubZeroBaseEntity, BinarySensorEntity):
    """Representation of a Sub-Zero binary sensor."""

    entity_description: SubZeroBinarySensorEntityDescription

    def __init__(
        self,
        coordinator: SubZeroDataUpdateCoordinator,
        serial_number: str,
        description: SubZeroBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator, serial_number, description.key)
        self.entity_description = description
        if description.translation_placeholders:
            self._attr_translation_placeholders = description.translation_placeholders

    @property
    def is_on(self) -> bool | None:
        """Return True if the binary sensor is on."""
        if not self.appliance:
            return None
        return self.entity_description.is_on_fn(self.appliance)

    @property
    def available(self) -> bool:
        """Cloud Connection reports outages, so it must stay available during one."""
        if self.entity_description.key == "connected":
            return True
        return super().available

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return extra attributes."""
        if not self.appliance or not self.entity_description.attrs_fn:
            return None
        return self.entity_description.attrs_fn(self.appliance)
