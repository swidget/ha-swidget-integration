"""Select platform — Pesna fan operating mode and override durations.

Only the IB-series fans (IB150/IB160, internally FV20-class) implement
``setFanMode`` in firmware (see ``pesna_comms.cpp::setFanMode``). Other
Pesna variants don't expose the ``mode`` function in their summary, so
gating on function presence handles model-detection without us having
to maintain a per-variant allowlist here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    ELITE_PLUS_FAN_MODEL_CODES,
    FAN_DEFAULT_CFM_CONFIG_KEYS,
)
from .controls import numbered_name
from .coordinator import (
    DEFAULT_FAN_BOOST_MINUTES,
    DEFAULT_FAN_OVERRIDE_MINUTES,
    SwidgetDataUpdateCoordinator,
)
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

# Recirculation only exists on the Elite Plus ERVs — the other units
# have no recirculation air path and silently ignore the mode request
# (the Operation register keeps answering the current mode).

# Synthetic mode: the ERV is in standby while the host toggle reports
# off. Firmware never reports this via ``mode`` (the last real mode
# stays in the datapoint) and doesn't accept it as a mode request —
# entering standby is the host toggle-off command instead.
_STANDBY_MODE = "standby"

# Strings the IB-series ``speed`` datapoint reports (and firmware
# accepts — ``setFanSpeed`` maps anything that isn't "high" to low).
_IB_SERIES_SPEEDS: tuple[str, ...] = ("low", "high")

# Synthetic speed, same story as ``standby`` above: while the unit is
# off the ``speed`` datapoint keeps reporting the last real speed, so
# the select shows "off" instead, and picking it sends toggle-off.
_SPEED_OFF = "off"


# How long a fresh selection outranks the live datapoints. Mid-ramp
# the datapoints flap (observed on an IB150: a mode change from
# standby read toggle on -> off -> on over ~22s, with the CFMs
# catching up last), so a longer window is more flap-proof — but it
# also delays the revert when a command didn't stick, and 30s was
# judged the better trade-off.
_SETTLE_SECONDS = 30.0


class _SettlingSelectMixin:
    """Hold steady through the ERV's ramp window.

    ``_note_selection`` records the user's choice and arms the shared
    per-component settle deadline on the coordinator. While the
    deadline is armed, ``_settled_option`` ignores the live derived
    value: the entity the user touched shows their choice, and any
    sibling select on the same component freezes on its last stable
    value — one command makes *both* ERV dropdowns (mode and speed)
    ramp-blind, since the flapping toggle would bounce either. Once
    the window lapses the device state is trusted again, so a command
    that didn't stick corrects itself within a minute.
    """

    _pending_option: str | None = None
    _last_stable: str | None = None

    def _note_selection(self, option: str) -> None:
        self._pending_option = option
        self.coordinator.fan_settle_until[self._component_id] = (
            time.monotonic() + _SETTLE_SECONDS
        )

    def _settled_option(self, derived: str | None) -> str | None:
        deadline = self.coordinator.fan_settle_until.get(self._component_id, 0.0)
        if time.monotonic() < deadline:
            if self._pending_option is not None:
                return self._pending_option
            if self._last_stable is not None:
                return self._last_stable
            return derived
        self._pending_option = None
        self._last_stable = derived
        return derived


def _fan_in_standby(functions: dict) -> bool:
    """Return True when the ERV is off (host toggle reports off).

    The toggle datapoint is the authority: it flips within ~200ms of
    a toggle command, while the airflow CFMs stay at 0 for ~30s of
    spin-up (observed on an IB150) — judging by CFMs leaves the select
    claiming "off"/"standby" long after the unit has started. Before
    the first state poll the toggle value is None; fall back to the
    CFM readings so a half-populated snapshot still classifies.
    """
    toggle = functions.get("toggle")
    if isinstance(toggle, dict) and isinstance(toggle.get("state"), str):
        return toggle["state"] == "off"
    cfms: list[int] = []
    for role in ("exhaust", "supply"):
        state = functions.get(role)
        if isinstance(state, dict):
            try:
                cfms.append(int(state.get("cfm") or 0))
            except (TypeError, ValueError):
                return False
    return bool(cfms) and all(cfm == 0 for cfm in cfms)


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
            # Speed is gated on ``mode`` too: firmware enables both on
            # exactly the IB-series, but only ``mode`` is declared in
            # the summary functions list — ``speed`` just leaks into
            # the functions dict from state after the first poll, so
            # it isn't a schema-stable key to wire entities off.
            if "mode" in component.functions:
                entities.append(SwidgetFanModeSelect(coordinator, component_id))
                entities.append(SwidgetFanSpeedSelect(coordinator, component_id))
            if "boost" in component.functions:
                entities.append(
                    SwidgetFanBoostDurationSelect(coordinator, component_id)
                )
            # ``timer`` is shared with the host load timer; the fan
            # variant is gated on an airflow function being present.
            if "timer" in component.functions and (
                "exhaust" in component.functions or "supply" in component.functions
            ):
                entities.append(
                    SwidgetFanOverrideDurationSelect(coordinator, component_id)
                )
            # One CFM picker per airflow direction, but only on fans
            # with the ``timer`` (speed override) feature. The ERVs
            # (IB-series) expose exhaust+supply datapoints yet aren't
            # meant to be driven per-direction — airflow is set through
            # the mode select, with boost as the only override — and
            # they carry no ``timer``, so this gate excludes them.
            # Only duplex fans need the role spelled out in the name.
            roles = [r for r in ("supply", "exhaust") if r in component.functions]
            if "timer" in component.functions:
                for role in roles:
                    entities.append(
                        SwidgetFanOverrideCfmSelect(
                            coordinator,
                            component_id,
                            role,
                            include_role_in_name=len(roles) > 1,
                        )
                    )
            # Default CFM is a persisted config field, one per airflow
            # direction the model drives (supply -> defaultSa, exhaust
            # -> defaultEa); gate on the firmware actually carrying the
            # field (older builds may not).
            config = device.device_config.config if device.device_config else {}
            component_config = (
                config.get("host", {}).get("components", {}).get(component_id, {})
            )
            default_roles = [
                role
                for role in roles
                if FAN_DEFAULT_CFM_CONFIG_KEYS[role] in component_config
            ]
            for role in default_roles:
                entities.append(
                    SwidgetFanDefaultCfmSelect(
                        coordinator,
                        component_id,
                        role,
                        include_role_in_name=len(default_roles) > 1,
                    )
                )
            # Runtime (ERV): persisted minutes value, plus "Auto" when
            # the firmware also carries the autoRuntime flag.
            if "runtime" in component_config:
                entities.append(
                    SwidgetFanRuntimeSelect(
                        coordinator,
                        component_id,
                        include_auto="autoRuntime" in component_config,
                    )
                )
            # Intermittent idle behavior (ERV): persisted config bit.
            if "intermittentMode" in component_config:
                entities.append(
                    SwidgetFanIntermittentModeSelect(coordinator, component_id)
                )
            # Per-tier airflow (ERV): one dropdown per Low/High/Boost x
            # supply/exhaust config field the firmware carries.
            for tier in _FAN_TIER_CFMS:
                if tier.config_key in component_config:
                    entities.append(
                        SwidgetFanTierCfmSelect(coordinator, component_id, tier)
                    )
            # Humidity-triggered ventilation (ERV): enable bit + target
            # percentage, combined into one dropdown. Elite Plus only —
            # the config fields exist on every FV20-class unit, but
            # only the Plus units have the humidity sensors the
            # feature needs (the Elite answers every humidity query
            # with the 0xFF "no reading" code).
            if (
                "humidityControl" in component_config
                and "humidityControlSetting" in component_config
                and str(getattr(component, "model_code", "") or "")
                in ELITE_PLUS_FAN_MODEL_CODES
            ):
                entities.append(
                    SwidgetFanHumidityControlSelect(coordinator, component_id)
                )

    async_add_entities(entities)


# Duration presets shared by the boost and fan-timer selects. Firmware
# carries both timers as a uint8 of minutes, so 255 (4h15m) is the hard
# ceiling for the timed options.
_TIMED_DURATION_OPTIONS: dict[str, int] = {
    "5 minutes": 5,
    "10 minutes": 10,
    "15 minutes": 15,
    "20 minutes": 20,
    "30 minutes": 30,
    "45 minutes": 45,
    "1 hour": 60,
    "2 hours": 120,
    "3 hours": 180,
    "4 hours": 240,
}
# Boost additionally supports running with no timer: "Until turned off"
# maps to 0 minutes, which the coordinator sends as a permanent boost
# (``{"mode": "on"}``).
_BOOST_DURATION_OPTIONS: dict[str, int] = {
    **_TIMED_DURATION_OPTIONS,
    "Until turned off": 0,
}
_BOOST_MINUTES_TO_OPTION = {v: k for k, v in _BOOST_DURATION_OPTIONS.items()}

# The speed override's 0 reads differently: "Until changed" means no
# revert timer at all — the manually set CFM simply persists, which is
# firmware's native behavior — so it comes first as the default.
_OVERRIDE_DURATION_OPTIONS: dict[str, int] = {
    "Until changed": 0,
    **_TIMED_DURATION_OPTIONS,
}
_OVERRIDE_MINUTES_TO_OPTION = {v: k for k, v in _OVERRIDE_DURATION_OPTIONS.items()}


class SwidgetFanModeSelect(_SettlingSelectMixin, SwidgetEntity, SelectEntity):
    """Select the fan operating mode (IB-series only).

    Reads the bare-string ``mode`` value from the host component's
    datapoint and shows it as the current option. Picking an option
    sends ``{"mode": "<value>"}`` per the SDK request schema.

    "standby" is synthetic: shown while the host toggle reports off
    (which masks the stale ``mode`` string firmware keeps reporting),
    entered by sending the host toggle-off, and left by picking any
    real mode.
    """

    _attr_translation_key = "fan_mode"

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the fan-mode select."""
        super().__init__(coordinator)
        self._attr_name = numbered_name(coordinator.device, "fan_mode", "Mode")
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_fan_mode"
        )
        # Offer recirculation only where the attached unit supports it
        # (see ELITE_PLUS_FAN_MODEL_CODES).
        try:
            model_code = str(
                coordinator.device.assemblies["host"]
                .components[component_id]
                .model_code
            )
        except (KeyError, AttributeError):
            model_code = ""
        self._attr_options = [
            *(
                mode
                for mode in _IB_SERIES_MODES
                if mode != "recirculation"
                or model_code in ELITE_PLUS_FAN_MODEL_CODES
            ),
            _STANDBY_MODE,
        ]

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
    def current_option(self) -> str | None:
        """Return the reported mode, or "standby" when the unit is off.

        A selection made within the settle window outranks the live
        value — the datapoints flap while the unit ramps.
        """
        functions = self._functions()
        if functions is None:
            return self._settled_option(None)
        if _fan_in_standby(functions):
            return self._settled_option(_STANDBY_MODE)
        value = functions.get("mode")
        if not isinstance(value, str):
            return self._settled_option(None)
        # Forward-compatibility: if firmware reports a mode we haven't
        # listed (e.g. a future variant), surface it as None rather than
        # claiming a wrong option, so the user sees "(unknown)" instead.
        return self._settled_option(
            value if value in self._attr_options else None
        )

    async def async_select_option(self, option: str) -> None:
        """Send the chosen mode, or the toggle-off for "standby".

        ``setFanMode`` only writes the mode register — power is a
        separate operation on this protocol (``FV20::on``), so a mode
        picked while the unit is in standby must be preceded by a
        toggle-on or the unit stays off with the new mode latched.
        The toggle-on is sent whenever we're not clearly running: if
        state was stale and the unit is already on, firmware drops a
        redundant toggle-on (component.cpp::1663), so it's harmless.
        """
        self._note_selection(option)
        if option == _STANDBY_MODE:
            await self.coordinator.device.send_command(
                assembly="host",
                component=self._component_id,
                function="toggle",
                command={"state": "off"},
            )
        else:
            functions = self._functions()
            if functions is None or _fan_in_standby(functions):
                await self.coordinator.device.send_command(
                    assembly="host",
                    component=self._component_id,
                    function="toggle",
                    command={"state": "on"},
                )
            # The fan ``mode`` request value is a bare string at the
            # component level ({"mode": "supply"}), not a nested object
            # — see component.cpp's keyIs<const char*>("mode") check.
            await self.coordinator.device.send_command(
                assembly="host",
                component=self._component_id,
                function="mode",
                command=option,
            )
        await self.coordinator.async_request_refresh()


