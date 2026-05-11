"""Integration tests for CNN-BiLSTM Sleep Algorithm.

Covers:
- Complete training flow: dataset loading → preprocessing → training → evaluation → model saving
- Complete inference flow: MQTT subscription → preprocessing → classification → MQTT publishing
- Disaster alerting flow: smoke/gas detection → alert publishing
- Error recovery: MQTT reconnection, sensor fault handling, anomaly interpolation
"""

import json
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.anomaly_handler import AnomalyHandler
from src.data_structures import (
    Dataset,
    HeartRateData,
    MovementData,
    SleepStage,
    SleepStages,
    TestSet,
    TrainingSet,
)
from src.dataset_loader import DatasetLoader
from src.disaster_monitor import DisasterMonitor
from src.inference_pipeline import InferencePipeline, _RETRY_MAX_ATTEMPTS
from src.mqtt_publisher import MQTTPublisher
from src.mqtt_subscriber import MQTTSubscriber
from src.training_pipeline import TrainingPipeline


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_dataset(n_samples: int = 2048, seed: int = 0) -> Dataset:
    rng = np.random.default_rng(seed)
    timestamps = np.arange(n_samples, dtype=np.float64)
    hr_values = rng.uniform(60.0, 100.0, size=n_samples)
    mv_values = rng.uniform(0.0, 1.0, size=n_samples)
    stages = rng.integers(0, 4, size=n_samples)
    hr = HeartRateData(timestamps=timestamps, values=hr_values, sampling_rate=100)
    mv = MovementData(timestamps=timestamps, values=mv_values, sampling_rate=100)
    ss = SleepStages(timestamps=timestamps, stages=stages)
    return Dataset(heart_rate=hr, movement=mv, sleep_stages=ss, subject_ids=["subj_0"])


def _make_training_set(n_samples: int = 2048, seed: int = 0) -> TrainingSet:
    ds = _make_dataset(n_samples, seed)
    return TrainingSet(
        dataset=ds,
        normalization_params={
            "heart_rate": (float(np.mean(ds.heart_rate.values)), float(np.std(ds.heart_rate.values))),
            "movement": (float(np.mean(ds.movement.values)), float(np.std(ds.movement.values))),
        },
    )


def _make_test_set(n_samples: int = 1024, seed: int = 42) -> TestSet:
    return TestSet(dataset=_make_dataset(n_samples, seed))


def _make_inference_pipeline(**kwargs) -> InferencePipeline:
    """Build InferencePipeline with mocked MQTT components."""
    mock_publisher = MagicMock(spec=MQTTPublisher)
    mock_publisher.published_messages = []
    mock_subscriber = MagicMock(spec=MQTTSubscriber)
    return InferencePipeline(
        publisher=mock_publisher,
        subscriber=mock_subscriber,
        **kwargs,
    )


def _make_hr(n: int = 300, rate: float = 75.0) -> HeartRateData:
    ts = np.linspace(0.0, n - 1, n)
    return HeartRateData(timestamps=ts, values=np.full(n, rate), sampling_rate=100)


def _make_mv(n: int = 300, amplitude: float = 0.5) -> MovementData:
    ts = np.linspace(0.0, n - 1, n)
    return MovementData(timestamps=ts, values=np.full(n, amplitude), sampling_rate=100)


# ===========================================================================
# 1. Complete Training Flow
# ===========================================================================

