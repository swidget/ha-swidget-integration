"""Data update coordinator for Swidget devices."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from swidget import SwidgetDevice
from swidget.exceptions import SwidgetException

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

FALLBACK_POLL_INTERVAL = timedelta(seconds=30)


def _structure_fingerprint(device: SwidgetDevice) -> tuple[Any, ...]:
    """Snapshot the host/insert layout used to detect physical re-seating.

    A Swidget host can be physically moved between bases (e.g. switch ->
    outlet), changing device_type, the insert, and the set of components
    on each assembly. We hash all of that into a tuple so we can compare
    successive summaries.
    """
    parts: list[Any] = []
    for key in sorted(device.assemblies):
        assembly = device.assemblies[key]
        component_sig = tuple(
            (cid, tuple(sorted(component.functions.keys())))
            for cid, component in sorted(assembly.components.items())
        )
        parts.append((key, getattr(assembly, "type", None), component_sig))
    return (
        getattr(device.device_type, "value", device.device_type),
        getattr(device.insert_type, "value", device.insert_type),
        tuple(parts),
    )


class SwidgetDataUpdateCoordinator(DataUpdateCoordinator[None]):
    """Coordinator that owns a single SwidgetDevice and refreshes it.

    State lives on the device itself (assemblies/components), so entities
    read attributes off ``coordinator.device``. The fallback poll exists
    in case the websocket drops; when websockets are healthy the push
    callbacks will keep state current and these polls are cheap no-ops.
    """

    def __init__(
        self, hass: HomeAssistant, device: SwidgetDevice, entry_id: str
    ) -> None:
        """Initialize the coordinator."""
        self.device = device
        self.entry_id = entry_id
        self._structure_fingerprint: tuple[Any, ...] | None = None
        self._reload_pending = False
        super().__init__(
            hass,
            _LOGGER,
            name=f"swidget {device.ip_address}",
            update_interval=FALLBACK_POLL_INTERVAL,
        )

    def capture_structure_fingerprint(self) -> None:
        """Record the current device layout as the baseline for change detection."""
        self._structure_fingerprint = _structure_fingerprint(self.device)

    async def _async_update_data(self) -> None:
        """Refresh device state from the device."""
        try:
            await self.device.update()
        except SwidgetException as err:
            raise UpdateFailed(f"Error communicating with device: {err}") from err

    async def _websocket_update(self, message: object) -> None:
        """Handle a push update from the device websocket.

        If a summary message indicates the device's host/insert layout
        changed (e.g. the user moved the WiFi insert into a different
        base), reload the config entry so every platform rebuilds its
        entities against the new structure.
        """
        if (
            isinstance(message, dict)
            and message.get("request_id") == "summary"
            and self._structure_fingerprint is not None
            and not self._reload_pending
        ):
            current = _structure_fingerprint(self.device)
            if current != self._structure_fingerprint:
                _LOGGER.info(
                    "Swidget device structure changed (%s -> %s); reloading entry",
                    self._structure_fingerprint,
                    current,
                )
                self._reload_pending = True
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(self.entry_id)
                )
                return
        self.async_set_updated_data(None)