class SwidgetFanSpeedSelect(_SettlingSelectMixin, SwidgetEntity, SelectEntity):
    """Select the fan speed (IB-series only).

    Reads the bare-string ``speed`` value from the host component's
    datapoint. Picking a speed sends ``{"speed": "<value>"}`` per the
    SDK request schema (firmware treats anything but "high" as low).

    "off" is synthetic, exactly like the mode select's "standby":
    shown while the host toggle reports off (which masks the stale
    ``speed`` string firmware keeps reporting), entered by sending
    the host toggle-off, and left by picking a real speed.
    """

    _attr_translation_key = "fan_speed"
    _attr_icon = "mdi:fan"
    _attr_options = [_SPEED_OFF, *_IB_SERIES_SPEEDS]

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the fan-speed select."""
        super().__init__(coordinator)
        self._attr_name = numbered_name(coordinator.device, "fan_speed", "Fan speed")
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_fan_speed"
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
    def current_option(self) -> str | None:
        """Return the reported speed, or "off" when the unit is off.

        A selection made within the settle window outranks the live
        value — the datapoints flap while the unit ramps.
        """
        functions = self._functions()
        if functions is None:
            return self._settled_option(None)
        if _fan_in_standby(functions):
            return self._settled_option(_SPEED_OFF)
        value = functions.get("speed")
        if not isinstance(value, str):
            return self._settled_option(None)
        # Forward-compatibility: an unlisted speed string surfaces as
        # None ("(unknown)") rather than a wrong option.
        return self._settled_option(
            value if value in self._attr_options else None
        )

    async def async_select_option(self, option: str) -> None:
        """Send the chosen speed, or the toggle-off for "off".

        Like ``setFanMode``, the speed request only writes the speed
        register — a speed picked while the unit is off must be
        preceded by a toggle-on or the unit stays off with the new
        speed latched. Firmware drops a redundant toggle-on if the
        unit turns out to be running already, so stale state is safe.
        """
        self._note_selection(option)
        if option == _SPEED_OFF:
            await self.coordinator.device.send_command(
                assembly="host",
                component=self._component_id,
                function="toggle",
                command={"state": "off"},
            )
        else:
            functions = self._functions()
            if functions is None or _fan_in_standby(functions):
                await self.coordinator.device.send_command(
                    assembly="host",
                    component=self._component_id,
                    function="toggle",
                    command={"state": "on"},
                )
            # Like ``mode``, the request value is a bare string at the
            # component level ({"speed": "high"}), not a nested object.
            await self.coordinator.device.send_command(
                assembly="host",
                component=self._component_id,
                function="speed",
                command=option,
            )
        await self.coordinator.async_request_refresh()


class SwidgetFanBoostDurationSelect(SwidgetEntity, SelectEntity, RestoreEntity):
    """Boost duration setting for Pesna fans.

    This is integration-side state, not a device readback: firmware has
    no stored boost duration (every boost request carries its own
    ``minutes``), so the chosen duration lives here — restored across
    restarts — and the Boost switch sends it when toggled on. "Until
    turned off" starts a permanent boost (``{"mode": "on"}``) instead
    of a timer.

    Changing the selection while a boost is active re-sends the boost
    immediately with the new duration; live countdown is on the
    "Boost remaining" sensor.
    """

    # Numeric name prefixes order the Controls list — see controls.py.
    _attr_translation_key = "fan_boost_duration"
    _attr_icon = "mdi:timer-cog-outline"
    _attr_options = list(_BOOST_DURATION_OPTIONS)

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the boost-duration select."""
        super().__init__(coordinator)
        self._attr_name = numbered_name(
            coordinator.device, "boost_duration", "Boost duration"
        )
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_fan_boost_duration"
        )

    async def async_added_to_hass(self) -> None:
        """Seed the shared duration from the last stored selection."""
        await super().async_added_to_hass()
        if self._component_id in self.coordinator.fan_boost_minutes:
            return
        minutes = DEFAULT_FAN_BOOST_MINUTES
        if (state := await self.async_get_last_state()) is not None and (
            state.state in _BOOST_DURATION_OPTIONS
        ):
            minutes = _BOOST_DURATION_OPTIONS[state.state]
        self.coordinator.fan_boost_minutes[self._component_id] = minutes

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
    def current_option(self) -> str | None:
        """Return the option matching the stored duration."""
        minutes = self.coordinator.fan_boost_minutes.get(
            self._component_id, DEFAULT_FAN_BOOST_MINUTES
        )
        return _BOOST_MINUTES_TO_OPTION.get(minutes)

    async def async_select_option(self, option: str) -> None:
        """Store the chosen duration; re-send the boost if one is running."""
        self.coordinator.fan_boost_minutes[self._component_id] = (
            _BOOST_DURATION_OPTIONS[option]
        )
        boost = self._boost_state()
        if boost is not None and boost.get("mode") in ("timer", "on"):
            await self.coordinator.async_set_fan_boost(self._component_id, True)
        else:
            self.async_write_ha_state()


