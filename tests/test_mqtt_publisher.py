"""Tests for MQTTPublisher.

Covers:
- Unit tests for publish_sleep_stage(), publish_environment_control(),
  publish_disaster_alert()
- JSON schema validation (required fields, correct types)
- QoS levels
- Topic routing
- Message tracking
- Property-based test: MQTT message format conformance (Property 14)

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 14.3, 14.4, 14.5, 14.6, 14.7
"""

import json
import time
from typing import Any, Dict

import pytest
from hypothesis import given, settings, strategies as st

from src.data_structures import SleepStage
from src.mqtt_publisher import (
    TOPIC_GAS_ALERT,
    TOPIC_HUMIDITY_CONTROL,
    TOPIC_LIGHTING_CONTROL,
    TOPIC_SLEEP_STAGE,
    TOPIC_SMOKE_ALERT,
    TOPIC_TEMPERATURE_CONTROL,
    QOS_DISASTER_ALERT,
    QOS_ENVIRONMENT_CONTROL,
    QOS_SLEEP_STAGE,
    MQTTPublisher,
    _validate_disaster_alert_schema,
    _validate_env_control_schema,
    _validate_sleep_stage_schema,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_publisher() -> MQTTPublisher:
    return MQTTPublisher(device_id="test_device_001")


# ---------------------------------------------------------------------------
# Initialisation tests
# ---------------------------------------------------------------------------

class TestMQTTPublisherInit:
    def test_default_attributes(self):
        pub = MQTTPublisher()
        assert pub.broker_address == "localhost"
        assert pub.broker_port == 1883
        assert pub.device_id == "sleep_monitor_001"
        assert pub.published_messages == []

    def test_custom_device_id(self):
        pub = MQTTPublisher(device_id="my_device")
        assert pub.device_id == "my_device"


# ---------------------------------------------------------------------------
# publish_sleep_stage tests (Requirements 12.1, 12.2, 12.3, 12.4)
# ---------------------------------------------------------------------------

class TestPublishSleepStage:
    def test_publishes_to_correct_topic(self):
        pub = make_publisher()
        pub.publish_sleep_stage(SleepStage.DEEP, 0.92)
        assert pub.published_messages[0]["topic"] == TOPIC_SLEEP_STAGE

    def test_uses_qos_1(self):
        pub = make_publisher()
        pub.publish_sleep_stage(SleepStage.DEEP, 0.92)
        assert pub.published_messages[0]["qos"] == QOS_SLEEP_STAGE
        assert pub.published_messages[0]["qos"] == 1

    def test_payload_contains_device_id(self):
        pub = make_publisher()
        result = pub.publish_sleep_stage(SleepStage.DEEP, 0.92)
        assert result["device_id"] == "test_device_001"

    def test_payload_contains_timestamp(self):
        pub = make_publisher()
        before = time.time()
        result = pub.publish_sleep_stage(SleepStage.DEEP, 0.92)
        after = time.time()
        assert before <= result["timestamp"] <= after

    def test_payload_contains_sleep_stage_name(self):
        pub = make_publisher()
        result = pub.publish_sleep_stage(SleepStage.REM, 0.85)
        assert result["sleep_stage"] == "REM"

    def test_payload_contains_confidence(self):
        pub = make_publisher()
        result = pub.publish_sleep_stage(SleepStage.LIGHT, 0.75)
        assert result["confidence"] == 0.75

    def test_all_sleep_stages_published(self):
        pub = make_publisher()
        for stage in SleepStage:
            pub.publish_sleep_stage(stage, 0.9)
        assert len(pub.published_messages) == 4

    def test_custom_device_id_override(self):
        pub = make_publisher()
        result = pub.publish_sleep_stage(SleepStage.AWAKE, 0.8, device_id="override_device")
        assert result["device_id"] == "override_device"

    def test_message_tracked(self):
        pub = make_publisher()
        pub.publish_sleep_stage(SleepStage.DEEP, 0.92)
        assert len(pub.published_messages) == 1

    def test_invalid_confidence_above_1_raises(self):
        pub = make_publisher()
        with pytest.raises(ValueError):
            pub.publish_sleep_stage(SleepStage.DEEP, 1.5)

    def test_invalid_confidence_below_0_raises(self):
        pub = make_publisher()
        with pytest.raises(ValueError):
            pub.publish_sleep_stage(SleepStage.DEEP, -0.1)

    def test_confidence_at_boundaries_accepted(self):
        pub = make_publisher()
        pub.publish_sleep_stage(SleepStage.DEEP, 0.0)
        pub.publish_sleep_stage(SleepStage.DEEP, 1.0)
        assert len(pub.published_messages) == 2


# ---------------------------------------------------------------------------
# publish_environment_control tests (Requirements 12.1, 12.2 via env controller)
# ---------------------------------------------------------------------------

class TestPublishEnvironmentControl:
    def test_lighting_publishes_to_correct_topic(self):
        pub = make_publisher()
        pub.publish_environment_control("lighting", 0.0, 1)
        assert pub.published_messages[0]["topic"] == TOPIC_LIGHTING_CONTROL

    def test_temperature_publishes_to_correct_topic(self):
        pub = make_publisher()
        pub.publish_environment_control("temperature", 20.0, 1)
        assert pub.published_messages[0]["topic"] == TOPIC_TEMPERATURE_CONTROL

    def test_humidity_publishes_to_correct_topic(self):
        pub = make_publisher()
        pub.publish_environment_control("humidity", 55.0, 1)
        assert pub.published_messages[0]["topic"] == TOPIC_HUMIDITY_CONTROL

    def test_uses_qos_1(self):
        pub = make_publisher()
        pub.publish_environment_control("lighting", 0.0, 1)
        assert pub.published_messages[0]["qos"] == QOS_ENVIRONMENT_CONTROL
        assert pub.published_messages[0]["qos"] == 1

    def test_payload_contains_control_type(self):
        pub = make_publisher()
        result = pub.publish_environment_control("lighting", 0.5, 2)
        assert result["control_type"] == "lighting"

    def test_payload_contains_target_value(self):
        pub = make_publisher()
        result = pub.publish_environment_control("temperature", 19.5, 1)
        assert result["target_value"] == 19.5

    def test_payload_contains_priority(self):
        pub = make_publisher()
        result = pub.publish_environment_control("humidity", 60.0, 3)
        assert result["priority"] == 3

    def test_payload_contains_timestamp(self):
        pub = make_publisher()
        before = time.time()
        result = pub.publish_environment_control("lighting", 0.0, 1)
        after = time.time()
        assert before <= result["timestamp"] <= after

    def test_invalid_control_type_raises(self):
        pub = make_publisher()
        with pytest.raises(ValueError):
            pub.publish_environment_control("invalid_type", 0.0, 1)

    def test_message_tracked(self):
        pub = make_publisher()
        pub.publish_environment_control("lighting", 0.0, 1)
        assert len(pub.published_messages) == 1


# ---------------------------------------------------------------------------
# publish_disaster_alert tests (Requirements 14.3, 14.4, 14.5, 14.6, 14.7)
# ---------------------------------------------------------------------------

class TestPublishDisasterAlert:
    def test_smoke_alert_publishes_to_correct_topic(self):
        pub = make_publisher()
        pub.publish_disaster_alert("smoke", "bedroom", 150.0, 100.0)
        assert pub.published_messages[0]["topic"] == TOPIC_SMOKE_ALERT

    def test_gas_alert_publishes_to_correct_topic(self):
        pub = make_publisher()
        pub.publish_disaster_alert("gas", "kitchen", 75.0, 50.0)
        assert pub.published_messages[0]["topic"] == TOPIC_GAS_ALERT

    def test_uses_qos_2(self):
        pub = make_publisher()
        pub.publish_disaster_alert("smoke", "bedroom", 150.0, 100.0)
        assert pub.published_messages[0]["qos"] == QOS_DISASTER_ALERT
        assert pub.published_messages[0]["qos"] == 2

    def test_payload_contains_alert_type(self):
        pub = make_publisher()
        result = pub.publish_disaster_alert("smoke", "bedroom", 150.0, 100.0)
        assert result["alert_type"] == "smoke"

    def test_payload_contains_sensor_location(self):
        pub = make_publisher()
        result = pub.publish_disaster_alert("smoke", "bedroom", 150.0, 100.0)
        assert result["sensor_location"] == "bedroom"

    def test_payload_contains_concentration(self):
        pub = make_publisher()
        result = pub.publish_disaster_alert("smoke", "bedroom", 150.0, 100.0)
        assert result["concentration"] == 150.0

    def test_payload_contains_threshold(self):
        pub = make_publisher()
        result = pub.publish_disaster_alert("smoke", "bedroom", 150.0, 100.0)
        assert result["threshold"] == 100.0

    def test_payload_contains_timestamp(self):
        pub = make_publisher()
        before = time.time()
        result = pub.publish_disaster_alert("smoke", "bedroom", 150.0, 100.0)
        after = time.time()
        assert before <= result["timestamp"] <= after

    def test_invalid_alert_type_raises(self):
        pub = make_publisher()
        with pytest.raises(ValueError):
            pub.publish_disaster_alert("fire", "bedroom", 150.0, 100.0)

    def test_message_tracked(self):
        pub = make_publisher()
        pub.publish_disaster_alert("smoke", "bedroom", 150.0, 100.0)
        assert len(pub.published_messages) == 1


# ---------------------------------------------------------------------------
# Schema validation unit tests
# ---------------------------------------------------------------------------

class TestSchemaValidation:
    def test_sleep_stage_schema_valid(self):
        payload = {
            "device_id": "dev_001",
            "timestamp": time.time(),
            "sleep_stage": "DEEP",
            "confidence": 0.92,
        }
        _validate_sleep_stage_schema(payload)  # should not raise

    def test_sleep_stage_schema_missing_field_raises(self):
        payload = {"device_id": "dev_001", "timestamp": time.time(), "sleep_stage": "DEEP"}
        with pytest.raises(ValueError, match="missing required fields"):
            _validate_sleep_stage_schema(payload)

    def test_sleep_stage_schema_wrong_type_raises(self):
        payload = {
            "device_id": 123,  # should be str
            "timestamp": time.time(),
            "sleep_stage": "DEEP",
            "confidence": 0.92,
        }
        with pytest.raises(ValueError):
            _validate_sleep_stage_schema(payload)

    def test_env_control_schema_valid(self):
        payload = {
            "control_type": "lighting",
            "target_value": 0.5,
            "priority": 1,
            "timestamp": time.time(),
        }
        _validate_env_control_schema(payload)  # should not raise

    def test_env_control_schema_missing_field_raises(self):
        payload = {"control_type": "lighting", "target_value": 0.5, "priority": 1}
        with pytest.raises(ValueError, match="missing required fields"):
            _validate_env_control_schema(payload)

    def test_env_control_schema_invalid_control_type_raises(self):
        payload = {
            "control_type": "fan",
            "target_value": 0.5,
            "priority": 1,
            "timestamp": time.time(),
        }
        with pytest.raises(ValueError):
            _validate_env_control_schema(payload)

    def test_disaster_alert_schema_valid(self):
        payload = {
            "alert_type": "smoke",
            "sensor_location": "bedroom",
            "concentration": 150.0,
            "threshold": 100.0,
            "timestamp": time.time(),
        }
        _validate_disaster_alert_schema(payload)  # should not raise

    def test_disaster_alert_schema_missing_field_raises(self):
        payload = {
            "alert_type": "smoke",
            "sensor_location": "bedroom",
            "concentration": 150.0,
            "threshold": 100.0,
        }
        with pytest.raises(ValueError, match="missing required fields"):
            _validate_disaster_alert_schema(payload)

    def test_disaster_alert_schema_invalid_alert_type_raises(self):
        payload = {
            "alert_type": "flood",
            "sensor_location": "bedroom",
            "concentration": 150.0,
            "threshold": 100.0,
            "timestamp": time.time(),
        }
        with pytest.raises(ValueError):
            _validate_disaster_alert_schema(payload)


# ---------------------------------------------------------------------------
# Property-based test: MQTT message format conformance (Property 14)
# Validates: Requirements 12.6, 13.7, 14.8
# ---------------------------------------------------------------------------

# Hypothesis strategies
_sleep_stage_st = st.sampled_from(list(SleepStage))
_confidence_st = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_control_type_st = st.sampled_from(["lighting", "temperature", "humidity"])
_target_value_st = st.floats(min_value=-100.0, max_value=100.0, allow_nan=False, allow_infinity=False)
_priority_st = st.integers(min_value=0, max_value=10)
_alert_type_st = st.sampled_from(["smoke", "gas"])
_location_st = st.text(min_size=1, max_size=50, alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters="_- "))
_concentration_st = st.floats(min_value=0.0, max_value=10000.0, allow_nan=False, allow_infinity=False)
_threshold_st = st.floats(min_value=0.0, max_value=10000.0, allow_nan=False, allow_infinity=False)


