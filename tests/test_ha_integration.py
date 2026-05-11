"""Unit tests for :mod:`src.ha_integration`.

These tests exercise the bridge in *offline mode* (no real broker required).
They focus on three behaviours that matter for Home Assistant compatibility:

1. **Discovery shape** — every entity payload contains the keys HA needs
   (``state_topic``, ``unique_id``, ``device`` block, value_template).
2. **State JSON contract** — fields the user supplies show up under exactly
   the keys the entity ``value_template``s expect.
3. **Lifecycle** — availability messages, retain flags, and last-will
   semantics behave as documented.
"""
from __future__ import annotations

import json
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from src.data_structures import SleepStage
from src.ha_integration import HAConfig, HomeAssistantBridge


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ha_config() -> HAConfig:
    """A minimal but realistic HA configuration."""
    return HAConfig(
        enabled=True,
        discovery_prefix="homeassistant",
        device_id="test_classifier",
        device_name="Test Classifier",
        state_topic="test_classifier/state",
        availability_topic="test_classifier/availability",
        publish_interval_seconds=30.0,
        expire_after_seconds=120,
    )


@pytest.fixture
def fake_client_factory():
    """Returns a callable that produces a fresh MagicMock paho client."""
    def _factory(**_kwargs: Any) -> MagicMock:
        client = MagicMock()
        client.connect = MagicMock(return_value=0)
        client.loop_start = MagicMock()
        client.loop_stop = MagicMock()
        client.disconnect = MagicMock()
        client.publish = MagicMock()
        client.username_pw_set = MagicMock()
        client.will_set = MagicMock()
        return client
    return _factory


@pytest.fixture
def bridge(ha_config: HAConfig, fake_client_factory) -> HomeAssistantBridge:
    return HomeAssistantBridge(
        config=ha_config,
        broker_address="broker.local",
        broker_port=1883,
        mqtt_client_factory=fake_client_factory,
    )


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


class TestHAConfig:
    def test_from_dict_uses_defaults_for_missing_keys(self):
        cfg = HAConfig.from_dict({"device_id": "x"})
        assert cfg.device_id == "x"
        assert cfg.discovery_prefix == "homeassistant"
        assert cfg.publish_interval_seconds == 30.0

    def test_from_dict_ignores_unknown_keys(self):
        cfg = HAConfig.from_dict({"device_id": "x", "garbage_key": 42})
        assert cfg.device_id == "x"
        assert not hasattr(cfg, "garbage_key")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_publishes_six_entities(self, bridge: HomeAssistantBridge):
        bridge._connected = True  # bypass real connect
        sent = bridge.publish_discovery()
        assert len(sent) == 6
        components = {item["payload"]["device"]["identifiers"][0] for item in sent}
        assert components == {"test_classifier"}

    def test_each_payload_has_required_ha_fields(self, bridge: HomeAssistantBridge):
        bridge._connected = True
        for entity in bridge.publish_discovery():
            payload = entity["payload"]
            for field in ("name", "state_topic", "unique_id", "value_template", "device"):
                assert field in payload, f"missing {field} in {payload}"
            assert payload["device"]["identifiers"] == ["test_classifier"]
            # unique_id must scope the entity to this device
            assert payload["unique_id"].startswith("test_classifier_")

    def test_topic_uses_discovery_prefix_and_device_id(
        self, bridge: HomeAssistantBridge,
    ):
        bridge._connected = True
        topics = [e["topic"] for e in bridge.publish_discovery()]
        for t in topics:
            assert t.startswith("homeassistant/")
            assert "/test_classifier/" in t
            assert t.endswith("/config")

    def test_binary_sensors_have_payload_on_off(self, bridge: HomeAssistantBridge):
        bridge._connected = True
        binary = [
            e for e in bridge.publish_discovery()
            if "binary_sensor" in e["topic"]
        ]
        assert len(binary) == 2  # smoke + gas
        for e in binary:
            assert e["payload"]["payload_on"] == "ON"
            assert e["payload"]["payload_off"] == "OFF"

    def test_sensor_units_present(self, bridge: HomeAssistantBridge):
        bridge._connected = True
        sent = bridge.publish_discovery()
        confidences = [p for p in sent if "sleep_confidence" in p["topic"]]
        assert confidences[0]["payload"]["unit_of_measurement"] == "%"
        heart = [p for p in sent if "heart_rate" in p["topic"]]
        assert heart[0]["payload"]["unit_of_measurement"] == "bpm"

    def test_remove_discovery_publishes_empty_retained(
        self, bridge: HomeAssistantBridge,
    ):
        bridge._connected = True
        bridge.remove_discovery()
        # Every entity should have an empty payload + retain=True
        retained_empty = [
            m for m in bridge.published_messages
            if m["retain"] is True and m["payload"] == ""
        ]
        assert len(retained_empty) == 6


# ---------------------------------------------------------------------------
# State updates
# ---------------------------------------------------------------------------


