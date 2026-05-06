"""Downloadable diagnostics dump for the device page."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_SECRET_KEY, DOMAIN
from .coordinator import SwidgetDataUpdateCoordinator

REDACT_CONFIG = {CONF_SECRET_KEY}
REDACT_DEVICE = {"mac", "mac_address"}


def _enum_value(value: object) -> Any:
    """Return the underlying value for an Enum, leaving primitives alone."""
    return getattr(value, "value", value)


def _device_snapshot(coordinator: SwidgetDataUpdateCoordinator) -> dict[str, Any]:
    """Build a structured snapshot of the device for diagnostics."""
    device = coordinator.device
    assemblies: dict[str, Any] = {}
    for key, assembly in device.assemblies.items():
        assemblies[key] = {
            "type": _enum_value(assembly.type),
            "id": assembly.id,
            "error": assembly.error,
            "components": {
                component_id: {
                    "functions": {
                        name: value
                        for name, value in component.functions.items()
                    },
                    # Summary-level fan extras (None / [] on non-fan hosts).
                    "max_cfm": getattr(component, "max_cfm", None),
                    "model_code": getattr(component, "model_code", None),
                    "modules": list(getattr(component, "modules", []) or []),
                }
                for component_id, component in assembly.components.items()
            },
        }
    return {
        "model": getattr(device, "model", None),
        "friendly_name": getattr(device, "friendly_name", None),
        "version": getattr(device, "version", None),
        "ip_address": getattr(device, "ip_address", None),
        "mac": getattr(device, "mac_address", None),
        "device_type": _enum_value(getattr(device, "device_type", None)),
        "insert_type": _enum_value(getattr(device, "insert_type", None)),
        "connected": getattr(device, "connected", None),
        "assemblies": assemblies,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: SwidgetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    return {
        "entry": async_redact_data(entry.as_dict(), REDACT_CONFIG),
        "device": async_redact_data(_device_snapshot(coordinator), REDACT_DEVICE),
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device
) -> dict[str, Any]:
    """Return diagnostics for a device."""
    coordinator: SwidgetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    return async_redact_data(_device_snapshot(coordinator), REDACT_DEVICE)
