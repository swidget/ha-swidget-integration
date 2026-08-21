"""Light platform — Swidget dimmer hosts."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .controls import numbered_name
from .coordinator import SwidgetDataUpdateCoordinator
from .entity import SwidgetEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Swidget dimmer light entities from a config entry."""
    coordinator: SwidgetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    device = coordinator.device

    entities: list[LightEntity] = []
    host = device.assemblies.get("host")
    if host is not None:
        # Detect dimmer by function presence rather than device_type so
        # any future host that exposes "level" also gets a light entity.
        for component_id, component in host.components.items():
            if "level" in component.functions:
                entities.append(SwidgetDimmerLight(coordinator, component_id))
            # The Pesna fans expose an integrated bath light via the
            # host-level ``light`` function — purely on/off, no dimming.
            if "light" in component.functions:
                entities.append(SwidgetFanLight(coordinator, component_id))

    # Insert-side RGB lights — currently the Advanced Guide Light, but
    # any insert component that declares ``cled`` (addressable LED) gets
    # one. Gate on function presence rather than insert type so future
    # cled-bearing inserts pick this up automatically.
    insert = device.assemblies.get("insert")
    if insert is not None:
        for component_id, component in insert.components.items():
            if "cled" in component.functions:
                entities.append(SwidgetGuideLight(coordinator, component_id))

    async_add_entities(entities)


class SwidgetDimmerLight(SwidgetEntity, LightEntity):
    """A Swidget dimmer surfaced as an HA light with brightness control.

    HA brightness is 0-255 while the device's ``level.now`` is 0-100,
    so the two are scaled at the boundary. Sending a non-zero level
    is enough to bring the load on; turning on without a brightness
    falls back to the device's stored on-default via ``toggle``.
    """

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the dimmer light."""
        super().__init__(coordinator)
        self._attr_name = numbered_name(coordinator.device, "main_light", "Light")
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_dimmer_{component_id}"
        )

    def _level_state(self) -> dict | None:
        """Return the current ``level`` datapoint, or None if not present."""
        try:
            value = (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
                .functions.get("level")
            )
        except (KeyError, AttributeError):
            return None
        return value if isinstance(value, dict) else None

    @property
    def is_on(self) -> bool | None:
        """Return whether the load is on, or None if state is unknown."""
        try:
            return self.coordinator.device.is_on
        except (TypeError, KeyError, AttributeError):
            return None

    @property
    def brightness(self) -> int | None:
        """Return brightness scaled to HA's 0-255 range, or None if unknown."""
        level = self._level_state()
        if level is None:
            return None
        try:
            now_pct = int(level.get("now") or 0)
        except (TypeError, ValueError):
            return None
        return round(now_pct * 255 / 100)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the dimmer, optionally to a specific brightness."""
        device = self.coordinator.device
        if ATTR_BRIGHTNESS in kwargs:
            # 1..255 -> 1..100; preserve "very dim" requests by rounding
            # up so brightness=1 doesn't become 0 (which would be off).
            ha_brightness = int(kwargs[ATTR_BRIGHTNESS])
            pct = max(1, round(ha_brightness * 100 / 255))
            await device.send_command(
                assembly="host",
                component=self._component_id,
                function="level",
                command={"now": pct},
            )
        else:
            await device.turn_on()
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the dimmer."""
        await self.coordinator.device.turn_off()
        await self.coordinator.async_request_refresh()


class SwidgetFanLight(SwidgetEntity, LightEntity):
    """The integrated light on a Pesna bath-fan host (on/off only)."""

    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_translation_key = "fan_light"

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the fan light entity."""
        super().__init__(coordinator)
        self._attr_name = numbered_name(coordinator.device, "fan_light", "Light")
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_fan_light"
        )

    def _light_state(self) -> dict | None:
        try:
            value = (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
                .functions.get("light")
            )
        except (KeyError, AttributeError):
            return None
        return value if isinstance(value, dict) else None

    @property
    def available(self) -> bool:
        return self._light_state() is not None

    @property
    def is_on(self) -> bool | None:
        state = self._light_state()
        if state is None:
            return None
        value = state.get("on")
        return value if isinstance(value, bool) else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.device.send_command(
            assembly="host",
            component=self._component_id,
            function="light",
            command={"on": True},
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.device.send_command(
            assembly="host",
            component=self._component_id,
            function="light",
            command={"on": False},
        )
        await self.coordinator.async_request_refresh()


class SwidgetGuideLight(SwidgetEntity, LightEntity):
    """Insert-side RGB light driven by the ``cled`` function.

    The firmware has no separate on/off — the LED is "off" when all
    three channels are zero. We cache the last non-zero color so that
    HA's turn_on (with no color argument, e.g. after a turn_off) can
    restore the previous look instead of defaulting to white forever.
    """

    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the RGB guide light."""
        super().__init__(coordinator)
        self._attr_name = numbered_name(
            coordinator.device, "guide_light", "Guide light"
        )
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_insert_{component_id}_cled"
        )
        # Default restore color is full white. Updated whenever we see a
        # non-zero state from the device, so toggling off then on returns
        # to the user's most recent picked color rather than (255,255,255).
        self._last_color: tuple[int, int, int] = (255, 255, 255)

    def _cled_state(self) -> dict | None:
        try:
            value = (
                self.coordinator.device.assemblies["insert"]
                .components[self._component_id]
                .functions.get("cled")
            )
        except (KeyError, AttributeError):
            return None
        return value if isinstance(value, dict) else None

    def _current_rgb(self) -> tuple[int, int, int] | None:
        state = self._cled_state()
        if state is None:
            return None
        try:
            r = int(state.get("r") or 0)
            g = int(state.get("g") or 0)
            b = int(state.get("b") or 0)
        except (TypeError, ValueError):
            return None
        return (r, g, b)

    @property
    def available(self) -> bool:
        state = self._cled_state()
        if state is None:
            return False
        # Per SDK: error != 0 is MissingHardware.
        return int(state.get("error") or 0) == 0

    @property
    def is_on(self) -> bool | None:
        rgb = self._current_rgb()
        if rgb is None:
            return None
        on = any(c > 0 for c in rgb)
        if on:
            self._last_color = rgb
        return on

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        return self._current_rgb()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on, optionally to a requested RGB color."""
        rgb_arg = kwargs.get(ATTR_RGB_COLOR)
        if rgb_arg is not None:
            rgb = (int(rgb_arg[0]), int(rgb_arg[1]), int(rgb_arg[2]))
        else:
            current = self._current_rgb()
            if current is not None and any(c > 0 for c in current):
                rgb = current
            else:
                rgb = self._last_color
        if any(c > 0 for c in rgb):
            self._last_color = rgb
        await self.coordinator.device.send_command(
            assembly="insert",
            component=self._component_id,
            function="cled",
            command={"state": {"r": rgb[0], "g": rgb[1], "b": rgb[2]}},
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off by zeroing all three channels."""
        current = self._current_rgb()
        if current is not None and any(c > 0 for c in current):
            self._last_color = current
        await self.coordinator.device.send_command(
            assembly="insert",
            component=self._component_id,
            function="cled",
            command={"state": {"r": 0, "g": 0, "b": 0}},
        )
        await self.coordinator.async_request_refresh()
