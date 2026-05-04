"""The Swidget integration."""

from __future__ import annotations

import logging

from swidget import SwidgetException
from swidget.discovery import discover_single

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_SECRET_KEY,
    CONF_TOKEN_NAME,
    CONF_USE_HTTPS,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import SwidgetDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Swidget from a config entry."""
    try:
        device = await discover_single(
            host=entry.data[CONF_HOST],
            token_name=entry.data[CONF_TOKEN_NAME],
            password=entry.data[CONF_SECRET_KEY],
            use_https=entry.data[CONF_USE_HTTPS],
            use_websockets=True,
        )
    except SwidgetException as err:
        raise ConfigEntryNotReady(f"Could not connect to {entry.data[CONF_HOST]}") from err

    # Pre-populate identity + state over HTTP. The SDK's websocket-mode
    # get_summary/get_state are fire-and-forget — they send a request and
    # return before the response is processed by the message handler — so
    # device.mac_address etc. won't be set in time for the device-registry
    # call below.
    try:
        summary = await device.make_http_request("GET", "summary")
        await device.process_summary(summary)
        state = await device.make_http_request("GET", "state")
        await device.process_state(state)
        # process_summary doesn't touch _friendly_name, so without this
        # the device registry would show "Unknown Swidget Device".
        await device.get_friendly_name()
    except SwidgetException as err:
        await device.close()
        raise ConfigEntryNotReady(f"Could not read state from {entry.data[CONF_HOST]}") from err

    coordinator = SwidgetDataUpdateCoordinator(hass, device)

    # Hook websocket pushes into the coordinator so subscribed entities
    # update immediately on device-side events.
    device.add_event_callback(coordinator._websocket_update)

    try:
        await device.start()
    except SwidgetException as err:
        await device.close()
        raise ConfigEntryNotReady(f"Could not start device {entry.data[CONF_HOST]}") from err

    # device.start() only opens the websocket; it doesn't pump messages.
    # Spawn the receiver loop ourselves so push updates actually reach the
    # registered callback. Tying it to the entry means HA cancels it on
    # unload.
    entry.async_create_background_task(
        hass,
        device.get_websocket().run(),
        name=f"swidget_websocket_{device.ip_address}",
    )

    # Pre-populated above; this just marks the coordinator healthy.
    await coordinator.async_config_entry_first_refresh()

    # Register the device once at setup so platforms can reference it
    # via identifiers without each one duplicating the metadata.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, device.mac_address)},
        connections={(dr.CONNECTION_NETWORK_MAC, dr.format_mac(device.mac_address))},
        manufacturer="Swidget",
        name=device.friendly_name,
        model=device.model,
        sw_version=device.version,
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Swidget config entry."""
    coordinator: SwidgetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Tear down the websocket and HTTP session regardless of platform
    # unload success — leaving sockets open would leak.
    await coordinator.device.stop()
    await coordinator.device.close()

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
