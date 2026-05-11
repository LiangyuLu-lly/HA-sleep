"""MQTT Subscriber for dual-sensor data acquisition.

Subscribes to heart rate, movement, smoke, and gas sensor topics.
Validates incoming JSON payloads, checks timestamp freshness (<5 seconds),
validates heart rate range [30, 200] bpm, and marks out-of-range data as anomalous.

Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9, 11.10
"""

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Optional paho-mqtt import
try:
    import paho.mqtt.client as mqtt

    PAHO_AVAILABLE = True
except ImportError:  # pragma: no cover
    PAHO_AVAILABLE = False
    mqtt = None  # type: ignore[assignment]

# Heart rate valid range (bpm)
HEART_RATE_MIN = 30.0
HEART_RATE_MAX = 200.0

# Maximum age of a message before it is discarded (seconds)
TIMESTAMP_MAX_AGE = 5.0


class MQTTSubscriber:
    """Subscribes to MQTT sensor topics and validates incoming messages.

    Supported topics:
    - ``sensors/heart_rate``  – heart rate sensor data
    - ``sensors/movement``    – movement/accelerometer data
    - ``sensors/smoke``       – smoke concentration data
    - ``sensors/gas``         – gas concentration data

    Message format (JSON):
    - Heart rate:  {"device_id": "...", "timestamp": <unix_float>, "heart_rate": <bpm>}
    - Movement:    {"device_id": "...", "timestamp": <unix_float>, "movement_amplitude": <float>}
    - Smoke:       {"device_id": "...", "timestamp": <unix_float>, "smoke_concentration": <float>}
    - Gas:         {"device_id": "...", "timestamp": <unix_float>, "gas_concentration": <float>}

    Validated messages are stored in ``self.received_messages``.  Each entry is a
    dict with the parsed payload fields plus an ``anomalous`` flag.
    """

    def __init__(
        self,
        broker_address: str = "localhost",
        broker_port: int = 1883,
        client_id: str = "",
    ) -> None:
        """Initialise the subscriber.

        Args:
            broker_address: MQTT broker hostname or IP address.
            broker_port: MQTT broker port (default 1883).
            client_id: Optional MQTT client identifier.
        """
        self.broker_address = broker_address
        self.broker_port = broker_port
        self.client_id = client_id

        # Accumulated validated messages keyed by topic
        self.received_messages: List[Dict[str, Any]] = []

        # Optional user-supplied callback invoked after each validated message
        self._message_callback: Optional[Callable[[Dict[str, Any]], None]] = None

        # Subscribed topics
        self._subscribed_topics: List[str] = []

        # Paho client (may be None when paho is unavailable or not yet connected)
        self._client: Optional[Any] = None

        if PAHO_AVAILABLE:
            self._client = mqtt.Client(client_id=client_id)
            self._client.on_message = self._paho_on_message

    # ------------------------------------------------------------------
    # Public subscription API
    # ------------------------------------------------------------------

    def subscribe_heart_rate(self, topic: str = "sensors/heart_rate") -> None:
        """Subscribe to the heart rate sensor topic.

        Args:
            topic: MQTT topic string (default ``sensors/heart_rate``).
        """
        self._subscribe(topic)

    def subscribe_movement(self, topic: str = "sensors/movement") -> None:
        """Subscribe to the movement sensor topic.

        Args:
            topic: MQTT topic string (default ``sensors/movement``).
        """
        self._subscribe(topic)

    def subscribe_smoke(self, topic: str = "sensors/smoke") -> None:
        """Subscribe to the smoke sensor topic.

        Args:
            topic: MQTT topic string (default ``sensors/smoke``).
        """
        self._subscribe(topic)

    def subscribe_gas(self, topic: str = "sensors/gas") -> None:
        """Subscribe to the gas sensor topic.

        Args:
            topic: MQTT topic string (default ``sensors/gas``).
        """
        self._subscribe(topic)

    def set_message_callback(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Register a callback invoked with each validated message dict."""
        self._message_callback = callback

    # ------------------------------------------------------------------
    # Message processing
    # ------------------------------------------------------------------

    def on_message(self, topic: str, payload: str) -> Optional[Dict[str, Any]]:
        """Parse, validate, and store an incoming MQTT message.

        This method is the central processing point for all sensor messages.
        It is called automatically by the Paho callback and can also be
        invoked directly (e.g. in tests).

        Processing steps:
        1. Parse JSON payload.
        2. Validate required fields are present.
        3. Check timestamp freshness – discard messages older than 5 seconds.
        4. Validate sensor-specific value ranges and mark anomalous if needed.
        5. Store the validated message and invoke the user callback.

        Args:
            topic: The MQTT topic the message arrived on.
            payload: Raw JSON string payload.

        Returns:
            The processed message dict (with ``anomalous`` flag) if the message
            was accepted, or ``None`` if it was discarded (e.g. stale timestamp
            or JSON parse error).
        """
        # --- Step 1: Parse JSON ---
        try:
            data: Dict[str, Any] = json.loads(payload)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Failed to parse JSON payload on topic '%s': %s", topic, exc)
            return None

        # --- Step 2: Validate common required fields ---
        if not isinstance(data, dict):
            logger.warning("Payload on topic '%s' is not a JSON object", topic)
            return None

        if "timestamp" not in data:
            logger.warning("Missing 'timestamp' field in message on topic '%s'", topic)
            return None

        # --- Step 3: Timestamp freshness check ---
        try:
            msg_timestamp = float(data["timestamp"])
        except (TypeError, ValueError):
            logger.warning("Invalid 'timestamp' value on topic '%s': %s", topic, data.get("timestamp"))
            return None

        age = time.time() - msg_timestamp
        if age > TIMESTAMP_MAX_AGE:
            logger.warning(
                "Discarding stale message on topic '%s': age=%.2fs (max %.1fs)",
                topic,
                age,
                TIMESTAMP_MAX_AGE,
            )
            return None

        # --- Step 4: Topic-specific validation ---
        message = dict(data)
        message["topic"] = topic
        message["anomalous"] = False

        if topic == "sensors/heart_rate":
            message = self._validate_heart_rate(message)
        elif topic == "sensors/movement":
            message = self._validate_movement(message)
        elif topic == "sensors/smoke":
            message = self._validate_smoke(message)
        elif topic == "sensors/gas":
            message = self._validate_gas(message)
        # Unknown topics are accepted without additional validation

        if message is None:
            return None

        # --- Step 5: Store and notify ---
        self.received_messages.append(message)
        if self._message_callback is not None:
            try:
                self._message_callback(message)
            except Exception as exc:  # pragma: no cover
                logger.error("Message callback raised an exception: %s", exc)

        return message

    # ------------------------------------------------------------------
    # Sensor-specific validators
    # ------------------------------------------------------------------

    def _validate_heart_rate(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Validate heart rate message fields.

        Marks the message as anomalous if ``heart_rate`` is outside [30, 200] bpm.
        Returns None (and logs a warning) if the required field is missing.
        """
        if "heart_rate" not in message:
            logger.warning(
                "Missing 'heart_rate' field in message on topic '%s'", message.get("topic")
            )
            return None

        try:
            hr_value = float(message["heart_rate"])
        except (TypeError, ValueError):
            logger.warning(
                "Invalid 'heart_rate' value: %s", message.get("heart_rate")
            )
            return None

        if not (HEART_RATE_MIN <= hr_value <= HEART_RATE_MAX):
            logger.warning(
                "Heart rate value %.1f bpm is outside valid range [%.0f, %.0f] bpm – marking anomalous",
                hr_value,
                HEART_RATE_MIN,
                HEART_RATE_MAX,
            )
            message["anomalous"] = True

        return message

    def _validate_movement(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Validate movement message fields.

        Marks the message as anomalous if ``movement_amplitude`` is negative
        (physically impossible amplitude).
        Returns None if the required field is missing.
        """
        if "movement_amplitude" not in message:
            logger.warning(
                "Missing 'movement_amplitude' field in message on topic '%s'",
                message.get("topic"),
            )
            return None

        try:
            mv_value = float(message["movement_amplitude"])
        except (TypeError, ValueError):
            logger.warning(
                "Invalid 'movement_amplitude' value: %s", message.get("movement_amplitude")
            )
            return None

        if mv_value < 0.0:
            logger.warning(
                "Movement amplitude %.4f is negative – marking anomalous", mv_value
            )
            message["anomalous"] = True

        return message

    def _validate_smoke(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Validate smoke sensor message fields.

        Marks the message as anomalous if ``smoke_concentration`` is negative.
        Returns None if the required field is missing.
        """
        if "smoke_concentration" not in message:
            logger.warning(
                "Missing 'smoke_concentration' field in message on topic '%s'",
                message.get("topic"),
            )
            return None

        try:
            smoke_value = float(message["smoke_concentration"])
        except (TypeError, ValueError):
            logger.warning(
                "Invalid 'smoke_concentration' value: %s", message.get("smoke_concentration")
            )
            return None

        if smoke_value < 0.0:
            logger.warning(
                "Smoke concentration %.2f is negative – marking anomalous", smoke_value
            )
            message["anomalous"] = True

        return message

    def _validate_gas(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Validate gas sensor message fields.

        Marks the message as anomalous if ``gas_concentration`` is negative.
        Returns None if the required field is missing.
        """
        if "gas_concentration" not in message:
            logger.warning(
                "Missing 'gas_concentration' field in message on topic '%s'",
                message.get("topic"),
            )
            return None

        try:
            gas_value = float(message["gas_concentration"])
        except (TypeError, ValueError):
            logger.warning(
                "Invalid 'gas_concentration' value: %s", message.get("gas_concentration")
            )
            return None

        if gas_value < 0.0:
            logger.warning(
                "Gas concentration %.2f is negative – marking anomalous", gas_value
            )
            message["anomalous"] = True

        return message

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _subscribe(self, topic: str) -> None:
        """Register a topic subscription (and subscribe via Paho if connected)."""
        if topic not in self._subscribed_topics:
            self._subscribed_topics.append(topic)
            logger.info("Registered subscription for topic: %s", topic)

        if self._client is not None:
            try:
                self._client.subscribe(topic, qos=1)
            except Exception as exc:  # pragma: no cover
                logger.warning("Could not subscribe to topic '%s' via Paho: %s", topic, exc)

    def _paho_on_message(self, client: Any, userdata: Any, msg: Any) -> None:  # pragma: no cover
        """Paho MQTT on_message callback – delegates to on_message()."""
        try:
            payload_str = msg.payload.decode("utf-8")
        except Exception as exc:
            logger.warning("Failed to decode message payload: %s", exc)
            return
        self.on_message(msg.topic, payload_str)

    def connect(self) -> None:  # pragma: no cover
        """Connect to the MQTT broker and start the network loop."""
        if self._client is None:
            raise RuntimeError("Paho MQTT is not available")
        self._client.connect(self.broker_address, self.broker_port)
        self._client.loop_start()

    def disconnect(self) -> None:  # pragma: no cover
        """Stop the network loop and disconnect from the broker."""
        if self._client is not None:
            self._client.loop_stop()
            self._client.disconnect()
