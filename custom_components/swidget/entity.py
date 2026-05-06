"""Base entity for Swidget devices."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, friendly_host_model
from .coordinator import SwidgetDataUpdateCoordinator


class SwidgetEntity(CoordinatorEntity[SwidgetDataUpdateCoordinator]):
    """Common base: ties every entity to the same HA device record."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        device = coordinator.device
        # See note in __init__.async_setup_entry: ``mac_address`` is the
        # device id, not a network MAC. Surfaced via ``serial_number``
        # so HA labels it correctly on the device-info card.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.mac_address)},
            manufacturer="Swidget",
            name=device.friendly_name,
            model=friendly_host_model(device.device_type, device.insert_type),
            model_id=device.model,
            serial_number=device.mac_address,
            sw_version=device.version,
            configuration_url=f"{device.uri_scheme}://{device.ip_address}",
        )
