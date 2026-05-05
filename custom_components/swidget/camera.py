"""Camera platform for Swidget video devices."""

from __future__ import annotations

from swidget import InsertType

from homeassistant.components.camera import Camera, CameraEntityFeature
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
    """Set up the Swidget camera from a config entry."""
    coordinator: SwidgetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    if coordinator.device.insert_type == InsertType.VIDEO:
        async_add_entities([SwidgetCamera(coordinator)])


class SwidgetCamera(SwidgetEntity, Camera):
    """Live RTSP view of the Swidget Video insert."""

    _attr_translation_key = "camera"
    _attr_name = "Camera"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the camera entity."""
        Camera.__init__(self)
        SwidgetEntity.__init__(self, coordinator)
        self._attr_unique_id = f"{coordinator.device.mac_address}_camera"

    @property
    def available(self) -> bool:
        """Camera is unavailable while the device's RTSP server is off."""
        return super().available and self.coordinator.device.rtsp_enabled is True

    async def stream_source(self) -> str | None:
        """Return the RTSP URL for HA's stream component."""
        return self.coordinator.device.rtsp_stream_source
