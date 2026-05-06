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
        for component_id, component in host.components.items():
            # The 3-tier load timer and the fan timer share the
            # ``timer`` tag but use different request shapes. Pick by
            # whether airflow functions are present — fans always
            # expose at least one of exhaust/supply.
            is_fan = (
                "exhaust" in component.functions or "supply" in component.functions
            )
            if "timer" in component.functions:
                if is_fan:
                    entities.append(
                        SwidgetFanTimerNumber(coordinator, component_id)
                    )
                else:
                    entities.append(
                        SwidgetTimerDurationNumber(coordinator, component_id)
                    )
            if "boost" in component.functions:
                entities.append(SwidgetFanBoostNumber(coordinator, component_id))
            if "dutyCycle" in component.functions:
                entities.append(
                    SwidgetFanDutyCycleNumber(coordinator, component_id)
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
    def name(self) -> str:
        """Annotate the slider name with the active mode for in-Controls visibility."""
        timer = self._timer_state()
        if timer is None:
            return "Timer"
        if int(timer.get("buttonLevel") or 0) == FORCE_ON_SENTINEL:
            return "Timer (Permanent on)"
        minutes = int(timer.get("buttonTimer") or 0)
        if minutes > 0:
            return f"Timer ({minutes} min remaining)"
        return "Timer (Off)"

    @property
    def icon(self) -> str | None:
        """Glanceable cue for the slider's current mode."""
        value = self.native_value
        if value is None:
            return None
        if value >= FORCE_ON_SENTINEL:
            return "mdi:infinity"
        if value > 0:
            return "mdi:timer-sand"
        return "mdi:timer-off-outline"

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


# Fan boost ``minutes`` is a uint8_t in firmware (see
# ``component.cpp::1023``), so 255 is the natural "permanent boost"
# sentinel — map the slider's rightmost position to ``mode: "on"``
# rather than a long-but-finite timer.
_FAN_BOOST_PERMANENT = 255


class SwidgetFanBoostNumber(SwidgetEntity, NumberEntity):
    """Combined boost slider for Pesna fans.

    Slider semantics mirror the existing host-timer slider:
      * 0 — turn boost off (sends ``{"mode": "off"}``)
      * 1..254 — start a boost timer for N minutes
        (sends ``{"mode": "timer", "minutes": N}``)
      * 255 — permanent boost (sends ``{"mode": "on"}``)

    The device reports back ``boost.mode`` plus an optional
    ``boost.minutes`` (only while a timer is running), which we
    translate back into a slider position.
    """

    _attr_name = "Boost"
    _attr_translation_key = "fan_boost"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = _FAN_BOOST_PERMANENT
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        super().__init__(coordinator)
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_fan_boost"
        )

    def _boost_state(self) -> dict | None:
        try:
            value = (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
                .functions.get("boost")
            )
        except (KeyError, AttributeError):
            return None
        return value if isinstance(value, dict) else None

    @property
    def available(self) -> bool:
        return self._boost_state() is not None

    @property
    def native_value(self) -> float | None:
        boost = self._boost_state()
        if boost is None:
            return None
        mode = boost.get("mode")
        if mode == "on":
            return _FAN_BOOST_PERMANENT
        if mode == "timer":
            try:
                return int(boost.get("minutes") or 0)
            except (TypeError, ValueError):
                return 0
        return 0

    @property
    def icon(self) -> str | None:
        value = self.native_value
        if value is None:
            return None
        if value >= _FAN_BOOST_PERMANENT:
            return "mdi:fan-plus"
        if value > 0:
            return "mdi:timer-sand"
        return "mdi:fan-off"

    async def async_set_native_value(self, value: float) -> None:
        target = int(value)
        if target >= _FAN_BOOST_PERMANENT:
            command: dict = {"mode": "on"}
        elif target <= 0:
            command = {"mode": "off"}
        else:
            command = {"mode": "timer", "minutes": target}
        await self.coordinator.device.send_command(
            assembly="host",
            component=self._component_id,
            function="boost",
            command=command,
        )
        await self.coordinator.async_request_refresh()


# Fan timer minutes is also a uint8_t, but a permanent-on sentinel
# isn't meaningful here (use the boost slider for that). Cap at 254
# so the slider's rightmost position is still a real timer value.
_FAN_TIMER_MAX = 254


class SwidgetFanTimerNumber(SwidgetEntity, NumberEntity):
    """Custom-timer override slider for Pesna fans.

    The fan ``timer`` request shape (``{"minutes": N}``) is *different*
    from the host load-timer's three-tier shape, which is why we route
    by host kind in setup. Setting 0 cancels the override; otherwise
    the device runs at its current speed for N minutes.
    """

    _attr_name = "Fan timer"
    _attr_translation_key = "fan_timer"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = _FAN_TIMER_MAX
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        super().__init__(coordinator)
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_fan_timer"
        )

    def _timer_state(self) -> dict | None:
        try:
            value = (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
                .functions.get("timer")
            )
        except (KeyError, AttributeError):
            return None
        # Per the datapoint spec, ``timer`` is omitted entirely when no
        # fan timer is active — treat that as "0 minutes remaining".
        return value if isinstance(value, dict) else None

    @property
    def native_value(self) -> float | None:
        timer = self._timer_state()
        if timer is None:
            return 0
        try:
            return int(timer.get("minutes") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def icon(self) -> str | None:
        value = self.native_value
        if value and value > 0:
            return "mdi:timer-sand"
        return "mdi:timer-off-outline"

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.device.send_command(
            assembly="host",
            component=self._component_id,
            function="timer",
            command={"minutes": int(value)},
        )
        await self.coordinator.async_request_refresh()


# Duty cycle is minutes-per-hour, so a sensible cap is 60. Firmware
# accepts a wider int range (see component.cpp::954) but >60 has no
# physical meaning.
_DUTY_CYCLE_MAX = 60


class SwidgetFanDutyCycleNumber(SwidgetEntity, NumberEntity):
    """Minutes-per-hour duty cycle for Pesna fans.

    Useful on continuous-ventilation modes where the fan runs only N
    minutes out of every 60 instead of full-time.
    """

    _attr_name = "Duty cycle"
    _attr_translation_key = "fan_duty_cycle"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = _DUTY_CYCLE_MAX
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        super().__init__(coordinator)
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_duty_cycle"
        )

    def _duty_state(self) -> dict | None:
        try:
            value = (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
                .functions.get("dutyCycle")
            )
        except (KeyError, AttributeError):
            return None
        return value if isinstance(value, dict) else None

    @property
    def available(self) -> bool:
        return self._duty_state() is not None

    @property
    def native_value(self) -> float | None:
        duty = self._duty_state()
        if duty is None:
            return None
        try:
            return int(duty.get("minutes") or 0)
        except (TypeError, ValueError):
            return None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.device.send_command(
            assembly="host",
            component=self._component_id,
            function="dutyCycle",
            command={"minutes": int(value)},
        )
        await self.coordinator.async_request_refresh()
