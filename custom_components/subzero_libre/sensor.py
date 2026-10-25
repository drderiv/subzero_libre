"""Sensor platform for Sub-Zero Libre integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import SubZeroAppliance
from .coordinator import SubZeroConfigEntry, SubZeroDataUpdateCoordinator
from .entity import (
    SubZeroBaseEntity,
    SubZeroEntityDescription,
    async_setup_discovery,
)


PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class SubZeroSensorEntityDescription(SensorEntityDescription, SubZeroEntityDescription):
    """Describes a Sub-Zero sensor entity."""

    value_fn: Callable[[SubZeroAppliance], Any] = lambda app: None


SENSOR_TYPES: tuple[SubZeroSensorEntityDescription, ...] = (
    SubZeroSensorEntityDescription(
        key="water_filter_pct_remaining",
        translation_key="water_filter_pct_remaining",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda app: app.properties.get("water_filter_pct_remaining"),
    ),
    SubZeroSensorEntityDescription(
        key="air_filter_pct_remaining",
        translation_key="air_filter_pct_remaining",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda app: app.properties.get("air_filter_pct_remaining"),
    ),
    SubZeroSensorEntityDescription(
        key="ap_rssi",
        translation_key="ap_rssi",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement="dBm",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda app: app.properties.get("ap_rssi"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SubZeroConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Sub-Zero sensors from a config entry with dynamic capability detection."""
    async_setup_discovery(entry, async_add_entities, SENSOR_TYPES, SubZeroSensor)


class SubZeroSensor(SubZeroBaseEntity, SensorEntity):
    """Representation of a Sub-Zero sensor."""

    entity_description: SubZeroSensorEntityDescription

    def __init__(
        self,
        coordinator: SubZeroDataUpdateCoordinator,
        serial_number: str,
        description: SubZeroSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator, serial_number, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        """Return the current sensor value."""
        if not self.appliance:
            return None
        if (val := self.entity_description.value_fn(self.appliance)) is None:
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return None
