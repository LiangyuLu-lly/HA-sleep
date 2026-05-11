"""Home Assistant integration via the MQTT Discovery protocol.

This module exposes :class:`HomeAssistantBridge`, a thin layer that sits on top
of the existing :class:`src.mqtt_publisher.MQTTPublisher` and translates the
project's domain events (sleep stage classifications, environment control
hints, disaster alerts) into the topic / payload conventions that Home
Assistant understands.

Why MQTT Discovery
------------------
Home Assistant subscribes to ``<discovery_prefix>/+/+/+/config`` topics by
default (the prefix is configurable, ``homeassistant`` is the standard).  Any
JSON payload published on those topics is interpreted as a new entity
definition and the entity becomes available immediately, with no manual
``configuration.yaml`` editing.  Subsequent state updates are then published to
``state_topic`` and HA renders them in the dashboard.

The bridge registers six entities on first publish:

    sensor.<device>_sleep_stage          (AWAKE/LIGHT/DEEP/REM)
    sensor.<device>_sleep_confidence     (% 0-100)
    sensor.<device>_heart_rate           (bpm)
    sensor.<device>_movement_intensity   (a.u.)
    binary_sensor.<device>_smoke_alarm
    binary_sensor.<device>_gas_alarm

All entities are grouped under a single HA *device* (``device_id`` from the
configuration) so the user sees one tidy card in the dashboard.

Design notes
------------
* The bridge **does not** issue calls to ``light.turn_on`` / ``climate.set_*``
  directly.  Following HA best practices, control logic lives in HA
  automations that listen to the sensor entities we publish.  A reference
  automation YAML is shipped in ``docs/ha_automations.yaml``.
* When ``paho-mqtt`` is not installed (or the broker is unreachable) the
  bridge falls back to logging mode so unit tests and offline demos still run.
* All publish operations are safe to call repeatedly; HA deduplicates by
  ``unique_id`` so re-running the service will not create duplicate entities.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from src.data_structures import SleepStage

logger = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt  # type: ignore[import]

    PAHO_AVAILABLE = True
except ImportError:  # pragma: no cover
    PAHO_AVAILABLE = False
    mqtt = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Configuration container
# ---------------------------------------------------------------------------


@dataclass
class HAConfig:
    """Parsed Home Assistant section of ``config/config.json``."""

    enabled: bool = True
    discovery_prefix: str = "homeassistant"
    device_id: str = "sleep_classifier_bedroom"
    device_name: str = "Bedroom Sleep Classifier"
    device_manufacturer: str = "CNN-BiLSTM Sleep Project"
    device_model: str = "CNN-BiLSTM-v1"
    device_sw_version: str = "1.0.0"
    state_topic: str = "sleep_classifier/state"
    availability_topic: str = "sleep_classifier/availability"
    publish_interval_seconds: float = 30.0
    expire_after_seconds: int = 120

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "HAConfig":
        """Build a config object from the nested ``home_assistant`` dict.

        Unknown fields are ignored so future config additions stay backward
        compatible.
        """
        valid_fields = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in raw.items() if k in valid_fields}
        return cls(**filtered)


@dataclass
class _EntityDef:
    """Internal description of one HA entity to publish via Discovery."""

    component: str            # "sensor" | "binary_sensor"
    object_id: str            # unique inside the device
    name: str                 # human-readable label shown in HA
    value_template: str       # Jinja2 template applied to state_topic JSON
    unit_of_measurement: Optional[str] = None
    device_class: Optional[str] = None
    icon: Optional[str] = None
    state_class: Optional[str] = None
    payload_on: Optional[str] = None   # binary_sensor only
    payload_off: Optional[str] = None


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------


class HomeAssistantBridge:
    """Bridge sleep-classifier events to a Home Assistant MQTT broker.

    Typical lifecycle::

        bridge = HomeAssistantBridge(ha_config, broker_host, broker_port)
        bridge.connect()
        bridge.publish_discovery()                # one-shot at startup
        bridge.publish_state(stage=..., conf=..., hr=..., mv=...)   # 30s loop
        ...
        bridge.publish_offline()                  # before shutdown
        bridge.disconnect()
    """

    # Entity catalogue (declared once at class load — values are pure data,
    # so it is fine to share across instances).
    _ENTITIES: List[_EntityDef] = [
        _EntityDef(
            component="sensor",
            object_id="sleep_stage",
            name="Sleep Stage",
            value_template="{{ value_json.sleep_stage }}",
            icon="mdi:bed",
        ),
        _EntityDef(
            component="sensor",
            object_id="sleep_confidence",
            name="Sleep Confidence",
            value_template="{{ value_json.confidence | float * 100 | round(1) }}",
            unit_of_measurement="%",
            state_class="measurement",
            icon="mdi:percent",
        ),
        _EntityDef(
            component="sensor",
            object_id="heart_rate",
            name="Heart Rate",
            value_template="{{ value_json.heart_rate | round(1) }}",
            unit_of_measurement="bpm",
            state_class="measurement",
            icon="mdi:heart-pulse",
        ),
        _EntityDef(
            component="sensor",
            object_id="movement_intensity",
            name="Movement Intensity",
            value_template="{{ value_json.movement | round(2) }}",
            state_class="measurement",
            icon="mdi:run",
        ),
        _EntityDef(
            component="binary_sensor",
            object_id="smoke_alarm",
            name="Smoke Alarm",
            value_template="{{ value_json.smoke_alarm }}",
            device_class="smoke",
            payload_on="ON",
            payload_off="OFF",
        ),
        _EntityDef(
            component="binary_sensor",
            object_id="gas_alarm",
            name="Gas Alarm",
            value_template="{{ value_json.gas_alarm }}",
            device_class="gas",
            payload_on="ON",
            payload_off="OFF",
        ),
    ]

    def __init__(
        self,
        config: HAConfig,
        broker_address: str = "localhost",
        broker_port: int = 1883,
        username: str = "",
        password: str = "",
        client_id: Optional[str] = None,
        mqtt_client_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        """Initialise the bridge.

        Args:
            config: Parsed :class:`HAConfig`.
            broker_address: MQTT broker host (typically same machine running HA).
            broker_port: Broker TCP port (1883 plain, 8883 TLS).
            username: Optional MQTT username (empty string = anonymous).
            password: Optional MQTT password.
            client_id: Override the default client id (defaults to device_id).
            mqtt_client_factory: Test seam — inject a fake paho.mqtt.Client.
        """
        self.config = config
        self.broker_address = broker_address
        self.broker_port = int(broker_port)
        self.username = username
        self.password = password
        self.client_id = client_id or config.device_id

        # When paho is not available, we still want everything in
        # ``published_messages`` so the demo / tests can inspect what *would*
        # have been sent.  This mirrors the strategy used in MQTTPublisher.
        self.published_messages: List[Dict[str, Any]] = []

        self._client: Optional[Any] = None
        if mqtt_client_factory is not None:
            self._client = mqtt_client_factory(client_id=self.client_id)
        elif PAHO_AVAILABLE:
            self._client = mqtt.Client(client_id=self.client_id)

        if self._client is not None and self.username:
            try:
                self._client.username_pw_set(self.username, self.password)
            except Exception as exc:  # pragma: no cover
                logger.warning("Failed to set MQTT credentials: %s", exc)

        # Last-Will-and-Testament: when the service dies, HA flips the
        # availability topic to ``offline`` and sensors are greyed-out in the
        # dashboard.  This must be set *before* ``connect()`` is called.
        if self._client is not None:
            try:
                self._client.will_set(
                    self.config.availability_topic,
                    payload="offline",
                    qos=1,
                    retain=True,
                )
            except Exception as exc:  # pragma: no cover
                logger.debug("will_set not available: %s", exc)

        self._connected = False

    # ------------------------------------------------------------------ #
    # Connection                                                         #
    # ------------------------------------------------------------------ #

    def connect(self, timeout: float = 5.0) -> bool:
        """Connect to the MQTT broker.  Returns True on success."""
        if self._client is None:
            logger.warning(
                "HomeAssistantBridge running in offline mode "
                "(paho-mqtt not installed)"
            )
            return False
        try:
            self._client.connect(self.broker_address, self.broker_port, keepalive=60)
            self._client.loop_start()
            self._connected = True
            logger.info(
                "Connected to MQTT broker %s:%d as '%s'",
                self.broker_address, self.broker_port, self.client_id,
            )
            return True
        except Exception as exc:
            logger.error(
                "MQTT connect failed (%s:%d): %s",
                self.broker_address, self.broker_port, exc,
            )
            self._connected = False
            return False

    def disconnect(self) -> None:
        """Stop the network loop and disconnect from the broker."""
        if self._client is None or not self._connected:
            return
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception as exc:  # pragma: no cover
            logger.warning("MQTT disconnect failed: %s", exc)
        finally:
            self._connected = False

    # ------------------------------------------------------------------ #
    # Discovery                                                          #
    # ------------------------------------------------------------------ #

    def _device_block(self) -> Dict[str, Any]:
        """Return the ``device`` block shared by every Discovery payload.

        Sharing the block (same ``identifiers``) is what makes HA group the
        six entities under one device card in the UI.
        """
        return {
            "identifiers": [self.config.device_id],
            "name": self.config.device_name,
            "manufacturer": self.config.device_manufacturer,
            "model": self.config.device_model,
            "sw_version": self.config.device_sw_version,
        }

    def _discovery_topic(self, entity: _EntityDef) -> str:
        return (
            f"{self.config.discovery_prefix}/{entity.component}"
            f"/{self.config.device_id}/{entity.object_id}/config"
        )

    def _discovery_payload(self, entity: _EntityDef) -> Dict[str, Any]:
        """Build the JSON payload for a single Discovery topic."""
        payload: Dict[str, Any] = {
            "name": entity.name,
            "unique_id": f"{self.config.device_id}_{entity.object_id}",
            "object_id": f"{self.config.device_id}_{entity.object_id}",
            "state_topic": self.config.state_topic,
            "availability_topic": self.config.availability_topic,
            "payload_available": "online",
            "payload_not_available": "offline",
            "value_template": entity.value_template,
            "expire_after": self.config.expire_after_seconds,
            "device": self._device_block(),
        }
        if entity.unit_of_measurement is not None:
            payload["unit_of_measurement"] = entity.unit_of_measurement
        if entity.device_class is not None:
            payload["device_class"] = entity.device_class
        if entity.icon is not None:
            payload["icon"] = entity.icon
        if entity.state_class is not None:
            payload["state_class"] = entity.state_class
        if entity.payload_on is not None:
            payload["payload_on"] = entity.payload_on
        if entity.payload_off is not None:
            payload["payload_off"] = entity.payload_off
        return payload

    def publish_discovery(self) -> List[Dict[str, Any]]:
        """Publish a Discovery message for every entity.

        Discovery messages are published **retained** so HA still finds them
        after a broker restart.  Returns the list of payloads that were sent
        (useful for unit tests).
        """
        sent: List[Dict[str, Any]] = []
        for entity in self._ENTITIES:
            topic = self._discovery_topic(entity)
            payload = self._discovery_payload(entity)
            self._publish_json(topic, payload, qos=1, retain=True)
            sent.append({"topic": topic, "payload": payload})
        # Announce availability *after* the entities are known to HA.
        self.publish_online()
        logger.info("Published Discovery for %d HA entities", len(sent))
        return sent

    def remove_discovery(self) -> None:
        """Tell HA to delete every entity registered by this bridge.

        HA interprets an empty retained payload on the discovery topic as
        "forget this entity".  Useful when uninstalling the service or moving
        ``device_id``.
        """
        for entity in self._ENTITIES:
            topic = self._discovery_topic(entity)
            self._publish_raw(topic, payload=b"", qos=1, retain=True)

    # ------------------------------------------------------------------ #
    # State updates                                                      #
    # ------------------------------------------------------------------ #

    def publish_state(
        self,
        *,
        sleep_stage: Optional[SleepStage] = None,
        confidence: Optional[float] = None,
        heart_rate: Optional[float] = None,
        movement: Optional[float] = None,
        smoke_alarm: Optional[bool] = None,
        gas_alarm: Optional[bool] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Publish a single consolidated state JSON.

        All entities listen to the same ``state_topic`` and pick their value
        from the JSON via ``value_template``.  This is the cheapest way to
        update many sensors atomically.

        Only fields provided (not ``None``) are included in the payload;
        omitted ones keep their previous value in HA until ``expire_after``
        elapses, at which point HA shows them as *unknown*.
        """
        payload: Dict[str, Any] = {"timestamp": time.time()}
        if sleep_stage is not None:
            payload["sleep_stage"] = sleep_stage.name
        if confidence is not None:
            payload["confidence"] = max(0.0, min(1.0, float(confidence)))
        if heart_rate is not None:
            payload["heart_rate"] = float(heart_rate)
        if movement is not None:
            payload["movement"] = float(movement)
        if smoke_alarm is not None:
            payload["smoke_alarm"] = "ON" if smoke_alarm else "OFF"
        if gas_alarm is not None:
            payload["gas_alarm"] = "ON" if gas_alarm else "OFF"
        if extra:
            payload.update(extra)

        self._publish_json(self.config.state_topic, payload, qos=1, retain=True)
        return payload

    def publish_online(self) -> None:
        """Mark the device available in HA."""
        self._publish_raw(
            self.config.availability_topic,
            payload=b"online",
            qos=1,
            retain=True,
        )

    def publish_offline(self) -> None:
        """Mark the device unavailable (grey-out in HA dashboard)."""
        self._publish_raw(
            self.config.availability_topic,
            payload=b"offline",
            qos=1,
            retain=True,
        )

    # ------------------------------------------------------------------ #
    # Internal publish helpers                                           #
    # ------------------------------------------------------------------ #

    def _publish_json(
        self,
        topic: str,
        payload: Dict[str, Any],
        qos: int = 1,
        retain: bool = False,
    ) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        self._publish_raw(topic, body.encode("utf-8"), qos=qos, retain=retain)

    def _publish_raw(
        self,
        topic: str,
        payload: bytes,
        qos: int,
        retain: bool,
    ) -> None:
        if self._client is not None and self._connected:
            try:
                self._client.publish(topic, payload, qos=qos, retain=retain)
            except Exception as exc:  # pragma: no cover
                logger.error("MQTT publish failed (%s): %s", topic, exc)
        # Always record so callers / tests can inspect, even in offline mode.
        self.published_messages.append(
            {
                "topic": topic,
                "payload": payload.decode("utf-8", errors="replace")
                if payload
                else "",
                "qos": qos,
                "retain": retain,
            }
        )
        logger.debug(
            "HA publish topic='%s' qos=%d retain=%s bytes=%d",
            topic, qos, retain, len(payload),
        )


__all__ = ["HAConfig", "HomeAssistantBridge"]
