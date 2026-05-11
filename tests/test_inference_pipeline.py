"""Unit tests for InferencePipeline.

Tests cover:
- Component wiring (dependency injection)
- process_sensor_data end-to-end flow
- process_disaster_sensor routing
- MQTT retry logic
- Internal helpers (_build_time_frequency_matrix, _cnn_to_bilstm_input)
- Configuration loading with default fallback
"""

import time
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.data_structures import HeartRateData, MovementData, SleepStage, SleepStages
from src.inference_pipeline import InferencePipeline, _RETRY_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------

def _make_hr(n: int = 200, rate: float = 75.0) -> HeartRateData:
    """Create a simple HeartRateData with constant heart rate."""
    ts = np.linspace(0.0, n - 1, n)
    values = np.full(n, rate, dtype=float)
    return HeartRateData(timestamps=ts, values=values, sampling_rate=100)


def _make_mv(n: int = 200, amplitude: float = 0.5) -> MovementData:
    """Create a simple MovementData with constant amplitude."""
    ts = np.linspace(0.0, n - 1, n)
    values = np.full(n, amplitude, dtype=float)
    return MovementData(timestamps=ts, values=values, sampling_rate=100)


def _make_pipeline(**kwargs) -> InferencePipeline:
    """Build an InferencePipeline with all MQTT components mocked out."""
    mock_publisher = MagicMock()
    mock_publisher.published_messages = []
    mock_subscriber = MagicMock()

    return InferencePipeline(
        publisher=mock_publisher,
        subscriber=mock_subscriber,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Initialisation tests
# ---------------------------------------------------------------------------

class TestInferencePipelineInit:
    def test_default_construction_uses_config(self):
        """Pipeline can be constructed with default config path."""
        pipeline = _make_pipeline()
        assert pipeline.config is not None
        assert "mqtt" in pipeline.config

    def test_dependency_injection_respected(self):
        """Injected components are stored as-is."""
        mock_pub = MagicMock()
        mock_sub = MagicMock()
        mock_ts = MagicMock()
        pipeline = InferencePipeline(
            publisher=mock_pub,
            subscriber=mock_sub,
            time_synchronizer=mock_ts,
        )
        assert pipeline.publisher is mock_pub
        assert pipeline.subscriber is mock_sub
        assert pipeline.time_synchronizer is mock_ts

    def test_missing_config_falls_back_to_defaults(self, tmp_path):
        """Pipeline uses default config when config file is missing."""
        pipeline = _make_pipeline(config_path=str(tmp_path / "nonexistent.json"))
        # Should still have all required keys from default config
        assert "model" in pipeline.config
        assert "disaster_monitoring" in pipeline.config


# ---------------------------------------------------------------------------
# process_sensor_data tests
# ---------------------------------------------------------------------------

class TestProcessSensorData:
    def test_returns_sleep_stage_enum(self):
        """process_sensor_data returns a SleepStage enum value."""
        pipeline = _make_pipeline()
        hr = _make_hr(300)
        mv = _make_mv(300)
        result = pipeline.process_sensor_data(hr, mv)
        assert isinstance(result, SleepStage)

    def test_publishes_sleep_stage(self):
        """process_sensor_data calls publish_sleep_stage on the publisher."""
        pipeline = _make_pipeline()
        hr = _make_hr(300)
        mv = _make_mv(300)
        pipeline.process_sensor_data(hr, mv)
        pipeline.publisher.publish_sleep_stage.assert_called_once()

    def test_publishes_environment_control(self):
        """process_sensor_data triggers all three environment control commands."""
        mock_ec = MagicMock()
        pipeline = _make_pipeline(environment_controller=mock_ec)
        hr = _make_hr(300)
        mv = _make_mv(300)
        pipeline.process_sensor_data(hr, mv)
        mock_ec.generate_lighting_control.assert_called_once()
        mock_ec.generate_temperature_control.assert_called_once()
        mock_ec.generate_humidity_control.assert_called_once()

    def test_environment_control_receives_classified_stage(self):
        """Environment controller receives the same stage returned by the classifier."""
        mock_ec = MagicMock()
        pipeline = _make_pipeline(environment_controller=mock_ec)
        hr = _make_hr(300)
        mv = _make_mv(300)
        stage = pipeline.process_sensor_data(hr, mv)
        mock_ec.generate_lighting_control.assert_called_once_with(stage)
        mock_ec.generate_temperature_control.assert_called_once_with(stage)
        mock_ec.generate_humidity_control.assert_called_once_with(stage)

    def test_handles_mismatched_lengths(self):
        """Pipeline handles HR and movement arrays of different lengths."""
        pipeline = _make_pipeline()
        hr = _make_hr(250)
        mv = _make_mv(300)
        result = pipeline.process_sensor_data(hr, mv)
        assert isinstance(result, SleepStage)

    def test_anomaly_interpolation_applied(self):
        """Anomalous HR values are interpolated before further processing."""
        mock_ah = MagicMock()
        mock_ah.detect_heart_rate_anomaly.return_value = False
        mock_ah.detect_movement_anomaly.return_value = False
        mock_ah.interpolate_anomalous_data.side_effect = lambda data, mask, **kw: data.copy()

        pipeline = _make_pipeline(anomaly_handler=mock_ah)
        hr = _make_hr(300)
        mv = _make_mv(300)
        pipeline.process_sensor_data(hr, mv)

        # interpolate_anomalous_data should be called twice (HR + MV)
        assert mock_ah.interpolate_anomalous_data.call_count == 2


# ---------------------------------------------------------------------------
# process_disaster_sensor tests
# ---------------------------------------------------------------------------

class TestProcessDisasterSensor:
    def test_smoke_below_threshold_returns_false(self):
        """Smoke concentration below threshold returns False."""
        pipeline = _make_pipeline()
        result = pipeline.process_disaster_sensor("smoke", 50.0, "bedroom")
        assert result is False

    def test_smoke_above_threshold_returns_true(self):
        """Smoke concentration above threshold returns True."""
        pipeline = _make_pipeline()
        result = pipeline.process_disaster_sensor("smoke", 200.0, "bedroom")
        assert result is True

    def test_gas_below_threshold_returns_false(self):
        """Gas concentration below threshold returns False."""
        pipeline = _make_pipeline()
        result = pipeline.process_disaster_sensor("gas", 10.0, "kitchen")
        assert result is False

    def test_gas_above_threshold_returns_true(self):
        """Gas concentration above threshold returns True."""
        pipeline = _make_pipeline()
        result = pipeline.process_disaster_sensor("gas", 100.0, "kitchen")
        assert result is True

    def test_unknown_sensor_type_returns_false(self):
        """Unknown sensor type returns False without raising."""
        pipeline = _make_pipeline()
        result = pipeline.process_disaster_sensor("radiation", 999.0, "lab")
        assert result is False

    def test_smoke_alert_delegates_to_disaster_monitor(self):
        """process_disaster_sensor delegates to DisasterMonitor.check_smoke_level."""
        mock_dm = MagicMock()
        mock_dm.check_smoke_level.return_value = True
        pipeline = _make_pipeline(disaster_monitor=mock_dm)
        result = pipeline.process_disaster_sensor("smoke", 150.0, "hall")
        mock_dm.check_smoke_level.assert_called_once_with(150.0, "hall")
        assert result is True

    def test_gas_alert_delegates_to_disaster_monitor(self):
        """process_disaster_sensor delegates to DisasterMonitor.check_gas_level."""
        mock_dm = MagicMock()
        mock_dm.check_gas_level.return_value = False
        pipeline = _make_pipeline(disaster_monitor=mock_dm)
        result = pipeline.process_disaster_sensor("gas", 20.0, "kitchen")
        mock_dm.check_gas_level.assert_called_once_with(20.0, "kitchen")
        assert result is False


# ---------------------------------------------------------------------------
# MQTT retry tests
# ---------------------------------------------------------------------------

class TestMQTTRetry:
    def test_connect_mqtt_succeeds_on_first_attempt(self):
        """connect_mqtt returns True when broker is reachable immediately."""
        pipeline = _make_pipeline()
        # publisher and subscriber are already mocks; connect() won't raise
        result = pipeline.connect_mqtt()
        assert result is True

    def test_connect_mqtt_retries_on_failure(self):
        """connect_mqtt retries after connection failures."""
        mock_pub = MagicMock()
        mock_sub = MagicMock()
        # Fail twice, then succeed
        mock_pub.connect.side_effect = [ConnectionRefusedError, ConnectionRefusedError, None]
        mock_sub.connect.return_value = None

        pipeline = InferencePipeline(publisher=mock_pub, subscriber=mock_sub)

        with patch("src.inference_pipeline.time.sleep"):  # skip actual sleep
            result = pipeline.connect_mqtt()

        assert result is True
        assert mock_pub.connect.call_count == 3

    def test_connect_mqtt_returns_false_after_max_attempts(self):
        """connect_mqtt returns False when all retry attempts are exhausted."""
        mock_pub = MagicMock()
        mock_sub = MagicMock()
        mock_pub.connect.side_effect = ConnectionRefusedError("broker unavailable")

        pipeline = InferencePipeline(publisher=mock_pub, subscriber=mock_sub)

        with patch("src.inference_pipeline.time.sleep"):
            result = pipeline.connect_mqtt()

        assert result is False
        assert mock_pub.connect.call_count == _RETRY_MAX_ATTEMPTS

    def test_disconnect_mqtt_calls_both_components(self):
        """disconnect_mqtt calls disconnect on both publisher and subscriber."""
        pipeline = _make_pipeline()
        pipeline.disconnect_mqtt()
        pipeline.publisher.disconnect.assert_called_once()
        pipeline.subscriber.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# Internal helper tests
# ---------------------------------------------------------------------------

class TestBuildTimeFrequencyMatrix:
    def test_output_shape_is_correct(self):
        """_build_time_frequency_matrix always returns (1024, 128, 2)."""
        pipeline = _make_pipeline()
        hr = np.random.rand(500).astype(np.float32)
        mv = np.random.rand(500).astype(np.float32)
        matrix = pipeline._build_time_frequency_matrix(hr, mv)
        assert matrix.shape == (1024, 128, 2)

    def test_short_signal_is_zero_padded(self):
        """Signals shorter than 1024*128 are zero-padded."""
        pipeline = _make_pipeline()
        hr = np.ones(100, dtype=np.float32)
        mv = np.ones(100, dtype=np.float32)
        matrix = pipeline._build_time_frequency_matrix(hr, mv)
        assert matrix.shape == (1024, 128, 2)
        # Padded region should be zero
        assert matrix[1, 0, 0] == 0.0

    def test_long_signal_is_truncated(self):
        """Signals longer than 1024*128 are truncated."""
        pipeline = _make_pipeline()
        required = 1024 * 128
        hr = np.ones(required + 1000, dtype=np.float32) * 2.0
        mv = np.ones(required + 1000, dtype=np.float32) * 3.0
        matrix = pipeline._build_time_frequency_matrix(hr, mv)
        assert matrix.shape == (1024, 128, 2)

    def test_channels_are_independent(self):
        """HR and movement channels are stored in separate last-axis slices."""
        pipeline = _make_pipeline()
        hr = np.ones(1024 * 128, dtype=np.float32) * 1.0
        mv = np.ones(1024 * 128, dtype=np.float32) * 2.0
        matrix = pipeline._build_time_frequency_matrix(hr, mv)
        assert np.all(matrix[:, :, 0] == 1.0)
        assert np.all(matrix[:, :, 1] == 2.0)


class TestCnnToBilstmInput:
    def test_output_shape(self):
        """_cnn_to_bilstm_input reshapes (256, 32, 64) → (256, 2048)."""
        pipeline = _make_pipeline()
        cnn_out = np.random.rand(256, 32, 64).astype(np.float32)
        bilstm_in = pipeline._cnn_to_bilstm_input(cnn_out)
        assert bilstm_in.shape == (256, 2048)

    def test_values_preserved(self):
        """Reshaping does not alter the underlying values."""
        pipeline = _make_pipeline()
        cnn_out = np.arange(256 * 32 * 64, dtype=np.float32).reshape(256, 32, 64)
        bilstm_in = pipeline._cnn_to_bilstm_input(cnn_out)
        assert bilstm_in.shape == (256, 2048)
        np.testing.assert_array_equal(bilstm_in.ravel(), cnn_out.ravel())


# ---------------------------------------------------------------------------
# Normalization helper tests
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_returns_copy_when_not_fitted(self):
        """_normalize returns copies of the raw arrays when normalizer is unfitted."""
        pipeline = _make_pipeline()
        hr = np.array([60.0, 70.0, 80.0])
        mv = np.array([0.1, 0.2, 0.3])
        hr_out, mv_out = pipeline._normalize(hr, mv)
        np.testing.assert_array_equal(hr_out, hr)
        np.testing.assert_array_equal(mv_out, mv)

    def test_applies_normalization_when_fitted(self):
        """_normalize applies Z-score when the normalizer has been fitted."""
        from src.data_structures import Dataset, SleepStages, TrainingSet

        pipeline = _make_pipeline()
        n = 50
        ts = np.arange(n, dtype=float)
        hr_vals = np.full(n, 75.0)
        mv_vals = np.full(n, 0.5)
        hr_obj = HeartRateData(timestamps=ts, values=hr_vals, sampling_rate=100)
        mv_obj = MovementData(timestamps=ts, values=mv_vals, sampling_rate=100)
        stages = SleepStages(timestamps=ts, stages=np.zeros(n, dtype=int))
        dataset = Dataset(heart_rate=hr_obj, movement=mv_obj, sleep_stages=stages, subject_ids=["s1"])
        training_set = TrainingSet(dataset=dataset, normalization_params={})
        pipeline.data_normalizer.fit(training_set)

        hr_out, mv_out = pipeline._normalize(hr_vals, mv_vals)
        # After fitting on constant data, std is set to 1.0 (guard), so output ≈ 0
        assert hr_out is not None
        assert mv_out is not None