class SwidgetFanOverrideDurationSelect(SwidgetEntity, SelectEntity, RestoreEntity):
    """Speed override duration setting for Pesna fans.

    The default, "Until changed", arms no timer: a manually set CFM
    simply persists (firmware's native behavior). Picking a time value
    sends the revert timer to the device right away, so the flow is
    "set a speed, then pick how long it should last". Selecting "Until
    changed" while a timer counts down cancels the countdown but holds
    the speed. The selection is restored across restarts.
    """

    _attr_translation_key = "fan_speed_override_duration"
    _attr_icon = "mdi:timer-cog-outline"
    _attr_options = list(_OVERRIDE_DURATION_OPTIONS)

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the override-duration select."""
        super().__init__(coordinator)
        self._attr_name = numbered_name(
            coordinator.device,
            "speed_override_duration",
            "Speed override duration",
        )
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}"
            f"_host_{self._component_id}_fan_speed_override_duration"
        )

    async def async_added_to_hass(self) -> None:
        """Seed the shared duration from the last stored selection."""
        await super().async_added_to_hass()
        if self._component_id in self.coordinator.fan_override_minutes:
            return
        minutes = DEFAULT_FAN_OVERRIDE_MINUTES
        if (state := await self.async_get_last_state()) is not None and (
            state.state in _OVERRIDE_DURATION_OPTIONS
        ):
            minutes = _OVERRIDE_DURATION_OPTIONS[state.state]
        self.coordinator.fan_override_minutes[self._component_id] = minutes

    def _timer_state(self) -> dict | None:
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
    def current_option(self) -> str | None:
        """Return the option matching the stored duration."""
        minutes = self.coordinator.fan_override_minutes.get(
            self._component_id, DEFAULT_FAN_OVERRIDE_MINUTES
        )
        return _OVERRIDE_MINUTES_TO_OPTION.get(minutes)

    async def async_select_option(self, option: str) -> None:
        """Store the chosen duration and apply it to the device.

        A time value arms the revert timer immediately. "Until changed"
        only needs a device command when a timer is already counting
        down (to cancel it while holding the speed); otherwise it's
        purely a stored preference.
        """
        minutes = _OVERRIDE_DURATION_OPTIONS[option]
        self.coordinator.fan_override_minutes[self._component_id] = minutes
        timer = self._timer_state()
        timer_running = (
            timer is not None and int(timer.get("minutes") or 0) > 0
        )
        if minutes > 0 or timer_running:
            await self.coordinator.async_arm_fan_override_timer(self._component_id)
        else:
            self.async_write_ha_state()


def _cfm_values(state: dict) -> list[int]:
    """Extract the pickable CFM values from an airflow datapoint.

    Firmware reports either a discrete ``allowed`` list or an
    ``allowed_range`` ``{min, max}`` (see ``component.cpp``, the
    ``hasCfmRange()`` branch). A range is offered in 10-CFM increments,
    with both endpoints always included even when they aren't multiples
    of 10. ``allowed`` may also be the string ``"unavailable"``.
    """
    allowed = state.get("allowed")
    if isinstance(allowed, list):
        return sorted(
            {int(v) for v in allowed if isinstance(v, (int, float)) and v > 0}
        )
    rng = state.get("allowed_range")
    if isinstance(rng, dict):
        lo = int(rng.get("min") or 0)
        hi = int(rng.get("max") or 0)
        if 0 < lo <= hi:
            values = list(range(lo + (-lo % 10), hi + 1, 10))
            if not values or values[0] != lo:
                values.insert(0, lo)
            if values[-1] != hi:
                values.append(hi)
            return values
    return []


def _cfm_allows_off(state: dict) -> bool:
    """Return True when the airflow's ``allowed`` list includes 0.

    Only fans whose firmware explicitly declares 0 a valid commanded
    value (the FV05 does) get an "Off" option; range-based fans manage
    on/off through the toggle instead.
    """
    allowed = state.get("allowed")
    return isinstance(allowed, list) and any(
        isinstance(v, (int, float)) and int(v) == 0 for v in allowed
    )


_CFM_OFF_OPTION = "Off"


class SwidgetFanOverrideCfmSelect(SwidgetEntity, SelectEntity):
    """CFM picker for the fan speed override.

    Choosing a value sends ``{"cfm": N}`` on the airflow function —
    the same temporary manual override the fan entity's percentage
    control issues, just with the device's real CFM steps instead of a
    0-100 scale. Options come straight from the live datapoint, so a
    firmware-side change to the allowed set shows up on the next poll.
    When the allowed set itself includes 0, an "Off" option represents
    it — so a stopped fan reads "Off" instead of no selection.
    """

    _attr_icon = "mdi:speedometer"

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        component_id: str,
        role: str,
        include_role_in_name: bool,
    ) -> None:
        """Initialize the CFM select for one airflow direction."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._role = role
        self._attr_name = numbered_name(
            coordinator.device,
            "speed_override_cfm",
            f"Speed override {role} CFM"
            if include_role_in_name
            else "Speed override CFM",
        )
        self._attr_translation_key = f"fan_override_cfm_{role}"
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}"
            f"_host_{component_id}_{role}_override_cfm"
        )

    def _airflow_state(self) -> dict | None:
        try:
            value = (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
                .functions.get(self._role)
            )
        except (KeyError, AttributeError):
            return None
        return value if isinstance(value, dict) else None

    @property
    def options(self) -> list[str]:
        """Return the allowed CFM values as labeled options."""
        state = self._airflow_state()
        if state is None:
            return []
        options = [f"{v} CFM" for v in _cfm_values(state)]
        if options and _cfm_allows_off(state):
            options.insert(0, _CFM_OFF_OPTION)
        return options

    @property
    def available(self) -> bool:
        """Available once the device has reported its allowed values."""
        return super().available and bool(self.options)

    @property
    def current_option(self) -> str | None:
        """Return the option matching the commanded CFM.

        Variable-speed fans report the *measured* airflow in ``cfm``,
        which jitters around the target (89/91 for a commanded 90), so
        it can't be matched against the steps directly. The commanded
        value lives in ``manual`` while an override runs, and in
        ``default`` otherwise; the exact live ``cfm`` is only trusted
        as a fallback for discrete-step fans that echo the set value
        (exhaust datapoints carry neither ``manual`` nor ``default``).
        """
        state = self._airflow_state()
        if state is None:
            return None
        values = _cfm_values(state)
        try:
            manual = int(state.get("manual") or 0)
            cfm = int(state.get("cfm") or 0)
            default = int(state.get("default") or 0)
        except (TypeError, ValueError):
            return None
        if manual > 0:
            return f"{manual} CFM" if manual in values else None
        if cfm <= 0:
            return _CFM_OFF_OPTION if _cfm_allows_off(state) else None
        if cfm in values:
            return f"{cfm} CFM"
        if default in values:
            return f"{default} CFM"
        return None

    async def async_select_option(self, option: str) -> None:
        """Send the chosen CFM (0 for "Off"), re-arming the revert timer.

        Firmware treats a user speed command as fresh intent and
        cancels any running custom timer (``setSupplyManual`` ->
        ``cancelCustomTimer``), so a duration picked *before* the speed
        would silently vanish. Re-sending the stored duration after the
        CFM write makes the two dropdowns order-independent; a stored
        0 ("Until changed") needs no re-arm, nor does "Off".
        """
        cfm = 0 if option == _CFM_OFF_OPTION else int(option.split()[0])
        await self.coordinator.device.send_command(
            assembly="host",
            component=self._component_id,
            function=self._role,
            command={"cfm": cfm},
        )
        if cfm > 0 and self.coordinator.fan_override_minutes.get(
            self._component_id, 0
        ):
            await self.coordinator.async_arm_fan_override_timer(self._component_id)
        await self.coordinator.async_request_refresh()


