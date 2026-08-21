"""Number platform — host load timer (3-tier) duration control."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ELITE_PLUS_FAN_MODEL_CODES
from .controls import numbered_name
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
            # The 3-tier load timer shares the ``timer`` tag with the
            # fan custom-CFM timer (a switch + select elsewhere) but
            # uses a different request shape. Fans always expose at
            # least one of exhaust/supply, so gate on their absence.
            is_fan = (
                "exhaust" in component.functions or "supply" in component.functions
            )
            if "timer" in component.functions and not is_fan:
                entities.append(
                    SwidgetTimerDurationNumber(coordinator, component_id)
                )
            # Balancing offset is a persisted config field; gate on the
            # firmware actually carrying it.
            config = device.device_config.config if device.device_config else {}
            component_config = (
                config.get("host", {}).get("components", {}).get(component_id, {})
            )
            if "offset" in component_config:
                entities.append(
                    SwidgetFanBalancingOffsetNumber(coordinator, component_id)
                )
            for bound in _FAN_BOUNDS:
                if bound.config_key in component_config:
                    entities.append(
                        SwidgetFanBoundNumber(coordinator, component_id, bound)
                    )
            model_code = str(getattr(component, "model_code", "") or "")
            for threshold in _FAN_CONFIG_THRESHOLDS:
                if threshold.config_key in component_config and (
                    not threshold.elite_plus_only
                    or model_code in ELITE_PLUS_FAN_MODEL_CODES
                ):
                    entities.append(
                        SwidgetFanConfigThresholdNumber(
                            coordinator, component_id, threshold
                        )
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
        # The dynamic ``name`` property builds on this numbered base so
        # the mode annotation doesn't break the Controls ordering.
        self._base_name = numbered_name(coordinator.device, "load_timer", "Timer")
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
            return self._base_name
        if int(timer.get("buttonLevel") or 0) == FORCE_ON_SENTINEL:
            return f"{self._base_name} (Permanent on)"
        minutes = int(timer.get("buttonTimer") or 0)
        if minutes > 0:
            return f"{self._base_name} ({minutes} min remaining)"
        return f"{self._base_name} (Off)"

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


# The balancing offset shifts the supply CFM, so magnitudes beyond the
# fan's own max CFM are meaningless; firmware stores it as an int16 and
# doesn't clamp, so we bound the input to ±maxCFM ourselves (falling
# back to ±500 when the summary doesn't carry maxCFM).
_OFFSET_FALLBACK_LIMIT = 500


class SwidgetFanBalancingOffsetNumber(SwidgetEntity, NumberEntity):
    """Mesh-balancing supply offset (config field ``offset``, CFM).

    A signed, persisted configuration value — positive pushes the
    supply above its operating point, negative below — so it lives in
    the device page's Configuration section next to the Balancing
    enable switch.
    """

    _attr_name = "Mesh Balance Offset"
    _attr_translation_key = "fan_balancing_offset"
    _attr_icon = "mdi:plus-minus-variant"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "CFM"

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the balancing-offset number."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_balancing_offset"
        )

    def _limit(self) -> int:
        try:
            max_cfm = int(
                getattr(
                    self.coordinator.device.assemblies["host"]
                    .components[self._component_id],
                    "max_cfm",
                    0,
                )
                or 0
            )
        except (KeyError, AttributeError, TypeError, ValueError):
            max_cfm = 0
        return max_cfm if max_cfm > 0 else _OFFSET_FALLBACK_LIMIT

    @property
    def native_min_value(self) -> float:
        return -self._limit()

    @property
    def native_max_value(self) -> float:
        return self._limit()

    def _config_value(self) -> int | None:
        device_config = self.coordinator.device.device_config
        if device_config is None:
            return None
        try:
            return int(
                device_config.config["host"]["components"][self._component_id][
                    "offset"
                ]
            )
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        """Available once the config field is known."""
        return super().available and self._config_value() is not None

    @property
    def native_value(self) -> float | None:
        return self._config_value()

    async def async_set_native_value(self, value: float) -> None:
        """Persist the chosen offset to device config."""
        await self.coordinator.async_apply_device_config(
            {"host": {"components": {self._component_id: {"offset": int(value)}}}}
        )


# Flat threshold values on the ERV (FV20 Setting2 register). Companions
# to the supply-limit enable switches in switch.py — the threshold is
# always stored, the flag decides whether it's in effect, so writing
# here never flips the enable. Ranges are the serial protocol's accept
# windows (``pesna_serial.cpp`` Setting2::parse): high-humidity 30-80 %,
# low-temp -10 to 20 °C.
@dataclass(frozen=True)
class _FanConfigThreshold:
    config_key: str
    name: str
    uid_suffix: str
    lo: int
    hi: int
    device_class: NumberDeviceClass
    unit: str
    icon: str
    # Humidity-dependent thresholds only work on the Elite Plus units
    # (the Elite has no humidity sensors — see switch.py).
    elite_plus_only: bool = False


_FAN_CONFIG_THRESHOLDS: tuple[_FanConfigThreshold, ...] = (
    _FanConfigThreshold(
        "lowTempThreshold", "Supply limit low temp threshold",
        "supply_limit_low_temp_threshold", -10, 20,
        NumberDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS,
        "mdi:snowflake-thermometer",
    ),
    _FanConfigThreshold(
        "highHumidityThreshold", "Supply limit high humidity threshold",
        "supply_limit_high_hum_threshold", 30, 80,
        NumberDeviceClass.HUMIDITY, PERCENTAGE,
        "mdi:water-percent-alert",
        elite_plus_only=True,
    ),
)


class SwidgetFanConfigThresholdNumber(SwidgetEntity, NumberEntity):
    """Threshold value for a flat fan config field.

    A persisted configuration value read from and written to
    ``device_config.host.components.<id>.<key>``; the companion switch
    (same name minus "threshold") turns the limit on and off.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        component_id: str,
        threshold: _FanConfigThreshold,
    ) -> None:
        """Initialize the config-threshold number."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._threshold = threshold
        self._attr_name = threshold.name
        self._attr_translation_key = f"fan_{threshold.uid_suffix}"
        self._attr_icon = threshold.icon
        self._attr_device_class = threshold.device_class
        self._attr_native_unit_of_measurement = threshold.unit
        self._attr_native_min_value = threshold.lo
        self._attr_native_max_value = threshold.hi
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}"
            f"_host_{component_id}_{threshold.uid_suffix}"
        )

    def _config_value(self) -> int | None:
        device_config = self.coordinator.device.device_config
        if device_config is None:
            return None
        try:
            return int(
                device_config.config["host"]["components"][self._component_id][
                    self._threshold.config_key
                ]
            )
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        """Available once the config field is known."""
        return super().available and self._config_value() is not None

    @property
    def native_value(self) -> float | None:
        """Return the stored threshold."""
        return self._config_value()

    async def async_set_native_value(self, value: float) -> None:
        """Persist the chosen threshold to device config."""
        await self.coordinator.async_apply_device_config(
            {
                "host": {
                    "components": {
                        self._component_id: {
                            self._threshold.config_key: int(value)
                        }
                    }
                }
            }
        )


# Sensor-bound thresholds (FV-15 Plus): each side of tempBounds /
# humidityBounds is a threshold value plus an enable flag. The value
# limits mirror the firmware's accept ranges (``pesna_fan.h``,
# kTempBound*/kHumidityBound*): temp min 14-40 °F, temp max 80-105 °F,
# humidity min 10-30 %, humidity max 40-80 %. Out-of-range writes are
# silently dropped on-device, so we bound the inputs ourselves.
@dataclass(frozen=True)
class _FanBound:
    config_key: str  # "tempBounds" | "humidityBounds"
    side: str  # "min" | "max"
    name: str
    uid_suffix: str
    lo: int
    hi: int
    device_class: NumberDeviceClass
    unit: str


_FAN_BOUNDS: tuple[_FanBound, ...] = (
    _FanBound(
        "tempBounds", "min", "Temperature min bound", "temp_bound_min",
        14, 40, NumberDeviceClass.TEMPERATURE, UnitOfTemperature.FAHRENHEIT,
    ),
    _FanBound(
        "tempBounds", "max", "Temperature max bound", "temp_bound_max",
        80, 105, NumberDeviceClass.TEMPERATURE, UnitOfTemperature.FAHRENHEIT,
    ),
    _FanBound(
        "humidityBounds", "min", "Humidity min bound", "humidity_bound_min",
        10, 30, NumberDeviceClass.HUMIDITY, PERCENTAGE,
    ),
    _FanBound(
        "humidityBounds", "max", "Humidity max bound", "humidity_bound_max",
        40, 80, NumberDeviceClass.HUMIDITY, PERCENTAGE,
    ),
)


class SwidgetFanBoundNumber(SwidgetEntity, NumberEntity):
    """Threshold value for one side of a fan sensor bound.

    Writes always carry the side's current enable flag alongside the
    value: per firmware's ``resolveBoundSide``, a bare value write
    would *enable* the side, while value+flag(false) just updates the
    stored threshold and stays disabled — so editing a disabled bound
    here safely stages the value for a later enable. The companion
    switch toggles the side on/off.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _attr_native_step = 1

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        component_id: str,
        bound: _FanBound,
    ) -> None:
        """Initialize the bound-threshold number."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._bound = bound
        self._attr_name = bound.name
        self._attr_translation_key = f"fan_{bound.uid_suffix}"
        self._attr_device_class = bound.device_class
        self._attr_native_unit_of_measurement = bound.unit
        self._attr_native_min_value = bound.lo
        self._attr_native_max_value = bound.hi
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}"
            f"_host_{component_id}_{bound.uid_suffix}"
        )

    def _bounds_config(self) -> dict | None:
        device_config = self.coordinator.device.device_config
        if device_config is None:
            return None
        try:
            value = device_config.config["host"]["components"][self._component_id][
                self._bound.config_key
            ]
        except (KeyError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    @property
    def available(self) -> bool:
        """Available once the bounds config is known."""
        return super().available and self._bounds_config() is not None

    @property
    def native_value(self) -> float | None:
        """Return the stored threshold (0 = never set -> unknown)."""
        bounds = self._bounds_config()
        if bounds is None:
            return None
        try:
            value = int(bounds.get(self._bound.side) or 0)
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    async def async_set_native_value(self, value: float) -> None:
        """Persist the threshold, preserving the side's enable state."""
        bounds = self._bounds_config() or {}
        enabled = bool(bounds.get(f"{self._bound.side}Enabled") or 0)
        await self.coordinator.async_apply_device_config(
            {
                "host": {
                    "components": {
                        self._component_id: {
                            self._bound.config_key: {
                                self._bound.side: int(value),
                                f"{self._bound.side}Enabled": enabled,
                            }
                        }
                    }
                }
            }
        )
