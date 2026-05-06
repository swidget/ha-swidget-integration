"""Select platform — Pesna fan operating mode.

Only the IB-series fans (IB150/IB160, internally FV20-class) implement
``setFanMode`` in firmware (see ``pesna_comms.cpp::setFanMode``). Other
Pesna variants don't expose the ``mode`` function in their summary, so
gating on function presence handles model-detection without us having
to maintain a per-variant allowlist here.
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SwidgetDataUpdateCoordinator
from .entity import SwidgetEntity

# Strings the firmware accepts for the IB-series ``mode`` request, per
# ``swidget-sdk/devices/swidgetV1/controllers/host/control/pesna_comms.cpp::setFanMode``.
# Sending anything else logs an error on-device and is a no-op.
_IB_SERIES_MODES: tuple[str, ...] = (
    "heatExchange",
    "supply",
    "exhaust",
    "recirculation",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Swidget select entities from a config entry."""
    coordinator: SwidgetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    device = coordinator.device

    entities: list[SelectEntity] = []
    host = device.assemblies.get("host")
    if host is not None:
        for component_id, component in host.components.items():
            if "mode" in component.functions:
                entities.append(SwidgetFanModeSelect(coordinator, component_id))

    async_add_entities(entities)


class SwidgetFanModeSelect(SwidgetEntity, SelectEntity):
    """Select the fan operating mode (IB-series only).

    Reads the bare-string ``mode`` value from the host component's
    datapoint and shows it as the current option. Picking an option
    sends ``{"mode": "<value>"}`` per the SDK request schema.
    """

    _attr_name = "Mode"
    _attr_translation_key = "fan_mode"
    _attr_options = list(_IB_SERIES_MODES)

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the fan-mode select."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_fan_mode"
        )

    @property
    def current_option(self) -> str | None:
        """Return the currently reported mode, or None if unknown."""
        try:
            value = (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
                .functions.get("mode")
            )
        except (KeyError, AttributeError):
            return None
        if not isinstance(value, str):
            return None
        # Forward-compatibility: if firmware reports a mode we haven't
        # listed (e.g. a future variant), surface it as None rather than
        # claiming a wrong option, so the user sees "(unknown)" instead.
        return value if value in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        """Send the chosen mode string to the device."""
        await self.coordinator.device.send_command(
            assembly="host",
            component=self._component_id,
            function="mode",
            command={"mode": option},
        )
        await self.coordinator.async_request_refresh()
