"""Unit tests for EnvironmentController.

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6
"""

import pytest
from unittest.mock import MagicMock

from src.data_structures import SleepStage
from src.environment_controller import EnvironmentController


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALL_STAGES = list(SleepStage)
REQUIRED_KEYS = {"control_type", "target_value", "priority", "timestamp"}


def _make_controller():
    return EnvironmentController()


# ---------------------------------------------------------------------------
# Return-shape tests (Req 13.1)
# ---------------------------------------------------------------------------

class TestReturnShape:
    def test_lighting_has_required_keys(self):
        ctrl = _make_controller()
        for stage in ALL_STAGES:
            result = ctrl.generate_lighting_control(stage)
            assert REQUIRED_KEYS.issubset(result.keys()), f"Missing keys for {stage}"

    def test_temperature_has_required_keys(self):
        ctrl = _make_controller()
        for stage in ALL_STAGES:
            result = ctrl.generate_temperature_control(stage)
            assert REQUIRED_KEYS.issubset(result.keys()), f"Missing keys for {stage}"

    def test_humidity_has_required_keys(self):
        ctrl = _make_controller()
        for stage in ALL_STAGES:
            result = ctrl.generate_humidity_control(stage)
            assert REQUIRED_KEYS.issubset(result.keys()), f"Missing keys for {stage}"

    def test_control_type_field_lighting(self):
        ctrl = _make_controller()
        assert ctrl.generate_lighting_control(SleepStage.AWAKE)["control_type"] == "lighting"

    def test_control_type_field_temperature(self):
        ctrl = _make_controller()
        assert ctrl.generate_temperature_control(SleepStage.AWAKE)["control_type"] == "temperature"

    def test_control_type_field_humidity(self):
        ctrl = _make_controller()
        assert ctrl.generate_humidity_control(SleepStage.AWAKE)["control_type"] == "humidity"


# ---------------------------------------------------------------------------
# Lighting control strategy (Req 13.2)
# ---------------------------------------------------------------------------

class TestLightingControl:
    def test_deep_sleep_lights_off(self):
        ctrl = _make_controller()
        cmd = ctrl.generate_lighting_control(SleepStage.DEEP)
        assert cmd["target_value"] == 0

    def test_rem_sleep_lights_off(self):
        ctrl = _make_controller()
        cmd = ctrl.generate_lighting_control(SleepStage.REM)
        assert cmd["target_value"] == 0

    def test_light_sleep_dim(self):
        ctrl = _make_controller()
        cmd = ctrl.generate_lighting_control(SleepStage.LIGHT)
        assert cmd["target_value"] == 10

    def test_awake_gradual_increase(self):
        ctrl = _make_controller()
        cmd = ctrl.generate_lighting_control(SleepStage.AWAKE)
        assert cmd["target_value"] == 50

    def test_awake_higher_brightness_than_light_sleep(self):
        ctrl = _make_controller()
        awake = ctrl.generate_lighting_control(SleepStage.AWAKE)["target_value"]
        light = ctrl.generate_lighting_control(SleepStage.LIGHT)["target_value"]
        assert awake > light


# ---------------------------------------------------------------------------
# Temperature control strategy (Req 13.3)
# ---------------------------------------------------------------------------

class TestTemperatureControl:
    def test_deep_sleep_target(self):
        ctrl = _make_controller()
        cmd = ctrl.generate_temperature_control(SleepStage.DEEP)
        assert cmd["target_value"] == 19
        assert cmd["min_value"] == 18
        assert cmd["max_value"] == 20

    def test_rem_sleep_target(self):
        ctrl = _make_controller()
        cmd = ctrl.generate_temperature_control(SleepStage.REM)
        assert cmd["target_value"] == 19

    def test_light_sleep_target(self):
        ctrl = _make_controller()
        cmd = ctrl.generate_temperature_control(SleepStage.LIGHT)
        assert cmd["target_value"] == 21
        assert cmd["min_value"] == 20
        assert cmd["max_value"] == 22

    def test_awake_target(self):
        ctrl = _make_controller()
        cmd = ctrl.generate_temperature_control(SleepStage.AWAKE)
        assert cmd["target_value"] == 23
        assert cmd["min_value"] == 22
        assert cmd["max_value"] == 24

    def test_awake_warmer_than_deep(self):
        ctrl = _make_controller()
        awake = ctrl.generate_temperature_control(SleepStage.AWAKE)["target_value"]
        deep = ctrl.generate_temperature_control(SleepStage.DEEP)["target_value"]
        assert awake > deep


