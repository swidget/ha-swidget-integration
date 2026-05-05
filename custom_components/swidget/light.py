"""Light platform — Swidget dimmer hosts."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
)
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

    async_add_entities(entities)


class SwidgetDimmerLight(SwidgetEntity, LightEntity):
    """A Swidget dimmer surfaced as an HA light with brightness control.

    HA brightness is 0-255 while the device's ``level.now`` is 0-100,
    so the two are scaled at the boundary. Sending a non-zero level
    is enough to bring the load on; turning on without a brightness
    falls back to the device's stored on-default via ``toggle``.
    """

    _attr_name = "Light"
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the dimmer light."""
        super().__init__(coordinator)
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
