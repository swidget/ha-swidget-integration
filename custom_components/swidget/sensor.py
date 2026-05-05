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
    entities: list[SensorEntity] = [
        SwidgetHostTypeSensor(coordinator),
        SwidgetInsertTypeSensor(coordinator),
    ]
    # One sensor per component on each assembly so the function list for
    # each component is visible directly on the device page (the state)
    # rather than buried under attributes.
    for assembly_key in ("host", "insert"):
        assembly = coordinator.device.assemblies.get(assembly_key)
        if assembly is None:
            continue
        for component_id in assembly.components:
            entities.append(
                SwidgetComponentSensor(coordinator, assembly_key, component_id)
            )
    # Active timer level (1-3) for any host component that exposes
    # the 3-tier load timer.
    host = coordinator.device.assemblies.get("host")
    if host is not None:
        for component_id, component in host.components.items():
            if "timer" in component.functions:
                entities.append(
                    SwidgetTimerLevelSensor(coordinator, component_id)
                )
    async_add_entities(entities)


def _enum_value(value: object) -> str | None:
    """Return the underlying string for an Enum, or str() the value."""
    inner = getattr(value, "value", value)
    if inner is None or inner == -1:
        return None
    return str(inner)


def _assembly_type_label(
    coordinator: SwidgetDataUpdateCoordinator,
    enum_value: object,
    assembly_key: str,
) -> str | None:
    """Resolve an assembly type label.

    Prefer the SDK enum's friendly value; fall back to the raw string
    the device sent so an SDK that's behind on a new firmware type
    still surfaces something useful instead of "Unknown".
    """
    label = _enum_value(enum_value)
    if label is not None:
        return label
    assembly = coordinator.device.assemblies.get(assembly_key)
    if assembly is None:
        return None
    return getattr(assembly, "type", None) or None


class SwidgetHostTypeSensor(SwidgetEntity, SensorEntity):
    """Reports the host (base device) type, e.g. switch, outlet, dimmer.

    Named "Host" (not "Host type") so it sorts as a prefix before the
    "Host component N" entities on the device page — HA orders diagnostic
    entities alphabetically by display name.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Host"

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the host type sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.mac_address}_host_type"

    @property
    def native_value(self) -> str | None:
        """Return the host device type."""
        return _assembly_type_label(
            self.coordinator, self.coordinator.device.device_type, "host"
        )


class SwidgetInsertTypeSensor(SwidgetEntity, SensorEntity):
    """Reports the insert type, e.g. USB, TEMP HUMI MOTION, video.

    Named "Insert" (not "Insert type") so it sorts as a prefix before
    the "Insert component N" entities on the device page.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Insert"

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the insert type sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.mac_address}_insert_type"

    @property
    def native_value(self) -> str | None:
        """Return the insert type."""
        return _assembly_type_label(
            self.coordinator, self.coordinator.device.insert_type, "insert"
        )


class SwidgetTimerLevelSensor(SwidgetEntity, SensorEntity):
    """Active timer level (1-3) reported by a host load timer.

    Reads ``buttonLevel`` from the timer datapoint. Returns 0 when no
    timer is running. Useful for automations that branch on which
    preset is currently active.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Timer level"

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the timer-level sensor."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_timer_level"
        )

    @property
    def native_value(self) -> int | None:
        """Return the active button-initiated timer level, or 0 if none."""
        try:
            value = (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
                .functions.get("timer")
            )
        except (KeyError, AttributeError):
            return None
        if not isinstance(value, dict):
            return None
        return int(value.get("buttonLevel") or 0)


class SwidgetComponentSensor(SwidgetEntity, SensorEntity):
    """One per component: state is the comma-joined function names."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        assembly_key: str,
        component_id: str,
    ) -> None:
        """Initialize a single-component diagnostic sensor."""
        super().__init__(coordinator)
        self._assembly_key = assembly_key
        self._component_id = component_id
        self._attr_name = f"{assembly_key.capitalize()} component {component_id}"
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_{assembly_key}_component_{component_id}"
        )

    def _component(self):
        assembly = self.coordinator.device.assemblies.get(self._assembly_key)
        if assembly is None:
            return None
        return assembly.components.get(self._component_id)

    @property
    def native_value(self) -> str | None:
        """Return the comma-joined function names for this component."""
        component = self._component()
        if component is None:
            return None
        names = sorted(component.functions.keys())
        return ", ".join(names) if names else "(no functions)"

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose function names plus their current values for inspection."""
        component = self._component()
        if component is None:
            return None
        return {
            "functions": dict(component.functions),
        }