class TestStateUpdates:
    def test_publish_state_emits_to_state_topic(
        self, bridge: HomeAssistantBridge,
    ):
        bridge._connected = True
        bridge.publish_state(
            sleep_stage=SleepStage.DEEP,
            confidence=0.87,
            heart_rate=58.0,
            movement=0.12,
        )
        payloads = [m for m in bridge.published_messages
                    if m["topic"] == "test_classifier/state"]
        assert len(payloads) == 1
        body = json.loads(payloads[0]["payload"])
        assert body["sleep_stage"] == "DEEP"
        assert body["confidence"] == pytest.approx(0.87)
        assert body["heart_rate"] == 58.0
        assert body["movement"] == 0.12

    def test_omitted_fields_not_included(self, bridge: HomeAssistantBridge):
        bridge._connected = True
        bridge.publish_state(sleep_stage=SleepStage.AWAKE)
        body = json.loads(bridge.published_messages[-1]["payload"])
        assert body["sleep_stage"] == "AWAKE"
        assert "heart_rate" not in body
        assert "movement" not in body

    def test_confidence_is_clamped_to_unit_interval(
        self, bridge: HomeAssistantBridge,
    ):
        bridge._connected = True
        bridge.publish_state(sleep_stage=SleepStage.LIGHT, confidence=2.5)
        body = json.loads(bridge.published_messages[-1]["payload"])
        assert body["confidence"] == 1.0
        bridge.publish_state(sleep_stage=SleepStage.LIGHT, confidence=-0.4)
        body = json.loads(bridge.published_messages[-1]["payload"])
        assert body["confidence"] == 0.0

    def test_alarms_map_to_on_off_strings(self, bridge: HomeAssistantBridge):
        bridge._connected = True
        bridge.publish_state(smoke_alarm=True, gas_alarm=False)
        body = json.loads(bridge.published_messages[-1]["payload"])
        assert body["smoke_alarm"] == "ON"
        assert body["gas_alarm"] == "OFF"

    def test_state_publishes_with_retain(self, bridge: HomeAssistantBridge):
        bridge._connected = True
        bridge.publish_state(sleep_stage=SleepStage.LIGHT, confidence=0.5)
        state_msgs = [m for m in bridge.published_messages
                      if m["topic"] == "test_classifier/state"]
        assert state_msgs[-1]["retain"] is True


# ---------------------------------------------------------------------------
# Availability lifecycle
# ---------------------------------------------------------------------------


class TestAvailability:
    def test_publish_discovery_marks_device_online(
        self, bridge: HomeAssistantBridge,
    ):
        bridge._connected = True
        bridge.publish_discovery()
        avail = [m for m in bridge.published_messages
                 if m["topic"] == "test_classifier/availability"]
        assert avail[-1]["payload"] == "online"
        assert avail[-1]["retain"] is True

    def test_publish_offline_marks_device_offline(
        self, bridge: HomeAssistantBridge,
    ):
        bridge._connected = True
        bridge.publish_offline()
        avail = [m for m in bridge.published_messages
                 if m["topic"] == "test_classifier/availability"]
        assert avail[-1]["payload"] == "offline"

    def test_last_will_set_on_init(self, ha_config, fake_client_factory):
        client_holder: List[MagicMock] = []

        def capturing_factory(**kwargs):
            c = fake_client_factory(**kwargs)
            client_holder.append(c)
            return c

        HomeAssistantBridge(
            config=ha_config,
            broker_address="x",
            mqtt_client_factory=capturing_factory,
        )
        assert client_holder, "expected exactly one client to be built"
        client_holder[0].will_set.assert_called_once()
        kwargs = client_holder[0].will_set.call_args.kwargs
        assert kwargs.get("payload") == "offline"
        assert kwargs.get("retain") is True


# ---------------------------------------------------------------------------
# Offline / no-paho fallback
# ---------------------------------------------------------------------------


class TestOfflineMode:
    def test_offline_publish_still_records_payloads(self, ha_config: HAConfig):
        bridge = HomeAssistantBridge(
            config=ha_config,
            broker_address="unreachable.invalid",
            mqtt_client_factory=lambda **_kw: None,  # type: ignore[arg-type, return-value]
        )
        # Bridge should not crash and should still track payloads.
        bridge.publish_discovery()
        assert len(bridge.published_messages) >= 6 + 1  # entities + availability

    def test_connect_returns_false_when_client_is_none(self, ha_config: HAConfig):
        bridge = HomeAssistantBridge(
            config=ha_config,
            broker_address="x",
            mqtt_client_factory=lambda **_kw: None,  # type: ignore[arg-type, return-value]
        )
        assert bridge.connect() is False


# ---------------------------------------------------------------------------
# MQTT credentials
# ---------------------------------------------------------------------------


class TestCredentials:
    def test_username_password_set_on_client(
        self, ha_config: HAConfig, fake_client_factory,
    ):
        seen: List[MagicMock] = []

        def factory(**kw):
            c = fake_client_factory(**kw)
            seen.append(c)
            return c

        HomeAssistantBridge(
            config=ha_config,
            broker_address="x",
            username="alice",
            password="secret",
            mqtt_client_factory=factory,
        )
        seen[0].username_pw_set.assert_called_once_with("alice", "secret")

    def test_empty_username_skips_auth(
        self, ha_config: HAConfig, fake_client_factory,
    ):
        seen: List[MagicMock] = []

        def factory(**kw):
            c = fake_client_factory(**kw)
            seen.append(c)
            return c

        HomeAssistantBridge(
            config=ha_config,
            broker_address="x",
            username="",
            mqtt_client_factory=factory,
        )
        seen[0].username_pw_set.assert_not_called()
