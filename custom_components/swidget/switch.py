"""Switch platform for Swidget devices."""

from __future__ import annotations

from typing import Any

from swidget import InsertType

from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
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
    """Set up Swidget switches from a config entry."""
    coordinator: SwidgetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    device = coordinator.device

    entities: list[SwitchEntity] = []

    # Dimmers expose their on/off through the light platform instead.
    # Fans don't have a host ``toggle`` at all — their on/off is the
    # exhaust/supply CFM going to 0, surfaced via the fan platform.
    # Gate on the ``toggle`` function being declared rather than on
    # device family so we materialize the power switch exactly when
    # the firmware will accept the ``toggle`` request.
    host = device.assemblies.get("host")
    has_toggle = host is not None and any(
        "toggle" in component.functions for component in host.components.values()
    )
    if has_toggle and not device.is_dimmer:
        entities.append(SwidgetPowerSwitch(coordinator))

    if device.insert_type == InsertType.USB:
        entities.append(SwidgetUsbInsertSwitch(coordinator))

    if device.insert_type == InsertType.VIDEO:
        entities.append(SwidgetRtspSwitch(coordinator))

    async_add_entities(entities)


class SwidgetPowerSwitch(SwidgetEntity, SwitchEntity):
    """The host on/off control for outlets, switches, and timer switches."""

    _attr_translation_key = "power"
    _attr_name = "Power"

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the host power switch."""
        super().__init__(coordinator)
        device = coordinator.device
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


class SwidgetUsbInsertSwitch(SwidgetEntity, SwitchEntity):
    """On/off control for the USB insert."""

    _attr_translation_key = "usb"
    _attr_name = "USB"
    _attr_device_class = SwitchDeviceClass.OUTLET

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the USB insert switch."""
        super().__init__(coordinator)
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
    _attr_name = "RTSP"

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the RTSP switch."""
        super().__init__(coordinator)
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