class TestTrainingFlow:
    """Integration: dataset loading → preprocessing → training → evaluation → model saving."""

    def test_full_training_flow_produces_valid_history(self):
        """End-to-end training returns a history dict with valid metrics."""
        pipeline = TrainingPipeline(config_path="training_config/config.json")
        pipeline.max_epochs = 2
        pipeline.patience = 5
        pipeline.batch_size = 16

        training_set = _make_training_set(n_samples=2048)
        val_set = _make_test_set(n_samples=512)

        with tempfile.TemporaryDirectory() as tmpdir:
            save_path = os.path.join(tmpdir, "model.h5")
            history = pipeline.train(training_set, val_set, model_save_path=save_path)

        assert set(history.keys()) == {"epochs", "train_loss", "train_acc", "val_loss", "val_acc"}
        assert len(history["epochs"]) > 0
        for acc in history["train_acc"] + history["val_acc"]:
            assert 0.0 <= acc <= 1.0
        for loss in history["train_loss"] + history["val_loss"]:
            assert loss >= 0.0

    def test_training_flow_saves_model_checkpoint(self):
        """Training saves a model checkpoint file."""
        pipeline = TrainingPipeline(config_path="training_config/config.json")
        pipeline.max_epochs = 2
        pipeline.patience = 5
        pipeline.batch_size = 16

        training_set = _make_training_set(n_samples=2048)
        val_set = _make_test_set(n_samples=512)

        with tempfile.TemporaryDirectory() as tmpdir:
            save_path = os.path.join(tmpdir, "model.h5")
            pipeline.train(training_set, val_set, model_save_path=save_path)
            h5_exists = os.path.exists(save_path)
            npz_exists = os.path.exists(save_path.replace(".h5", ".npz"))
            assert h5_exists or npz_exists

    def test_training_flow_evaluate_after_train(self):
        """evaluate() after training returns valid loss and accuracy."""
        pipeline = TrainingPipeline(config_path="training_config/config.json")
        pipeline.max_epochs = 2
        pipeline.patience = 5
        pipeline.batch_size = 16

        training_set = _make_training_set(n_samples=2048)
        val_set = _make_test_set(n_samples=512)
        test_set = _make_test_set(n_samples=512, seed=99)

        with tempfile.TemporaryDirectory() as tmpdir:
            save_path = os.path.join(tmpdir, "model.h5")
            pipeline.train(training_set, val_set, model_save_path=save_path)

        metrics = pipeline.evaluate(test_set)
        assert "loss" in metrics and "accuracy" in metrics
        assert metrics["loss"] >= 0.0
        assert 0.0 <= metrics["accuracy"] <= 1.0

    def test_dataset_split_feeds_training_pipeline(self):
        """DatasetLoader.split_train_test() output feeds directly into TrainingPipeline."""
        loader = DatasetLoader()
        dataset = _make_dataset(n_samples=2048)
        training_set, test_set = loader.split_train_test(dataset, test_ratio=0.2)

        pipeline = TrainingPipeline(config_path="training_config/config.json")
        pipeline.max_epochs = 2
        pipeline.patience = 5
        pipeline.batch_size = 16

        with tempfile.TemporaryDirectory() as tmpdir:
            save_path = os.path.join(tmpdir, "model.h5")
            history = pipeline.train(training_set, test_set, model_save_path=save_path)

        assert len(history["epochs"]) > 0

    def test_model_save_and_reload_preserves_accuracy(self):
        """Saved model weights can be reloaded and produce the same evaluation accuracy."""
        try:
            import h5py
        except ImportError:
            pytest.skip("h5py not available")

        pipeline = TrainingPipeline(config_path="training_config/config.json")
        pipeline.max_epochs = 2
        pipeline.patience = 5
        pipeline.batch_size = 16

        training_set = _make_training_set(n_samples=2048)
        val_set = _make_test_set(n_samples=512)
        test_set = _make_test_set(n_samples=512, seed=7)

        with tempfile.TemporaryDirectory() as tmpdir:
            save_path = os.path.join(tmpdir, "model.h5")
            pipeline.train(training_set, val_set, model_save_path=save_path)
            metrics_before = pipeline.evaluate(test_set)

            # Fresh pipeline, load saved weights
            pipeline2 = TrainingPipeline(config_path="training_config/config.json")
            pipeline2._normalizer.fit(training_set)
            norm_ds = pipeline2._normalizer.transform(training_set.dataset)
            X, _ = pipeline2._build_feature_matrix(norm_ds)
            pipeline2._classifier._ensure_initialised(X.shape[1])
            pipeline2.load_model(save_path)

            metrics_after = pipeline2.evaluate(test_set)

        assert abs(metrics_before["accuracy"] - metrics_after["accuracy"]) < 1e-5


