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
from homeassistant.exceptions import HomeAssistantError
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

    # Gate on the firmware actually carrying the field — older builds
    # may omit it. Reading the config dict directly avoids guessing.
    config = device.device_config.config if device.device_config else {}
    if "debugEnabled" in config.get("configServer", {}):
        entities.append(SwidgetAllowHttpSwitch(coordinator))
    if "cloud_connection" in config.get("mqtt", {}):
        entities.append(SwidgetMqttCloudSwitch(coordinator))

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


class SwidgetAllowHttpSwitch(SwidgetEntity, SwitchEntity):
    """One-way toggle for ``configServer.debugEnabled``.

    Once HTTP traffic is allowed on the device's config server, the
    firmware doesn't permit disabling it from the network — that has to
    happen on the device itself. We model this by accepting turn_on and
    rejecting turn_off with a HomeAssistantError so the user gets
    explicit feedback rather than a silently-ignored click.
    """

    _attr_translation_key = "allow_http"
    _attr_name = "Allow HTTP Traffic"

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the allow-HTTP switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.mac_address}_allow_http"

    @property
    def is_on(self) -> bool | None:
        """Return current ``configServer.debugEnabled``, or None if unknown."""
        device_config = self.coordinator.device.device_config
        if device_config is None:
            return None
        try:
            return bool(device_config.config["configServer"]["debugEnabled"])
        except (KeyError, TypeError):
            return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Allow HTTP traffic on the config server."""
        await self.coordinator.device.set_device_config(
            {"configServer": {"debugEnabled": True}}
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Reject — the device doesn't support disabling this from the network."""
        raise HomeAssistantError(
            "Allow HTTP Traffic can't be disabled remotely; revert it on the device."
        )


class SwidgetMqttCloudSwitch(SwidgetEntity, SwitchEntity):
    """Toggle for ``mqtt.cloud_connection``.

    Enabling can be NACKed by the firmware — e.g. when the device is
    in local-provision mode the response body is::

        {"mqtt": {"cloud_connection": false, "error": "LocalProvisionMode"}}

    The HTTP status is still 200, so we have to look at the body. We
    POST directly here instead of going through ``set_device_config``
    so the response is reachable; on a NACK we surface the firmware's
    error string via HomeAssistantError so the user sees the actual
    reason instead of a silent no-op.
    """

    _attr_translation_key = "mqtt_cloud"
    _attr_name = "MQTT cloud connection"

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the MQTT cloud-connection switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.mac_address}_mqtt_cloud"

    @property
    def is_on(self) -> bool | None:
        """Return current ``mqtt.cloud_connection``, or None if unknown."""
        device_config = self.coordinator.device.device_config
        if device_config is None:
            return None
        try:
            return bool(device_config.config["mqtt"]["cloud_connection"])
        except (KeyError, TypeError):
            return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the MQTT cloud connection, surfacing firmware NACKs."""
        device = self.coordinator.device
        response = await device.make_http_request(
            "POST",
            "device_config",
            json_payload={"mqtt": {"cloud_connection": True}},
        )
        mqtt = response.get("mqtt") if isinstance(response, dict) else None
        if not isinstance(mqtt, dict) or mqtt.get("cloud_connection") is not True:
            # cloud_connection didn't flip true → NACK. Prefer the
            # device's error string when present.
            error_msg = (
                mqtt.get("error")
                if isinstance(mqtt, dict) and mqtt.get("error")
                else "device rejected the change"
            )
            raise HomeAssistantError(
                f"Could not enable MQTT cloud connection: {error_msg}"
            )
        # On success, refresh the full cache via HTTP. The POST response
        # is partial-shape (only ``mqtt``); processing it wholesale would
        # clobber every other top-level config key.
        config = await device.make_http_request("GET", "device_config")
        await device.process_device_config(config)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the MQTT cloud connection."""
        await self.coordinator.device.set_device_config(
            {"mqtt": {"cloud_connection": False}}
        )
        await self.coordinator.async_request_refresh()
