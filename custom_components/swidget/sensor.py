"""Sensor platform — diagnostic structure sensors plus insert measurements."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    CONCENTRATION_PARTS_PER_BILLION,
    CONCENTRATION_PARTS_PER_MILLION,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfLength,
    UnitOfPower,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SwidgetDataUpdateCoordinator
from .entity import SwidgetEntity


@dataclass(frozen=True, kw_only=True)
class SwidgetInsertSensorDescription(SensorEntityDescription):
    """Description for a sensor that reads a single field of an insert function.

    ``function`` is the function tag in ``component.functions`` (e.g.
    ``"temperature"``); ``field`` is the key inside that function's
    datapoint dict (e.g. ``"now"``). Adding a new measurement here is
    almost always a one-entry tuple addition below.
    """

    function: str
    field: str = "now"


@dataclass(frozen=True, kw_only=True)
class SwidgetHostSensorDescription(SensorEntityDescription):
    """Description for a sensor reading a host-component function.

    Mirrors ``SwidgetInsertSensorDescription`` but targets host-side
    functions (the fan-only ``mode``/``status``/``speed``/``indoors``/
    ``outdoors``/``boost``/``dutyCycle``/``error`` set). ``field`` is
    optional — when the function value is a bare scalar (e.g. ``mode``
    is reported as ``"continuous"`` directly), set ``field=None``.
    """

    function: str
    field: str | None = "now"
    # What to report when the underlying field is absent. Several fan
    # fields are firmware-omitted when "nothing is happening"
    # (``boost.minutes`` only while a boost timer runs, the entire
    # ``timer`` object only while a fan timer is armed, ``error.code``
    # only while there is an error). Setting a default here lets the
    # entity surface a meaningful resting value instead of "Unknown".
    default_value: float | int | str | None = None


# Catalogue of insert measurement sensors. New sensors usually require
# only an entry in this tuple — the generic SwidgetInsertSensor below
# handles the lookup, error gating, and unique_id wiring.
INSERT_SENSOR_DESCRIPTIONS: tuple[SwidgetInsertSensorDescription, ...] = (
    SwidgetInsertSensorDescription(
        key="temperature",
        function="temperature",
        name="Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    SwidgetInsertSensorDescription(
        key="humidity",
        function="humidity",
        name="Humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
    ),
    # AIR QUALITY insert — ``aq`` component carries iaq/eco2/tvoc; the
    # companion ``pressure`` component carries ``bp``. The IAQ index is
    # the Bosch BSEC scale (0–500), which doesn't quite match HA's AQI
    # device class semantics, so we leave its device_class unset.
    SwidgetInsertSensorDescription(
        key="iaq",
        function="iaq",
        name="Air quality index",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
    ),
    SwidgetInsertSensorDescription(
        key="eco2",
        function="eco2",
        name="Equivalent CO2",
        device_class=SensorDeviceClass.CO2,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
    ),
    SwidgetInsertSensorDescription(
        key="tvoc",
        function="tvoc",
        name="TVOC",
        device_class=SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS_PARTS,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=CONCENTRATION_PARTS_PER_BILLION,
    ),
    SwidgetInsertSensorDescription(
        key="pressure",
        function="bp",
        name="Pressure",
        device_class=SensorDeviceClass.PRESSURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPressure.HPA,
        suggested_display_precision=1,
    ),
    # CO2 / PM inserts use ``conc`` rather than ``now`` for the primary
    # reading. Each also reports a ``qual`` integer grade — not surfaced
    # as a separate entity for now to keep the sensor count manageable.
    SwidgetInsertSensorDescription(
        key="co2",
        function="co2",
        field="conc",
        name="CO2",
        device_class=SensorDeviceClass.CO2,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
    ),
    SwidgetInsertSensorDescription(
        key="pm1_0",
        function="pm1_0",
        field="conc",
        name="PM1.0",
        device_class=SensorDeviceClass.PM1,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    ),
    SwidgetInsertSensorDescription(
        key="pm2_5",
        function="pm2_5",
        field="conc",
        name="PM2.5",
        device_class=SensorDeviceClass.PM25,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    ),
    SwidgetInsertSensorDescription(
        key="pm10",
        function="pm10",
        field="conc",
        name="PM10",
        device_class=SensorDeviceClass.PM10,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    ),
    # Distance / proximity insert. ``distance`` is only emitted while
    # ``detected`` is true, so the sensor will show "Unknown" between
    # detections — the binary sensor for ``detected`` is the right
    # signal for "is something there".
    SwidgetInsertSensorDescription(
        key="distance",
        function="proximity",
        field="distance",
        name="Distance",
        device_class=SensorDeviceClass.DISTANCE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfLength.MILLIMETERS,
    ),
)


# Host-side fan sensors. Each entry materialises only when the function
# tag appears in the host component's summary, so non-fan hosts don't
# get any of these. ``field=None`` denotes a function whose datapoint
# is a bare scalar (``mode``/``status``/``speed``).
HOST_FAN_SENSOR_DESCRIPTIONS: tuple[SwidgetHostSensorDescription, ...] = (
    SwidgetHostSensorDescription(
        key="fan_mode",
        function="mode",
        field=None,
        name="Mode",
    ),
    SwidgetHostSensorDescription(
        key="fan_status",
        function="status",
        field=None,
        name="Status",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    SwidgetHostSensorDescription(
        key="fan_speed",
        function="speed",
        field=None,
        name="Speed",
    ),
    SwidgetHostSensorDescription(
        key="fan_error",
        function="error",
        field="code",
        name="Error",
        entity_category=EntityCategory.DIAGNOSTIC,
        default_value="OK",
    ),
    SwidgetHostSensorDescription(
        key="exhaust_cfm",
        function="exhaust",
        field="cfm",
        name="Exhaust CFM",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfVolumeFlowRate.CUBIC_FEET_PER_MINUTE,
    ),
    SwidgetHostSensorDescription(
        key="supply_cfm",
        function="supply",
        field="cfm",
        name="Supply CFM",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfVolumeFlowRate.CUBIC_FEET_PER_MINUTE,
    ),
    SwidgetHostSensorDescription(
        key="indoor_temperature",
        function="indoors",
        field="temperature",
        name="Indoor temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    SwidgetHostSensorDescription(
        key="indoor_humidity",
        function="indoors",
        field="humidity",
        name="Indoor humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
    ),
    SwidgetHostSensorDescription(
        key="outdoor_temperature",
        function="outdoors",
        field="temperature",
        name="Outdoor temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
    ),
    SwidgetHostSensorDescription(
        key="outdoor_humidity",
        function="outdoors",
        field="humidity",
        name="Outdoor humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
    ),
    SwidgetHostSensorDescription(
        key="duty_cycle",
        function="dutyCycle",
        field="minutes",
        name="Duty cycle",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTime.MINUTES,
    ),
    SwidgetHostSensorDescription(
        key="fan_timer_remaining",
        function="timer",
        field="minutes",
        name="Fan timer remaining",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        default_value=0,
    ),
    SwidgetHostSensorDescription(
        key="boost_remaining",
        function="boost",
        field="minutes",
        name="Boost remaining",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        default_value=0,
    ),
    SwidgetHostSensorDescription(
        key="boost_mode",
        function="boost",
        field="mode",
        name="Boost mode",
    ),
    SwidgetHostSensorDescription(
        key="balancing_offset",
        function="balancing",
        field="offset",
        name="Balancing offset",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


# Host-side power sensors. Materialise per host component that declares
# the ``power`` function — that's the firmware's signal that the
# component has current monitoring (a duplex outlet's measurement-only
# socket also declares ``power`` even though it has no toggle).
HOST_POWER_SENSOR_DESCRIPTIONS: tuple[SwidgetHostSensorDescription, ...] = (
    SwidgetHostSensorDescription(
        key="power_current",
        function="power",
        field="current",
        name="Current power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=1,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Swidget diagnostic sensors from a config entry."""
    coordinator: SwidgetDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        SwidgetHostTypeSensor(coordinator),
        SwidgetInsertTypeSensor(coordinator),
        SwidgetRssiSensor(coordinator),
    ]
    # One sensor per component on each assembly so the function list for
    # each component is visible directly on the device page (the state)
    # rather than buried under attributes.
    for assembly_key in ("host", "insert"):
        assembly = coordinator.device.assemblies.get(assembly_key)
        if assembly is None:
            continue
        for component_id in assembly.components:
            entities.append(
                SwidgetComponentSensor(coordinator, assembly_key, component_id)
            )
    # Active timer level (1-3) and human-readable mode for any host
    # component that exposes the 3-tier load timer. Skip fan hosts —
    # they share the ``timer`` tag but with a different (single
    # ``minutes`` field) shape, surfaced via the fan-specific
    # ``fan_timer_remaining`` sensor / ``Fan timer`` slider instead.
    host = coordinator.device.assemblies.get("host")
    if host is not None:
        for component_id, component in host.components.items():
            is_fan = (
                "exhaust" in component.functions
                or "supply" in component.functions
            )
            if "timer" in component.functions and not is_fan:
                entities.append(
                    SwidgetTimerLevelSensor(coordinator, component_id)
                )
                entities.append(
                    SwidgetTimerStatusSensor(coordinator, component_id)
                )
    # Insert measurement sensors driven by the description catalogue.
    # Detect by function presence on each insert component so a sensor
    # only materialises when the underlying datapoint actually exists.
    insert = coordinator.device.assemblies.get("insert")
    if insert is not None:
        for component_id, component in insert.components.items():
            for description in INSERT_SENSOR_DESCRIPTIONS:
                if description.function in component.functions:
                    entities.append(
                        SwidgetInsertSensor(coordinator, component_id, description)
                    )
    # Host-side fan sensors. Gated *both* on the host being a fan
    # (exhaust/supply present) AND on the specific function being
    # listed — the ``timer`` tag in particular is shared with the
    # 3-tier load-timer hosts but with a different payload shape, so
    # the function-presence check alone would mis-attach the
    # ``Fan timer remaining`` sensor to a 20/40/60 timer switch.
    if host is not None:
        for component_id, component in host.components.items():
            is_fan = (
                "exhaust" in component.functions
                or "supply" in component.functions
            )
            if not is_fan:
                continue
            for description in HOST_FAN_SENSOR_DESCRIPTIONS:
                if description.function in component.functions:
                    entities.append(
                        SwidgetHostFunctionSensor(
                            coordinator, component_id, description
                        )
                    )
    # Host power sensors. Any component that declares the ``power``
    # function gets the trio (current / today's average / average-on).
    # Duplex outlets declare ``power`` on both sockets — including the
    # measurement-only one — so we iterate the full component list and
    # name them based on whether the component is controllable:
    #   - power + toggle → the controlled socket / load (no suffix when
    #     it's the only controlled one; ``(component N)`` if there are
    #     several, e.g. multi-load dimmers)
    #   - power but no toggle → the duplex's measurement-only socket,
    #     surfaced as ``(uncontrolled outlet)``
    if host is not None:
        power_components = [
            cid
            for cid, component in host.components.items()
            if "power" in component.functions
        ]
        controllable = [
            cid
            for cid in power_components
            if "toggle" in host.components[cid].functions
        ]
        for component_id in power_components:
            component = host.components[component_id]
            if "toggle" not in component.functions:
                name_suffix = "uncontrolled outlet"
            elif len(controllable) > 1:
                name_suffix = f"component {component_id}"
            else:
                name_suffix = ""
            for description in HOST_POWER_SENSOR_DESCRIPTIONS:
                entities.append(
                    SwidgetHostPowerSensor(
                        coordinator, component_id, description, name_suffix
                    )
                )
    async_add_entities(entities)