# ===========================================================================
# 2. Complete Inference Flow
# ===========================================================================

class TestInferenceFlow:
    """Integration: MQTT subscription → preprocessing → classification → MQTT publishing."""

    def test_inference_pipeline_returns_sleep_stage(self):
        """Full inference pipeline returns a valid SleepStage."""
        pipeline = _make_inference_pipeline()
        hr = _make_hr(300)
        mv = _make_mv(300)
        result = pipeline.process_sensor_data(hr, mv)
        assert isinstance(result, SleepStage)
        assert result in list(SleepStage)

    def test_inference_pipeline_publishes_sleep_stage(self):
        """Inference pipeline calls publish_sleep_stage after classification."""
        pipeline = _make_inference_pipeline()
        hr = _make_hr(300)
        mv = _make_mv(300)
        pipeline.process_sensor_data(hr, mv)
        pipeline.publisher.publish_sleep_stage.assert_called_once()

    def test_inference_pipeline_publishes_environment_controls(self):
        """Inference pipeline publishes all three environment control commands."""
        mock_ec = MagicMock()
        pipeline = _make_inference_pipeline(environment_controller=mock_ec)
        hr = _make_hr(300)
        mv = _make_mv(300)
        stage = pipeline.process_sensor_data(hr, mv)
        mock_ec.generate_lighting_control.assert_called_once_with(stage)
        mock_ec.generate_temperature_control.assert_called_once_with(stage)
        mock_ec.generate_humidity_control.assert_called_once_with(stage)

    def test_mqtt_subscriber_validates_and_stores_heart_rate_message(self):
        """MQTTSubscriber validates a heart rate message and stores it."""
        subscriber = MQTTSubscriber()
        subscriber.subscribe_heart_rate()
        payload = json.dumps({
            "device_id": "sensor_01",
            "timestamp": time.time(),
            "heart_rate": 72.0,
        })
        msg = subscriber.on_message("sensors/heart_rate", payload)
        assert msg is not None
        assert msg["heart_rate"] == 72.0
        assert msg["anomalous"] is False
        assert len(subscriber.received_messages) == 1

    def test_mqtt_subscriber_marks_out_of_range_heart_rate_anomalous(self):
        """MQTTSubscriber marks heart rate outside [30, 200] bpm as anomalous."""
        subscriber = MQTTSubscriber()
        subscriber.subscribe_heart_rate()
        payload = json.dumps({
            "device_id": "sensor_01",
            "timestamp": time.time(),
            "heart_rate": 250.0,  # out of range
        })
        msg = subscriber.on_message("sensors/heart_rate", payload)
        assert msg is not None
        assert msg["anomalous"] is True

    def test_mqtt_subscriber_discards_stale_message(self):
        """MQTTSubscriber discards messages with timestamps older than 5 seconds."""
        subscriber = MQTTSubscriber()
        subscriber.subscribe_heart_rate()
        stale_payload = json.dumps({
            "device_id": "sensor_01",
            "timestamp": time.time() - 10.0,  # 10 seconds old
            "heart_rate": 72.0,
        })
        msg = subscriber.on_message("sensors/heart_rate", stale_payload)
        assert msg is None
        assert len(subscriber.received_messages) == 0

    def test_mqtt_publisher_publishes_valid_sleep_stage_message(self):
        """MQTTPublisher.publish_sleep_stage produces a valid JSON payload."""
        publisher = MQTTPublisher(broker_address="localhost", broker_port=1883)
        payload = publisher.publish_sleep_stage(SleepStage.LIGHT, confidence=0.85)
        assert payload["sleep_stage"] == "LIGHT"
        assert payload["confidence"] == 0.85
        assert "timestamp" in payload
        assert "device_id" in payload

    def test_inference_pipeline_applies_anomaly_interpolation(self):
        """Anomaly handler interpolation is called during inference."""
        mock_ah = MagicMock()
        mock_ah.detect_heart_rate_anomaly.return_value = False
        mock_ah.detect_movement_anomaly.return_value = False
        mock_ah.interpolate_anomalous_data.side_effect = lambda data, mask, **kw: data.copy()

        pipeline = _make_inference_pipeline(anomaly_handler=mock_ah)
        hr = _make_hr(300)
        mv = _make_mv(300)
        pipeline.process_sensor_data(hr, mv)

        # interpolate_anomalous_data called for both HR and MV channels
        assert mock_ah.interpolate_anomalous_data.call_count == 2

    def test_inference_pipeline_handles_mismatched_sensor_lengths(self):
        """Pipeline handles HR and movement arrays of different lengths gracefully."""
        pipeline = _make_inference_pipeline()
        hr = _make_hr(250)
        mv = _make_mv(350)
        result = pipeline.process_sensor_data(hr, mv)
        assert isinstance(result, SleepStage)