# Runtime is written to the fan's Operation register as a uint8 of
# minutes (see ``pesna_fan_20.cpp`` setConfig); the panel offers 5-60
# in 5-minute steps. "Auto" is a separate config bit: ``autoRuntime``
# (a Setting2 flag) — while it's 1 the fixed runtime is not in effect,
# so the select shows "Auto" regardless of the stored minutes.
_RUNTIME_AUTO_OPTION = "Auto"
_RUNTIME_OPTIONS: dict[str, int] = {
    f"{m} minutes": m for m in range(5, 61, 5)
}


class SwidgetFanRuntimeSelect(SwidgetEntity, SelectEntity):
    """Persisted fan runtime (config fields ``runtime``/``autoRuntime``).

    One dropdown covers both fields: picking a duration writes
    ``runtime`` (and clears ``autoRuntime`` so the fixed value takes
    effect); picking "Auto" sets ``autoRuntime`` to 1. Display follows
    the same rule — ``autoRuntime == 1`` shows "Auto", otherwise the
    stored minutes.
    """

    _attr_name = "Runtime"
    _attr_translation_key = "fan_runtime"
    _attr_icon = "mdi:timer-sand"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        component_id: str,
        include_auto: bool,
    ) -> None:
        """Initialize the runtime select."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._include_auto = include_auto
        self._attr_options = list(_RUNTIME_OPTIONS) + (
            [_RUNTIME_AUTO_OPTION] if include_auto else []
        )
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_runtime"
        )

    def _component_config(self) -> dict:
        device_config = self.coordinator.device.device_config
        config = device_config.config if device_config else {}
        value = (
            config.get("host", {})
            .get("components", {})
            .get(self._component_id, {})
        )
        return value if isinstance(value, dict) else {}

    @property
    def available(self) -> bool:
        """Available once the runtime config field is known."""
        return super().available and "runtime" in self._component_config()

    @property
    def current_option(self) -> str | None:
        """Return "Auto" when autoRuntime is set, else the stored minutes."""
        component_config = self._component_config()
        try:
            if self._include_auto and int(
                component_config.get("autoRuntime") or 0
            ):
                return _RUNTIME_AUTO_OPTION
            minutes = int(component_config["runtime"])
        except (KeyError, TypeError, ValueError):
            return None
        for label, value in _RUNTIME_OPTIONS.items():
            if value == minutes:
                return label
        return None

    async def async_select_option(self, option: str) -> None:
        """Persist the chosen runtime (or auto mode) to device config."""
        if option == _RUNTIME_AUTO_OPTION:
            payload: dict = {"autoRuntime": 1}
        else:
            payload = {"runtime": _RUNTIME_OPTIONS[option]}
            # Leaving auto mode: the fixed runtime only takes effect
            # while autoRuntime is clear.
            if self._include_auto:
                payload["autoRuntime"] = 0
        await self.coordinator.async_apply_device_config(
            {"host": {"components": {self._component_id: payload}}}
        )


# Idle-phase behavior for intermittent (duty-cycled) operation — an
# FV20 Setting2 bit (1=Recirculation, 0=Standby, per ``pesna_fan.h``).
# A select rather than a switch: an "Intermittent mode" toggle would
# misread as enabling intermittent operation itself, which is the
# ``dutyCycle`` function's job.
_INTERMITTENT_MODE_OPTIONS: dict[str, int] = {
    "Standby": 0,
    "Recirculation": 1,
}


class SwidgetFanIntermittentModeSelect(SwidgetEntity, SelectEntity):
    """Persisted intermittent idle behavior (config ``intermittentMode``).

    Chooses what the ERV does during the idle portion of each hour
    when running intermittently: stop entirely (Standby) or keep
    circulating indoor air without outdoor exchange (Recirculation —
    Elite Plus units only, per the app's feature matrix, though the
    config bit itself is present on all FV20-class ERVs).
    """

    _attr_name = "Intermittent mode"
    _attr_translation_key = "fan_intermittent_mode"
    _attr_icon = "mdi:autorenew"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(_INTERMITTENT_MODE_OPTIONS)

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the intermittent-mode select."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_intermittent_mode"
        )

    def _config_value(self) -> int | None:
        device_config = self.coordinator.device.device_config
        if device_config is None:
            return None
        try:
            return int(
                device_config.config["host"]["components"][self._component_id][
                    "intermittentMode"
                ]
            )
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        """Available once the config field is known."""
        return super().available and self._config_value() is not None

    @property
    def current_option(self) -> str | None:
        """Return the label for the stored config bit."""
        value = self._config_value()
        if value is None:
            return None
        for label, bit in _INTERMITTENT_MODE_OPTIONS.items():
            if bit == value:
                return label
        return None

    async def async_select_option(self, option: str) -> None:
        """Persist the chosen idle behavior to device config."""
        await self.coordinator.async_apply_device_config(
            {
                "host": {
                    "components": {
                        self._component_id: {
                            "intermittentMode": _INTERMITTENT_MODE_OPTIONS[option]
                        }
                    }
                }
            }
        )


# Persisted per-tier airflow (ERV): the Low/High/Boost notch CFM for
# each direction (config ``lowSa``/``lowEa``/``highSa``/``highEa``/
# ``boostSa``/``boostEa``), written to the Panasonic unit as Volume
# notch commands (``pesna_fan_20.cpp`` setConfig ``cfm_cfg_pair``).
# Dropdowns rather than free-typed numbers: the panel works in 10-CFM
# steps, and firmware stores an unclamped uint8, so a fixed option
# list keeps invalid values unenterable. Options run 10..maxCFM from
# the summary (endpoint always included).
@dataclass(frozen=True)
class _FanTierCfm:
    config_key: str
    name: str
    uid_suffix: str
    icon: str


_FAN_TIER_CFMS: tuple[_FanTierCfm, ...] = (
    _FanTierCfm("lowSa", "Low supply CFM", "low_sa_cfm", "mdi:speedometer-slow"),
    _FanTierCfm("lowEa", "Low exhaust CFM", "low_ea_cfm", "mdi:speedometer-slow"),
    _FanTierCfm("highSa", "High supply CFM", "high_sa_cfm", "mdi:speedometer"),
    _FanTierCfm("highEa", "High exhaust CFM", "high_ea_cfm", "mdi:speedometer"),
    _FanTierCfm(
        "boostSa", "Boost supply CFM", "boost_sa_cfm", "mdi:rocket-launch-outline"
    ),
    _FanTierCfm(
        "boostEa", "Boost exhaust CFM", "boost_ea_cfm", "mdi:rocket-launch-outline"
    ),
)

# When the summary doesn't carry maxCFM, fall back to the largest
# airflow any supported Pesna unit can move.
_TIER_CFM_FALLBACK_MAX = 200


class SwidgetFanTierCfmSelect(SwidgetEntity, SelectEntity):
    """Persisted CFM for one speed tier and airflow direction.

    These are the airflows the ERV runs at in its Low/High speeds and
    during Boost — configuration values, not live commands, so they
    live in the device page's Configuration section. Read from and
    written to ``device_config.host.components.<id>.<key>``. A stored
    value that isn't on a 10-CFM step surfaces as unknown rather than
    a wrong option.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        component_id: str,
        tier: _FanTierCfm,
    ) -> None:
        """Initialize the tier-CFM select."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._tier = tier
        self._attr_name = tier.name
        self._attr_translation_key = f"fan_{tier.uid_suffix}"
        self._attr_icon = tier.icon
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}"
            f"_host_{component_id}_{tier.uid_suffix}"
        )

    def _max_cfm(self) -> int:
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
        return max_cfm if max_cfm > 0 else _TIER_CFM_FALLBACK_MAX

    def _values(self) -> list[int]:
        """Return the pickable CFM steps (10s up to and incl. maxCFM)."""
        max_cfm = self._max_cfm()
        values = list(range(10, max_cfm + 1, 10))
        if values[-1] != max_cfm:
            values.append(max_cfm)
        return values

    @property
    def options(self) -> list[str]:
        """Return the CFM steps as labeled options."""
        return [f"{v} CFM" for v in self._values()]

    def _config_value(self) -> int | None:
        device_config = self.coordinator.device.device_config
        if device_config is None:
            return None
        try:
            return int(
                device_config.config["host"]["components"][self._component_id][
                    self._tier.config_key
                ]
            )
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        """Available once the config field is known."""
        return super().available and self._config_value() is not None

    @property
    def current_option(self) -> str | None:
        """Return the stored tier CFM, if it sits on an offered step."""
        value = self._config_value()
        if value is None or value not in self._values():
            return None
        return f"{value} CFM"

    async def async_select_option(self, option: str) -> None:
        """Persist the chosen tier CFM to device config."""
        await self.coordinator.async_apply_device_config(
            {
                "host": {
                    "components": {
                        self._component_id: {
                            self._tier.config_key: int(option.split()[0])
                        }
                    }
                }
            }
        )


# Humidity-triggered ventilation (ERV): the ``humidityControl`` enable
# bit and the ``humidityControlSetting`` indoor target share a single
# dropdown — "Off", or the target percentage (which also enables the
# feature). Targets span the protocol's accept window, 30-80 %
# (``pesna_fan.h``), in the panel's 5 % steps. Selecting "Off" only
# clears the enable bit; the stored target is preserved for re-enable.
_HUMIDITY_CONTROL_OFF = "Off"
_HUMIDITY_CONTROL_TARGETS: tuple[int, ...] = tuple(range(30, 81, 5))


class SwidgetFanHumidityControlSelect(SwidgetEntity, SelectEntity):
    """Humidity-triggered ventilation target (config fields
    ``humidityControl`` + ``humidityControlSetting``).

    Needs the humidity sensors only the Elite Plus units carry — on
    other units the config fields still exist but the feature has no
    readings to act on. The companion "Outside humidity sensing"
    switch adds the outdoor sensor to the comparison.
    """

    _attr_name = "Humidity control"
    _attr_translation_key = "fan_humidity_control"
    _attr_icon = "mdi:water-percent"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = [
        _HUMIDITY_CONTROL_OFF,
        *(f"{target}%" for target in _HUMIDITY_CONTROL_TARGETS),
    ]

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the humidity-control select."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_humidity_control"
        )

    def _component_config(self) -> dict:
        device_config = self.coordinator.device.device_config
        config = device_config.config if device_config else {}
        value = (
            config.get("host", {})
            .get("components", {})
            .get(self._component_id, {})
        )
        return value if isinstance(value, dict) else {}

    @property
    def available(self) -> bool:
        """Available once both config fields are known."""
        component_config = self._component_config()
        return (
            super().available
            and "humidityControl" in component_config
            and "humidityControlSetting" in component_config
        )

    @property
    def current_option(self) -> str | None:
        """Return "Off", or the enabled target percentage."""
        component_config = self._component_config()
        try:
            if not int(component_config["humidityControl"]):
                return _HUMIDITY_CONTROL_OFF
            target = int(component_config["humidityControlSetting"])
        except (KeyError, TypeError, ValueError):
            return None
        # An off-step stored target (or the 0xFF "never set" sentinel)
        # surfaces as unknown rather than a wrong option.
        return f"{target}%" if target in _HUMIDITY_CONTROL_TARGETS else None

    async def async_select_option(self, option: str) -> None:
        """Persist the chosen target (enabling), or clear the enable bit."""
        if option == _HUMIDITY_CONTROL_OFF:
            payload: dict = {"humidityControl": 0}
        else:
            payload = {
                "humidityControl": 1,
                "humidityControlSetting": int(option.rstrip("%")),
            }
        await self.coordinator.async_apply_device_config(
            {"host": {"components": {self._component_id: payload}}}
        )


class SwidgetFanDefaultCfmSelect(SwidgetEntity, SelectEntity):
    """Persisted default CFM (config field ``defaultSa``/``defaultEa``).

    This is the speed the fan returns to when an override, boost, or
    timer ends — a configuration value, not a live command, so it lives
    in the device page's Configuration section. Options come from the
    same allowed list / 10-CFM-stepped range as the Speed override CFM
    picker; the value is read from and written to
    ``device_config.host.components.<id>.defaultSa`` (supply) or
    ``.defaultEa`` (exhaust).
    """

    _attr_icon = "mdi:speedometer-medium"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        component_id: str,
        role: str,
        include_role_in_name: bool,
    ) -> None:
        """Initialize the default-CFM select for one airflow direction."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._role = role
        self._config_key = FAN_DEFAULT_CFM_CONFIG_KEYS[role]
        self._attr_name = (
            f"Default {role} CFM" if include_role_in_name else "Default CFM"
        )
        self._attr_translation_key = f"fan_default_cfm_{role}"
        # "defaultSa" -> "default_sa" — matches the pre-existing
        # supply-only unique_id so FV15 registry entries carry over.
        uid_suffix = f"default_{self._config_key[-2:].lower()}"
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_{uid_suffix}"
        )

    def _airflow_state(self) -> dict | None:
        try:
            value = (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
                .functions.get(self._role)
            )
        except (KeyError, AttributeError):
            return None
        return value if isinstance(value, dict) else None

    def _config_default(self) -> int | None:
        device_config = self.coordinator.device.device_config
        if device_config is None:
            return None
        try:
            value = device_config.config["host"]["components"][self._component_id][
                self._config_key
            ]
        except (KeyError, TypeError):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @property
    def options(self) -> list[str]:
        """Return the allowed CFM values as labeled options."""
        state = self._airflow_state()
        if state is None:
            return []
        return [f"{v} CFM" for v in _cfm_values(state)]

    @property
    def available(self) -> bool:
        """Available once allowed values and the config field are known."""
        return (
            super().available
            and bool(self.options)
            and self._config_default() is not None
        )

    @property
    def current_option(self) -> str | None:
        """Return the configured default, if it sits on an offered step."""
        default = self._config_default()
        state = self._airflow_state()
        if default is None or state is None or default not in _cfm_values(state):
            return None
        return f"{default} CFM"

    async def async_select_option(self, option: str) -> None:
        """Persist the chosen default CFM to device config."""
        await self.coordinator.async_apply_device_config(
            {
                "host": {
                    "components": {
                        self._component_id: {self._config_key: int(option.split()[0])}
                    }
                }
            }
        )
