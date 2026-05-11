"""Disaster Monitor for smoke and gas hazard detection.

Monitors smoke and gas sensor readings against configurable safety thresholds
and publishes alerts via MQTT with QoS 2 when thresholds are exceeded.

Requirements: 14.1, 14.2
"""

import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class DisasterMonitor:
    """Monitors smoke and gas sensor levels and publishes alerts when thresholds are exceeded.

    Reads safety thresholds from config (disaster_monitoring.smoke_threshold and
    disaster_monitoring.gas_threshold). When a concentration exceeds its threshold,
    an alert is published via MQTTPublisher with QoS 2.

    Requirements: 14.1, 14.2
    """

    def __init__(
        self,
        smoke_threshold: float,
        gas_threshold: float,
        mqtt_publisher=None,
    ) -> None:
        """Initialize with safety thresholds.

        Args:
            smoke_threshold: Smoke concentration threshold (e.g. 100.0 ppm).
                             Loaded from config disaster_monitoring.smoke_threshold.
            gas_threshold: Gas concentration threshold (e.g. 50.0 ppm).
                           Loaded from config disaster_monitoring.gas_threshold.
            mqtt_publisher: Optional MQTTPublisher instance used to publish alerts.
                            If None, alerts are only logged.
        """
        self.smoke_threshold = smoke_threshold
        self.gas_threshold = gas_threshold
        self._mqtt_publisher = mqtt_publisher

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_smoke_level(
        self,
        smoke_concentration: float,
        sensor_location: str = "unknown",
    ) -> bool:
        """Check if smoke concentration exceeds the safety threshold.

        If the concentration exceeds the threshold and an MQTTPublisher is
        configured, a smoke alert is published to ``alert/smoke`` with QoS 2.

        Args:
            smoke_concentration: Measured smoke concentration value.
            sensor_location: Location of the sensor (included in alert message).

        Returns:
            True if smoke_concentration > smoke_threshold, False otherwise.
        """
        exceeded = smoke_concentration > self.smoke_threshold
        if exceeded:
            logger.warning(
                "Smoke threshold exceeded: %.2f > %.2f at %s",
                smoke_concentration,
                self.smoke_threshold,
                sensor_location,
            )
            self._publish_alert(
                alert_type="smoke",
                sensor_location=sensor_location,
                concentration=smoke_concentration,
                threshold=self.smoke_threshold,
            )
        return exceeded

    def check_gas_level(
        self,
        gas_concentration: float,
        sensor_location: str = "unknown",
    ) -> bool:
        """Check if gas concentration exceeds the safety threshold.

        If the concentration exceeds the threshold and an MQTTPublisher is
        configured, a gas alert is published to ``alert/gas`` with QoS 2.

        Args:
            gas_concentration: Measured gas concentration value.
            sensor_location: Location of the sensor (included in alert message).

        Returns:
            True if gas_concentration > gas_threshold, False otherwise.
        """
        exceeded = gas_concentration > self.gas_threshold
        if exceeded:
            logger.warning(
                "Gas threshold exceeded: %.2f > %.2f at %s",
                gas_concentration,
                self.gas_threshold,
                sensor_location,
            )
            self._publish_alert(
                alert_type="gas",
                sensor_location=sensor_location,
                concentration=gas_concentration,
                threshold=self.gas_threshold,
            )
        return exceeded

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _publish_alert(
        self,
        alert_type: str,
        sensor_location: str,
        concentration: float,
        threshold: float,
    ) -> None:
        """Publish a disaster alert via MQTT with QoS 2.

        Args:
            alert_type: ``"smoke"`` or ``"gas"``.
            sensor_location: Location of the triggering sensor.
            concentration: Measured concentration value.
            threshold: Safety threshold that was exceeded.
        """
        if self._mqtt_publisher is None:
            return

        try:
            self._mqtt_publisher.publish_disaster_alert(
                alert_type=alert_type,
                sensor_location=sensor_location,
                concentration=concentration,
                threshold=threshold,
                qos=2,
            )
        except Exception as exc:  # pragma: no cover
            logger.error("Failed to publish %s alert: %s", alert_type, exc)

    # ------------------------------------------------------------------
    # Factory helper
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls, config: dict, mqtt_publisher=None) -> "DisasterMonitor":
        """Create a DisasterMonitor from a config dict.

        Reads ``disaster_monitoring.smoke_threshold`` and
        ``disaster_monitoring.gas_threshold`` from the config.

        Args:
            config: Configuration dictionary (e.g. loaded from training_config.json).
            mqtt_publisher: Optional MQTTPublisher instance.

        Returns:
            Configured DisasterMonitor instance.
        """
        dm_cfg = config.get("disaster_monitoring", {})
        smoke_threshold = float(dm_cfg.get("smoke_threshold", 100.0))
        gas_threshold = float(dm_cfg.get("gas_threshold", 50.0))
        return cls(
            smoke_threshold=smoke_threshold,
            gas_threshold=gas_threshold,
            mqtt_publisher=mqtt_publisher,
        )
