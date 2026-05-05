"""Number platform — host load timer (3-tier) duration control."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SwidgetDataUpdateCoordinator
from .entity import SwidgetEntity

# The "20/40/60 switch" tops out at 60 minutes per preset level
# (3 levels = 180 min total). 240 leaves headroom for hosts that
# may report a longer max via firmware updates without silently
# capping user input.
TIMER_MAX_MINUTES = 240


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Swidget number entities (timer controls) from a config entry."""
    coordinator: SwidgetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    device = coordinator.device

    entities: list[NumberEntity] = []
    host = device.assemblies.get("host")
    if host is not None:
        # Per SDK request_handling.md, the 3-tier host load timer is
        # available on any component whose "functions" includes "timer".
        # Don't gate on device_type — keeps us correct if firmware
        # exposes the function on a non-TimerSwitch host.
        for component_id, component in host.components.items():
            if "timer" in component.functions:
                entities.append(
                    SwidgetTimerDurationNumber(coordinator, component_id)
                )

    async_add_entities(entities)


class SwidgetTimerDurationNumber(SwidgetEntity, NumberEntity):
    """Set / read the host load timer duration in minutes.

    Reading returns the active button-initiated timer (the auto-timer
    fields aren't surfaced — both physical button and HA-issued commands
    land in buttonTimer / buttonLevel). Writing sends ``{"duration": N}``
    — set to 0 to cancel the active timer. To turn the load on
    permanently, use the existing power switch (toggle), not this.
    """

    _attr_name = "Timer"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 0
    _attr_native_max_value = TIMER_MAX_MINUTES
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the timer-duration number."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_timer_duration"
        )

    def _timer_state(self) -> dict | None:
        """Return the current timer datapoint, or None if not present.

        The device responds with a dict on success and the literal
        string ``"nack"`` on a malformed request — only a dict is useful
        for state display.
        """
        try:
            value = (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
                .functions.get("timer")
            )
        except (KeyError, AttributeError):
            return None
        return value if isinstance(value, dict) else None

    @property
    def native_value(self) -> float | None:
        """Return the active button-initiated timer duration in minutes."""
        timer = self._timer_state()
        if timer is None:
            return None
        return int(timer.get("buttonTimer") or 0)

    @property
    def available(self) -> bool:
        """Available once we've seen a valid timer state from the device."""
        return self._timer_state() is not None

    async def async_set_native_value(self, value: float) -> None:
        """Send a new timer duration to the device. 0 cancels the timer."""
        await self.coordinator.device.send_command(
            assembly="host",
            component=self._component_id,
            function="timer",
            command={"duration": int(value)},
        )
        await self.coordinator.async_request_refresh()
