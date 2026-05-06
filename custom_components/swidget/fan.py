"""Fan platform — Swidget Pesna fan controllers.

A single host component on a Pesna fan can independently expose
``exhaust`` and ``supply`` airflow control. Each is its own
``FanEntity`` so users can automate them separately. CFM is mapped to
HA's percentage scale using the ``maxCFM`` figure from the summary;
when the device reports a discrete ``allowed`` set in the datapoint we
use that as the speed list so the UI snaps to real device steps.
"""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from .const import DOMAIN
from .coordinator import SwidgetDataUpdateCoordinator
from .entity import SwidgetEntity

# Fallback used when the summary doesn't expose maxCFM (which would be
# unusual). 110 covers the FV05 high speed; if we end up here for a
# bigger unit the percentage will still be monotonically correct and
# the user can read the actual CFM off the dedicated sensor.
_DEFAULT_MAX_CFM = 110


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Swidget fan entities from a config entry."""
    coordinator: SwidgetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    device = coordinator.device

    entities: list[FanEntity] = []
    host = device.assemblies.get("host")
    if host is not None:
        for component_id, component in host.components.items():
            if "exhaust" in component.functions:
                entities.append(
                    SwidgetAirflowFan(coordinator, component_id, "exhaust")
                )
            if "supply" in component.functions:
                entities.append(
                    SwidgetAirflowFan(coordinator, component_id, "supply")
                )

    async_add_entities(entities)


class SwidgetAirflowFan(SwidgetEntity, FanEntity):
    """One direction (exhaust or supply) of a Pesna fan, surfaced as a fan entity."""

    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        component_id: str,
        role: str,
    ) -> None:
        """Initialize the airflow fan entity.

        ``role`` is the function tag (``"exhaust"`` or ``"supply"``) and
        also drives both the user-visible name and the unique id, so
        the two entities don't collide on a duplex fan.
        """
        super().__init__(coordinator)
        self._component_id = component_id
        self._role = role
        self._attr_name = role.capitalize()
        self._attr_translation_key = f"fan_{role}"
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_{role}"
        )

    # ---- internal helpers -----------------------------------------------

    def _component(self):
        try:
            return (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
            )
        except (KeyError, AttributeError):
            return None

    def _function_state(self) -> dict | None:
        component = self._component()
        if component is None:
            return None
        value = component.functions.get(self._role)
        return value if isinstance(value, dict) else None

    def _max_cfm(self) -> int:
        component = self._component()
        if component is not None:
            value = getattr(component, "max_cfm", None)
            if isinstance(value, int) and value > 0:
                return value
        return _DEFAULT_MAX_CFM

    def _allowed_cfms(self) -> list[int] | None:
        """Return the device's discrete CFM steps if the datapoint provided one."""
        state = self._function_state()
        if state is None:
            return None
        allowed = state.get("allowed")
        if isinstance(allowed, list) and allowed:
            try:
                values = sorted({int(v) for v in allowed if v is not None})
            except (TypeError, ValueError):
                return None
            return values or None
        return None

    # ---- HA properties --------------------------------------------------

    @property
    def available(self) -> bool:
        """Available once we've seen a CFM datapoint for this direction."""
        return self._function_state() is not None

    @property
    def is_on(self) -> bool | None:
        """On whenever the device is reporting a non-zero CFM."""
        state = self._function_state()
        if state is None:
            return None
        try:
            return int(state.get("cfm") or 0) > 0
        except (TypeError, ValueError):
            return None

    @property
    def percentage(self) -> int | None:
        """Map CFM onto HA's 0-100 percentage.

        Returns 0 explicitly when the device is idle: ``ranged_value_to_percentage``
        with the low end of the range at 1 maps cfm=0 to a negative value
        which would render badly in the UI.
        """
        state = self._function_state()
        if state is None:
            return None
        try:
            cfm = int(state.get("cfm") or 0)
        except (TypeError, ValueError):
            return None
        if cfm <= 0:
            return 0
        max_cfm = self._max_cfm()
        return ranged_value_to_percentage((1, max_cfm), cfm)

    @property
    def speed_count(self) -> int:
        """Use the device's discrete CFM steps as the speed count when known."""
        allowed = self._allowed_cfms()
        if allowed is not None:
            return len(allowed)
        return self._max_cfm()

    # ---- HA commands ----------------------------------------------------

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn the airflow on, optionally to a specific percentage."""
        target_percentage = percentage if percentage is not None else 100
        await self.async_set_percentage(target_percentage)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop airflow on this direction (CFM = 0)."""
        await self._send_cfm(0)

    async def async_set_percentage(self, percentage: int) -> None:
        """Translate HA percentage into the appropriate CFM value.

        When the device exposes an ``allowed`` set we snap to the
        nearest allowed step so we never send a value the firmware will
        nack; otherwise we scale linearly against ``maxCFM``.
        """
        if percentage <= 0:
            await self._send_cfm(0)
            return

        allowed = self._allowed_cfms()
        if allowed:
            # Pick the allowed step nearest to the requested percentage,
            # measured against maxCFM so the mapping stays monotone with
            # how the percentage was derived in the first place.
            max_cfm = self._max_cfm()
            target_cfm = max(1, math.ceil(max_cfm * percentage / 100))
            cfm = min(allowed, key=lambda v: abs(v - target_cfm))
        else:
            cfm = max(
                1,
                int(round(percentage_to_ranged_value((1, self._max_cfm()), percentage))),
            )

        await self._send_cfm(cfm)

    async def _send_cfm(self, cfm: int) -> None:
        await self.coordinator.device.send_command(
            assembly="host",
            component=self._component_id,
            function=self._role,
            command={"cfm": int(cfm)},
        )
        await self.coordinator.async_request_refresh()