def _enum_value(value: object) -> str | None:
    """Return the underlying string for an Enum, or str() the value."""
    inner = getattr(value, "value", value)
    if inner is None or inner == -1:
        return None
    return str(inner)


def _assembly_type_label(
    coordinator: SwidgetDataUpdateCoordinator,
    enum_value: object,
    assembly_key: str,
) -> str | None:
    """Resolve an assembly type label.

    Prefer the SDK enum's friendly value; fall back to the raw string
    the device sent so an SDK that's behind on a new firmware type
    still surfaces something useful instead of "Unknown".
    """
    label = _enum_value(enum_value)
    if label is not None:
        return label
    assembly = coordinator.device.assemblies.get(assembly_key)
    if assembly is None:
        return None
    return getattr(assembly, "type", None) or None


class SwidgetInsertSensor(SwidgetEntity, SensorEntity):
    """Generic insert sensor driven by a SwidgetInsertSensorDescription.

    Reads ``description.field`` out of ``component.functions[description.function]``
    and reports unavailable when the function reports ``error != 0``
    (the SDK's MissingHardware signal).
    """

    entity_description: SwidgetInsertSensorDescription

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        component_id: str,
        description: SwidgetInsertSensorDescription,
    ) -> None:
        """Initialize an insert sensor for the given description."""
        super().__init__(coordinator)
        self.entity_description = description
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_insert_{component_id}_{description.key}"
        )

    def _function_state(self) -> dict | None:
        """Return the live datapoint dict for this function, or None."""
        try:
            value = (
                self.coordinator.device.assemblies["insert"]
                .components[self._component_id]
                .functions.get(self.entity_description.function)
            )
        except (KeyError, AttributeError):
            return None
        return value if isinstance(value, dict) else None

    @property
    def available(self) -> bool:
        """Available once we've seen a valid (error-free) datapoint."""
        state = self._function_state()
        if state is None:
            return False
        # Per SDK: error != 0 means MissingHardware; readings aren't
        # meaningful in that case, so report unavailable rather than
        # surfacing stale or zeroed values.
        return int(state.get("error") or 0) == 0

    @property
    def native_value(self) -> float | int | str | None:
        """Return the field value pulled from the function datapoint."""
        state = self._function_state()
        if state is None:
            return None
        value = state.get(self.entity_description.field)
        if value is None:
            return None
        if isinstance(value, bool):
            # Booleans need to be excluded explicitly because they're a
            # subclass of int — handing one to a SensorEntity would
            # render as 1/0 and lie about the device class.
            return None
        if isinstance(value, (int, float, str)):
            return value
        return None


