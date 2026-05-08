"""Binary sensor platform — insert binary functions."""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SwidgetDataUpdateCoordinator
from .entity import SwidgetEntity


@dataclass(frozen=True, kw_only=True)
class SwidgetHostBinarySensorDescription(BinarySensorEntityDescription):
    """Description for a host-component binary sensor.

    Two reader styles to cover the fan datapoint shapes:
    - ``mode == "field"`` (default) reads ``state[field]`` as a bool.
    - ``mode == "modules_triggered"`` reads ``state[field]`` as a string
      (``"triggered"`` / ``"dormant"``) and reports True iff
      ``"triggered"``. Used for the per-module map under ``modules``.
    """

    function: str
    field: str
    reader: str = "field"


@dataclass(frozen=True, kw_only=True)
class SwidgetInsertBinarySensorDescription(BinarySensorEntityDescription):
    """Description for a binary sensor that reads one bool field on an insert function.

    ``function`` is the function tag in ``component.functions`` (e.g.
    ``"occupied"``); ``field`` is the bool key inside that function's
    datapoint (e.g. ``"state"``). ``attribute_fields`` maps any extra
    fields worth surfacing as state attributes — ``(attr_name,
    function_field)`` pairs.
    """

    function: str
    field: str = "state"
    attribute_fields: tuple[tuple[str, str], ...] = dataclass_field(
        default_factory=tuple
    )


