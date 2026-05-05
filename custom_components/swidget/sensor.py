"""Diagnostic sensors that surface device structure on the device page."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SwidgetDataUpdateCoordinator
from .entity import SwidgetEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Swidget diagnostic sensors from a config entry."""
    coordinator: SwidgetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            SwidgetHostTypeSensor(coordinator),
            SwidgetInsertTypeSensor(coordinator),
            SwidgetAssemblyComponentsSensor(coordinator, "host"),
            SwidgetAssemblyComponentsSensor(coordinator, "insert"),
        ]
    )


def _enum_value(value: object) -> str | None:
    """Return the underlying string for an Enum, or str() the value."""
    inner = getattr(value, "value", value)
    if inner is None or inner == -1:
        return None
    return str(inner)


class SwidgetHostTypeSensor(SwidgetEntity, SensorEntity):
    """Reports the host (base device) type, e.g. switch, outlet, dimmer."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Host type"

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the host type sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.mac_address}_host_type"

    @property
    def native_value(self) -> str | None:
        """Return the host device type."""
        return _enum_value(self.coordinator.device.device_type)


class SwidgetInsertTypeSensor(SwidgetEntity, SensorEntity):
    """Reports the insert type, e.g. USB, TEMP HUMI MOTION, video."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Insert type"

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the insert type sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.mac_address}_insert_type"

    @property
    def native_value(self) -> str | None:
        """Return the insert type."""
        return _enum_value(self.coordinator.device.insert_type)


class SwidgetAssemblyComponentsSensor(SwidgetEntity, SensorEntity):
    """Per-assembly summary: component count plus per-component function lists in attributes."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, assembly_key: str
    ) -> None:
        """Initialize a per-assembly diagnostic sensor."""
        super().__init__(coordinator)
        self._assembly_key = assembly_key
        self._attr_name = f"{assembly_key.capitalize()} components"
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_{assembly_key}_components"
        )

    @property
    def native_value(self) -> int | None:
        """Return the number of components on this assembly."""
        assembly = self.coordinator.device.assemblies.get(self._assembly_key)
        if assembly is None:
            return None
        return len(assembly.components)

    @property
    def extra_state_attributes(self) -> dict[str, list[str]] | None:
        """Return per-component function names for inspection on the device page."""
        assembly = self.coordinator.device.assemblies.get(self._assembly_key)
        if assembly is None:
            return None
        return {
            component_id: sorted(component.functions.keys())
            for component_id, component in assembly.components.items()
        }
