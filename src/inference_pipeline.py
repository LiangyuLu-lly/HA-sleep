"""Main inference pipeline for CNN-BiLSTM Sleep Algorithm.

Wires all components together:
  MQTT_Subscriber → Time_Synchronizer → Anomaly_Handler → Data_Normalizer
  → Wavelet_Denoiser / Movement_Filter → CNN_Extractor → BiLSTM_Analyzer
  → Sleep_Classifier → MQTT_Publisher

Also integrates Environment_Controller (sleep-stage-based smart home control)
and Disaster_Monitor (smoke / gas alerting).

Requirements: All integration requirements
"""

import logging
import time
from typing import Dict, Optional, Tuple

import numpy as np

from training_config.config_loader import load_config
from src.anomaly_handler import AnomalyHandler
from src.bilstm_analyzer import BiLSTMAnalyzer
from src.cnn_extractor import CNNExtractor
from src.data_normalizer import DataNormalizer
from src.data_structures import HeartRateData, MovementData, SleepStage
from src.disaster_monitor import DisasterMonitor
from src.environment_controller import EnvironmentController
from src.movement_filter import MovementFilter
from src.mqtt_publisher import MQTTPublisher
from src.mqtt_subscriber import MQTTSubscriber
from src.sleep_classifier import SleepClassifier
from src.time_synchronizer import TimeSynchronizer
from src.wavelet_denoiser import WaveletDenoiser

logger = logging.getLogger(__name__)

# Exponential backoff settings for MQTT reconnection
_RETRY_BASE_DELAY = 1.0   # seconds
_RETRY_MAX_DELAY = 60.0   # seconds
_RETRY_MAX_ATTEMPTS = 10


