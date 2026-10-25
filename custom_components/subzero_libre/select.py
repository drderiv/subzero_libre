"""Select platform for Sub-Zero Libre integration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .client import SubZeroAppliance
from .coordinator import SubZeroConfigEntry, SubZeroDataUpdateCoordinator
from .entity import SubZeroBaseEntity, SubZeroEntityDescription, async_setup_discovery
from .modes import (
    HUMIDITY_SETTINGS,
    ICE_MAKER_SETTINGS,
    MODE_FLAGS,
    MODE_SETTINGS,
    changed_settings,
    current_humidity,
    current_ice_maker,
    current_mode,
)

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class SubZeroSelectEntityDescription(SelectEntityDescription, SubZeroEntityDescription):
    """Describes a Sub-Zero select entity.

    ``settings`` maps each option to the property values that produce it; the
    current option is derived from telemetry by ``current_fn``.
    """

    settings: Mapping[str, Mapping[str, Any]]
    current_fn: Callable[[SubZeroAppliance], str | None]


SELECT_TYPES: tuple[SubZeroSelectEntityDescription, ...] = (
    SubZeroSelectEntityDescription(
        key="mode",
        translation_key="mode",
        settings=MODE_SETTINGS,
        current_fn=lambda app: current_mode(app.properties),
        supported_fn=lambda app: any(flag in app.properties for flag in MODE_FLAGS),
    ),
    SubZeroSelectEntityDescription(
        key="ice_maker_mode",
        translation_key="ice_maker_mode",
        settings=ICE_MAKER_SETTINGS,
        current_fn=lambda app: current_ice_maker(app.properties),
        supported_fn=lambda app: "ice_maker_on" in app.properties,
    ),
    SubZeroSelectEntityDescription(
        key="humidity_control",
        translation_key="humidity_control",
        settings=HUMIDITY_SETTINGS,
        current_fn=lambda app: current_humidity(app.properties),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SubZeroConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Sub-Zero selects from a config entry with dynamic capability detection."""
    async_setup_discovery(entry, async_add_entities, SELECT_TYPES, SubZeroSelect)


class SubZeroSelect(SubZeroBaseEntity, SelectEntity):
    """Representation of a Sub-Zero multi-option setting."""

    entity_description: SubZeroSelectEntityDescription

    def __init__(
        self,
        coordinator: SubZeroDataUpdateCoordinator,
        serial_number: str,
        description: SubZeroSelectEntityDescription,
    ) -> None:
        """Initialize the select."""
        super().__init__(coordinator, serial_number, description.key)
        self.entity_description = description
        self._attr_options = list(description.settings)

    @property
    def current_option(self) -> str | None:
        """Return the currently active option."""
        if not self.appliance:
            return None
        return self.entity_description.current_fn(self.appliance)

    async def async_select_option(self, option: str) -> None:
        """Apply an option, sending only the properties that differ from telemetry."""
        if not self.appliance:
            return
        changes = changed_settings(
            self.appliance.properties, self.entity_description.settings[option]
        )
        if changes:
            await self.coordinator.async_set_properties(self.serial_number, changes)