# Catalogue of insert binary sensors. Adding one is normally a single
# entry — the generic SwidgetInsertBinarySensor handles the lookup,
# error gating, unique_id, and attribute extraction.
INSERT_BINARY_SENSOR_DESCRIPTIONS: tuple[
    SwidgetInsertBinarySensorDescription, ...
] = (
    SwidgetInsertBinarySensorDescription(
        key="motion",
        function="occupied",
        name="Motion",
        device_class=BinarySensorDeviceClass.MOTION,
        attribute_fields=(("seconds_in_state", "secondsInState"),),
    ),
    # Water detector. ``state`` is leak presence (MOISTURE);
    # ``connected`` is cable health (CONNECTIVITY — ON when the probe
    # cable is plugged in and reading). Both come from the same
    # ``water`` function and live on the same insert component, so the
    # existing setup loop materialises them together.
    SwidgetInsertBinarySensorDescription(
        key="water_leak",
        function="water",
        field="state",
        name="Water leak",
        device_class=BinarySensorDeviceClass.MOISTURE,
        attribute_fields=(("seconds_in_state", "secondsInState"),),
    ),
    SwidgetInsertBinarySensorDescription(
        key="water_cable",
        function="water",
        field="connected",
        name="Water sensor cable",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    # Distance / proximity insert. ``detected`` is the bool presence
    # signal; the distance reading is surfaced as a sensor below.
    SwidgetInsertBinarySensorDescription(
        key="proximity_detected",
        function="proximity",
        field="detected",
        name="Object detected",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
    ),
)


# Host-side binary sensors. The fan ``filter`` payload carries two
# bool fields directly; the ``modules`` payload is a string-valued map
# (``"triggered"`` / ``"dormant"``) keyed by add-on module name, so we
# expose one binary sensor per detected module string.
HOST_BINARY_SENSOR_DESCRIPTIONS: tuple[
    SwidgetHostBinarySensorDescription, ...
] = (
    SwidgetHostBinarySensorDescription(
        key="filter_needs_cleaning",
        function="filter",
        field="needsCleaning",
        name="Filter needs cleaning",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    SwidgetHostBinarySensorDescription(
        key="filter_needs_replacement",
        function="filter",
        field="needsReplacement",
        name="Filter needs replacement",
        device_class=BinarySensorDeviceClass.PROBLEM,
    ),
    SwidgetHostBinarySensorDescription(
        key="condensation_module",
        function="modules",
        field="condensation",
        reader="modules_triggered",
        name="Condensation triggered",
        device_class=BinarySensorDeviceClass.MOISTURE,
    ),
    SwidgetHostBinarySensorDescription(
        key="motion_module",
        function="modules",
        field="motion",
        reader="modules_triggered",
        name="Motion module triggered",
        device_class=BinarySensorDeviceClass.MOTION,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Swidget binary sensors from a config entry."""
    coordinator: SwidgetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    device = coordinator.device

    entities: list[BinarySensorEntity] = []
    insert = device.assemblies.get("insert")
    if insert is not None:
        for component_id, component in insert.components.items():
            for description in INSERT_BINARY_SENSOR_DESCRIPTIONS:
                if description.function in component.functions:
                    entities.append(
                        SwidgetInsertBinarySensor(
                            coordinator, component_id, description
                        )
                    )

    host = device.assemblies.get("host")
    if host is not None:
        for component_id, component in host.components.items():
            # All HOST_BINARY_SENSOR_DESCRIPTIONS are fan-only today —
            # gate the whole loop on fan-ness (exhaust/supply presence)
            # so future non-fan hosts that happen to share a function
            # tag don't inherit the wrong reader.
            is_fan = (
                "exhaust" in component.functions
                or "supply" in component.functions
            )
            if not is_fan:
                continue
            # Module-triggered sensors are gated additionally on the
            # summary's ``modules`` list — only expose what the device
            # actually has installed, not every possible module name.
            installed_modules = set(getattr(component, "modules", []) or [])
            for description in HOST_BINARY_SENSOR_DESCRIPTIONS:
                if description.function not in component.functions:
                    continue
                if (
                    description.reader == "modules_triggered"
                    and description.field not in installed_modules
                ):
                    continue
                entities.append(
                    SwidgetHostBinarySensor(coordinator, component_id, description)
                )

    async_add_entities(entities)


class SwidgetInsertBinarySensor(SwidgetEntity, BinarySensorEntity):
    """Generic insert binary sensor driven by a description."""

    entity_description: SwidgetInsertBinarySensorDescription

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        component_id: str,
        description: SwidgetInsertBinarySensorDescription,
    ) -> None:
        """Initialize an insert binary sensor for the given description."""
        super().__init__(coordinator)
        self.entity_description = description
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_insert_{component_id}_{description.key}"
        )

    def _function_state(self) -> dict | None:
        """Return the live datapoint dict for this function, or None."""
        try:
            value = (
                self.coordinator.device.assemblies["insert"]
                .components[self._component_id]
                .functions.get(self.entity_description.function)
            )
        except (KeyError, AttributeError):
            return None
        return value if isinstance(value, dict) else None

    def _is_healthy(self, state: dict) -> bool:
        """Return True when the function's hardware is reporting normally."""
        return int(state.get("error") or 0) == 0

    @property
    def is_on(self) -> bool | None:
        """Return whether the bool field is set, or None when unknown."""
        state = self._function_state()
        if state is None or not self._is_healthy(state):
            return None
        value = state.get(self.entity_description.field)
        return value if isinstance(value, bool) else None

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Surface any description-declared extra fields as attributes."""
        if not self.entity_description.attribute_fields:
            return None
        state = self._function_state()
        if state is None:
            return None
        attrs: dict[str, object] = {}
        for attr_name, source_field in self.entity_description.attribute_fields:
            value = state.get(source_field)
            if value is not None:
                attrs[attr_name] = value
        return attrs or None


class SwidgetHostBinarySensor(SwidgetEntity, BinarySensorEntity):
    """Generic host-component binary sensor."""

    entity_description: SwidgetHostBinarySensorDescription

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        component_id: str,
        description: SwidgetHostBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_{description.key}"
        )

    def _function_state(self) -> dict | None:
        try:
            value = (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
                .functions.get(self.entity_description.function)
            )
        except (KeyError, AttributeError):
            return None
        return value if isinstance(value, dict) else None

    @property
    def is_on(self) -> bool | None:
        state = self._function_state()
        if state is None:
            return None
        raw = state.get(self.entity_description.field)
        if self.entity_description.reader == "modules_triggered":
            return raw == "triggered" if isinstance(raw, str) else None
        return raw if isinstance(raw, bool) else None
