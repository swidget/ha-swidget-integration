"""Config flow for the Swidget integration."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from swidget import SwidgetException, detect_secure
from swidget.discovery import (
    SwidgetDiscoveredDevice,
    device_id_from_ssdp,
    discover_devices,
    discover_single,
)

from homeassistant.components import ssdp
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST
from homeassistant.helpers import selector

from .const import (
    CONF_SECRET_KEY,
    CONF_TOKEN_NAME,
    CONF_USE_HTTPS,
    DEFAULT_TOKEN_NAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

CONF_DEVICE = "device"


async def _async_validate_credentials(
    host: str, use_https: bool, token_name: str, secret_key: str
) -> tuple[str, str]:
    """Open the device with the given credentials and read its identity.

    Returns (friendly_name, device_id). The device id is what the firmware
    reports as ``mac`` in /api/v1/summary — it's the canonical per-device
    string, but it's a real MAC only on pico (12 hex). On video it's a
    longer opaque id (24 hex), so we treat it as an opaque key throughout
    the flow rather than running it through format_mac.
    """
    device = await discover_single(
        host=host,
        token_name=token_name,
        password=secret_key,
        use_https=use_https,
        use_websockets=False,
    )
    try:
        await device.update()
        return device.friendly_name, device.mac_address
    finally:
        await device.close()


class SwidgetConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a Swidget config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        # Carried across steps once we've probed/selected a device.
        self._host: str | None = None
        self._use_https: bool | None = None
        self._mac: str | None = None
        self._friendly_name: str | None = None
        # Populated by the scan step.
        self._discovered_devices: dict[str, SwidgetDiscoveredDevice] = {}

    # --------------------------------------------------------------------- #
    # Entry point: scan vs. manual.
    # --------------------------------------------------------------------- #

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Entry point: let the user pick scan vs. manual entry."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["scan", "manual"],
        )

    # --------------------------------------------------------------------- #
    # Manual entry: ask for host only, then probe to decide next step.
    # --------------------------------------------------------------------- #

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manually enter a host. Probes the device to decide next step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            try:
                self._use_https = await detect_secure(host)
            except SwidgetException:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error probing %s", host)
                errors["base"] = "unknown"
            else:
                self._host = host
                if self._use_https:
                    return await self.async_step_credentials()
                return await self._async_finalize_no_auth()

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema({vol.Required(CONF_HOST): str}),
            errors=errors,
        )

    # --------------------------------------------------------------------- #
    # Credentials: only shown for HTTPS-firmware devices.
    # --------------------------------------------------------------------- #

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect token + secret for an HTTPS-firmware device."""
        assert self._host is not None and self._use_https is True
        errors: dict[str, str] = {}
        if user_input is not None:
            token_name = user_input[CONF_TOKEN_NAME]
            secret_key = user_input[CONF_SECRET_KEY]
            try:
                friendly_name, device_id = await _async_validate_credentials(
                    host=self._host,
                    use_https=True,
                    token_name=token_name,
                    secret_key=secret_key,
                )
            except SwidgetException:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error connecting to %s", self._host)
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(device_id)
                self._abort_if_unique_id_configured(updates={CONF_HOST: self._host})
                return self.async_create_entry(
                    title=friendly_name,
                    data={
                        CONF_HOST: self._host,
                        CONF_USE_HTTPS: True,
                        CONF_TOKEN_NAME: token_name,
                        CONF_SECRET_KEY: secret_key,
                    },
                )

        # The description references both {name} and {host}; formatjs errors
        # if either is missing. Provide empty strings as fallback for paths
        # that don't know the friendly name yet (e.g. manual entry).
        placeholders = {
            "name": self._friendly_name or "",
            "host": self._host or "",
        }

        # The token *name* is just an HTTP header name — not a secret —
        # but HA's frontend masks any field whose key contains "token",
        # "secret", "key", etc. by default. Force a plain text input via
        # an explicit TextSelector so the user can see what they're
        # typing. The secret key stays masked (PASSWORD).
        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_TOKEN_NAME, default=DEFAULT_TOKEN_NAME
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.TEXT
                        )
                    ),
                    vol.Required(CONF_SECRET_KEY): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.PASSWORD
                        )
                    ),
                }
            ),
            description_placeholders=placeholders,
            errors=errors,
        )

    # --------------------------------------------------------------------- #
    # Active scan via the SDK's M-SEARCH.
    # --------------------------------------------------------------------- #

    async def async_step_scan(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show a list of SSDP-discovered devices; probe the chosen one."""
        errors: dict[str, str] = {}
        if user_input is not None:
            device_id = user_input[CONF_DEVICE]
            discovered = self._discovered_devices[device_id]
            await self.async_set_unique_id(device_id, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            try:
                self._use_https = await detect_secure(discovered.host)
            except SwidgetException:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error probing %s", discovered.host)
                errors["base"] = "unknown"
            else:
                self._host = discovered.host
                self._mac = device_id
                self._friendly_name = discovered.friendly_name
                if self._use_https:
                    return await self.async_step_credentials()
                return await self._async_finalize_no_auth()

        # Fresh scan; filter out already-configured devices. The library now
        # keys discovered_devices by the canonical device id, so a direct
        # match against entry.unique_id is correct for both pico and video.
        configured = {entry.unique_id for entry in self._async_current_entries()}
        self._discovered_devices = await discover_devices()
        choices = {
            device_id: f"{dev.friendly_name} ({dev.host})"
            for device_id, dev in self._discovered_devices.items()
            if device_id not in configured
        }
        if not choices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="scan",
            data_schema=vol.Schema({vol.Required(CONF_DEVICE): vol.In(choices)}),
            errors=errors,
        )

    # --------------------------------------------------------------------- #
    # Passive SSDP discovery (HA pushes the device into the flow).
    # --------------------------------------------------------------------- #

    async def async_step_ssdp(
        self, discovery_info: ssdp.SsdpServiceInfo
    ) -> ConfigFlowResult:
        """Handle a device discovered via HA's SSDP listener."""
        location = discovery_info.ssdp_location or ""
        host = urlparse(location).hostname
        # The canonical device id is encoded in the USN, but its shape is
        # ST-dependent (pico = last UUID segment, video = leading 24 hex of
        # the padded UUID), so go through the shared parser. We can't just
        # fetch /api/v1/summary here — HTTPS+auth devices won't answer
        # without credentials, which we don't have until async_step_credentials.
        device_id = device_id_from_ssdp(
            discovery_info.ssdp_usn or "",
            (discovery_info.ssdp_st or "").lower(),
        )
        if not host or not device_id:
            return self.async_abort(reason="invalid_discovery_info")

        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})

        # Pull a friendly name from the SERVER header if present:
        # `<os> <type>+<insert>/"<friendly name>"`.
        server = discovery_info.ssdp_headers.get("SERVER", "")
        friendly_name = server.split("/")[-1].strip('"') if "/" in server else host

        try:
            self._use_https = await detect_secure(host)
        except SwidgetException:
            return self.async_abort(reason="cannot_connect")

        self._host = host
        self._mac = device_id
        self._friendly_name = friendly_name
        self.context["title_placeholders"] = {"name": friendly_name, "host": host}

        if self._use_https:
            return await self.async_step_credentials()
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm-only step for HTTP-firmware devices found via SSDP."""
        assert self._host is not None and self._use_https is False
        if user_input is not None:
            return await self._async_finalize_no_auth()

        self._set_confirm_only()
        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders={
                "name": self._friendly_name or "",
                "host": self._host,
            },
        )

    # --------------------------------------------------------------------- #
    # Shared finalize path for HTTP-mode devices.
    # --------------------------------------------------------------------- #

    async def _async_finalize_no_auth(self) -> ConfigFlowResult:
        """Connect (no creds), read identity, and create the entry."""
        assert self._host is not None and self._use_https is False
        try:
            friendly_name, device_id = await _async_validate_credentials(
                host=self._host,
                use_https=False,
                token_name="",
                secret_key="",
            )
        except SwidgetException:
            # Probe said HTTP works but the SDK couldn't reach it; surface as error.
            return self.async_abort(reason="cannot_connect")

        await self.async_set_unique_id(device_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: self._host})
        return self.async_create_entry(
            title=friendly_name,
            data={
                CONF_HOST: self._host,
                CONF_USE_HTTPS: False,
                CONF_TOKEN_NAME: "",
                CONF_SECRET_KEY: "",
            },
        )
