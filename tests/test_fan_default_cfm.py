"""Default CFM regression tests using HA entities and a mocked coordinator.

Run with ``python -m unittest discover -s tests`` after installing
Home Assistant and the python-swidget version in manifest.json.
"""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from custom_components.swidget.select import SwidgetFanDefaultCfmSelect


def make_select(airflow, default=0, role="exhaust"):
    """Build an entity with synthetic state and no connection to a fan."""
    config_key = {"exhaust": "defaultEa", "supply": "defaultSa"}[role]
    coordinator = MagicMock()
    coordinator.config_entry = None
    coordinator.last_update_success = True
    coordinator.async_apply_device_config = AsyncMock()
    coordinator.device = SimpleNamespace(
        mac_address="001122334455",
        friendly_name="Test fan",
        device_type="pesna_fv05",
        insert_type="control",
        model="FAN_PICO_S3",
        version="test",
        uri_scheme="http",
        ip_address="127.0.0.1",
        assemblies={
            "host": SimpleNamespace(
                components={"1": SimpleNamespace(functions={role: airflow})}
            )
        },
        device_config=SimpleNamespace(
            config={"host": {"components": {"1": {config_key: default}}}}
        ),
    )
    return SwidgetFanDefaultCfmSelect(coordinator, "1", role, include_role_in_name=True)


class DefaultCfmStateTests(unittest.TestCase):
    """Verify the configured default, independently of the running speed."""

    def test_zero_default_is_off_while_fan_is_running(self):
        for role in ("exhaust", "supply"):
            for default in (0, "0"):
                with self.subTest(role=role, default=default):
                    entity = make_select(
                        {"cfm": 150, "allowed": [0, 50, 100, 150]}, default, role
                    )
                    self.assertEqual(
                        entity.options, ["Off", "50 CFM", "100 CFM", "150 CFM"]
                    )
                    self.assertEqual(entity.current_option, "Off")
                    self.assertEqual(entity.state, "Off")
                    self.assertTrue(entity.available)

    def test_off_is_only_allowed_option(self):
        entity = make_select({"cfm": 0, "allowed": [0]})
        self.assertEqual(entity.options, ["Off"])
        self.assertEqual(entity.state, "Off")
        self.assertTrue(entity.available)

    def test_nonzero_default_is_preserved(self):
        entity = make_select({"cfm": 150, "allowed": [0, 50, 100, 150]}, 100)
        self.assertEqual(entity.state, "100 CFM")

    def test_fan_without_zero_support_does_not_offer_off(self):
        entity = make_select({"allowed": [50, 100, 150]})
        self.assertEqual(entity.options, ["50 CFM", "100 CFM", "150 CFM"])
        self.assertIsNone(entity.current_option)

    def test_range_based_fan_keeps_existing_options(self):
        entity = make_select({"allowed_range": {"min": 55, "max": 85}}, 70)
        self.assertEqual(
            entity.options, ["55 CFM", "60 CFM", "70 CFM", "80 CFM", "85 CFM"]
        )
        self.assertEqual(entity.state, "70 CFM")
        entity.coordinator.device.device_config.config["host"]["components"]["1"][
            "defaultEa"
        ] = 0
        self.assertIsNone(entity.current_option)

    def test_unsupported_default_remains_unknown(self):
        entity = make_select({"allowed": [0, 50, 100, 150]}, 75)
        self.assertIsNone(entity.current_option)

    def test_missing_or_invalid_config_is_not_off(self):
        for default in (None, "invalid"):
            with self.subTest(default=default):
                entity = make_select({"allowed": [0, 50, 100, 150]}, default)
                self.assertIsNone(entity.current_option)
                self.assertFalse(entity.available)
        entity.coordinator.device.device_config = None
        self.assertIsNone(entity.current_option)
        self.assertFalse(entity.available)

    def test_missing_airflow_is_not_off(self):
        for airflow in (None, {}, {"allowed": "unavailable"}):
            with self.subTest(airflow=airflow):
                entity = make_select(airflow)
                self.assertEqual(entity.options, [])
                self.assertIsNone(entity.current_option)
                self.assertFalse(entity.available)


class DefaultCfmWriteTests(unittest.IsolatedAsyncioTestCase):
    """Verify selections persist the correct config field, without commands."""

    async def test_select_option_writes_config(self):
        for role, config_key in (("exhaust", "defaultEa"), ("supply", "defaultSa")):
            for option, value in (("Off", 0), ("100 CFM", 100)):
                with self.subTest(role=role, option=option):
                    entity = make_select({"allowed": [0, 50, 100, 150]}, role=role)
                    await entity.async_select_option(option)
                    entity.coordinator.async_apply_device_config.assert_awaited_once_with(
                        {"host": {"components": {"1": {config_key: value}}}}
                    )


if __name__ == "__main__":
    unittest.main()
