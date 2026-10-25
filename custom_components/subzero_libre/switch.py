"""Switch platform for Sub-Zero Libre integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import (
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import SubZeroConfigEntry, SubZeroDataUpdateCoordinator
from .entity import (
    SubZeroBaseEntity,
    SubZeroEntityDescription,
    async_setup_discovery,
)


PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class SubZeroSwitchEntityDescription(SwitchEntityDescription, SubZeroEntityDescription):
    """Describes a Sub-Zero switch entity."""

    # Values written to the appliance; some settings are 0/1 integers, not booleans
    on_value: Any = True
    off_value: Any = False


SWITCH_TYPES: tuple[SubZeroSwitchEntityDescription, ...] = (
    SubZeroSwitchEntityDescription(
        key="air_filter_on",
        translation_key="air_filter_on",
    ),
    SubZeroSwitchEntityDescription(
        key="crisp_temp_mode",
        translation_key="crisp_temp_mode",
        on_value=1,
        off_value=0,
    ),
    SubZeroSwitchEntityDescription(
        key="night_mode",
        translation_key="night_mode",
        on_value=1,
        off_value=0,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SubZeroConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Sub-Zero switches from a config entry with dynamic capability detection."""
    async_setup_discovery(entry, async_add_entities, SWITCH_TYPES, SubZeroSwitch)


class SubZeroSwitch(SubZeroBaseEntity, SwitchEntity):
    """Representation of a Sub-Zero switch."""

    entity_description: SubZeroSwitchEntityDescription

    def __init__(
        self,
        coordinator: SubZeroDataUpdateCoordinator,
        serial_number: str,
        description: SubZeroSwitchEntityDescription,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator, serial_number, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool:
        """Return True if the switch is on."""
        if not self.appliance:
            return False
        return bool(self.appliance.properties.get(self.entity_description.key))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the switch."""
        await self.coordinator.async_set_property(
            self.serial_number,
            self.entity_description.key,
            self.entity_description.on_value,
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the switch."""
        await self.coordinator.async_set_property(
            self.serial_number,
            self.entity_description.key,
            self.entity_description.off_value,
        )