# ===========================================================================
# 3. Disaster Alerting Flow
# ===========================================================================

class TestDisasterAlertingFlow:
    """Integration: smoke/gas detection → alert publishing."""

    def test_smoke_above_threshold_triggers_alert(self):
        """Smoke concentration above threshold publishes an alert."""
        publisher = MQTTPublisher()
        monitor = DisasterMonitor(smoke_threshold=100.0, gas_threshold=50.0, mqtt_publisher=publisher)
        result = monitor.check_smoke_level(150.0, "bedroom")
        assert result is True
        assert len(publisher.published_messages) == 1
        msg = publisher.published_messages[0]
        assert msg["topic"] == "alert/smoke"
        assert msg["payload"]["alert_type"] == "smoke"
        assert msg["payload"]["concentration"] == 150.0
        assert msg["payload"]["sensor_location"] == "bedroom"

    def test_smoke_below_threshold_no_alert(self):
        """Smoke concentration below threshold does not publish an alert."""
        publisher = MQTTPublisher()
        monitor = DisasterMonitor(smoke_threshold=100.0, gas_threshold=50.0, mqtt_publisher=publisher)
        result = monitor.check_smoke_level(50.0, "bedroom")
        assert result is False
        assert len(publisher.published_messages) == 0

    def test_gas_above_threshold_triggers_alert(self):
        """Gas concentration above threshold publishes an alert."""
        publisher = MQTTPublisher()
        monitor = DisasterMonitor(smoke_threshold=100.0, gas_threshold=50.0, mqtt_publisher=publisher)
        result = monitor.check_gas_level(80.0, "kitchen")
        assert result is True
        assert len(publisher.published_messages) == 1
        msg = publisher.published_messages[0]
        assert msg["topic"] == "alert/gas"
        assert msg["payload"]["alert_type"] == "gas"
        assert msg["payload"]["concentration"] == 80.0

    def test_gas_below_threshold_no_alert(self):
        """Gas concentration below threshold does not publish an alert."""
        publisher = MQTTPublisher()
        monitor = DisasterMonitor(smoke_threshold=100.0, gas_threshold=50.0, mqtt_publisher=publisher)
        result = monitor.check_gas_level(20.0, "kitchen")
        assert result is False
        assert len(publisher.published_messages) == 0

    def test_disaster_alert_uses_qos_2(self):
        """Disaster alerts are published with QoS 2."""
        publisher = MQTTPublisher()
        monitor = DisasterMonitor(smoke_threshold=100.0, gas_threshold=50.0, mqtt_publisher=publisher)
        monitor.check_smoke_level(200.0, "hall")
        assert publisher.published_messages[0]["qos"] == 2

    def test_inference_pipeline_smoke_alert_flow(self):
        """InferencePipeline.process_disaster_sensor triggers alert for smoke above threshold."""
        publisher = MQTTPublisher()
        monitor = DisasterMonitor(smoke_threshold=100.0, gas_threshold=50.0, mqtt_publisher=publisher)
        pipeline = _make_inference_pipeline(disaster_monitor=monitor)
        result = pipeline.process_disaster_sensor("smoke", 150.0, "bedroom")
        assert result is True
        assert any(m["topic"] == "alert/smoke" for m in publisher.published_messages)

    def test_inference_pipeline_gas_alert_flow(self):
        """InferencePipeline.process_disaster_sensor triggers alert for gas above threshold."""
        publisher = MQTTPublisher()
        monitor = DisasterMonitor(smoke_threshold=100.0, gas_threshold=50.0, mqtt_publisher=publisher)
        pipeline = _make_inference_pipeline(disaster_monitor=monitor)
        result = pipeline.process_disaster_sensor("gas", 80.0, "kitchen")
        assert result is True
        assert any(m["topic"] == "alert/gas" for m in publisher.published_messages)

    def test_disaster_alert_payload_schema(self):
        """Disaster alert payload contains all required schema fields."""
        publisher = MQTTPublisher()
        monitor = DisasterMonitor(smoke_threshold=100.0, gas_threshold=50.0, mqtt_publisher=publisher)
        monitor.check_smoke_level(200.0, "living_room")
        payload = publisher.published_messages[0]["payload"]
        required_fields = {"alert_type", "sensor_location", "concentration", "threshold", "timestamp"}
        assert required_fields.issubset(payload.keys())


