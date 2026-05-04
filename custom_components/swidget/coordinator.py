"""Data update coordinator for Swidget devices."""

from __future__ import annotations

from datetime import timedelta
import logging

from swidget import SwidgetDevice
from swidget.exceptions import SwidgetException

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

FALLBACK_POLL_INTERVAL = timedelta(seconds=30)


class SwidgetDataUpdateCoordinator(DataUpdateCoordinator[None]):
    """Coordinator that owns a single SwidgetDevice and refreshes it.

    State lives on the device itself (assemblies/components), so entities
    read attributes off ``coordinator.device``. The fallback poll exists
    in case the websocket drops; when websockets are healthy the push
    callbacks will keep state current and these polls are cheap no-ops.
    """

    def __init__(self, hass: HomeAssistant, device: SwidgetDevice) -> None:
        """Initialize the coordinator."""
        self.device = device
        super().__init__(
            hass,
            _LOGGER,
            name=f"swidget {device.ip_address}",
            update_interval=FALLBACK_POLL_INTERVAL,
        )

    async def _async_update_data(self) -> None:
        """Refresh device state from the device."""
        try:
            await self.device.update()
        except SwidgetException as err:
            raise UpdateFailed(f"Error communicating with device: {err}") from err

    async def _websocket_update(self, _message: object) -> None:
        """Handle a push update from the device websocket."""
        self.async_set_updated_data(None)
