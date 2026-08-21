"""Data update coordinator for Swidget devices."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
from typing import Any

from swidget import SwidgetDevice
from swidget.exceptions import SwidgetException

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

_LOGGER = logging.getLogger(__name__)

FALLBACK_POLL_INTERVAL = timedelta(seconds=30)
# Re-fetch device_config periodically to recover from cache drift.
# The SDK's update() deliberately skips device_config to keep polls
# cheap, and an unsolicited partial-update websocket push will
# wholesale-replace the local cache (process_device_config doesn't
# merge). A periodic full GET — sent over the websocket when connected
# — is the cheap, eventually-consistent net: any corruption heals
# itself within one tick.
DEVICE_CONFIG_REFRESH_INTERVAL_SEC = 5 * 60

# Fan boost has no stored duration in firmware — every boost request
# carries its own ``minutes`` (a uint8, so 255 is the ceiling). The
# desired duration therefore lives integration-side, shared between the
# Boost switch (which sends it on turn-on) and the Boost duration
# select (which edits it). 0 means "no timer": boost runs until
# turned off (sent as ``{"mode": "on"}``).
DEFAULT_FAN_BOOST_MINUTES = 20

# Same integration-side storage for the fan's speed override (the
# ``timer`` function): the user sets a temporary CFM via the fan speed
# entity, which firmware holds until changed; optionally a timer
# reverts it to the default CFM after N minutes. 0 = "Until changed":
# no timer is armed and the override persists. Shared between the
# Speed override switch and its duration select.
DEFAULT_FAN_OVERRIDE_MINUTES = 0


def _structure_fingerprint(device: SwidgetDevice) -> tuple[Any, ...]:
    """Snapshot the host/insert layout used to detect physical re-seating.

    A Swidget host can be physically moved between bases (e.g. switch ->
    outlet), changing device_type, the insert, and the set of components
    on each assembly. We hash all of that into a tuple so we can compare
    successive summaries.

    Critically the per-component signal is the summary's declared
    function list, NOT ``component.functions.keys()``. Firmware emits
    state-only keys (e.g. ``modules`` on FV05) that get merged into
    ``functions`` by ``process_state`` and then dropped on the next
    ``process_summary`` rebuild — which would flap the fingerprint
    every poll cycle and reload the entry in a loop.
    """
    parts: list[Any] = []
    for key in sorted(device.assemblies):
        assembly = device.assemblies[key]
        component_sig = tuple(
            (
                cid,
                tuple(sorted(getattr(component, "summary_functions", ()) or ())),
            )
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
        # Desired boost duration per host component, in minutes. Seeded
        # by the Boost duration select on restore; read by the Boost switch.
        self.fan_boost_minutes: dict[str, int] = {}
        # Same for the speed override timer (Speed override switch +
        # its duration select). 0 = no timer ("Until changed").
        self.fan_override_minutes: dict[str, int] = {}
        # ``time.monotonic()`` deadline per host component until which
        # the ERV's datapoints are considered mid-ramp and untrusted —
        # they flap while the unit spins up/down. Set by the mode/speed
        # selects on every selection; read by both so one command
        # freezes the other dropdown too.
        self.fan_settle_until: dict[str, float] = {}
        self._structure_fingerprint: tuple[Any, ...] | None = None
        self._reload_pending = False
        # Initialise to "now" so we don't issue an immediate redundant
        # device_config refresh — entry setup already pulled it via HTTP.
        self._device_config_last_refresh: float = time.monotonic()
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
        # Periodic device_config refresh, gated by elapsed time rather
        # than by tick count so the cadence is independent of the poll
        # interval. ``get_device_config`` is fire-and-forget over the
        # websocket — we don't await the response here; it lands on the
        # message callback and updates the cache asynchronously.
        now = time.monotonic()
        if now - self._device_config_last_refresh >= DEVICE_CONFIG_REFRESH_INTERVAL_SEC:
            try:
                await self.device.get_device_config()
                self._device_config_last_refresh = now
            except SwidgetException as err:
                # A failed refresh shouldn't fail the whole coordinator
                # update — state is the priority signal; the cache will
                # try again next tick.
                _LOGGER.warning("device_config refresh failed: %s", err)

    async def async_apply_device_config(self, updates: dict) -> None:
        """Write a sparse config update and re-read until the device settles.

        The Pesna fans apply several config fields (runtime/autoRuntime,
        sensor bounds, balancing, default CFM) to the fan over the
        serial link asynchronously — the full GET that set_device_config
        issues right after its POST can still return the pre-ack values,
        which would freeze a stale reading into the cache until the next
        periodic refresh (up to DEVICE_CONFIG_REFRESH_INTERVAL_SEC).
        Schedule a couple of short-delay re-reads so entities settle on
        the acked values within seconds instead.
        """
        await self.device.set_device_config(updates)
        await self.async_request_refresh()

        async def _settle() -> None:
            for delay in (3, 10):
                await asyncio.sleep(delay)
                try:
                    await self.device.get_device_config()
                except SwidgetException as err:
                    _LOGGER.debug("post-write config re-read failed: %s", err)
                    return
                self.async_update_listeners()

        self.hass.async_create_task(_settle())

    async def async_set_fan_boost(self, component_id: str, enable: bool) -> None:
        """Start or stop a fan boost using the stored per-component duration."""
        if not enable:
            command: dict = {"mode": "off"}
        elif minutes := self.fan_boost_minutes.get(
            component_id, DEFAULT_FAN_BOOST_MINUTES
        ):
            command = {"mode": "timer", "minutes": minutes}
        else:
            command = {"mode": "on"}
        await self.device.send_command(
            assembly="host",
            component=component_id,
            function="boost",
            command=command,
        )
        await self.async_request_refresh()

    async def async_arm_fan_override_timer(self, component_id: str) -> None:
        """Send the stored speed-override duration to the device.

        A stored 0 ("Until changed") sends ``{"minutes": 0}``, which
        firmware treats as an explicit clear that holds the current
        speed — exactly the "persist until the user changes it"
        semantics; it's a no-op when no timer is counting down.
        """
        minutes = self.fan_override_minutes.get(
            component_id, DEFAULT_FAN_OVERRIDE_MINUTES
        )
        await self.device.send_command(
            assembly="host",
            component=component_id,
            function="timer",
            command={"minutes": minutes},
        )
        await self.async_request_refresh()

    async def async_end_fan_override(self, component_id: str) -> None:
        """End the speed override via the host toggle.

        Firmware's fan ``on()`` cancels a running custom timer and
        clears boost/manual/balance overrides, restoring the default
        CFM without a manual volume write — sending the default as a
        CFM command instead would itself register as a new manual
        override and clobber speed changes made on the unit's controls.

        A plain toggle-on can't be used while the fan is running:
        ``HostComponent::on()`` (component.cpp::1664) drops a toggle-on
        when ``data.powerOn`` is already true, so it never reaches the
        override cleanup. Until firmware lets a redundant toggle-on
        through for fan hosts, cycle off -> on; the off performs the
        full cleanup and the on restarts operation at the default CFM
        (at the price of a brief motor blip).
        """
        for state in ("off", "on"):
            await self.device.send_command(
                assembly="host",
                component=component_id,
                function="toggle",
                command={"state": state},
            )
        await self.async_request_refresh()

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