class SwidgetHostFunctionSensor(SwidgetEntity, SensorEntity):
    """Generic host-component sensor driven by SwidgetHostSensorDescription.

    Supports two payload shapes: nested (function value is a dict and
    the sensor reads ``description.field`` from it) and bare (function
    value is the scalar itself; ``description.field`` is ``None``). The
    fan-only ``mode``/``status``/``speed`` tags use the bare form per
    the SDK datapoint spec.
    """

    entity_description: SwidgetHostSensorDescription

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        component_id: str,
        description: SwidgetHostSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_{description.key}"
        )

    def _function_value(self) -> object | None:
        try:
            return (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
                .functions.get(self.entity_description.function)
            )
        except (KeyError, AttributeError):
            return None

    @property
    def available(self) -> bool:
        # When the description supplies a default we're always able to
        # report *something* (the default), even if the firmware has
        # omitted the function or field — surface that as available so
        # the card doesn't show "Unavailable" for a normal resting state.
        if self.entity_description.default_value is not None:
            return True
        return self._function_value() is not None

    @property
    def native_value(self) -> float | int | str | None:
        default = self.entity_description.default_value
        value = self._function_value()
        if value is None:
            return default
        if self.entity_description.field is None:
            # Bare-scalar functions (mode/status/speed) report directly.
            if isinstance(value, bool):
                return default
            if isinstance(value, (int, float, str)):
                return value
            return default
        if not isinstance(value, dict):
            return default
        field_value = value.get(self.entity_description.field)
        if field_value is None or isinstance(field_value, bool):
            return default
        if isinstance(field_value, (int, float, str)):
            return field_value
        return default