# **Validates: Requirements 12.6**
@given(stage=_sleep_stage_st, confidence=_confidence_st)
@settings(max_examples=100)
def test_property_sleep_stage_message_format(stage: SleepStage, confidence: float):
    """Property 14 (partial): For any valid sleep stage and confidence,
    the published message must conform to the predefined JSON schema.
    """
    pub = make_publisher()
    result = pub.publish_sleep_stage(stage, confidence)

    # All required fields present
    assert "device_id" in result
    assert "timestamp" in result
    assert "sleep_stage" in result
    assert "confidence" in result

    # Correct types
    assert isinstance(result["device_id"], str)
    assert isinstance(result["timestamp"], float)
    assert isinstance(result["sleep_stage"], str)
    assert isinstance(result["confidence"], float)

    # sleep_stage is a valid SleepStage name
    assert result["sleep_stage"] in {s.name for s in SleepStage}

    # confidence in [0, 1]
    assert 0.0 <= result["confidence"] <= 1.0

    # Message is JSON-serialisable
    json.dumps(result)

    # Tracked in published_messages
    assert len(pub.published_messages) == 1
    assert pub.published_messages[0]["topic"] == TOPIC_SLEEP_STAGE
    assert pub.published_messages[0]["qos"] == 1


# **Validates: Requirements 13.7**
@given(
    control_type=_control_type_st,
    target_value=_target_value_st,
    priority=_priority_st,
)
@settings(max_examples=100)
def test_property_environment_control_message_format(
    control_type: str, target_value: float, priority: int
):
    """Property 14 (partial): For any valid environment control parameters,
    the published message must conform to the predefined JSON schema.
    """
    pub = make_publisher()
    result = pub.publish_environment_control(control_type, target_value, priority)

    # All required fields present
    assert "control_type" in result
    assert "target_value" in result
    assert "priority" in result
    assert "timestamp" in result

    # Correct types
    assert isinstance(result["control_type"], str)
    assert isinstance(result["target_value"], float)
    assert isinstance(result["priority"], int)
    assert isinstance(result["timestamp"], float)

    # control_type is valid
    assert result["control_type"] in {"lighting", "temperature", "humidity"}

    # Message is JSON-serialisable
    json.dumps(result)

    # Tracked with correct QoS
    assert len(pub.published_messages) == 1
    assert pub.published_messages[0]["qos"] == 1


# **Validates: Requirements 14.8**
@given(
    alert_type=_alert_type_st,
    sensor_location=_location_st,
    concentration=_concentration_st,
    threshold=_threshold_st,
)
@settings(max_examples=100)
def test_property_disaster_alert_message_format(
    alert_type: str, sensor_location: str, concentration: float, threshold: float
):
    """Property 14 (partial): For any valid disaster alert parameters,
    the published message must conform to the predefined JSON schema.
    """
    pub = make_publisher()
    result = pub.publish_disaster_alert(alert_type, sensor_location, concentration, threshold)

    # All required fields present
    assert "alert_type" in result
    assert "sensor_location" in result
    assert "concentration" in result
    assert "threshold" in result
    assert "timestamp" in result

    # Correct types
    assert isinstance(result["alert_type"], str)
    assert isinstance(result["sensor_location"], str)
    assert isinstance(result["concentration"], float)
    assert isinstance(result["threshold"], float)
    assert isinstance(result["timestamp"], float)

    # alert_type is valid
    assert result["alert_type"] in {"smoke", "gas"}

    # Message is JSON-serialisable
    json.dumps(result)

    # Tracked with QoS 2
    assert len(pub.published_messages) == 1
    assert pub.published_messages[0]["qos"] == 2
