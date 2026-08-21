"""Switch platform for Swidget devices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from swidget import InsertType

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    ELITE_PLUS_FAN_MODEL_CODES,
    FAN_DEFAULT_CFM_CONFIG_KEYS,
)
from .controls import numbered_name
from .coordinator import SwidgetDataUpdateCoordinator
from .entity import SwidgetEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Swidget switches from a config entry."""
    coordinator: SwidgetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    device = coordinator.device

    entities: list[SwitchEntity] = []

    # Dimmers expose their on/off through the light platform instead.
    # Gate on the ``toggle`` function being declared rather than on
    # device family so we materialize the power switch exactly when
    # the firmware will accept the ``toggle`` request. Fans without a
    # host toggle (the FV05 — firmware deliberately gives it no onOff,
    # see component.cpp::generateComponentSettings) get an emulated
    # power switch below instead. ERVs (IB-series, identified by the
    # ``mode`` function) do carry a toggle but are meant to run
    # continuously — airflow is managed through the mode select and
    # boost, so they get no user-facing power control at all.
    host = device.assemblies.get("host")
    wants_power_switch = host is not None and any(
        "toggle" in component.functions and "mode" not in component.functions
        for component in host.components.values()
    )
    if wants_power_switch and not device.is_dimmer:
        entities.append(SwidgetPowerSwitch(coordinator))

    # Config-field gates below: check the firmware actually carries the
    # field — older builds may omit it. Reading the config dict
    # directly avoids guessing.
    config = device.device_config.config if device.device_config else {}

    # Fan boost and the speed override are both on/off overrides; their
    # durations come from companion select entities via the coordinator.
    # The ``timer`` tag is shared with the host load timer, so gate the
    # fan variant on an airflow function being present.
    if host is not None:
        for component_id, component in host.components.items():
            is_fan = (
                "exhaust" in component.functions or "supply" in component.functions
            )
            if is_fan and "toggle" not in component.functions:
                entities.append(SwidgetFanPowerSwitch(coordinator, component_id))
            if "boost" in component.functions:
                entities.append(SwidgetFanBoostSwitch(coordinator, component_id))
            # The override on/off switch only exists where the host
            # ``toggle`` does: ending an override rides on the toggle
            # off→on cycle. Toggle-less fans (FV05) manage overrides
            # purely through the CFM + duration selects — picking a
            # speed starts one, the timer (or re-picking the default)
            # ends it.
            if (
                is_fan
                and "timer" in component.functions
                and "toggle" in component.functions
            ):
                entities.append(
                    SwidgetFanSpeedOverrideSwitch(coordinator, component_id)
                )
            # Mesh-balancing enable is a persisted config field; gate on
            # the firmware actually carrying it.
            component_config = (
                config.get("host", {}).get("components", {}).get(component_id, {})
            )
            if "balancing" in component_config:
                entities.append(
                    SwidgetFanBalancingSwitch(coordinator, component_id)
                )
            for bound in _FAN_BOUND_SWITCHES:
                if bound.config_key in component_config:
                    entities.append(
                        SwidgetFanBoundSwitch(coordinator, component_id, bound)
                    )
            model_code = str(getattr(component, "model_code", "") or "")
            for flag in _FAN_CONFIG_FLAG_SWITCHES:
                if flag.config_key in component_config and (
                    not flag.elite_plus_only
                    or model_code in ELITE_PLUS_FAN_MODEL_CODES
                ):
                    entities.append(
                        SwidgetFanConfigFlagSwitch(coordinator, component_id, flag)
                    )

    if device.insert_type == InsertType.USB:
        entities.append(SwidgetUsbInsertSwitch(coordinator))

    if device.insert_type == InsertType.VIDEO:
        entities.append(SwidgetRtspSwitch(coordinator))

    if "debugEnabled" in config.get("configServer", {}):
        entities.append(SwidgetAllowHttpSwitch(coordinator))
    if "cloud_connection" in config.get("mqtt", {}):
        entities.append(SwidgetMqttCloudSwitch(coordinator))

    async_add_entities(entities)


