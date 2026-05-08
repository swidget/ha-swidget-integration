"""Update platform — surface available firmware versions and trigger installs."""

from __future__ import annotations

from datetime import timedelta
import logging
import re
from typing import Any

from swidget import SwidgetException

from homeassistant.components.update import (
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN
from .coordinator import SwidgetDataUpdateCoordinator
from .entity import SwidgetEntity

_LOGGER = logging.getLogger(__name__)

# Firmware checks are inherently slow-moving — there's no point hammering
# /api/v1/update at the platform default cadence. 6h hits a reasonable
# balance between picking up newly-published versions and not waking the
# device's HTTP server unnecessarily.
SCAN_INTERVAL = timedelta(hours=6)

# Match strict ``major.minor.patch`` so we can compare numerically. The
# entries the firmware returns today look like that, but we accept any
# ``-suffix`` (e.g. ``1.6.86-test``) and ignore it for sort purposes.
_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def _semver_key(version: str) -> tuple[int, int, int] | None:
    """Return a sortable key for a semver string, or None if unparseable."""
    match = _SEMVER_RE.match(version)
    if match is None:
        return None
    return tuple(int(p) for p in match.groups())  # type: ignore[return-value]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Swidget firmware-update entity."""
    coordinator: SwidgetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    # Probe once so we don't permanently attach a broken entity to
    # legacy firmware that doesn't expose /api/v1/update. ``check_for_
    # updates`` raises SwidgetException on any HTTP error (including
    # 404), which is exactly the signal we want. We also keep the
    # response and seed the entity with it, so users see fresh state
    # immediately on (re)load instead of waiting a full SCAN_INTERVAL.
    try:
        initial_versions = await coordinator.device.check_for_updates()
    except SwidgetException:
        _LOGGER.debug(
            "Skipping update entity for %s; device did not answer /api/v1/update",
            coordinator.device.ip_address,
        )
        return
    _LOGGER.info(
        "Swidget %s firmware probe: installed=%s available=%s",
        coordinator.device.ip_address,
        getattr(coordinator.device, "version", None),
        initial_versions,
    )
    async_add_entities([SwidgetUpdateEntity(coordinator, initial_versions)])


class SwidgetUpdateEntity(SwidgetEntity, UpdateEntity):
    """Firmware update entity backed by /api/v1/update.

    Polls the device for the available-versions array every
    ``SCAN_INTERVAL`` and exposes the highest semver version as the
    ``latest_version``. Declaring ``SPECIFIC_VERSION`` lets users pick
    any element of the array via HA's UI — handy for downgrades or
    skipping a specific build.
    """

    _attr_name = "Firmware"
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.SPECIFIC_VERSION
    )
    # We drive the firmware check ourselves on a timer registered in
    # ``async_added_to_hass`` (and again when the user hits Refresh,
    # which routes to ``async_update``). HA's platform polling is
    # disabled — letting the CoordinatorEntity defaults stand — because
    # the parent class's ``async_update`` would otherwise short-circuit
    # to ``coordinator.async_request_refresh`` and never run our check.

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        initial_versions: list[str] | None = None,
    ) -> None:
        """Initialize the update entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.mac_address}_firmware"
        self._available_versions: list[str] = [
            v for v in (initial_versions or []) if isinstance(v, str)
        ]

    async def async_added_to_hass(self) -> None:
        """Wire up the periodic firmware-check timer."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_scheduled_refresh, SCAN_INTERVAL
            )
        )

    async def _async_scheduled_refresh(self, _now: Any) -> None:
        """Timer callback: refresh and write state."""
        await self._async_refresh_versions()
        self.async_write_ha_state()

    @property
    def installed_version(self) -> str | None:
        """Currently-running firmware version reported by the summary."""
        return getattr(self.coordinator.device, "version", None) or None

    @property
    def latest_version(self) -> str | None:
        """Highest semver version offered by the device, or current when none."""
        if not self._available_versions:
            return self.installed_version
        # Prefer a numeric semver compare; fall back to the array's last
        # element if every entry is unparseable so we still surface
        # *something* to the user instead of misreporting "up to date".
        keyed = [
            (key, version)
            for version in self._available_versions
            if (key := _semver_key(version)) is not None
        ]
        if keyed:
            return max(keyed, key=lambda kv: kv[0])[1]
        return self._available_versions[-1]

    async def async_update(self) -> None:
        """Refresh on demand (homeassistant.update_entity service / refresh button)."""
        await self._async_refresh_versions()

    async def _async_refresh_versions(self) -> None:
        """Hit /api/v1/update and stash the resulting list."""
        ip = getattr(self.coordinator.device, "ip_address", "?")
        _LOGGER.info("Swidget %s firmware check: requesting /api/v1/update", ip)
        try:
            versions = await self.coordinator.device.check_for_updates()
        except SwidgetException as err:
            _LOGGER.warning("Update check failed for %s: %s", ip, err)
            return
        # ``check_for_updates`` returns a lex-sorted list — re-store as
        # a plain list so latest_version can do its own semver sort.
        self._available_versions = [v for v in versions if isinstance(v, str)]
        _LOGGER.info(
            "Swidget %s firmware check: installed=%s available=%s -> latest=%s",
            ip,
            self.installed_version,
            self._available_versions,
            self.latest_version,
        )

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Trigger a firmware install on the device."""
        target = version or self.latest_version
        if not target or target == self.installed_version:
            raise HomeAssistantError("No newer firmware version available")
        try:
            await self.coordinator.device.update_version(target)
        except SwidgetException as err:
            raise HomeAssistantError(
                f"Could not start firmware update to {target}: {err}"
            ) from err
        # No progress signal from firmware: the card will continue to
        # show the old installed_version (and the device will go
        # Unavailable while it reboots), then naturally update once the
        # next poll cycle picks up the new version. Flagging in_progress
        # without a corresponding "complete" signal would leave the
        # card stuck on "Installing" indefinitely.