# ---------------------------------------------------------------------------
# Humidity control strategy (Req 13.4)
# ---------------------------------------------------------------------------

class TestHumidityControl:
    def test_deep_sleep_target(self):
        ctrl = _make_controller()
        cmd = ctrl.generate_humidity_control(SleepStage.DEEP)
        assert cmd["target_value"] == 55
        assert cmd["min_value"] == 50
        assert cmd["max_value"] == 60

    def test_rem_sleep_target(self):
        ctrl = _make_controller()
        cmd = ctrl.generate_humidity_control(SleepStage.REM)
        assert cmd["target_value"] == 55

    def test_light_sleep_target(self):
        ctrl = _make_controller()
        cmd = ctrl.generate_humidity_control(SleepStage.LIGHT)
        assert cmd["target_value"] == 55

    def test_awake_target(self):
        ctrl = _make_controller()
        cmd = ctrl.generate_humidity_control(SleepStage.AWAKE)
        assert cmd["target_value"] == 50
        assert cmd["min_value"] == 40
        assert cmd["max_value"] == 60


# ---------------------------------------------------------------------------
# Priority ordering (Req 13.5)
# ---------------------------------------------------------------------------

class TestPriority:
    """Deep/REM sleep commands should have higher priority (lower number) than AWAKE."""

    def test_lighting_deep_higher_priority_than_awake(self):
        ctrl = _make_controller()
        deep_p = ctrl.generate_lighting_control(SleepStage.DEEP)["priority"]
        awake_p = ctrl.generate_lighting_control(SleepStage.AWAKE)["priority"]
        assert deep_p < awake_p

    def test_temperature_deep_higher_priority_than_awake(self):
        ctrl = _make_controller()
        deep_p = ctrl.generate_temperature_control(SleepStage.DEEP)["priority"]
        awake_p = ctrl.generate_temperature_control(SleepStage.AWAKE)["priority"]
        assert deep_p < awake_p

    def test_humidity_deep_higher_priority_than_awake(self):
        ctrl = _make_controller()
        deep_p = ctrl.generate_humidity_control(SleepStage.DEEP)["priority"]
        awake_p = ctrl.generate_humidity_control(SleepStage.AWAKE)["priority"]
        assert deep_p < awake_p


# ---------------------------------------------------------------------------
# MQTT integration (Req 13.6)
# ---------------------------------------------------------------------------

class TestMQTTIntegration:
    def _make_mock_publisher(self):
        pub = MagicMock()
        pub.publish_environment_control = MagicMock(return_value={})
        return pub

    def test_lighting_publishes_when_publisher_provided(self):
        pub = self._make_mock_publisher()
        ctrl = EnvironmentController(mqtt_publisher=pub)
        ctrl.generate_lighting_control(SleepStage.DEEP)
        pub.publish_environment_control.assert_called_once_with(
            control_type="lighting", target_value=0, priority=1
        )

    def test_temperature_publishes_when_publisher_provided(self):
        pub = self._make_mock_publisher()
        ctrl = EnvironmentController(mqtt_publisher=pub)
        ctrl.generate_temperature_control(SleepStage.DEEP)
        pub.publish_environment_control.assert_called_once_with(
            control_type="temperature", target_value=19, priority=1
        )

    def test_humidity_publishes_when_publisher_provided(self):
        pub = self._make_mock_publisher()
        ctrl = EnvironmentController(mqtt_publisher=pub)
        ctrl.generate_humidity_control(SleepStage.DEEP)
        pub.publish_environment_control.assert_called_once_with(
            control_type="humidity", target_value=55, priority=1
        )

    def test_no_publish_without_publisher(self):
        """No MQTT calls when no publisher is injected."""
        ctrl = EnvironmentController()
        # Should not raise
        ctrl.generate_lighting_control(SleepStage.AWAKE)
        ctrl.generate_temperature_control(SleepStage.AWAKE)
        ctrl.generate_humidity_control(SleepStage.AWAKE)

    def test_all_stages_publish_lighting(self):
        pub = self._make_mock_publisher()
        ctrl = EnvironmentController(mqtt_publisher=pub)
        for stage in ALL_STAGES:
            ctrl.generate_lighting_control(stage)
        assert pub.publish_environment_control.call_count == len(ALL_STAGES)
