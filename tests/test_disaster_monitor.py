"""Unit tests for DisasterMonitor.

Tests cover:
- Threshold detection for smoke and gas levels
- MQTT alert publishing with QoS 2
- from_config factory method
- Edge cases (exactly at threshold, below threshold)

Requirements: 14.1, 14.2
"""

import pytest
from unittest.mock import MagicMock, call

from src.disaster_monitor import DisasterMonitor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SMOKE_THRESHOLD = 100.0
GAS_THRESHOLD = 50.0


@pytest.fixture
def monitor():
    """DisasterMonitor without MQTT publisher."""
    return DisasterMonitor(smoke_threshold=SMOKE_THRESHOLD, gas_threshold=GAS_THRESHOLD)


@pytest.fixture
def mock_publisher():
    return MagicMock()


@pytest.fixture
def monitor_with_publisher(mock_publisher):
    return DisasterMonitor(
        smoke_threshold=SMOKE_THRESHOLD,
        gas_threshold=GAS_THRESHOLD,
        mqtt_publisher=mock_publisher,
    )


# ---------------------------------------------------------------------------
# check_smoke_level tests
# ---------------------------------------------------------------------------

class TestCheckSmokeLevel:
    def test_returns_false_when_below_threshold(self, monitor):
        assert monitor.check_smoke_level(50.0) is False

    def test_returns_false_when_at_threshold(self, monitor):
        # strictly greater than, so equal should be False
        assert monitor.check_smoke_level(SMOKE_THRESHOLD) is False

    def test_returns_true_when_above_threshold(self, monitor):
        assert monitor.check_smoke_level(SMOKE_THRESHOLD + 0.01) is True

    def test_returns_true_for_large_value(self, monitor):
        assert monitor.check_smoke_level(9999.0) is True

    def test_returns_false_for_zero(self, monitor):
        assert monitor.check_smoke_level(0.0) is False

    def test_publishes_alert_when_exceeded(self, monitor_with_publisher, mock_publisher):
        monitor_with_publisher.check_smoke_level(150.0, sensor_location="bedroom")
        mock_publisher.publish_disaster_alert.assert_called_once_with(
            alert_type="smoke",
            sensor_location="bedroom",
            concentration=150.0,
            threshold=SMOKE_THRESHOLD,
            qos=2,
        )

    def test_no_alert_when_not_exceeded(self, monitor_with_publisher, mock_publisher):
        monitor_with_publisher.check_smoke_level(50.0, sensor_location="bedroom")
        mock_publisher.publish_disaster_alert.assert_not_called()

    def test_no_alert_when_at_threshold(self, monitor_with_publisher, mock_publisher):
        monitor_with_publisher.check_smoke_level(SMOKE_THRESHOLD, sensor_location="hall")
        mock_publisher.publish_disaster_alert.assert_not_called()

    def test_no_publisher_does_not_raise(self, monitor):
        # Should not raise even without a publisher
        result = monitor.check_smoke_level(200.0, sensor_location="kitchen")
        assert result is True

    def test_default_sensor_location_used(self, monitor_with_publisher, mock_publisher):
        monitor_with_publisher.check_smoke_level(200.0)
        args = mock_publisher.publish_disaster_alert.call_args
        assert args.kwargs["sensor_location"] == "unknown"


# ---------------------------------------------------------------------------
# check_gas_level tests
# ---------------------------------------------------------------------------

class TestCheckGasLevel:
    def test_returns_false_when_below_threshold(self, monitor):
        assert monitor.check_gas_level(10.0) is False

    def test_returns_false_when_at_threshold(self, monitor):
        assert monitor.check_gas_level(GAS_THRESHOLD) is False

    def test_returns_true_when_above_threshold(self, monitor):
        assert monitor.check_gas_level(GAS_THRESHOLD + 0.01) is True

    def test_returns_true_for_large_value(self, monitor):
        assert monitor.check_gas_level(5000.0) is True

    def test_returns_false_for_zero(self, monitor):
        assert monitor.check_gas_level(0.0) is False

    def test_publishes_alert_when_exceeded(self, monitor_with_publisher, mock_publisher):
        monitor_with_publisher.check_gas_level(80.0, sensor_location="kitchen")
        mock_publisher.publish_disaster_alert.assert_called_once_with(
            alert_type="gas",
            sensor_location="kitchen",
            concentration=80.0,
            threshold=GAS_THRESHOLD,
            qos=2,
        )

    def test_no_alert_when_not_exceeded(self, monitor_with_publisher, mock_publisher):
        monitor_with_publisher.check_gas_level(20.0, sensor_location="kitchen")
        mock_publisher.publish_disaster_alert.assert_not_called()

    def test_no_alert_when_at_threshold(self, monitor_with_publisher, mock_publisher):
        monitor_with_publisher.check_gas_level(GAS_THRESHOLD, sensor_location="hall")
        mock_publisher.publish_disaster_alert.assert_not_called()

    def test_no_publisher_does_not_raise(self, monitor):
        result = monitor.check_gas_level(100.0, sensor_location="garage")
        assert result is True

    def test_default_sensor_location_used(self, monitor_with_publisher, mock_publisher):
        monitor_with_publisher.check_gas_level(100.0)
        args = mock_publisher.publish_disaster_alert.call_args
        assert args.kwargs["sensor_location"] == "unknown"


# ---------------------------------------------------------------------------
# from_config factory tests
# ---------------------------------------------------------------------------

class TestFromConfig:
    def test_loads_thresholds_from_config(self):
        config = {
            "disaster_monitoring": {
                "smoke_threshold": 200.0,
                "gas_threshold": 75.0,
            }
        }
        dm = DisasterMonitor.from_config(config)
        assert dm.smoke_threshold == 200.0
        assert dm.gas_threshold == 75.0

    def test_uses_defaults_when_section_missing(self):
        dm = DisasterMonitor.from_config({})
        assert dm.smoke_threshold == 100.0
        assert dm.gas_threshold == 50.0

    def test_passes_publisher_to_instance(self):
        publisher = MagicMock()
        config = {"disaster_monitoring": {"smoke_threshold": 100.0, "gas_threshold": 50.0}}
        dm = DisasterMonitor.from_config(config, mqtt_publisher=publisher)
        assert dm._mqtt_publisher is publisher

    def test_loads_from_real_config_values(self):
        """Matches the actual config/config.json values."""
        config = {
            "disaster_monitoring": {
                "smoke_threshold": 100.0,
                "gas_threshold": 50.0,
            }
        }
        dm = DisasterMonitor.from_config(config)
        assert dm.smoke_threshold == 100.0
        assert dm.gas_threshold == 50.0


# ---------------------------------------------------------------------------
# Alert QoS level tests
# ---------------------------------------------------------------------------

class TestAlertQoS:
    def test_smoke_alert_uses_qos_2(self, monitor_with_publisher, mock_publisher):
        monitor_with_publisher.check_smoke_level(999.0, "room")
        _, kwargs = mock_publisher.publish_disaster_alert.call_args
        assert kwargs["qos"] == 2

    def test_gas_alert_uses_qos_2(self, monitor_with_publisher, mock_publisher):
        monitor_with_publisher.check_gas_level(999.0, "room")
        _, kwargs = mock_publisher.publish_disaster_alert.call_args
        assert kwargs["qos"] == 2
