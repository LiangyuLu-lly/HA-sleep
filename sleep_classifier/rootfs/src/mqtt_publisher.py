"""MQTT Publisher for sleep stage, environment control, and disaster alerts.

Publishes sleep stage results, environment control commands, and disaster
alert messages via MQTT with appropriate QoS levels and JSON schema validation.

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 14.3, 14.4, 14.5, 14.6, 14.7
"""

import json
import logging
import time
from typing import Any, Dict, List, Optional

from src.data_structures import SleepStage

logger = logging.getLogger(__name__)

# Optional paho-mqtt import
try:
    import paho.mqtt.client as mqtt

    PAHO_AVAILABLE = True
except ImportError:  # pragma: no cover
    PAHO_AVAILABLE = False
    mqtt = None  # type: ignore[assignment]

# Topic constants
TOPIC_SLEEP_STAGE = "sleep/stage"
TOPIC_LIGHTING_CONTROL = "control/lighting"
TOPIC_TEMPERATURE_CONTROL = "control/temperature"
TOPIC_HUMIDITY_CONTROL = "control/humidity"
TOPIC_SMOKE_ALERT = "alert/smoke"
TOPIC_GAS_ALERT = "alert/gas"

# QoS levels
QOS_SLEEP_STAGE = 1
QOS_ENVIRONMENT_CONTROL = 1
QOS_DISASTER_ALERT = 2

# Latency thresholds (seconds)
SLEEP_STAGE_LATENCY_LIMIT = 0.5   # 500 ms
DISASTER_ALERT_LATENCY_LIMIT = 0.1  # 100 ms

# Valid control types for environment control
VALID_CONTROL_TYPES = {"lighting", "temperature", "humidity"}

# Valid alert types for disaster alerts
VALID_ALERT_TYPES = {"smoke", "gas"}

# JSON schema required fields
_SLEEP_STAGE_REQUIRED = {"device_id", "timestamp", "sleep_stage", "confidence"}
_ENV_CONTROL_REQUIRED = {"control_type", "target_value", "priority", "timestamp"}
_DISASTER_ALERT_REQUIRED = {"alert_type", "sensor_location", "concentration", "threshold", "timestamp"}


def _validate_sleep_stage_schema(payload: Dict[str, Any]) -> None:
    """Validate sleep stage message against required JSON schema.

    Args:
        payload: Message payload dict.

    Raises:
        ValueError: If required fields are missing or have incorrect types.
    """
    missing = _SLEEP_STAGE_REQUIRED - payload.keys()
    if missing:
        raise ValueError(f"Sleep stage message missing required fields: {missing}")

    if not isinstance(payload["device_id"], str):
        raise ValueError("'device_id' must be a string")
    if not isinstance(payload["timestamp"], (int, float)):
        raise ValueError("'timestamp' must be a number")
    if not isinstance(payload["sleep_stage"], str):
        raise ValueError("'sleep_stage' must be a string")
    if not isinstance(payload["confidence"], (int, float)):
        raise ValueError("'confidence' must be a number")
    if not (0.0 <= float(payload["confidence"]) <= 1.0):
        raise ValueError("'confidence' must be in [0, 1]")


def _validate_env_control_schema(payload: Dict[str, Any]) -> None:
    """Validate environment control message against required JSON schema.

    Args:
        payload: Message payload dict.

    Raises:
        ValueError: If required fields are missing or have incorrect types.
    """
    missing = _ENV_CONTROL_REQUIRED - payload.keys()
    if missing:
        raise ValueError(f"Environment control message missing required fields: {missing}")

    if not isinstance(payload["control_type"], str):
        raise ValueError("'control_type' must be a string")
    if payload["control_type"] not in VALID_CONTROL_TYPES:
        raise ValueError(f"'control_type' must be one of {VALID_CONTROL_TYPES}")
    if not isinstance(payload["target_value"], (int, float)):
        raise ValueError("'target_value' must be a number")
    if not isinstance(payload["priority"], int):
        raise ValueError("'priority' must be an integer")
    if not isinstance(payload["timestamp"], (int, float)):
        raise ValueError("'timestamp' must be a number")


