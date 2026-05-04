"""Config flow for the Swidget integration."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from swidget import SwidgetException
from swidget.discovery import (
    SwidgetDiscoveredDevice,
    discover_devices,
    discover_single,
)

from homeassistant.components import ssdp
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import format_mac

from .const import (
    CONF_SECRET_KEY,
    CONF_TOKEN_NAME,
    CONF_USE_HTTPS,
    DEFAULT_TOKEN_NAME,
    DEFAULT_USE_HTTPS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE = "device"


def _credentials_schema(
    *,
    include_host: bool = True,
    default_host: str = "",
) -> vol.Schema:
    """Return the schema for credential entry, optionally with a host field."""
    fields: dict[Any, Any] = {}
    if include_host:
        fields[vol.Optional(CONF_HOST, default=default_host)] = str
    fields[vol.Required(CONF_TOKEN_NAME, default=DEFAULT_TOKEN_NAME)] = str
    fields[vol.Required(CONF_SECRET_KEY)] = str
    fields[vol.Required(CONF_USE_HTTPS, default=DEFAULT_USE_HTTPS)] = bool
    return vol.Schema(fields)


async def _async_try_connect(data: dict[str, Any]) -> str:
    """Validate credentials by talking to the device. Returns the friendly name.

    Raises SwidgetException on connection/auth failure.
    """
    device = await discover_single(
        host=data[CONF_HOST],
        token_name=data[CONF_TOKEN_NAME],
        password=data[CONF_SECRET_KEY],
        use_https=data[CONF_USE_HTTPS],
        use_websockets=False,
    )
    try:
        await device.update()
        return device.friendly_name
    finally:
        await device.close()


class SwidgetConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a Swidget config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._discovered_host: str | None = None
        self._discovered_name: str | None = None
        self._discovered_devices: dict[str, SwidgetDiscoveredDevice] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a user-initiated flow.

        Empty host triggers an SSDP scan and the pick-device step.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_HOST):
                return await self.async_step_pick_device()

            try:
                title = await _async_try_connect(user_input)
            except SwidgetException:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001 - surface unknowns to UI
                _LOGGER.exception("Unexpected error connecting to Swidget device")
                errors["base"] = "unknown"
            else:
                # We have to fetch the device once more to learn its MAC for
                # the unique_id; _async_try_connect doesn't expose it. Cheap
                # because the device is local.
                return await self._async_finish(user_input, title)

        return self.async_show_form(
            step_id="user",
            data_schema=_credentials_schema(),
            errors=errors,
        )

    async def async_step_pick_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show a list of SSDP-discovered devices and collect credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            mac = user_input[CONF_DEVICE]
            discovered = self._discovered_devices[mac]
            await self.async_set_unique_id(format_mac(mac), raise_on_progress=False)
            self._abort_if_unique_id_configured()

            data = {
                CONF_HOST: discovered.host,
                CONF_TOKEN_NAME: user_input[CONF_TOKEN_NAME],
                CONF_SECRET_KEY: user_input[CONF_SECRET_KEY],
                CONF_USE_HTTPS: user_input[CONF_USE_HTTPS],
            }
            try:
                title = await _async_try_connect(data)
            except SwidgetException:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error connecting to Swidget device")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=title or discovered.friendly_name, data=data
                )

        # Fresh scan; filter out already-configured devices.
        configured = {entry.unique_id for entry in self._async_current_entries()}
        self._discovered_devices = await discover_devices()
        choices = {
            mac: f"{dev.friendly_name} ({dev.host})"
            for mac, dev in self._discovered_devices.items()
            if format_mac(mac) not in configured
        }
        if not choices:
            return self.async_abort(reason="no_devices_found")

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE): vol.In(choices),
                vol.Required(CONF_TOKEN_NAME, default=DEFAULT_TOKEN_NAME): str,
                vol.Required(CONF_SECRET_KEY): str,
                vol.Required(CONF_USE_HTTPS, default=DEFAULT_USE_HTTPS): bool,
            }
        )
        return self.async_show_form(
            step_id="pick_device", data_schema=schema, errors=errors
        )

    async def async_step_ssdp(
        self, discovery_info: ssdp.SsdpServiceInfo
    ) -> ConfigFlowResult:
        """Handle a device discovered via SSDP."""
        location = discovery_info.ssdp_location or ""
        host = urlparse(location).hostname
        usn = discovery_info.ssdp_usn or ""
        # USN looks like "uuid:...-<MAC>"; the MAC is the last hyphenated chunk.
        mac = usn.split("-")[-1] if usn else ""
        if not host or not mac:
            return self.async_abort(reason="invalid_discovery_info")

        await self.async_set_unique_id(format_mac(mac))
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})

        # Pull a friendly name from the SERVER header if present:
        # `<os> <type>+<insert>/"<friendly name>"`
        server = discovery_info.ssdp_headers.get("SERVER", "")
        friendly_name = server.split("/")[-1].strip('"') if "/" in server else host

        self._discovered_host = host
        self._discovered_name = friendly_name
        self.context["title_placeholders"] = {"name": friendly_name, "host": host}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to confirm a discovered device and supply credentials."""
        assert self._discovered_host is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {CONF_HOST: self._discovered_host, **user_input}
            try:
                title = await _async_try_connect(data)
            except SwidgetException:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error connecting to Swidget device")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=title or self._discovered_name or self._discovered_host,
                    data=data,
                )

        return self.async_show_form(
            step_id="discovery_confirm",
            data_schema=_credentials_schema(include_host=False),
            description_placeholders={
                "name": self._discovered_name or "",
                "host": self._discovered_host,
            },
            errors=errors,
        )

    async def _async_finish(
        self, data: dict[str, Any], title: str
    ) -> ConfigFlowResult:
        """Create the entry after a successful user-step validation.

        Re-opens the device briefly to read the MAC for unique_id. The
        SDK's discover_single doesn't expose the MAC out of the box, but
        update() populates ``device.mac_address``.
        """
        device = await discover_single(
            host=data[CONF_HOST],
            token_name=data[CONF_TOKEN_NAME],
            password=data[CONF_SECRET_KEY],
            use_https=data[CONF_USE_HTTPS],
            use_websockets=False,
        )
        try:
            await device.update()
            mac = device.mac_address
        finally:
            await device.close()

        await self.async_set_unique_id(format_mac(mac))
        self._abort_if_unique_id_configured(updates={CONF_HOST: data[CONF_HOST]})
        return self.async_create_entry(title=title, data=data)
