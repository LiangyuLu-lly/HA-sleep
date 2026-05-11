"""Unit tests for :mod:`src.device_discovery`."""
from __future__ import annotations

from typing import Dict, List

import pytest

from src.device_discovery import (
    DeviceDiscovery,
    DiscoveryConfig,
    DiscoveryResult,
)
from src.ha_api_client import HAEntity


# ---------------------------------------------------------------------------
# Helper: build a HAEntity quickly
# ---------------------------------------------------------------------------


def _entity(
    entity_id: str,
    *,
    state: str = "0",
    device_class: str = "",
    unit: str = "",
    friendly_name: str = "",
    area: str = "",
) -> HAEntity:
    attrs: Dict[str, object] = {}
    if friendly_name:
        attrs["friendly_name"] = friendly_name
    if device_class:
        attrs["device_class"] = device_class
    if unit:
        attrs["unit_of_measurement"] = unit
    if area:
        attrs["area_id"] = area
    return HAEntity(entity_id=entity_id, state=state, attributes=attrs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def default_config() -> DiscoveryConfig:
    return DiscoveryConfig()


@pytest.fixture
def bedroom_entities() -> List[HAEntity]:
    """A realistic mix of entities reflecting a typical bedroom setup."""
    return [
        # Physiological sensors
        _entity("sensor.mi_band_5_heart_rate", state="72", unit="bpm",
                friendly_name="Mi Band Heart Rate", area="bedroom"),
        _entity("sensor.zepp_pulse", state="74", unit="bpm",
                friendly_name="Zepp Pulse", area="bedroom"),
        _entity("sensor.bedroom_mmwave_motion", state="0.4",
                friendly_name="Bedroom mmWave Motion", area="bedroom"),
        _entity("binary_sensor.bedroom_motion", state="off",
                device_class="motion", friendly_name="Motion Sensor",
                area="bedroom"),
        # Environment sensors
        _entity("sensor.bedroom_temperature", state="22.3",
                device_class="temperature", unit="°C", area="bedroom"),
        _entity("sensor.bedroom_humidity", state="48",
                device_class="humidity", unit="%", area="bedroom"),
        _entity("sensor.bedroom_illuminance", state="3",
                device_class="illuminance", unit="lx", area="bedroom"),
        # Actionable devices
        _entity("light.bedroom_ceiling", state="off",
                friendly_name="Bedroom Ceiling Light", area="bedroom"),
        _entity("light.bedroom_lamp", state="on", area="bedroom"),
        _entity("climate.bedroom_ac", state="cool", area="bedroom"),
        _entity("humidifier.bedroom_humidifier", state="on", area="bedroom"),
        _entity("fan.bedroom_fan", state="off", area="bedroom"),
        _entity("switch.bedroom_outlet", state="off", area="bedroom"),
        _entity("media_player.bedroom_speaker", state="idle", area="bedroom"),
        # Entities outside the bedroom — must be filtered out by area filter
        _entity("light.kitchen", state="on", area="kitchen"),
        _entity("sensor.kitchen_temperature", state="25", unit="°C",
                area="kitchen"),
        _entity("climate.living_room_ac", state="off", area="living_room"),
    ]


# ---------------------------------------------------------------------------
# Configuration parsing
# ---------------------------------------------------------------------------


class TestDiscoveryConfig:
    def test_from_dict_filters_unknown_keys(self):
        raw = {
            "heart_rate_keywords": ["pulse", "hr"],
            "junk": "ignored",
        }
        cfg = DiscoveryConfig.from_dict(raw)
        assert cfg.heart_rate_keywords == ["pulse", "hr"]

    def test_defaults_are_sensible(self):
        cfg = DiscoveryConfig()
        assert "heart_rate" in cfg.heart_rate_keywords
        assert "light" in cfg.controllable_domains
        assert "climate" in cfg.controllable_domains


# ---------------------------------------------------------------------------
# Discovery — no area filter
# ---------------------------------------------------------------------------


class TestDiscoveryNoFilter:
    def test_finds_all_heart_rate_sensors(self, default_config, bedroom_entities):
        result = DeviceDiscovery(default_config).discover(bedroom_entities)
        ids = {e.entity_id for e in result.sensors.heart_rate}
        assert ids == {"sensor.mi_band_5_heart_rate", "sensor.zepp_pulse"}

    def test_finds_movement_via_keyword_and_device_class(
        self, default_config, bedroom_entities,
    ):
        result = DeviceDiscovery(default_config).discover(bedroom_entities)
        ids = {e.entity_id for e in result.sensors.movement}
        assert "sensor.bedroom_mmwave_motion" in ids
        assert "binary_sensor.bedroom_motion" in ids

    def test_finds_actionable_devices(self, default_config, bedroom_entities):
        result = DeviceDiscovery(default_config).discover(bedroom_entities)
        assert len(result.devices.lights) == 3   # 2 bedroom + kitchen
        assert len(result.devices.climates) == 2  # bedroom + living_room
        assert len(result.devices.fans) == 1
        assert len(result.devices.humidifiers) == 1

    def test_temperature_and_humidity_have_distinct_buckets(
        self, default_config, bedroom_entities,
    ):
        result = DeviceDiscovery(default_config).discover(bedroom_entities)
        temp_ids = {e.entity_id for e in result.sensors.temperature}
        hum_ids = {e.entity_id for e in result.sensors.humidity}
        assert "sensor.bedroom_temperature" in temp_ids
        assert "sensor.bedroom_humidity" in hum_ids
        assert temp_ids.isdisjoint(hum_ids)


# ---------------------------------------------------------------------------
# Discovery — with area filter
# ---------------------------------------------------------------------------


class TestDiscoveryWithAreaFilter:
    def test_area_filter_excludes_other_rooms(self, bedroom_entities):
        cfg = DiscoveryConfig(area_filter="bedroom")
        result = DeviceDiscovery(cfg).discover(bedroom_entities)

        assert all("kitchen" not in e.entity_id and "living_room" not in e.entity_id
                   for e in result.devices.lights)
        assert all("kitchen" not in e.entity_id and "living_room" not in e.entity_id
                   for e in result.devices.climates)

    def test_area_filter_keeps_bedroom_entities(self, bedroom_entities):
        cfg = DiscoveryConfig(area_filter="bedroom")
        result = DeviceDiscovery(cfg).discover(bedroom_entities)
        assert len(result.devices.lights) == 2
        assert len(result.sensors.heart_rate) == 2


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_entity_list(self, default_config):
        result = DeviceDiscovery(default_config).discover([])
        assert result.sensors.heart_rate == []
        assert result.devices.lights == []
        assert not result.has_minimum_sensors()

    def test_has_minimum_sensors_with_only_hr(self, default_config):
        entities = [_entity("sensor.x_pulse", state="60", unit="bpm")]
        result = DeviceDiscovery(default_config).discover(entities)
        assert result.has_minimum_sensors()

    def test_has_minimum_sensors_with_only_movement(self, default_config):
        entities = [
            _entity("binary_sensor.motion", state="on", device_class="motion"),
        ]
        result = DeviceDiscovery(default_config).discover(entities)
        assert result.has_minimum_sensors()

    def test_explicit_excludes_takes_priority(self, bedroom_entities):
        cfg = DiscoveryConfig(
            explicit_excludes=["sensor.mi_band_5_heart_rate"],
        )
        result = DeviceDiscovery(cfg).discover(bedroom_entities)
        ids = {e.entity_id for e in result.sensors.heart_rate}
        assert "sensor.mi_band_5_heart_rate" not in ids
        assert "sensor.zepp_pulse" in ids

    def test_explicit_includes_overrides_area_filter(self):
        cfg = DiscoveryConfig(
            area_filter="bedroom",
            explicit_includes=["sensor.guest_pulse"],
        )
        entities = [
            _entity("sensor.guest_pulse", state="68", unit="bpm",
                    area="guest_room"),
        ]
        result = DeviceDiscovery(cfg).discover(entities)
        assert any(e.entity_id == "sensor.guest_pulse"
                   for e in result.sensors.heart_rate)

    def test_subscribed_ids_are_deduped(self, default_config):
        entities = [
            _entity("sensor.dual_role", state="22",
                    device_class="temperature", unit="°C",
                    friendly_name="Bedroom temperature"),
        ]
        result = DeviceDiscovery(default_config).discover(entities)
        ids = result.sensors.all_subscribed_entity_ids()
        # If the entity matched both temperature and "temp" keyword, it should
        # still only appear once.
        assert ids.count("sensor.dual_role") == 1


# ---------------------------------------------------------------------------
# DiscoveryResult API
# ---------------------------------------------------------------------------


class TestDiscoveryResult:
    def test_log_summary_does_not_raise(self, default_config, bedroom_entities,
                                        caplog):
        result = DeviceDiscovery(default_config).discover(bedroom_entities)
        with caplog.at_level("INFO"):
            result.log_summary()
        assert any("Device discovery" in r.message for r in caplog.records)

    def test_as_summary_returns_entity_ids(self, default_config, bedroom_entities):
        result = DeviceDiscovery(default_config).discover(bedroom_entities)
        summary = result.sensors.as_summary()
        assert "heart_rate" in summary
        assert isinstance(summary["heart_rate"], list)
        for value in summary["heart_rate"]:
            assert isinstance(value, str)