def _validate_disaster_alert_schema(payload: Dict[str, Any]) -> None:
    """Validate disaster alert message against required JSON schema.

    Args:
        payload: Message payload dict.

    Raises:
        ValueError: If required fields are missing or have incorrect types.
    """
    missing = _DISASTER_ALERT_REQUIRED - payload.keys()
    if missing:
        raise ValueError(f"Disaster alert message missing required fields: {missing}")

    if not isinstance(payload["alert_type"], str):
        raise ValueError("'alert_type' must be a string")
    if payload["alert_type"] not in VALID_ALERT_TYPES:
        raise ValueError(f"'alert_type' must be one of {VALID_ALERT_TYPES}")
    if not isinstance(payload["sensor_location"], str):
        raise ValueError("'sensor_location' must be a string")
    if not isinstance(payload["concentration"], (int, float)):
        raise ValueError("'concentration' must be a number")
    if not isinstance(payload["threshold"], (int, float)):
        raise ValueError("'threshold' must be a number")
    if not isinstance(payload["timestamp"], (int, float)):
        raise ValueError("'timestamp' must be a number")


class MQTTPublisher:
    """Publishes sleep stage, environment control, and disaster alert messages via MQTT.

    All messages are validated against predefined JSON schemas before publishing.
    Published messages are tracked in ``self.published_messages`` for testing.

    Topics and QoS levels:
    - ``sleep/stage``           – sleep stage results (QoS 1)
    - ``control/lighting``      – lighting control commands (QoS 1)
    - ``control/temperature``   – temperature control commands (QoS 1)
    - ``control/humidity``      – humidity control commands (QoS 1)
    - ``alert/smoke``           – smoke disaster alerts (QoS 2)
    - ``alert/gas``             – gas disaster alerts (QoS 2)

    Latency constraints:
    - Sleep stage messages: < 500 ms
    - Disaster alert messages: < 100 ms
    """

    def __init__(
        self,
        broker_address: str = "localhost",
        broker_port: int = 1883,
        device_id: str = "sleep_monitor_001",
        client_id: str = "",
    ) -> None:
        """Initialise the publisher.

        Args:
            broker_address: MQTT broker hostname or IP address.
            broker_port: MQTT broker port (default 1883).
            device_id: Device identifier included in sleep stage messages.
            client_id: Optional MQTT client identifier.
        """
        self.broker_address = broker_address
        self.broker_port = broker_port
        self.device_id = device_id
        self.client_id = client_id

        # Track all published messages for testing/inspection
        self.published_messages: List[Dict[str, Any]] = []

        # Paho client (may be None when paho is unavailable or not yet connected)
        self._client: Optional[Any] = None

        if PAHO_AVAILABLE:
            self._client = mqtt.Client(client_id=client_id)

    # ------------------------------------------------------------------
    # Public publish API
    # ------------------------------------------------------------------

    def publish_sleep_stage(
        self,
        stage: SleepStage,
        confidence: float,
        device_id: Optional[str] = None,
        qos: int = QOS_SLEEP_STAGE,
    ) -> Dict[str, Any]:
        """Publish a sleep stage result to the ``sleep/stage`` topic.

        Validates the message against the sleep stage JSON schema and measures
        publish latency (must be < 500 ms).

        Args:
            stage: The detected sleep stage (SleepStage enum).
            confidence: Classification confidence in [0, 1].
            device_id: Override the default device ID.
            qos: MQTT QoS level (default 1).

        Returns:
            The published message payload dict.

        Raises:
            ValueError: If the message fails schema validation.
        """
        start_time = time.time()

        payload: Dict[str, Any] = {
            "device_id": device_id if device_id is not None else self.device_id,
            "timestamp": start_time,
            "sleep_stage": stage.name,
            "confidence": float(confidence),
        }

        _validate_sleep_stage_schema(payload)

        self._publish(TOPIC_SLEEP_STAGE, payload, qos)

        elapsed = time.time() - start_time
        if elapsed > SLEEP_STAGE_LATENCY_LIMIT:
            logger.warning(
                "Sleep stage publish latency %.3fs exceeded limit of %.3fs",
                elapsed,
                SLEEP_STAGE_LATENCY_LIMIT,
            )

        return payload

    def publish_environment_control(
        self,
        control_type: str,
        target_value: float,
        priority: int,
        qos: int = QOS_ENVIRONMENT_CONTROL,
    ) -> Dict[str, Any]:
        """Publish an environment control command.

        Publishes to the appropriate topic based on ``control_type``:
        - ``"lighting"``     → ``control/lighting``
        - ``"temperature"``  → ``control/temperature``
        - ``"humidity"``     → ``control/humidity``

        Args:
            control_type: Type of control (``"lighting"``, ``"temperature"``, or ``"humidity"``).
            target_value: Target value for the controlled parameter.
            priority: Command priority (integer).
            qos: MQTT QoS level (default 1).

        Returns:
            The published message payload dict.

        Raises:
            ValueError: If the message fails schema validation.
        """
        payload: Dict[str, Any] = {
            "control_type": control_type,
            "target_value": float(target_value),
            "priority": int(priority),
            "timestamp": time.time(),
        }

        _validate_env_control_schema(payload)

        topic_map = {
            "lighting": TOPIC_LIGHTING_CONTROL,
            "temperature": TOPIC_TEMPERATURE_CONTROL,
            "humidity": TOPIC_HUMIDITY_CONTROL,
        }
        topic = topic_map[control_type]

        self._publish(topic, payload, qos)
        return payload

    def publish_disaster_alert(
        self,
        alert_type: str,
        sensor_location: str,
        concentration: float,
        threshold: float,
        qos: int = QOS_DISASTER_ALERT,
    ) -> Dict[str, Any]:
        """Publish a disaster alert message.

        Publishes to ``alert/smoke`` or ``alert/gas`` depending on ``alert_type``.
        Uses QoS 2 by default to ensure exactly-once delivery.
        Measures publish latency (must be < 100 ms).

        Args:
            alert_type: Type of alert (``"smoke"`` or ``"gas"``).
            sensor_location: Location of the sensor that triggered the alert.
            concentration: Measured concentration value.
            threshold: Safety threshold that was exceeded.
            qos: MQTT QoS level (default 2).

        Returns:
            The published message payload dict.

        Raises:
            ValueError: If the message fails schema validation.
        """
        start_time = time.time()

        payload: Dict[str, Any] = {
            "alert_type": alert_type,
            "sensor_location": sensor_location,
            "concentration": float(concentration),
            "threshold": float(threshold),
            "timestamp": start_time,
        }

        _validate_disaster_alert_schema(payload)

        topic_map = {
            "smoke": TOPIC_SMOKE_ALERT,
            "gas": TOPIC_GAS_ALERT,
        }
        topic = topic_map[alert_type]

        self._publish(topic, payload, qos)

        elapsed = time.time() - start_time
        if elapsed > DISASTER_ALERT_LATENCY_LIMIT:
            logger.warning(
                "Disaster alert publish latency %.3fs exceeded limit of %.3fs",
                elapsed,
                DISASTER_ALERT_LATENCY_LIMIT,
            )

        return payload

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _publish(self, topic: str, payload: Dict[str, Any], qos: int) -> None:
        """Serialise payload to JSON, publish via Paho (if available), and track.

        Args:
            topic: MQTT topic string.
            payload: Message payload dict (must be JSON-serialisable).
            qos: MQTT QoS level.
        """
        payload_json = json.dumps(payload)

        if self._client is not None:
            try:
                self._client.publish(topic, payload_json, qos=qos)
            except Exception as exc:  # pragma: no cover
                logger.error("Failed to publish to topic '%s': %s", topic, exc)

        # Always track regardless of Paho availability
        self.published_messages.append({
            "topic": topic,
            "payload": payload,
            "qos": qos,
        })
        logger.debug("Published to '%s' (QoS %d): %s", topic, qos, payload_json)

    def connect(self) -> None:  # pragma: no cover
        """Connect to the MQTT broker."""
        if self._client is None:
            raise RuntimeError("Paho MQTT is not available")
        self._client.connect(self.broker_address, self.broker_port)
        self._client.loop_start()

    def disconnect(self) -> None:  # pragma: no cover
        """Disconnect from the MQTT broker."""
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
