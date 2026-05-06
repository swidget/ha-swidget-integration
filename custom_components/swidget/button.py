"""Button platform — host load timer level advance."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SwidgetDataUpdateCoordinator
from .entity import SwidgetEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Swidget button entities (timer level advance) from a config entry."""
    coordinator: SwidgetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    device = coordinator.device

    entities: list[ButtonEntity] = []
    host = device.assemblies.get("host")
    if host is not None:
        for component_id, component in host.components.items():
            # Skip fans: the ``timer`` tag exists on Pesna fans too
            # but with the ``{"minutes": N}`` shape — there's no
            # 1->2->3 advance concept, so the button would do nothing.
            is_fan = (
                "exhaust" in component.functions
                or "supply" in component.functions
            )
            if "timer" in component.functions and not is_fan:
                entities.append(
                    SwidgetTimerAdvanceButton(coordinator, component_id)
                )

    async_add_entities(entities)


class SwidgetTimerAdvanceButton(SwidgetEntity, ButtonEntity):
    """Advance the host load timer to the next preset level (1 -> 2 -> 3 -> off).

    Sends ``{"up": true}`` per request_handling.md — same effect as the
    physical timer button on a 20/40/60 switch.
    """

    _attr_name = "Advance timer level"

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the timer-advance button."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_timer_advance"
        )

    async def async_press(self) -> None:
        """Send the advance-level command to the device."""
        await self.coordinator.device.send_command(
            assembly="host",
            component=self._component_id,
            function="timer",
            command={"up": True},
        )
        await self.coordinator.async_request_refresh()
