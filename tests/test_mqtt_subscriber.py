"""Tests for MQTTSubscriber.

Covers:
- Unit tests for subscribe_*() methods and on_message() callback
- JSON parsing, timestamp freshness validation, heart rate range validation
- Anomaly marking for out-of-range data
- Requirements: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6, 11.7, 11.8, 11.9, 11.10
"""

import json
import time
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from src.mqtt_subscriber import (
    HEART_RATE_MAX,
    HEART_RATE_MIN,
    TIMESTAMP_MAX_AGE,
    MQTTSubscriber,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fresh_ts() -> float:
    """Return a timestamp that is 1 second old (well within the 5-second window)."""
    return time.time() - 1.0


def stale_ts() -> float:
    """Return a timestamp that is 10 seconds old (outside the 5-second window)."""
    return time.time() - 10.0


def hr_payload(heart_rate: float, ts: float = None) -> str:
    return json.dumps({
        "device_id": "hr_sensor_001",
        "timestamp": ts if ts is not None else fresh_ts(),
        "heart_rate": heart_rate,
    })


def mv_payload(movement_amplitude: float, ts: float = None) -> str:
    return json.dumps({
        "device_id": "mv_sensor_001",
        "timestamp": ts if ts is not None else fresh_ts(),
        "movement_amplitude": movement_amplitude,
    })


def smoke_payload(smoke_concentration: float, ts: float = None) -> str:
    return json.dumps({
        "device_id": "smoke_sensor_001",
        "timestamp": ts if ts is not None else fresh_ts(),
        "smoke_concentration": smoke_concentration,
    })


def gas_payload(gas_concentration: float, ts: float = None) -> str:
    return json.dumps({
        "device_id": "gas_sensor_001",
        "timestamp": ts if ts is not None else fresh_ts(),
        "gas_concentration": gas_concentration,
    })


# ---------------------------------------------------------------------------
# Initialisation tests
# ---------------------------------------------------------------------------

class TestMQTTSubscriberInit:
    def test_default_attributes(self):
        sub = MQTTSubscriber()
        assert sub.broker_address == "localhost"
        assert sub.broker_port == 1883
        assert sub.received_messages == []
        assert sub._subscribed_topics == []

    def test_custom_broker(self):
        sub = MQTTSubscriber(broker_address="192.168.1.1", broker_port=8883)
        assert sub.broker_address == "192.168.1.1"
        assert sub.broker_port == 8883


# ---------------------------------------------------------------------------
# Subscription registration tests (Requirements 11.1, 11.2)
# ---------------------------------------------------------------------------

class TestSubscriptionRegistration:
    def test_subscribe_heart_rate_registers_topic(self):
        sub = MQTTSubscriber()
        sub.subscribe_heart_rate()
        assert "sensors/heart_rate" in sub._subscribed_topics

    def test_subscribe_movement_registers_topic(self):
        sub = MQTTSubscriber()
        sub.subscribe_movement()
        assert "sensors/movement" in sub._subscribed_topics

    def test_subscribe_smoke_registers_topic(self):
        sub = MQTTSubscriber()
        sub.subscribe_smoke()
        assert "sensors/smoke" in sub._subscribed_topics

    def test_subscribe_gas_registers_topic(self):
        sub = MQTTSubscriber()
        sub.subscribe_gas()
        assert "sensors/gas" in sub._subscribed_topics

    def test_subscribe_custom_topic(self):
        sub = MQTTSubscriber()
        sub.subscribe_heart_rate(topic="custom/hr")
        assert "custom/hr" in sub._subscribed_topics

    def test_duplicate_subscription_not_added_twice(self):
        sub = MQTTSubscriber()
        sub.subscribe_heart_rate()
        sub.subscribe_heart_rate()
        assert sub._subscribed_topics.count("sensors/heart_rate") == 1


# ---------------------------------------------------------------------------
# JSON parsing tests (Requirements 11.3, 11.4)
# ---------------------------------------------------------------------------

class TestJSONParsing:
    def test_valid_heart_rate_message_accepted(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/heart_rate", hr_payload(72.5))
        assert result is not None
        assert result["heart_rate"] == 72.5

    def test_invalid_json_returns_none(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/heart_rate", "not-json")
        assert result is None

    def test_non_object_json_returns_none(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/heart_rate", json.dumps([1, 2, 3]))
        assert result is None

    def test_missing_timestamp_returns_none(self):
        sub = MQTTSubscriber()
        payload = json.dumps({"device_id": "x", "heart_rate": 72.5})
        result = sub.on_message("sensors/heart_rate", payload)
        assert result is None

    def test_valid_movement_message_accepted(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/movement", mv_payload(0.15))
        assert result is not None
        assert result["movement_amplitude"] == 0.15

    def test_valid_smoke_message_accepted(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/smoke", smoke_payload(50.0))
        assert result is not None
        assert result["smoke_concentration"] == 50.0

    def test_valid_gas_message_accepted(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/gas", gas_payload(25.0))
        assert result is not None
        assert result["gas_concentration"] == 25.0


# ---------------------------------------------------------------------------
# Timestamp freshness tests (Requirement 11.5, 11.8)
# ---------------------------------------------------------------------------

class TestTimestampFreshness:
    def test_fresh_message_accepted(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/heart_rate", hr_payload(72.5, ts=fresh_ts()))
        assert result is not None

    def test_stale_message_discarded(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/heart_rate", hr_payload(72.5, ts=stale_ts()))
        assert result is None

    def test_stale_message_not_stored(self):
        sub = MQTTSubscriber()
        sub.on_message("sensors/heart_rate", hr_payload(72.5, ts=stale_ts()))
        assert len(sub.received_messages) == 0

    def test_exactly_at_boundary_discarded(self):
        """A message exactly TIMESTAMP_MAX_AGE seconds old should be discarded."""
        sub = MQTTSubscriber()
        ts = time.time() - TIMESTAMP_MAX_AGE - 0.001  # just over the limit
        result = sub.on_message("sensors/heart_rate", hr_payload(72.5, ts=ts))
        assert result is None

    def test_invalid_timestamp_returns_none(self):
        sub = MQTTSubscriber()
        payload = json.dumps({"device_id": "x", "timestamp": "not-a-number", "heart_rate": 72.5})
        result = sub.on_message("sensors/heart_rate", payload)
        assert result is None


# ---------------------------------------------------------------------------
# Heart rate validation tests (Requirements 11.6, 11.9)
# ---------------------------------------------------------------------------

class TestHeartRateValidation:
    def test_valid_heart_rate_not_anomalous(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/heart_rate", hr_payload(72.5))
        assert result is not None
        assert result["anomalous"] is False

    def test_heart_rate_at_lower_bound_not_anomalous(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/heart_rate", hr_payload(HEART_RATE_MIN))
        assert result is not None
        assert result["anomalous"] is False

    def test_heart_rate_at_upper_bound_not_anomalous(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/heart_rate", hr_payload(HEART_RATE_MAX))
        assert result is not None
        assert result["anomalous"] is False

    def test_heart_rate_below_range_marked_anomalous(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/heart_rate", hr_payload(HEART_RATE_MIN - 1))
        assert result is not None
        assert result["anomalous"] is True

    def test_heart_rate_above_range_marked_anomalous(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/heart_rate", hr_payload(HEART_RATE_MAX + 1))
        assert result is not None
        assert result["anomalous"] is True

    def test_heart_rate_zero_marked_anomalous(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/heart_rate", hr_payload(0.0))
        assert result is not None
        assert result["anomalous"] is True

    def test_heart_rate_negative_marked_anomalous(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/heart_rate", hr_payload(-10.0))
        assert result is not None
        assert result["anomalous"] is True

    def test_missing_heart_rate_field_returns_none(self):
        sub = MQTTSubscriber()
        payload = json.dumps({"device_id": "x", "timestamp": fresh_ts()})
        result = sub.on_message("sensors/heart_rate", payload)
        assert result is None

    def test_anomalous_heart_rate_still_stored(self):
        """Out-of-range data should be stored (marked anomalous), not discarded."""
        sub = MQTTSubscriber()
        sub.on_message("sensors/heart_rate", hr_payload(250.0))
        assert len(sub.received_messages) == 1
        assert sub.received_messages[0]["anomalous"] is True


# ---------------------------------------------------------------------------
# Movement validation tests (Requirements 11.7, 11.10)
# ---------------------------------------------------------------------------

class TestMovementValidation:
    def test_valid_movement_not_anomalous(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/movement", mv_payload(0.15))
        assert result is not None
        assert result["anomalous"] is False

    def test_zero_movement_not_anomalous(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/movement", mv_payload(0.0))
        assert result is not None
        assert result["anomalous"] is False

    def test_negative_movement_marked_anomalous(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/movement", mv_payload(-0.5))
        assert result is not None
        assert result["anomalous"] is True

    def test_missing_movement_amplitude_returns_none(self):
        sub = MQTTSubscriber()
        payload = json.dumps({"device_id": "x", "timestamp": fresh_ts()})
        result = sub.on_message("sensors/movement", payload)
        assert result is None


# ---------------------------------------------------------------------------
# Smoke and gas validation tests
# ---------------------------------------------------------------------------

class TestSmokeValidation:
    def test_valid_smoke_not_anomalous(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/smoke", smoke_payload(50.0))
        assert result is not None
        assert result["anomalous"] is False

    def test_negative_smoke_marked_anomalous(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/smoke", smoke_payload(-1.0))
        assert result is not None
        assert result["anomalous"] is True

    def test_missing_smoke_concentration_returns_none(self):
        sub = MQTTSubscriber()
        payload = json.dumps({"device_id": "x", "timestamp": fresh_ts()})
        result = sub.on_message("sensors/smoke", payload)
        assert result is None


class TestGasValidation:
    def test_valid_gas_not_anomalous(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/gas", gas_payload(25.0))
        assert result is not None
        assert result["anomalous"] is False

    def test_negative_gas_marked_anomalous(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/gas", gas_payload(-5.0))
        assert result is not None
        assert result["anomalous"] is True

    def test_missing_gas_concentration_returns_none(self):
        sub = MQTTSubscriber()
        payload = json.dumps({"device_id": "x", "timestamp": fresh_ts()})
        result = sub.on_message("sensors/gas", payload)
        assert result is None


# ---------------------------------------------------------------------------
# Message storage and callback tests
# ---------------------------------------------------------------------------

class TestMessageStorage:
    def test_valid_message_stored(self):
        sub = MQTTSubscriber()
        sub.on_message("sensors/heart_rate", hr_payload(72.5))
        assert len(sub.received_messages) == 1

    def test_multiple_messages_stored(self):
        sub = MQTTSubscriber()
        sub.on_message("sensors/heart_rate", hr_payload(72.5))
        sub.on_message("sensors/movement", mv_payload(0.1))
        sub.on_message("sensors/smoke", smoke_payload(30.0))
        assert len(sub.received_messages) == 3

    def test_invalid_message_not_stored(self):
        sub = MQTTSubscriber()
        sub.on_message("sensors/heart_rate", "bad-json")
        assert len(sub.received_messages) == 0

    def test_topic_stored_in_message(self):
        sub = MQTTSubscriber()
        result = sub.on_message("sensors/heart_rate", hr_payload(72.5))
        assert result["topic"] == "sensors/heart_rate"

    def test_callback_invoked_on_valid_message(self):
        sub = MQTTSubscriber()
        received: list = []
        sub.set_message_callback(received.append)
        sub.on_message("sensors/heart_rate", hr_payload(72.5))
        assert len(received) == 1
        assert received[0]["heart_rate"] == 72.5

    def test_callback_not_invoked_on_invalid_message(self):
        sub = MQTTSubscriber()
        received: list = []
        sub.set_message_callback(received.append)
        sub.on_message("sensors/heart_rate", "bad-json")
        assert len(received) == 0

    def test_unknown_topic_accepted_without_extra_validation(self):
        sub = MQTTSubscriber()
        payload = json.dumps({"device_id": "x", "timestamp": fresh_ts(), "value": 42})
        result = sub.on_message("sensors/unknown", payload)
        assert result is not None
        assert result["anomalous"] is False