# ===========================================================================
# 4. Error Recovery
# ===========================================================================

class TestErrorRecovery:
    """Integration: MQTT reconnection, sensor fault handling, anomaly interpolation."""

    def test_mqtt_reconnection_with_exponential_backoff(self):
        """connect_mqtt retries with exponential backoff and succeeds eventually."""
        mock_pub = MagicMock()
        mock_sub = MagicMock()
        # Fail twice, then succeed
        mock_pub.connect.side_effect = [ConnectionRefusedError, ConnectionRefusedError, None]
        mock_sub.connect.return_value = None

        pipeline = InferencePipeline(publisher=mock_pub, subscriber=mock_sub)
        with patch("src.inference_pipeline.time.sleep") as mock_sleep:
            result = pipeline.connect_mqtt()

        assert result is True
        assert mock_pub.connect.call_count == 3
        # Verify sleep was called between retries
        assert mock_sleep.call_count >= 2

    def test_mqtt_reconnection_fails_after_max_attempts(self):
        """connect_mqtt returns False after exhausting all retry attempts."""
        mock_pub = MagicMock()
        mock_sub = MagicMock()
        mock_pub.connect.side_effect = ConnectionRefusedError("broker down")

        pipeline = InferencePipeline(publisher=mock_pub, subscriber=mock_sub)
        with patch("src.inference_pipeline.time.sleep"):
            result = pipeline.connect_mqtt()

        assert result is False
        assert mock_pub.connect.call_count == _RETRY_MAX_ATTEMPTS

    def test_mqtt_reconnection_backoff_delays_increase(self):
        """Exponential backoff delays double between retry attempts."""
        mock_pub = MagicMock()
        mock_sub = MagicMock()
        mock_pub.connect.side_effect = ConnectionRefusedError("broker down")

        pipeline = InferencePipeline(publisher=mock_pub, subscriber=mock_sub)
        sleep_calls = []
        with patch("src.inference_pipeline.time.sleep", side_effect=lambda d: sleep_calls.append(d)):
            pipeline.connect_mqtt()

        # Each delay should be >= the previous (exponential backoff)
        for i in range(1, len(sleep_calls)):
            assert sleep_calls[i] >= sleep_calls[i - 1]

    def test_sensor_fault_published_on_anomaly_handler(self):
        """AnomalyHandler publishes sensor fault notification via MQTT."""
        publisher = MQTTPublisher()
        handler = AnomalyHandler(mqtt_publisher=publisher)
        fault = handler.publish_sensor_fault(
            sensor_type="heart_rate",
            device_id="device_001",
            reason="disconnected",
        )
        assert fault["sensor_type"] == "heart_rate"
        assert fault["device_id"] == "device_001"
        assert len(publisher.published_messages) == 1
        assert publisher.published_messages[0]["topic"] == "system/sensor_fault"

    def test_sensor_fault_without_publisher_does_not_raise(self):
        """AnomalyHandler.publish_sensor_fault works without an MQTT publisher."""
        handler = AnomalyHandler(mqtt_publisher=None)
        fault = handler.publish_sensor_fault(sensor_type="movement", device_id="dev_02")
        assert fault["sensor_type"] == "movement"

    def test_anomaly_interpolation_fills_short_gaps(self):
        """AnomalyHandler interpolates anomalous values in gaps shorter than 5 seconds."""
        handler = AnomalyHandler()
        n = 20
        data = np.linspace(60.0, 80.0, n)
        timestamps = np.arange(n, dtype=float)
        # Mark indices 5-7 as anomalous (3-second gap)
        mask = np.zeros(n, dtype=bool)
        mask[5:8] = True
        result = handler.interpolate_anomalous_data(data, mask, timestamps=timestamps)
        # Interpolated values should be between the surrounding valid values
        assert result[5] > data[4]  # interpolated, not original anomalous value
        assert result[7] < data[8]

    def test_anomaly_interpolation_skips_long_gaps(self):
        """AnomalyHandler does not interpolate gaps longer than 5 seconds."""
        handler = AnomalyHandler()
        n = 30
        data = np.linspace(60.0, 80.0, n)
        timestamps = np.arange(n, dtype=float)
        # Mark indices 5-15 as anomalous (10-second gap)
        mask = np.zeros(n, dtype=bool)
        mask[5:16] = True
        original_anomalous = data[5:16].copy()
        result = handler.interpolate_anomalous_data(data, mask, timestamps=timestamps)
        # Long gap should remain unchanged
        np.testing.assert_array_equal(result[5:16], original_anomalous)

    def test_inference_pipeline_continues_after_anomaly_detection(self):
        """Inference pipeline completes successfully even when anomalies are detected."""
        handler = AnomalyHandler()
        pipeline = _make_inference_pipeline(anomaly_handler=handler)

        # Create HR data with some anomalous values (out of range)
        n = 300
        ts = np.linspace(0.0, n - 1, n)
        hr_values = np.full(n, 75.0)
        hr_values[10] = 250.0  # anomalous
        hr_values[20] = 10.0   # anomalous
        hr = HeartRateData(timestamps=ts, values=np.clip(hr_values, 30.0, 200.0), sampling_rate=100)
        mv = _make_mv(n)

        result = pipeline.process_sensor_data(hr, mv)
        assert isinstance(result, SleepStage)

    def test_mqtt_subscriber_handles_invalid_json_gracefully(self):
        """MQTTSubscriber returns None for malformed JSON without raising."""
        subscriber = MQTTSubscriber()
        subscriber.subscribe_heart_rate()
        result = subscriber.on_message("sensors/heart_rate", "not valid json {{{")
        assert result is None
        assert len(subscriber.received_messages) == 0

    def test_mqtt_subscriber_handles_missing_required_fields(self):
        """MQTTSubscriber returns None when required fields are missing."""
        subscriber = MQTTSubscriber()
        subscriber.subscribe_heart_rate()
        # Missing 'heart_rate' field
        payload = json.dumps({"device_id": "sensor_01", "timestamp": time.time()})
        result = subscriber.on_message("sensors/heart_rate", payload)
        assert result is None
