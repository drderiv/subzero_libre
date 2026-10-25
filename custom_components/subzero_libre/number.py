"""Number platform for Sub-Zero Libre integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import SubZeroAppliance
from .const import DOMAIN, get_setpoint_range
from .coordinator import SubZeroConfigEntry, SubZeroDataUpdateCoordinator
from .entity import (
    SubZeroBaseEntity,
    SubZeroEntityDescription,
    async_setup_discovery,
)


PARALLEL_UPDATES = 1


def _prop_as_float(key: str) -> Callable[[SubZeroAppliance], float | None]:
    """Return a value_fn reading telemetry property `key` as a float."""
    return lambda app: float(v) if (v := app.properties.get(key)) is not None else None


@dataclass(frozen=True, kw_only=True)
class SubZeroNumberEntityDescription(NumberEntityDescription, SubZeroEntityDescription):
    """Describes a Sub-Zero number entity."""

    value_fn: Callable[[SubZeroAppliance], float | None] = lambda app: None
    # Returns an exception translation key when writes are currently not allowed. The
    # value stays visible (like the mobile app's greyed-out display) but changes fail.
    blocked_fn: Callable[[SubZeroAppliance], str | None] = lambda app: None


NUMBER_TYPES: tuple[SubZeroNumberEntityDescription, ...] = (
    SubZeroNumberEntityDescription(
        key="ref_set_temp",
        translation_key="ref_set_temp",
        device_class=NumberDeviceClass.TEMPERATURE,
        mode=NumberMode.BOX,
        native_step=1.0,
        value_fn=_prop_as_float("ref_set_temp"),
    ),
    SubZeroNumberEntityDescription(
        key="frz_set_temp",
        translation_key="frz_set_temp",
        device_class=NumberDeviceClass.TEMPERATURE,
        mode=NumberMode.BOX,
        native_step=1.0,
        value_fn=_prop_as_float("frz_set_temp"),
    ),
    SubZeroNumberEntityDescription(
        key="crisp_set_temp",
        translation_key="crisp_set_temp",
        # In automatic mode the appliance holds the crisper at refrigerator - 2 degF
        blocked_fn=lambda app: "crisper_auto_on" if app.properties.get("crisp_temp_mode") else None,
        device_class=NumberDeviceClass.TEMPERATURE,
        mode=NumberMode.BOX,
        native_step=1.0,
        value_fn=_prop_as_float("crisp_set_temp"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SubZeroConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Sub-Zero number entities from a config entry with dynamic capability detection."""
    async_setup_discovery(entry, async_add_entities, NUMBER_TYPES, SubZeroNumberEntity)


class SubZeroNumberEntity(SubZeroBaseEntity, NumberEntity):
    """Representation of a Sub-Zero temperature setpoint number entity."""

    entity_description: SubZeroNumberEntityDescription

    def __init__(
        self,
        coordinator: SubZeroDataUpdateCoordinator,
        serial_number: str,
        description: SubZeroNumberEntityDescription,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator, serial_number, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        """Return the current setpoint value."""
        if not self.appliance:
            return None
        return self.entity_description.value_fn(self.appliance)

    def _setpoint_range(self) -> tuple[float, float]:
        """Return (min, max) for this appliance model and temperature unit."""
        appliance = self.appliance
        return get_setpoint_range(
            appliance.appliance_id if appliance else "",
            self.entity_description.key,
            appliance.temp_unit if appliance else "F",
        )

    @property
    def native_min_value(self) -> float:
        """Return minimum setpoint value."""
        return self._setpoint_range()[0]

    @property
    def native_max_value(self) -> float:
        """Return maximum setpoint value."""
        return self._setpoint_range()[1]

    @property
    def native_unit_of_measurement(self) -> str:
        """Return temperature unit."""
        if self.appliance and self.appliance.temp_unit == "C":
            return UnitOfTemperature.CELSIUS
        return UnitOfTemperature.FAHRENHEIT

    async def async_set_native_value(self, value: float) -> None:
        """Set new temperature setpoint value."""
        if self.appliance and (blocked := self.entity_description.blocked_fn(self.appliance)):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key=blocked)
        await self.coordinator.async_set_property(
            self.serial_number,
            self.entity_description.key,
            int(round(value)),
        )