class SwidgetHostPowerSensor(SwidgetHostFunctionSensor):
    """Host power sensor with per-component name disambiguation.

    Duplex outlets declare ``power`` on both sockets, so two of these
    can land on a single device. The caller passes a name suffix —
    ``"uncontrolled outlet"`` for the duplex's measurement-only socket
    (no ``toggle``), ``"component N"`` for multi-load setups, or empty
    when no disambiguation is needed.
    """

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        component_id: str,
        description: SwidgetHostSensorDescription,
        name_suffix: str,
    ) -> None:
        super().__init__(coordinator, component_id, description)
        if name_suffix:
            self._attr_name = f"{description.name} ({name_suffix})"


class SwidgetHostTypeSensor(SwidgetEntity, SensorEntity):
    """Reports the host (base device) type, e.g. switch, outlet, dimmer.

    Named "Host" (not "Host type") so it sorts as a prefix before the
    "Host component N" entities on the device page — HA orders diagnostic
    entities alphabetically by display name.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Host"

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the host type sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.mac_address}_host_type"

    @property
    def native_value(self) -> str | None:
        """Return the host device type."""
        return _assembly_type_label(
            self.coordinator, self.coordinator.device.device_type, "host"
        )


class SwidgetInsertTypeSensor(SwidgetEntity, SensorEntity):
    """Reports the insert type, e.g. USB, TEMP HUMI MOTION, video.

    Named "Insert" (not "Insert type") so it sorts as a prefix before
    the "Insert component N" entities on the device page.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Insert"

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the insert type sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.mac_address}_insert_type"

    @property
    def native_value(self) -> str | None:
        """Return the insert type."""
        return _assembly_type_label(
            self.coordinator, self.coordinator.device.insert_type, "insert"
        )