class InferencePipeline:
    """End-to-end inference pipeline for real-time sleep stage classification.

    Accepts dependency-injected component instances so that each component can
    be replaced with a mock or custom implementation in tests.  When a
    component is not supplied the pipeline creates a default instance using
    parameters from the loaded configuration.

    Usage::

        pipeline = InferencePipeline()
        stage = pipeline.process_sensor_data(hr_data, mv_data)
        alert = pipeline.process_disaster_sensor("smoke", 150.0, "bedroom")
    """

    def __init__(
        self,
        config_path: str = "training_config/config.json",
        *,
        subscriber: Optional[MQTTSubscriber] = None,
        publisher: Optional[MQTTPublisher] = None,
        time_synchronizer: Optional[TimeSynchronizer] = None,
        anomaly_handler: Optional[AnomalyHandler] = None,
        data_normalizer: Optional[DataNormalizer] = None,
        wavelet_denoiser: Optional[WaveletDenoiser] = None,
        movement_filter: Optional[MovementFilter] = None,
        cnn_extractor: Optional[CNNExtractor] = None,
        bilstm_analyzer: Optional[BiLSTMAnalyzer] = None,
        sleep_classifier: Optional[SleepClassifier] = None,
        environment_controller: Optional[EnvironmentController] = None,
        disaster_monitor: Optional[DisasterMonitor] = None,
    ) -> None:
        """Initialise all pipeline components from config, with DI overrides.

        Args:
            config_path: Path to JSON configuration file.  Falls back to
                         built-in defaults when the file is missing.
            subscriber: Optional injected MQTTSubscriber.
            publisher: Optional injected MQTTPublisher.
            time_synchronizer: Optional injected TimeSynchronizer.
            anomaly_handler: Optional injected AnomalyHandler.
            data_normalizer: Optional injected DataNormalizer.
            wavelet_denoiser: Optional injected WaveletDenoiser.
            movement_filter: Optional injected MovementFilter.
            cnn_extractor: Optional injected CNNExtractor.
            bilstm_analyzer: Optional injected BiLSTMAnalyzer.
            sleep_classifier: Optional injected SleepClassifier.
            environment_controller: Optional injected EnvironmentController.
            disaster_monitor: Optional injected DisasterMonitor.
        """
        self.config = load_config(config_path)
        mqtt_cfg = self.config.get("mqtt", {})

        # --- MQTT layer ---
        self.publisher: MQTTPublisher = publisher or MQTTPublisher(
            broker_address=mqtt_cfg.get("broker_address", "localhost"),
            broker_port=mqtt_cfg.get("broker_port", 1883),
        )
        self.subscriber: MQTTSubscriber = subscriber or MQTTSubscriber(
            broker_address=mqtt_cfg.get("broker_address", "localhost"),
            broker_port=mqtt_cfg.get("broker_port", 1883),
        )

        # --- Preprocessing ---
        self.time_synchronizer: TimeSynchronizer = time_synchronizer or TimeSynchronizer()
        self.anomaly_handler: AnomalyHandler = anomaly_handler or AnomalyHandler(
            mqtt_publisher=self.publisher
        )
        self.data_normalizer: DataNormalizer = data_normalizer or DataNormalizer(
            config_path=config_path
        )
        self.wavelet_denoiser: WaveletDenoiser = wavelet_denoiser or WaveletDenoiser(
            config_path=config_path
        )
        self.movement_filter: MovementFilter = movement_filter or MovementFilter(
            config_path=config_path
        )

        # --- Feature extraction & classification ---
        self.cnn_extractor: CNNExtractor = cnn_extractor or CNNExtractor()
        self.bilstm_analyzer: BiLSTMAnalyzer = bilstm_analyzer or BiLSTMAnalyzer()
        self.sleep_classifier: SleepClassifier = sleep_classifier or SleepClassifier()

        # --- Smart home control ---
        self.environment_controller: EnvironmentController = (
            environment_controller or EnvironmentController(mqtt_publisher=self.publisher)
        )

        # --- Disaster monitoring ---
        self.disaster_monitor: DisasterMonitor = disaster_monitor or DisasterMonitor.from_config(
            self.config, mqtt_publisher=self.publisher
        )

        logger.info("InferencePipeline initialised successfully.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_sensor_data(
        self,
        hr_data: HeartRateData,
        mv_data: MovementData,
    ) -> SleepStage:
        """Process dual-sensor data through the full inference pipeline.

        Pipeline stages:
        1. Time synchronization (align HR + movement timestamps)
        2. Anomaly detection & interpolation on both channels
        3. Z-score normalization (uses fitted parameters when available)
        4. Wavelet denoising on heart-rate channel
        5. Movement bandpass filtering on movement channel
        6. Build time-frequency matrix (1024 × 128 × 2)
        7. CNN feature extraction
        8. BiLSTM temporal analysis
        9. Sleep stage classification
        10. Publish sleep stage + environment control commands via MQTT

        Args:
            hr_data: Heart rate sensor data.
            mv_data: Movement sensor data.

        Returns:
            Predicted SleepStage enum value.
        """
        # 1. Time synchronization
        aligned_hr, aligned_mv = self.time_synchronizer.align_data(hr_data, mv_data)

        # 2. Anomaly detection & interpolation — heart rate
        hr_anomaly_mask = self._detect_hr_anomalies(aligned_hr.values)
        hr_values_clean = self.anomaly_handler.interpolate_anomalous_data(
            aligned_hr.values,
            hr_anomaly_mask,
            timestamps=aligned_hr.timestamps,
        )

        # Anomaly detection & interpolation — movement
        mv_anomaly_mask = np.array(
            [self.anomaly_handler.detect_movement_anomaly(v) for v in aligned_mv.values]
        )
        mv_values_clean = self.anomaly_handler.interpolate_anomalous_data(
            aligned_mv.values,
            mv_anomaly_mask,
            timestamps=aligned_mv.timestamps,
        )

        # 3. Z-score normalization (skip if normalizer not yet fitted)
        hr_norm, mv_norm = self._normalize(hr_values_clean, mv_values_clean)

        # 4. Wavelet denoising on heart-rate channel
        hr_denoised = self.wavelet_denoiser.denoise(hr_norm)

        # 5. Movement bandpass filtering
        mv_filtered = self.movement_filter.filter(mv_norm)

        # 6. Build time-frequency matrix (1024 × 128 × 2)
        tf_matrix = self._build_time_frequency_matrix(hr_denoised, mv_filtered)

        # 7. CNN feature extraction
        cnn_features = self.cnn_extractor.extract_features(tf_matrix)

        # 8. BiLSTM temporal analysis — flatten spatial dims to sequence
        bilstm_input = self._cnn_to_bilstm_input(cnn_features)
        bilstm_output = self.bilstm_analyzer.analyze(bilstm_input)

        # 9. Build the same feature vector the classifier was trained on:
        #    mean-pooled BiLSTM output + 12 handcrafted statistics from the
        #    raw windows.  Lazy-import to avoid a circular dependency.
        from src.training_pipeline import TrainingPipeline
        deep_feat = bilstm_output.mean(axis=0)  # (2*hidden_units,)
        handcrafted = TrainingPipeline._handcrafted_features(hr_denoised, mv_filtered)
        feature_vec = np.concatenate([deep_feat, handcrafted]).astype(np.float32)

        # 10. Sleep stage classification
        stage, confidence = self.sleep_classifier.classify(feature_vec)

        # 10. Publish results
        self.publisher.publish_sleep_stage(stage, confidence)
        self._publish_environment_control(stage)

        logger.info("Classified sleep stage: %s (confidence=%.3f)", stage.name, confidence)
        return stage

    def process_disaster_sensor(
        self,
        sensor_type: str,
        concentration: float,
        location: str,
    ) -> bool:
        """Check a disaster sensor reading and publish an alert if threshold exceeded.

        Args:
            sensor_type: ``"smoke"`` or ``"gas"``.
            concentration: Measured concentration value.
            location: Sensor location string (included in alert payload).

        Returns:
            ``True`` if the threshold was exceeded and an alert was published,
            ``False`` otherwise.
        """
        if sensor_type == "smoke":
            return self.disaster_monitor.check_smoke_level(concentration, location)
        elif sensor_type == "gas":
            return self.disaster_monitor.check_gas_level(concentration, location)
        else:
            logger.warning("Unknown disaster sensor type: %s", sensor_type)
            return False

    def connect_mqtt(self) -> bool:
        """Connect to the MQTT broker with exponential backoff retry.

        Attempts up to ``_RETRY_MAX_ATTEMPTS`` connections, doubling the wait
        time between each attempt (capped at ``_RETRY_MAX_DELAY`` seconds).

        Returns:
            ``True`` if connection succeeded, ``False`` if all attempts failed.
        """
        delay = _RETRY_BASE_DELAY
        for attempt in range(1, _RETRY_MAX_ATTEMPTS + 1):
            try:
                self.publisher.connect()
                self.subscriber.connect()
                logger.info("MQTT connected on attempt %d.", attempt)
                return True
            except Exception as exc:
                logger.warning(
                    "MQTT connection attempt %d/%d failed: %s. Retrying in %.1fs.",
                    attempt,
                    _RETRY_MAX_ATTEMPTS,
                    exc,
                    delay,
                )
                time.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_DELAY)

        logger.error("MQTT connection failed after %d attempts.", _RETRY_MAX_ATTEMPTS)
        return False

    def disconnect_mqtt(self) -> None:
        """Disconnect publisher and subscriber from the MQTT broker."""
        try:
            self.publisher.disconnect()
        except Exception as exc:  # pragma: no cover
            logger.warning("Error disconnecting publisher: %s", exc)
        try:
            self.subscriber.disconnect()
        except Exception as exc:  # pragma: no cover
            logger.warning("Error disconnecting subscriber: %s", exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _detect_hr_anomalies(self, hr_values: np.ndarray) -> np.ndarray:
        """Build a boolean anomaly mask for a heart-rate value array."""
        mask = np.zeros(len(hr_values), dtype=bool)
        prev = hr_values[0] if len(hr_values) > 0 else 75.0
        for i, val in enumerate(hr_values):
            time_delta = 1.0  # assume 1-second intervals when timestamps unavailable
            mask[i] = self.anomaly_handler.detect_heart_rate_anomaly(val, prev, time_delta)
            if not mask[i]:
                prev = val
        return mask

    def _normalize(
        self, hr_values: np.ndarray, mv_values: np.ndarray
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Apply Z-score normalization if the normalizer has been fitted.

        When the normalizer has not been fitted (e.g. during inference without
        prior training), the raw values are returned unchanged.
        """
        if self.data_normalizer._fitted:
            from src.data_structures import Dataset, HeartRateData, MovementData, SleepStages

            # Build minimal Dataset wrappers for the normalizer
            n = len(hr_values)
            ts = np.arange(n, dtype=float)
            # Clamp HR values to valid range for HeartRateData construction
            hr_clamped = np.clip(hr_values, 30.0, 200.0)
            hr_obj = HeartRateData(timestamps=ts, values=hr_clamped, sampling_rate=1)
            mv_obj = MovementData(timestamps=ts, values=mv_values, sampling_rate=1)
            stages_obj = SleepStages(
                timestamps=ts, stages=np.zeros(n, dtype=int)
            )
            from src.data_structures import Dataset
            dataset = Dataset(
                heart_rate=hr_obj,
                movement=mv_obj,
                sleep_stages=stages_obj,
                subject_ids=["inference"],
            )
            normalized = self.data_normalizer.transform(dataset)
            return normalized.heart_rate.values, normalized.movement.values
        else:
            logger.debug("DataNormalizer not fitted; skipping normalization.")
            return hr_values.copy(), mv_values.copy()

    def _build_time_frequency_matrix(
        self, hr_signal: np.ndarray, mv_signal: np.ndarray
    ) -> np.ndarray:
        """Build a (1024, 128, 2) time-frequency matrix from 1-D signals.

        Uses a simple STFT-like approach: reshape the signal into 1024 time
        frames × 128 frequency bins.  When the signal is shorter than required
        it is zero-padded; when longer it is truncated.

        Args:
            hr_signal: 1-D heart-rate signal (any length).
            mv_signal: 1-D movement signal (any length).

        Returns:
            Array of shape (1024, 128, 2).
        """
        required = 1024 * 128  # 131 072 samples

        def _pad_or_truncate(sig: np.ndarray) -> np.ndarray:
            if len(sig) >= required:
                return sig[:required]
            padded = np.zeros(required, dtype=np.float32)
            padded[: len(sig)] = sig
            return padded

        hr_flat = _pad_or_truncate(hr_signal.astype(np.float32))
        mv_flat = _pad_or_truncate(mv_signal.astype(np.float32))

        hr_matrix = hr_flat.reshape(1024, 128)
        mv_matrix = mv_flat.reshape(1024, 128)

        return np.stack([hr_matrix, mv_matrix], axis=-1)  # (1024, 128, 2)

    def _cnn_to_bilstm_input(self, cnn_features: np.ndarray) -> np.ndarray:
        """Reshape CNN output (256, 32, 64) to BiLSTM input (T, feature_dim).

        Flattens the spatial dimensions (32 × 64 = 2048) and treats the 256
        time steps as the sequence length.

        Args:
            cnn_features: Array of shape (256, 32, 64).

        Returns:
            Array of shape (256, 2048).
        """
        # cnn_features: (H, W, C) = (256, 32, 64)
        H = cnn_features.shape[0]
        return cnn_features.reshape(H, -1)  # (256, 32*64)

    def _publish_environment_control(self, stage: SleepStage) -> None:
        """Generate and publish lighting, temperature, and humidity commands."""
        try:
            self.environment_controller.generate_lighting_control(stage)
            self.environment_controller.generate_temperature_control(stage)
            self.environment_controller.generate_humidity_control(stage)
        except Exception as exc:  # pragma: no cover
            logger.error("Failed to publish environment control commands: %s", exc)