class SwidgetPowerSwitch(SwidgetEntity, SwitchEntity):
    """The host on/off control for outlets, switches, and timer switches."""

    # The device page sorts Controls alphabetically by name with no way
    # to specify an order, so Controls-section names carry a numeric
    # prefix computed per device — see controls.py for the canonical
    # order and the rules for adding a control.
    _attr_translation_key = "power"

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the host power switch."""
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_name = numbered_name(device, "power", "Power")
        self._attr_unique_id = f"{device.mac_address}_power"
        self._attr_device_class = (
            SwitchDeviceClass.OUTLET if device.is_outlet else SwitchDeviceClass.SWITCH
        )

    @property
    def is_on(self) -> bool | None:
        """Return whether the device is currently on, or None if unknown.

        SwidgetComponent initializes ``functions`` entries to None as
        placeholders before process_state populates them. If state hasn't
        landed yet (or process_state silently failed), the SDK's is_on
        crashes on ``None["state"]``. Surface that as "unknown" instead.
        """
        try:
            return self.coordinator.device.is_on
        except (TypeError, KeyError, AttributeError):
            return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the device on."""
        await self.coordinator.device.turn_on()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the device off."""
        await self.coordinator.device.turn_off()
        await self.coordinator.async_request_refresh()


class SwidgetFanPowerSwitch(SwidgetEntity, SwitchEntity):
    """Emulated on/off for fans whose firmware exposes no ``toggle``.

    The FV05 protocol has no discrete power command and its firmware
    (unlike the FV15's) doesn't emulate one, so we do the same mapping
    integration-side that the FV15 driver does on-device: off writes
    CFM 0 on every airflow direction, on restores each direction's
    configured default CFM (``defaultEa``/``defaultSa``). On the FV05
    a CFM write also cancels any running boost, so turning off is a
    full "stop the fan" like the FV15 Plus toggle.

    If the firmware ever grows a real ``toggle`` for these models, the
    function-presence gating switches over to SwidgetPowerSwitch
    automatically and this entity simply stops being created.
    """

    _attr_translation_key = "fan_power"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the emulated fan power switch."""
        super().__init__(coordinator)
        self._attr_name = numbered_name(coordinator.device, "power", "Power")
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_fan_power"
        )

    def _airflow_states(self) -> dict[str, dict]:
        """Return the live datapoint per airflow direction present."""
        try:
            functions = (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
                .functions
            )
        except (KeyError, AttributeError):
            return {}
        return {
            role: state
            for role in ("exhaust", "supply")
            if isinstance(state := functions.get(role), dict)
        }

    def _default_cfm(self, role: str, state: dict) -> int:
        """Return the CFM to restore when turning on.

        Prefers the persisted default from device config; falls back to
        the lowest positive allowed step so older firmware without the
        config field still turns on gently rather than not at all.
        """
        device_config = self.coordinator.device.device_config
        config = device_config.config if device_config else {}
        component_config = (
            config.get("host", {}).get("components", {}).get(self._component_id, {})
        )
        try:
            default = int(component_config[FAN_DEFAULT_CFM_CONFIG_KEYS[role]])
            if default > 0:
                return default
        except (KeyError, TypeError, ValueError):
            pass
        allowed = state.get("allowed")
        if isinstance(allowed, list):
            positive = [int(v) for v in allowed if isinstance(v, (int, float)) and v > 0]
            if positive:
                return min(positive)
        rng = state.get("allowed_range")
        if isinstance(rng, dict) and int(rng.get("min") or 0) > 0:
            return int(rng["min"])
        return 0

    @property
    def available(self) -> bool:
        """Available once at least one airflow datapoint has been seen."""
        return super().available and bool(self._airflow_states())

    @property
    def is_on(self) -> bool | None:
        """On while any airflow direction reports a non-zero CFM."""
        states = self._airflow_states()
        if not states:
            return None
        return any(int(state.get("cfm") or 0) > 0 for state in states.values())

    async def _send_cfm(self, role: str, cfm: int) -> None:
        await self.coordinator.device.send_command(
            assembly="host",
            component=self._component_id,
            function=role,
            command={"cfm": cfm},
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Restore each airflow direction to its default CFM."""
        for role, state in self._airflow_states().items():
            cfm = self._default_cfm(role, state)
            if cfm > 0:
                await self._send_cfm(role, cfm)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop all airflow (CFM 0)."""
        for role in self._airflow_states():
            await self._send_cfm(role, 0)
        await self.coordinator.async_request_refresh()


class SwidgetFanBoostSwitch(SwidgetEntity, SwitchEntity):
    """Boost on/off for Pesna fans.

    Turning on starts a boost using the "Boost duration" select's
    setting (a timer, or permanent for "Until turned off"); turning
    off ends the boost. Remaining time and mode are reported by the
    "Boost remaining" / "Boost mode" sensors.
    """

    _attr_translation_key = "fan_boost"
    _attr_icon = "mdi:fan-plus"

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the fan boost switch."""
        super().__init__(coordinator)
        self._attr_name = numbered_name(coordinator.device, "boost", "Boost")
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
        """Available once we've seen a valid boost state from the device."""
        return super().available and self._boost_state() is not None

    @property
    def is_on(self) -> bool | None:
        """Return whether a boost (timed or permanent) is running."""
        boost = self._boost_state()
        if boost is None:
            return None
        return boost.get("mode") in ("timer", "on")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start a boost with the configured duration."""
        await self.coordinator.async_set_fan_boost(self._component_id, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """End the boost."""
        await self.coordinator.async_set_fan_boost(self._component_id, False)


class SwidgetFanSpeedOverrideSwitch(SwidgetEntity, SwitchEntity):
    """Speed override on/off for Pesna fans.

    The override *is* the manually set speed: pick a temporary CFM via
    the fan entity and firmware holds it until changed. This switch
    reflects that state — on while a non-default speed (or its
    countdown) is in effect — and turning it off ends the override,
    reverting to the default CFM. The "Speed override duration" select
    decides whether a revert timer is armed; with "Until changed" no
    timer runs and the speed persists. Remaining time is on the
    "Speed override remaining" sensor.
    """

    _attr_translation_key = "fan_speed_override"
    _attr_icon = "mdi:fan-clock"

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the speed override switch."""
        super().__init__(coordinator)
        self._attr_name = numbered_name(
            coordinator.device, "speed_override", "Speed override"
        )
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_fan_speed_override"
        )

    def _functions(self) -> dict | None:
        try:
            return (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
                .functions
            )
        except (KeyError, AttributeError):
            return None

    @property
    def is_on(self) -> bool | None:
        """Return whether a speed override is in effect.

        True while the override timer counts down, or while the
        datapoint carries a ``manual`` CFM — firmware includes it only
        while a temporary manual override is running, so it's the
        authoritative marker. The live ``cfm`` reading is deliberately
        not compared against ``default``: variable-speed fans report
        measured airflow that jitters around the target (111 vs a 110
        default at idle), which would flap this switch. Boost never
        sets ``manual``, so it doesn't read as a speed override.
        """
        functions = self._functions()
        if functions is None:
            return None
        timer = functions.get("timer")
        if isinstance(timer, dict) and int(timer.get("minutes") or 0) > 0:
            return True
        for tag in ("supply", "exhaust"):
            state = functions.get(tag)
            if isinstance(state, dict) and int(state.get("manual") or 0) > 0:
                return True
        return False

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Arm the revert timer if a duration is selected.

        With "Until changed" selected this sends a harmless timer clear
        — the override itself is created by setting a speed, so there's
        nothing else to start.
        """
        await self.coordinator.async_arm_fan_override_timer(self._component_id)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """End the override: cancel any timer and revert to the default CFM."""
        await self.coordinator.async_end_fan_override(self._component_id)


class SwidgetFanBalancingSwitch(SwidgetEntity, SwitchEntity):
    """Mesh-balancing enable (config field ``balancing``, 0/1).

    A persisted configuration value, so it lives in the device page's
    Configuration section alongside the balancing offset. Firmware also
    drops balancing tombstones on any balancing config write, treating
    it as fresh local intent.
    """

    _attr_name = "Mesh Balancing"
    _attr_translation_key = "fan_balancing"
    _attr_icon = "mdi:scale-balance"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the balancing-enable switch."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_balancing_enable"
        )

    def _config_value(self) -> int | None:
        device_config = self.coordinator.device.device_config
        if device_config is None:
            return None
        try:
            return int(
                device_config.config["host"]["components"][self._component_id][
                    "balancing"
                ]
            )
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        """Available once the config field is known."""
        return super().available and self._config_value() is not None

    @property
    def is_on(self) -> bool | None:
        """Return the configured balancing enable."""
        value = self._config_value()
        return None if value is None else bool(value)

    async def _write(self, enabled: int) -> None:
        await self.coordinator.async_apply_device_config(
            {"host": {"components": {self._component_id: {"balancing": enabled}}}}
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable mesh balancing."""
        await self._write(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable mesh balancing."""
        await self._write(0)


# Enable flags for the FV-15 Plus sensor bounds. The threshold values
# themselves are the companion number entities in number.py — names are
# chosen so each switch sorts directly after its value in the
# Configuration section ("Temperature min bound" / "... bound enabled").
@dataclass(frozen=True)
class _FanBoundSwitch:
    config_key: str  # "tempBounds" | "humidityBounds"
    side: str  # "min" | "max"
    name: str
    uid_suffix: str
    icon: str


_FAN_BOUND_SWITCHES: tuple[_FanBoundSwitch, ...] = (
    _FanBoundSwitch(
        "tempBounds", "min", "Temperature min bound enabled",
        "temp_bound_min_enabled", "mdi:thermometer-chevron-down",
    ),
    _FanBoundSwitch(
        "tempBounds", "max", "Temperature max bound enabled",
        "temp_bound_max_enabled", "mdi:thermometer-chevron-up",
    ),
    _FanBoundSwitch(
        "humidityBounds", "min", "Humidity min bound enabled",
        "humidity_bound_min_enabled", "mdi:water-minus",
    ),
    _FanBoundSwitch(
        "humidityBounds", "max", "Humidity max bound enabled",
        "humidity_bound_max_enabled", "mdi:water-plus",
    ),
)


class SwidgetFanBoundSwitch(SwidgetEntity, SwitchEntity):
    """Enable flag for one side of a fan sensor bound.

    Writes are flag-only: firmware's ``resolveBoundSide`` re-enables
    with the side's last-enabled value on ``{"<side>Enabled": true}``
    and preserves the stored value on disable. An enable with no stored
    value to restore is rejected on-device (the whole pair is dropped),
    in which case the switch simply snaps back off — set the companion
    threshold number first.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        component_id: str,
        bound: _FanBoundSwitch,
    ) -> None:
        """Initialize the bound-enable switch."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._bound = bound
        self._attr_name = bound.name
        self._attr_translation_key = f"fan_{bound.uid_suffix}"
        self._attr_icon = bound.icon
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
    def is_on(self) -> bool | None:
        """Return the side's configured enable flag."""
        bounds = self._bounds_config()
        if bounds is None:
            return None
        try:
            return bool(int(bounds.get(f"{self._bound.side}Enabled") or 0))
        except (TypeError, ValueError):
            return None

    async def _write(self, enabled: bool) -> None:
        await self.coordinator.async_apply_device_config(
            {
                "host": {
                    "components": {
                        self._component_id: {
                            self._bound.config_key: {
                                f"{self._bound.side}Enabled": enabled
                            }
                        }
                    }
                }
            }
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the bound with its last stored threshold."""
        await self._write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the bound, preserving the stored threshold."""
        await self._write(False)


# Flat 0/1 config flags on the ERV (FV20 Setting2 register, written to
# the Panasonic unit over the Pesna serial link). The supply limits
# restrict supply airflow when the outdoor conditions cross their
# thresholds; the thresholds themselves are companion number entities
# in number.py, named so each pair sorts together in the Configuration
# section ("Supply limit low temp" / "... low temp threshold").
@dataclass(frozen=True)
class _FanConfigFlag:
    config_key: str
    name: str
    uid_suffix: str
    icon: str
    # Humidity-dependent features only work on the Elite Plus units —
    # the config bits exist on every FV20-class ERV, but the Elite
    # answers every humidity query with the 0xFF "no reading" code,
    # so the entity would be a no-op there.
    elite_plus_only: bool = False


_FAN_CONFIG_FLAG_SWITCHES: tuple[_FanConfigFlag, ...] = (
    _FanConfigFlag(
        "supplyLimitLowTemp", "Supply limit low temp",
        "supply_limit_low_temp", "mdi:snowflake-thermometer",
    ),
    _FanConfigFlag(
        "supplyLimitHighHum", "Supply limit high humidity",
        "supply_limit_high_hum", "mdi:water-percent-alert",
        elite_plus_only=True,
    ),
    # Whether humidity control compares against the outdoor humidity
    # sensor (another Setting2 bit, alongside the supply limits).
    # Named to sort directly under the "Humidity control" select in
    # the Configuration section; the uid suffix keeps the original
    # name so the registry entry carries over.
    _FanConfigFlag(
        "outsideHumidityCSensing", "Humidity sensing outdoors",
        "outside_humidity_sensing", "mdi:cloud-percent",
        elite_plus_only=True,
    ),
)


class SwidgetFanConfigFlagSwitch(SwidgetEntity, SwitchEntity):
    """Enable flag for a flat 0/1 fan config field.

    Same shape as the mesh-balancing switch: the field is a persisted
    configuration value read from and written to
    ``device_config.host.components.<id>.<key>``.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        component_id: str,
        flag: _FanConfigFlag,
    ) -> None:
        """Initialize the config-flag switch."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._flag = flag
        self._attr_name = flag.name
        self._attr_translation_key = f"fan_{flag.uid_suffix}"
        self._attr_icon = flag.icon
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}"
            f"_host_{component_id}_{flag.uid_suffix}"
        )

    def _config_value(self) -> int | None:
        device_config = self.coordinator.device.device_config
        if device_config is None:
            return None
        try:
            return int(
                device_config.config["host"]["components"][self._component_id][
                    self._flag.config_key
                ]
            )
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        """Available once the config field is known."""
        return super().available and self._config_value() is not None

    @property
    def is_on(self) -> bool | None:
        """Return the configured enable flag."""
        value = self._config_value()
        return None if value is None else bool(value)

    async def _write(self, enabled: int) -> None:
        await self.coordinator.async_apply_device_config(
            {
                "host": {
                    "components": {
                        self._component_id: {self._flag.config_key: enabled}
                    }
                }
            }
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the config flag."""
        await self._write(1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the config flag."""
        await self._write(0)


class SwidgetUsbInsertSwitch(SwidgetEntity, SwitchEntity):
    """On/off control for the USB insert."""

    _attr_translation_key = "usb"
    _attr_device_class = SwitchDeviceClass.OUTLET

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the USB insert switch."""
        super().__init__(coordinator)
        self._attr_name = numbered_name(coordinator.device, "usb", "USB")
        self._attr_unique_id = f"{coordinator.device.mac_address}_usb"

    @property
    def is_on(self) -> bool | None:
        """Return whether the USB insert is currently on, or None if unknown."""
        try:
            return self.coordinator.device.usb_is_on
        except (TypeError, KeyError, AttributeError):
            return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the USB insert on."""
        await self.coordinator.device.turn_on_usb_insert()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the USB insert off."""
        await self.coordinator.device.turn_off_usb_insert()
        await self.coordinator.async_request_refresh()


class SwidgetRtspSwitch(SwidgetEntity, SwitchEntity):
    """Toggle the video insert's RTSP server."""

    _attr_translation_key = "rtsp"

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the RTSP switch."""
        super().__init__(coordinator)
        self._attr_name = numbered_name(coordinator.device, "rtsp", "RTSP")
        self._attr_unique_id = f"{coordinator.device.mac_address}_rtsp"

    @property
    def is_on(self) -> bool | None:
        """Return whether the RTSP server is enabled, or None if unknown."""
        return self.coordinator.device.rtsp_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the RTSP server."""
        await self.coordinator.device.set_rtsp_enabled(True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the RTSP server."""
        await self.coordinator.device.set_rtsp_enabled(False)
        await self.coordinator.async_request_refresh()


class SwidgetAllowHttpSwitch(SwidgetEntity, SwitchEntity):
    """One-way toggle for ``configServer.debugEnabled``.

    Once HTTP traffic is allowed on the device's config server, the
    firmware doesn't permit disabling it from the network — that has to
    happen on the device itself. We model this by accepting turn_on and
    rejecting turn_off with a HomeAssistantError so the user gets
    explicit feedback rather than a silently-ignored click.
    """

    _attr_translation_key = "allow_http"

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the allow-HTTP switch."""
        super().__init__(coordinator)
        self._attr_name = numbered_name(
            coordinator.device, "http", "HTTP traffic"
        )
        self._attr_unique_id = f"{coordinator.device.mac_address}_allow_http"

    @property
    def is_on(self) -> bool | None:
        """Return current ``configServer.debugEnabled``, or None if unknown."""
        device_config = self.coordinator.device.device_config
        if device_config is None:
            return None
        try:
            return bool(device_config.config["configServer"]["debugEnabled"])
        except (KeyError, TypeError):
            return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Allow HTTP traffic on the config server."""
        await self.coordinator.device.set_device_config(
            {"configServer": {"debugEnabled": True}}
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Reject — the device doesn't support disabling this from the network."""
        raise HomeAssistantError(
            "Allow HTTP Traffic can't be disabled remotely; revert it on the device."
        )


class SwidgetMqttCloudSwitch(SwidgetEntity, SwitchEntity):
    """Toggle for ``mqtt.cloud_connection``.

    Enabling can be NACKed by the firmware — e.g. when the device is
    in local-provision mode the response body is::

        {"mqtt": {"cloud_connection": false, "error": "LocalProvisionMode"}}

    The HTTP status is still 200, so we have to look at the body. We
    POST directly here instead of going through ``set_device_config``
    so the response is reachable; on a NACK we surface the firmware's
    error string via HomeAssistantError so the user sees the actual
    reason instead of a silent no-op.
    """

    _attr_translation_key = "mqtt_cloud"

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the MQTT cloud-connection switch."""
        super().__init__(coordinator)
        self._attr_name = numbered_name(
            coordinator.device, "mqtt", "MQTT cloud connection"
        )
        self._attr_unique_id = f"{coordinator.device.mac_address}_mqtt_cloud"

    @property
    def is_on(self) -> bool | None:
        """Return current ``mqtt.cloud_connection``, or None if unknown."""
        device_config = self.coordinator.device.device_config
        if device_config is None:
            return None
        try:
            return bool(device_config.config["mqtt"]["cloud_connection"])
        except (KeyError, TypeError):
            return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the MQTT cloud connection, surfacing firmware NACKs."""
        device = self.coordinator.device
        response = await device.make_http_request(
            "POST",
            "device_config",
            json_payload={"mqtt": {"cloud_connection": True}},
        )
        mqtt = response.get("mqtt") if isinstance(response, dict) else None
        if not isinstance(mqtt, dict) or mqtt.get("cloud_connection") is not True:
            # cloud_connection didn't flip true → NACK. Prefer the
            # device's error string when present.
            error_msg = (
                mqtt.get("error")
                if isinstance(mqtt, dict) and mqtt.get("error")
                else "device rejected the change"
            )
            raise HomeAssistantError(
                f"Could not enable MQTT cloud connection: {error_msg}"
            )
        # On success, refresh the full cache via HTTP. The POST response
        # is partial-shape (only ``mqtt``); processing it wholesale would
        # clobber every other top-level config key.
        config = await device.make_http_request("GET", "device_config")
        await device.process_device_config(config)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the MQTT cloud connection."""
        await self.coordinator.device.set_device_config(
            {"mqtt": {"cloud_connection": False}}
        )
        await self.coordinator.async_request_refresh()
