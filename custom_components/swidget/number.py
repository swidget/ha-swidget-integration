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

# Sliding the entity to its rightmost position (FORCE_ON_SENTINEL)
# sends the device's "force permanent on" magic value (level 255),
# overriding any active timer without cycling the load. Anything
# in (0, FORCE_ON_SENTINEL) is treated as a duration in minutes.
# 0 cancels the timer (turning the load off, matching device behavior).
FORCE_ON_SENTINEL = 255


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
    """Combined timer + force-on slider for the host load timer.

    Slider semantics:
      * 0 — cancel the timer (load goes off, matching device behavior)
      * 1..254 — start a timer for N minutes
      * 255 (max) — force the load permanently on, overriding any
        active timer (sends ``{"level": 255}``)

    Reading prefers ``buttonLevel == 255`` (which the device reports
    after a force-on) over ``buttonTimer`` so the slider stays pinned
    at the rightmost position while the load is in permanent-on mode.
    """

    _attr_name = "Timer"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = FORCE_ON_SENTINEL
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the timer-duration slider."""
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
        """Return the slider position reflecting current timer/force-on state."""
        timer = self._timer_state()
        if timer is None:
            return None
        # buttonLevel == 255 is the device's "permanent on" indicator.
        # Pin the slider at the sentinel so the UI doesn't snap back to
        # 0 (buttonTimer is 0 in that mode).
        if int(timer.get("buttonLevel") or 0) == FORCE_ON_SENTINEL:
            return FORCE_ON_SENTINEL
        return int(timer.get("buttonTimer") or 0)

    @property
    def available(self) -> bool:
        """Available once we've seen a valid timer state from the device."""
        return self._timer_state() is not None

    async def async_set_native_value(self, value: float) -> None:
        """Translate slider position into the appropriate timer command."""
        target = int(value)
        if target >= FORCE_ON_SENTINEL:
            command: dict = {"level": FORCE_ON_SENTINEL}
        else:
            # Includes target == 0, which the device treats as cancel.
            command = {"duration": target}
        await self.coordinator.device.send_command(
            assembly="host",
            component=self._component_id,
            function="timer",
            command=command,
        )
        await self.coordinator.async_request_refresh()
