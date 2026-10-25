"""Base entity for Sub-Zero Libre integration."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import SubZeroAppliance
from .const import DOMAIN
from .coordinator import SubZeroConfigEntry, SubZeroDataUpdateCoordinator


def always_supported(_appliance: SubZeroAppliance) -> bool:
    """`supported_fn` for entities every appliance gets regardless of telemetry."""
    return True


@dataclass(frozen=True, kw_only=True)
class SubZeroEntityDescription(EntityDescription):
    """Entity description with a capability check used for dynamic discovery."""

    # Defaults to "the appliance has reported a value for `key`".
    supported_fn: Callable[[SubZeroAppliance], bool] | None = None

    def supported(self, appliance: SubZeroAppliance) -> bool:
        """Return True if this appliance model/state supports the entity."""
        if self.supported_fn is not None:
            return self.supported_fn(appliance)
        return appliance.properties.get(self.key) is not None


@callback
def async_setup_discovery[D: SubZeroEntityDescription](
    entry: SubZeroConfigEntry,
    async_add_entities: AddEntitiesCallback,
    descriptions: Iterable[D],
    factory: Callable[[SubZeroDataUpdateCoordinator, str, D], SubZeroBaseEntity],
    dynamic_descriptions: Callable[[SubZeroAppliance], Iterable[D]] | None = None,
) -> None:
    """Add entities for every supported (appliance, description) pair, now and as telemetry arrives.

    ``dynamic_descriptions`` may derive extra descriptions from an appliance's reported
    properties (e.g. door sensors named by pattern); they must still pass ``supported``.
    """
    coordinator = entry.runtime_data
    descriptions = tuple(descriptions)
    known: set[tuple[str, str]] = set()

    @callback
    def _discover() -> None:
        new_entities = []
        for serial, appliance in coordinator.data.items():
            extra = dynamic_descriptions(appliance) if dynamic_descriptions else ()
            for description in (*descriptions, *extra):
                key = (serial, description.key)
                if key not in known and description.supported(appliance):
                    known.add(key)
                    new_entities.append(factory(coordinator, serial, description))
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(coordinator.async_add_listener(_discover))
    _discover()


class SubZeroBaseEntity(CoordinatorEntity[SubZeroDataUpdateCoordinator]):
    """Common base for Sub-Zero entities."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SubZeroDataUpdateCoordinator,
        serial_number: str,
        entity_key: str,
    ) -> None:
        """Initialize the base entity."""
        super().__init__(coordinator)
        self._serial_number = serial_number
        self._entity_key = entity_key
        self._attr_unique_id = f"{serial_number}_{entity_key}"

        appliance = coordinator.data[serial_number]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial_number)},
            manufacturer="Sub-Zero",
            name=appliance.name,
            model=appliance.model_description,
            model_id=appliance.appliance_id,
            serial_number=serial_number,
        )

    @property
    def serial_number(self) -> str:
        """Return the appliance serial number."""
        return self._serial_number

    @property
    def appliance(self) -> SubZeroAppliance | None:
        """Return the appliance data from coordinator."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get(self._serial_number)

    @property
    def available(self) -> bool:
        """Return True while data is current (SignalR stream up or last REST poll succeeded)."""
        appliance = self.appliance
        return appliance is not None and (
            appliance.is_connected or self.coordinator.last_update_success
        )
