"""Dynamic numbering for Controls-section entity names.

The HA device page sorts the Controls section strictly alphabetically
by entity name with no ordering hook, so Controls-section names carry a
numeric prefix ("1. Power"). Which controls exist varies by model — the
WhisperFresh Plus has a host ``toggle`` but no light, the FV05 has a
light but no toggle and gets an emulated fan power switch instead —
so fixed prefixes leave gaps and strays. Instead, ``_control_slots``
mirrors each platform's setup-time gating to predict the device's
actual control set, and ``numbered_name`` assigns contiguous numbers in
the canonical order below.

Adding a Controls-section entity to any platform therefore means:
  * insert its slot key at the desired position in ``_control_slots``,
    gated exactly like the platform's setup code, and
  * name the entity via ``numbered_name(device, slot, base_name)``.
Config- and diagnostic-category entities sort within their own device
page sections and don't take part.
"""

from __future__ import annotations

from typing import Any

from swidget import InsertType


def _control_slots(device: Any) -> list[str]:
    """Return the ordered slot keys this device will materialize.

    Must stay in lockstep with the platform setup gates (light.py,
    switch.py, select.py, number.py, button.py) — a slot
    listed here but never created leaves a hole in the numbering, and
    an entity created without a slot ends up unnumbered at the bottom.
    """
    host = device.assemblies.get("host")
    components = list(host.components.values()) if host is not None else []
    insert = device.assemblies.get("insert")
    insert_components = (
        list(insert.components.values()) if insert is not None else []
    )
    config = device.device_config.config if device.device_config else {}

    def host_has(tag: str) -> bool:
        return any(tag in c.functions for c in components)

    has_toggle = host_has("toggle")
    fan_components = [
        c
        for c in components
        if "exhaust" in c.functions or "supply" in c.functions
    ]
    fan_has_timer = any("timer" in c.functions for c in fan_components)
    has_load_timer = any(
        "timer" in c.functions and c not in fan_components for c in components
    )

    # ERVs (IB-series, identified by the ``mode`` function) carry a
    # toggle but run continuously — no user-facing power control.
    has_power_toggle = any(
        "toggle" in c.functions and "mode" not in c.functions for c in components
    )

    slots: list[str] = []
    # Primary on/off first. "power" is either the real toggle switch or
    # the emulated fan power switch (toggle-less fans, e.g. the FV05) —
    # never both; dimmers surface on/off through the light platform.
    if (has_power_toggle and not device.is_dimmer) or (
        fan_components and not has_toggle
    ):
        slots.append("power")
    if host_has("level"):
        slots.append("main_light")
    if host_has("light"):
        slots.append("fan_light")
    # ``speed`` rides on ``mode``: firmware enables both for exactly the
    # IB-series, and only ``mode`` is declared in the summary functions
    # list (``speed`` merely leaks in from state after the first poll).
    if host_has("mode"):
        slots.append("fan_mode")
        slots.append("fan_speed")
    if host_has("boost"):
        slots.append("boost")
        slots.append("boost_duration")
    # The override switch rides on the host toggle (see switch.py);
    # toggle-less fans get only the CFM + duration selects.
    if fan_has_timer and has_toggle:
        slots.append("speed_override")
    if fan_has_timer:
        # One CFM select per direction; both share the slot number and
        # order alphabetically within it (exhaust before supply).
        # Gated on ``timer`` like the rest of the override group — ERVs
        # have airflow datapoints but no per-direction control.
        slots.append("speed_override_cfm")
        slots.append("speed_override_duration")
    if has_load_timer:
        slots.append("load_timer")
        slots.append("timer_advance")
    if device.insert_type == InsertType.USB:
        slots.append("usb")
    if any("cled" in c.functions for c in insert_components):
        slots.append("guide_light")
    if device.insert_type == InsertType.VIDEO:
        slots.append("rtsp")
    if "debugEnabled" in config.get("configServer", {}):
        slots.append("http")
    if "cloud_connection" in config.get("mqtt", {}):
        slots.append("mqtt")
    return slots


def numbered_name(device: Any, slot: str, base_name: str) -> str:
    """Return ``base_name`` with this device's ordering prefix.

    Numbers are zero-padded to the widest index so devices with ten or
    more controls still sort correctly ("02." < "10."); with nine or
    fewer the prefix stays single-digit. An unknown slot falls back to
    the bare name (it will sort after the numbered controls) rather
    than failing setup.
    """
    slots = _control_slots(device)
    if slot not in slots:
        return base_name
    width = len(str(len(slots)))
    return f"{str(slots.index(slot) + 1).zfill(width)}. {base_name}"
