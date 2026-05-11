"""Unit tests for AnomalyHandler.

Requirements: 18.1, 18.2, 18.3, 18.4, 18.5, 18.6, 18.7, 18.8
"""

import time
from unittest.mock import MagicMock

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.anomaly_handler import (
    HR_MAX,
    HR_MAX_RATE_OF_CHANGE,
    HR_MIN,
    MAX_INTERPOLATION_GAP_SECONDS,
    MV_MIN,
    TOPIC_SENSOR_FAULT,
    AnomalyHandler,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def handler() -> AnomalyHandler:
    return AnomalyHandler()


@pytest.fixture
def mock_publisher():
    pub = MagicMock()
    pub.published_messages = []

    def _publish(topic, payload, qos):
        pub.published_messages.append({"topic": topic, "payload": payload, "qos": qos})

    pub._publish.side_effect = _publish
    return pub


@pytest.fixture
def handler_with_publisher(mock_publisher) -> AnomalyHandler:
    return AnomalyHandler(mqtt_publisher=mock_publisher)


# ---------------------------------------------------------------------------
# detect_heart_rate_anomaly – range checks (Req 18.1)
# ---------------------------------------------------------------------------


class TestDetectHeartRateAnomalyRange:
    def test_valid_heart_rate_returns_false(self, handler):
        assert handler.detect_heart_rate_anomaly(72.0, 70.0, 1.0) is False

    def test_lower_boundary_valid(self, handler):
        assert handler.detect_heart_rate_anomaly(HR_MIN, HR_MIN, 1.0) is False

    def test_upper_boundary_valid(self, handler):
        assert handler.detect_heart_rate_anomaly(HR_MAX, HR_MAX, 1.0) is False

    def test_below_lower_boundary_is_anomaly(self, handler):
        assert handler.detect_heart_rate_anomaly(HR_MIN - 0.1, 30.0, 1.0) is True

    def test_above_upper_boundary_is_anomaly(self, handler):
        assert handler.detect_heart_rate_anomaly(HR_MAX + 0.1, 200.0, 1.0) is True

    def test_zero_heart_rate_is_anomaly(self, handler):
        assert handler.detect_heart_rate_anomaly(0.0, 60.0, 1.0) is True

    def test_negative_heart_rate_is_anomaly(self, handler):
        assert handler.detect_heart_rate_anomaly(-10.0, 60.0, 1.0) is True

    def test_very_high_heart_rate_is_anomaly(self, handler):
        assert handler.detect_heart_rate_anomaly(300.0, 60.0, 1.0) is True


# ---------------------------------------------------------------------------
# detect_heart_rate_anomaly – rate-of-change checks (Req 18.2)
# ---------------------------------------------------------------------------


class TestDetectHeartRateAnomalyRateOfChange:
    def test_slow_change_is_not_anomaly(self, handler):
        # 10 bpm change over 1 second = 10 bpm/s < 50 bpm/s
        assert handler.detect_heart_rate_anomaly(80.0, 70.0, 1.0) is False

    def test_exactly_at_limit_is_not_anomaly(self, handler):
        # 50 bpm/s is the limit; exactly at limit should NOT be anomalous
        assert handler.detect_heart_rate_anomaly(120.0, 70.0, 1.0) is False

    def test_rate_exceeds_limit_is_anomaly(self, handler):
        # 51 bpm change over 1 second = 51 bpm/s > 50 bpm/s
        assert handler.detect_heart_rate_anomaly(121.0, 70.0, 1.0) is True

    def test_large_change_over_short_time_is_anomaly(self, handler):
        # 30 bpm change over 0.5 seconds = 60 bpm/s > 50 bpm/s
        assert handler.detect_heart_rate_anomaly(100.0, 70.0, 0.5) is True

    def test_zero_time_delta_skips_rate_check(self, handler):
        # time_delta=0 should not raise and should not flag rate-of-change
        assert handler.detect_heart_rate_anomaly(72.0, 70.0, 0.0) is False

    def test_negative_time_delta_skips_rate_check(self, handler):
        assert handler.detect_heart_rate_anomaly(72.0, 70.0, -1.0) is False


# ---------------------------------------------------------------------------
# detect_movement_anomaly (Req 18.3)
# ---------------------------------------------------------------------------


class TestDetectMovementAnomaly:
    def test_zero_amplitude_is_valid(self, handler):
        assert handler.detect_movement_anomaly(0.0) is False

    def test_positive_amplitude_is_valid(self, handler):
        assert handler.detect_movement_anomaly(1.5) is False

    def test_large_positive_amplitude_is_valid(self, handler):
        assert handler.detect_movement_anomaly(1000.0) is False

    def test_negative_amplitude_is_anomaly(self, handler):
        assert handler.detect_movement_anomaly(-0.001) is True

    def test_large_negative_amplitude_is_anomaly(self, handler):
        assert handler.detect_movement_anomaly(-50.0) is True


# ---------------------------------------------------------------------------
# interpolate_anomalous_data (Req 18.4, 18.5)
# ---------------------------------------------------------------------------


class TestInterpolateAnomalousData:
    def test_no_anomalies_returns_unchanged(self, handler):
        data = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        mask = np.array([False, False, False, False, False])
        result = handler.interpolate_anomalous_data(data, mask)
        np.testing.assert_array_equal(result, data)

    def test_single_anomaly_interpolated(self, handler):
        # Middle point is anomalous; should be interpolated to 2.0
        data = np.array([1.0, 999.0, 3.0])
        mask = np.array([False, True, False])
        timestamps = np.array([0.0, 1.0, 2.0])
        result = handler.interpolate_anomalous_data(data, mask, timestamps=timestamps)
        assert result[1] == pytest.approx(2.0)
        assert result[0] == 1.0
        assert result[2] == 3.0

    def test_short_gap_is_interpolated(self, handler):
        # 3-point gap spanning 3 seconds (< 5 s limit)
        data = np.array([0.0, 999.0, 999.0, 999.0, 4.0])
        mask = np.array([False, True, True, True, False])
        timestamps = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        result = handler.interpolate_anomalous_data(data, mask, timestamps=timestamps)
        # Linear interpolation between 0.0 at t=0 and 4.0 at t=4
        assert result[1] == pytest.approx(1.0)
        assert result[2] == pytest.approx(2.0)
        assert result[3] == pytest.approx(3.0)

    def test_long_gap_is_not_interpolated(self, handler):
        # Gap spans 6 seconds (> 5 s limit) – should remain unchanged
        data = np.array([1.0, 999.0, 999.0, 999.0, 999.0, 999.0, 7.0])
        mask = np.array([False, True, True, True, True, True, False])
        timestamps = np.array([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
        result = handler.interpolate_anomalous_data(data, mask, timestamps=timestamps)
        # Anomalous values should be unchanged
        np.testing.assert_array_equal(result[1:6], data[1:6])

    def test_gap_exactly_at_limit_is_not_interpolated(self, handler):
        # Gap duration == MAX_INTERPOLATION_GAP_SECONDS (5 s) – not interpolated
        n_anomalous = 4
        data = np.array([0.0] + [999.0] * n_anomalous + [5.0])
        mask = np.array([False] + [True] * n_anomalous + [False])
        timestamps = np.array([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
        result = handler.interpolate_anomalous_data(data, mask, timestamps=timestamps)
        # gap_duration = 5.0 - 0.0 = 5.0 which is NOT < 5.0, so no interpolation
        np.testing.assert_array_equal(result[1:5], data[1:5])

    def test_interpolated_values_within_neighbour_range(self, handler):
        """Req 18.9 / Property 16: interpolated values must be within [min, max]
        of the surrounding valid data."""
        data = np.array([10.0, 999.0, 999.0, 30.0])
        mask = np.array([False, True, True, False])
        timestamps = np.array([0.0, 1.0, 2.0, 3.0])
        result = handler.interpolate_anomalous_data(data, mask, timestamps=timestamps)
        assert result[1] >= min(data[0], data[3])
        assert result[1] <= max(data[0], data[3])
        assert result[2] >= min(data[0], data[3])
        assert result[2] <= max(data[0], data[3])

    def test_edge_anomaly_not_interpolated(self, handler):
        # Anomaly at the start – no previous valid point
        data = np.array([999.0, 2.0, 3.0])
        mask = np.array([True, False, False])
        timestamps = np.array([0.0, 1.0, 2.0])
        result = handler.interpolate_anomalous_data(data, mask, timestamps=timestamps)
        assert result[0] == 999.0  # unchanged

    def test_uses_sampling_rate_when_no_timestamps(self, handler):
        # Without timestamps, gap duration estimated from sampling_rate
        # 3 anomalous points at 1 Hz => gap = 4 s < 5 s => interpolated
        data = np.array([0.0, 999.0, 999.0, 999.0, 4.0])
        mask = np.array([False, True, True, True, False])
        result = handler.interpolate_anomalous_data(data, mask, sampling_rate=1.0)
        assert result[1] == pytest.approx(1.0)
        assert result[2] == pytest.approx(2.0)
        assert result[3] == pytest.approx(3.0)

    def test_original_data_not_mutated(self, handler):
        data = np.array([1.0, 999.0, 3.0])
        mask = np.array([False, True, False])
        timestamps = np.array([0.0, 1.0, 2.0])
        original = data.copy()
        handler.interpolate_anomalous_data(data, mask, timestamps=timestamps)
        np.testing.assert_array_equal(data, original)


# ---------------------------------------------------------------------------
# publish_sensor_fault (Req 18.6, 18.7, 18.8)
# ---------------------------------------------------------------------------


class TestPublishSensorFault:
    def test_returns_payload_dict(self, handler):
        payload = handler.publish_sensor_fault("heart_rate", device_id="hr_001")
        assert payload["sensor_type"] == "heart_rate"
        assert payload["device_id"] == "hr_001"
        assert "timestamp" in payload

    def test_publishes_to_correct_topic(self, handler_with_publisher, mock_publisher):
        handler_with_publisher.publish_sensor_fault("heart_rate")
        assert len(mock_publisher.published_messages) == 1
        assert mock_publisher.published_messages[0]["topic"] == TOPIC_SENSOR_FAULT

    def test_publishes_movement_fault(self, handler_with_publisher, mock_publisher):
        handler_with_publisher.publish_sensor_fault("movement", device_id="mv_001")
        msg = mock_publisher.published_messages[0]
        assert msg["payload"]["sensor_type"] == "movement"
        assert msg["payload"]["device_id"] == "mv_001"

    def test_payload_contains_timestamp(self, handler_with_publisher, mock_publisher):
        before = time.time()
        handler_with_publisher.publish_sensor_fault("heart_rate")
        after = time.time()
        ts = mock_publisher.published_messages[0]["payload"]["timestamp"]
        assert before <= ts <= after

    def test_no_publisher_does_not_raise(self, handler):
        # Should not raise even without an MQTT publisher
        payload = handler.publish_sensor_fault("heart_rate")
        assert payload["sensor_type"] == "heart_rate"

    def test_default_qos_is_1(self, handler_with_publisher, mock_publisher):
        handler_with_publisher.publish_sensor_fault("movement")
        assert mock_publisher.published_messages[0]["qos"] == 1

    def test_custom_reason_included(self, handler_with_publisher, mock_publisher):
        handler_with_publisher.publish_sensor_fault(
            "heart_rate", reason="signal_lost"
        )
        assert mock_publisher.published_messages[0]["payload"]["reason"] == "signal_lost"


# ---------------------------------------------------------------------------
# Property-Based Tests
# ---------------------------------------------------------------------------


# Feature: cnn-bilstm-sleep-algorithm, Property 16: Interpolation fill range constraint
@settings(max_examples=100)
@given(
    valid_before=st.floats(min_value=-1000.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    valid_after=st.floats(min_value=-1000.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    anomaly_values=st.lists(
        st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=1,
        max_size=4,
    ),
)
def test_property_16_interpolated_values_within_neighbour_range(
    valid_before, valid_after, anomaly_values
):
    """**Validates: Requirements 18.9**

    Property 16: For ALL sequences with anomalous data, interpolated values
    SHALL be within the range of adjacent valid data [min, max].
    """
    handler = AnomalyHandler()

    n_anomalous = len(anomaly_values)
    # Build a sequence: [valid_before, ...anomalies..., valid_after]
    data = np.array([valid_before] + anomaly_values + [valid_after], dtype=float)
    mask = np.array([False] + [True] * n_anomalous + [False])

    # Use timestamps that keep the gap strictly under the 5-second limit
    # gap_duration = timestamps[-1] - timestamps[0] = n_anomalous + 1
    # We need gap_duration < MAX_INTERPOLATION_GAP_SECONDS (5.0)
    # so limit n_anomalous to 4 (gap = 5 - 0 = 5 which is NOT < 5, so use 3 max for safety)
    # With n_anomalous in [1,3] gap = n_anomalous+1 in [2,4] < 5 → always interpolated
    # With n_anomalous=4 gap = 5 which is NOT < 5 → not interpolated (skip check)
    timestamps = np.arange(len(data), dtype=float)
    gap_duration = timestamps[-1] - timestamps[0]

    result = handler.interpolate_anomalous_data(data, mask, timestamps=timestamps)

    if gap_duration < MAX_INTERPOLATION_GAP_SECONDS:
        # Interpolation should have occurred; verify range constraint
        lo = min(valid_before, valid_after)
        hi = max(valid_before, valid_after)
        for i in range(1, 1 + n_anomalous):
            assert result[i] >= lo - 1e-9, (
                f"Interpolated value {result[i]} is below min({valid_before}, {valid_after})={lo}"
            )
            assert result[i] <= hi + 1e-9, (
                f"Interpolated value {result[i]} is above max({valid_before}, {valid_after})={hi}"
            )
    # If gap >= limit, values are left unchanged — no range constraint to check
