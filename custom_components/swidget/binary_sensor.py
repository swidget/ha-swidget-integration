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