class SwidgetRssiSensor(SwidgetEntity, SensorEntity):
    """WiFi signal strength in dBm.

    The SDK populates ``device.rssi`` from ``state["connection"]["rssi"]``
    inside ``process_state`` and falls back to ``0`` on exception. We
    treat ``0`` (and a missing attribute, before the first state
    arrives) as unavailable since 0 dBm is non-physical for a connected
    WiFi device — it's the SDK's missing-data sentinel.

    Disabled by default to match the HA convention for diagnostic WiFi
    RSSI sensors.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_name = "Signal strength"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT

    def __init__(self, coordinator: SwidgetDataUpdateCoordinator) -> None:
        """Initialize the RSSI sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device.mac_address}_rssi"

    def _rssi(self) -> int | None:
        value = getattr(self.coordinator.device, "rssi", None)
        if value in (None, 0):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @property
    def available(self) -> bool:
        return self._rssi() is not None

    @property
    def native_value(self) -> int | None:
        return self._rssi()


class SwidgetTimerLevelSensor(SwidgetEntity, SensorEntity):
    """Active timer level (1-3) reported by a host load timer.

    Reads ``buttonLevel`` from the timer datapoint. Returns 0 when no
    timer is running. Useful for automations that branch on which
    preset is currently active.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Timer level"

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the timer-level sensor."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_timer_level"
        )

    @property
    def native_value(self) -> int | None:
        """Return the active button-initiated timer level, or 0 if none."""
        try:
            value = (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
                .functions.get("timer")
            )
        except (KeyError, AttributeError):
            return None
        if not isinstance(value, dict):
            return None
        return int(value.get("buttonLevel") or 0)


class SwidgetTimerStatusSensor(SwidgetEntity, SensorEntity):
    """Human-readable timer mode: "Off", "Timer N min", or "Permanent on".

    Pairs with the Timer slider so the device page reads in plain
    English at a glance, since the slider value alone (e.g. "255 min")
    doesn't convey the force-on mode.
    """

    _attr_name = "Timer status"

    def __init__(
        self, coordinator: SwidgetDataUpdateCoordinator, component_id: str
    ) -> None:
        """Initialize the timer-status sensor."""
        super().__init__(coordinator)
        self._component_id = component_id
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_host_{component_id}_timer_status"
        )

    @property
    def native_value(self) -> str | None:
        """Return a plain-English description of the current timer mode."""
        try:
            value = (
                self.coordinator.device.assemblies["host"]
                .components[self._component_id]
                .functions.get("timer")
            )
        except (KeyError, AttributeError):
            return None
        if not isinstance(value, dict):
            return None
        if int(value.get("buttonLevel") or 0) == 255:
            return "Permanent on"
        minutes = int(value.get("buttonTimer") or 0)
        if minutes > 0:
            return f"Timer {minutes} min"
        return "Off"

    @property
    def icon(self) -> str | None:
        """Match the slider's icon vocabulary so the two read together."""
        state = self.native_value
        if state == "Permanent on":
            return "mdi:infinity"
        if state and state.startswith("Timer "):
            return "mdi:timer-sand"
        if state == "Off":
            return "mdi:timer-off-outline"
        return None


class SwidgetComponentSensor(SwidgetEntity, SensorEntity):
    """One per component: state is the comma-joined function names."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: SwidgetDataUpdateCoordinator,
        assembly_key: str,
        component_id: str,
    ) -> None:
        """Initialize a single-component diagnostic sensor."""
        super().__init__(coordinator)
        self._assembly_key = assembly_key
        self._component_id = component_id
        self._attr_name = f"{assembly_key.capitalize()} component {component_id}"
        self._attr_unique_id = (
            f"{coordinator.device.mac_address}_{assembly_key}_component_{component_id}"
        )

    def _component(self):
        assembly = self.coordinator.device.assemblies.get(self._assembly_key)
        if assembly is None:
            return None
        return assembly.components.get(self._component_id)

    @property
    def native_value(self) -> str | None:
        """Return the comma-joined function names for this component.

        Reads ``summary_functions`` rather than ``functions.keys()`` so
        the value is the declared schema, not the runtime dict that
        gets state-only keys merged in by ``process_state`` (and would
        otherwise flap as those keys come and go on each summary rebuild).
        """
        component = self._component()
        if component is None:
            return None
        names = sorted(getattr(component, "summary_functions", ()) or ())
        return ", ".join(names) if names else "(no functions)"

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Expose function names plus their current values for inspection."""
        component = self._component()
        if component is None:
            return None
        return {
            "functions": dict(component.functions),
        }
