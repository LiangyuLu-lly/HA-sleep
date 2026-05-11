"""Anomaly Handler for dual-sensor heart rate and movement data.

Detects anomalous sensor readings, interpolates short gaps, detects sensor
faults, and publishes fault notifications via MQTT.

Requirements: 18.1, 18.2, 18.3, 18.4, 18.5, 18.6, 18.7, 18.8
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Heart rate valid range (bpm)
HR_MIN = 30.0
HR_MAX = 200.0

# Maximum allowed heart rate rate-of-change (bpm/s)
HR_MAX_RATE_OF_CHANGE = 50.0

# Movement amplitude must be non-negative
MV_MIN = 0.0

# MQTT topic for sensor fault notifications
TOPIC_SENSOR_FAULT = "system/sensor_fault"

# Maximum gap duration for interpolation (seconds)
MAX_INTERPOLATION_GAP_SECONDS = 5.0


class AnomalyHandler:
    """Detects and handles anomalous heart rate and movement sensor data.

    Responsibilities:
    - Detect out-of-range or physiologically implausible heart rate values
    - Detect out-of-range movement amplitude values
    - Interpolate anomalous data points for gaps shorter than 5 seconds
    - Detect sensor disconnection and publish fault messages to MQTT
    - Log all anomaly events with timestamps

    The ``mqtt_publisher`` argument is optional; when provided it must expose a
    ``_publish(topic, payload, qos)`` method (compatible with
    :class:`src.mqtt_publisher.MQTTPublisher`).  When omitted, fault messages
    are only logged.
    """

    def __init__(self, mqtt_publisher: Optional[Any] = None) -> None:
        """Initialise the anomaly handler.

        Args:
            mqtt_publisher: Optional MQTT publisher used to send sensor-fault
                messages.  Must expose ``_publish(topic, payload, qos)``.
        """
        self._mqtt_publisher = mqtt_publisher

    # ------------------------------------------------------------------
    # Public detection API
    # ------------------------------------------------------------------

    def detect_heart_rate_anomaly(
        self,
        hr_value: float,
        prev_hr_value: float,
        time_delta: float,
    ) -> bool:
        """Detect whether a heart rate reading is anomalous.

        A reading is anomalous when:
        - It falls outside the valid range [30, 200] bpm  (Req 18.1)
        - The rate of change exceeds 50 bpm/s             (Req 18.2)

        Args:
            hr_value: Current heart rate reading (bpm).
            prev_hr_value: Previous valid heart rate reading (bpm).
            time_delta: Time elapsed since the previous reading (seconds).
                        Must be > 0 for rate-of-change check.

        Returns:
            ``True`` if the reading is anomalous, ``False`` otherwise.
        """
        # Range check (Req 18.1)
        if not (HR_MIN <= hr_value <= HR_MAX):
            logger.warning(
                "Heart rate anomaly detected at %.3f: value %.2f bpm outside [%s, %s] bpm",
                time.time(),
                hr_value,
                HR_MIN,
                HR_MAX,
            )
            return True

        # Rate-of-change check (Req 18.2)
        if time_delta > 0:
            rate_of_change = abs(hr_value - prev_hr_value) / time_delta
            if rate_of_change > HR_MAX_RATE_OF_CHANGE:
                logger.warning(
                    "Heart rate anomaly detected at %.3f: rate of change %.2f bpm/s exceeds %.1f bpm/s",
                    time.time(),
                    rate_of_change,
                    HR_MAX_RATE_OF_CHANGE,
                )
                return True

        return False

    def detect_movement_anomaly(self, mv_value: float) -> bool:
        """Detect whether a movement amplitude reading is anomalous.

        A reading is anomalous when the amplitude is negative (Req 18.3).

        Args:
            mv_value: Movement amplitude reading.

        Returns:
            ``True`` if the reading is anomalous, ``False`` otherwise.
        """
        if mv_value < MV_MIN:
            logger.warning(
                "Movement anomaly detected at %.3f: amplitude %.4f is negative",
                time.time(),
                mv_value,
            )
            return True
        return False

    def interpolate_anomalous_data(
        self,
        data: np.ndarray,
        anomaly_mask: np.ndarray,
        timestamps: Optional[np.ndarray] = None,
        sampling_rate: float = 1.0,
    ) -> np.ndarray:
        """Interpolate anomalous data points using linear interpolation.

        Only gaps shorter than 5 seconds are interpolated (Req 18.4, 18.5).
        Longer gaps are left unchanged (the caller should handle them
        separately, e.g. by marking the segment as unreliable).

        Uses :func:`numpy.interp` for linear interpolation (Req constraint).

        Args:
            data: 1-D array of sensor values.
            anomaly_mask: Boolean array of the same length as ``data``.
                          ``True`` marks anomalous (to-be-interpolated) points.
            timestamps: Optional 1-D array of timestamps (seconds) aligned with
                        ``data``.  When provided, gap duration is computed from
                        the timestamps; otherwise it is estimated from
                        ``sampling_rate``.
            sampling_rate: Samples per second, used to estimate gap duration
                           when ``timestamps`` is not provided.

        Returns:
            A copy of ``data`` with short anomalous gaps replaced by linearly
            interpolated values.
        """
        result = data.copy().astype(float)
        n = len(data)

        if not np.any(anomaly_mask):
            return result

        valid_mask = ~anomaly_mask
        valid_indices = np.where(valid_mask)[0]

        if len(valid_indices) < 2:
            # Not enough valid points to interpolate – return unchanged
            logger.warning(
                "Insufficient valid data points for interpolation at %.3f",
                time.time(),
            )
            return result

        # Build a time axis for gap-duration estimation
        if timestamps is not None:
            time_axis = np.asarray(timestamps, dtype=float)
        else:
            time_axis = np.arange(n, dtype=float) / sampling_rate

        # Identify contiguous anomalous runs and decide whether to interpolate
        anomalous_indices = np.where(anomaly_mask)[0]

        # Group consecutive anomalous indices into runs
        runs: List[List[int]] = []
        if len(anomalous_indices) > 0:
            run: List[int] = [anomalous_indices[0]]
            for idx in anomalous_indices[1:]:
                if idx == run[-1] + 1:
                    run.append(idx)
                else:
                    runs.append(run)
                    run = [idx]
            runs.append(run)

        # For each run, check gap duration and interpolate if short enough
        indices_to_interpolate: List[int] = []
        for run in runs:
            start_idx = run[0]
            end_idx = run[-1]

            # Determine the bounding valid timestamps
            prev_valid = valid_indices[valid_indices < start_idx]
            next_valid = valid_indices[valid_indices > end_idx]

            if len(prev_valid) == 0 or len(next_valid) == 0:
                # Gap at the edge – cannot interpolate
                continue

            gap_start_time = time_axis[prev_valid[-1]]
            gap_end_time = time_axis[next_valid[0]]
            gap_duration = gap_end_time - gap_start_time

            if gap_duration < MAX_INTERPOLATION_GAP_SECONDS:
                indices_to_interpolate.extend(run)
            else:
                logger.info(
                    "Anomalous gap of %.2f s at index %d exceeds %.1f s limit; skipping interpolation",
                    gap_duration,
                    start_idx,
                    MAX_INTERPOLATION_GAP_SECONDS,
                )

        if indices_to_interpolate:
            interpolated_values = np.interp(
                time_axis[indices_to_interpolate],
                time_axis[valid_indices],
                data[valid_indices],
            )
            result[indices_to_interpolate] = interpolated_values
            logger.info(
                "Interpolated %d anomalous data points at %.3f",
                len(indices_to_interpolate),
                time.time(),
            )

        return result

    # ------------------------------------------------------------------
    # Sensor fault detection and MQTT publishing
    # ------------------------------------------------------------------

    def publish_sensor_fault(
        self,
        sensor_type: str,
        device_id: str = "unknown",
        reason: str = "disconnected",
        qos: int = 1,
    ) -> Dict[str, Any]:
        """Publish a sensor fault notification to ``system/sensor_fault``.

        Called when a sensor disconnection is detected (Req 18.6, 18.7).
        The event is always logged regardless of whether an MQTT publisher is
        configured (Req 18.8).

        Args:
            sensor_type: Type of the faulty sensor, e.g. ``"heart_rate"`` or
                         ``"movement"``.
            device_id: Identifier of the faulty device.
            reason: Human-readable reason for the fault.
            qos: MQTT QoS level (default 1).

        Returns:
            The fault message payload dict.
        """
        payload: Dict[str, Any] = {
            "sensor_type": sensor_type,
            "device_id": device_id,
            "reason": reason,
            "timestamp": time.time(),
        }

        logger.error(
            "Sensor fault [%s] device=%s reason=%s at %.3f",
            sensor_type,
            device_id,
            reason,
            payload["timestamp"],
        )

        if self._mqtt_publisher is not None:
            try:
                self._mqtt_publisher._publish(TOPIC_SENSOR_FAULT, payload, qos)
            except Exception as exc:  # pragma: no cover
                logger.error(
                    "Failed to publish sensor fault to '%s': %s",
                    TOPIC_SENSOR_FAULT,
                    exc,
                )
        else:
            # No publisher – log the serialised payload so it is traceable
            logger.warning(
                "No MQTT publisher configured; sensor fault payload: %s",
                json.dumps(payload),
            )

        return payload
