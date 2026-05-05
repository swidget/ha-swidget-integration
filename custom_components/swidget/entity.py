"""Base entity for Swidget devices."""

from __future__ import annotations

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SwidgetDataUpdateCoordinator


class SwidgetEntity(CoordinatorEntity[SwidgetDataUpdateCoordinator]):
    """Common base: ties every entity to the same HA device record."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        device = coordinator.device
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.mac_address)},
            connections={(dr.CONNECTION_NETWORK_MAC, dr.format_mac(device.mac_address))},
            manufacturer="Swidget",
            name=device.friendly_name,
            model="Swidget WiFi Insert",
            model_id=device.model,
            sw_version=device.version,
        )
