"""Tests for the slot-binding + bilingual-keyword extensions of
:mod:`src.device_discovery`.

These exercises model real-world fixtures the user has on their HA setup:

* Xiaomi Smart Band 9 Pro publishing a heart-rate sensor in English,
* an ESPHome-bridged R60ABD1 mmWave radar that emits Chinese-named
  ``TS5 \u8fd0\u52a8\u72b6\u6001`` (movement) and ``TS6 \u547c\u5438\u4fe1\u606f`` (breathing) sensors,
* a Mi Home aircon and a couple of lights that the user wants the add-on
  to control \u2014 sometimes via auto-discovery, sometimes via explicit slot
  bindings filled from the add-on Configuration tab.
"""
from __future__ import annotations

from typing import Dict, List

import pytest

from src.device_discovery import (
    DEFAULT_BREATHING_KEYWORDS,
    DEFAULT_HEART_RATE_KEYWORDS,
    DEFAULT_MOVEMENT_KEYWORDS,
    DeviceDiscovery,
    DiscoveryConfig,
)
from src.ha_api_client import HAEntity


# ---------------------------------------------------------------------------
# Helpers
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
    """Mirror the helper in test_device_discovery.py for fixture parity."""
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
# Fixtures: a real-world R60ABD1 + Xiaomi Mi Band installation
# ---------------------------------------------------------------------------


@pytest.fixture
def r60abd1_radar_entities() -> List[HAEntity]:
    """Subset of the entities ESPHome creates for a SleepRadar R60ABD1.

    Friendly names are exactly the Chinese strings the user reported in
    Settings \u2192 Devices & Services.  Entity IDs follow the ``slugify``
    transformation HA performs on the friendly name.
    """
    return [
        _entity(
            "sensor.sleepradar_r60abd1_ts5_yundong_zhuangtai",
            state="active", friendly_name="TS5 \u8fd0\u52a8\u72b6\u6001",
        ),
        _entity(
            "sensor.sleepradar_r60abd1_ts6_huxi_xinxi",
            state="16", unit="rpm", friendly_name="TS6 \u547c\u5438\u4fe1\u606f",
        ),
        _entity(
            "sensor.sleepradar_r60abd1_ts7_shuimian_zhuangtai",
            state="light_sleep", friendly_name="TS7 \u7761\u7720\u72b6\u6001",
        ),
        _entity(
            "sensor.sleepradar_r60abd1_ts1_distance",
            state="0.85", unit="m", friendly_name="T1D \u76ee\u68071\u8ddd\u79bb",
        ),
        # Trapping noise: a non-physio sensor that should NOT match.
        _entity(
            "sensor.sleepradar_r60abd1_ts1_product_id",
            state="R60ABD1", friendly_name="TS2 \u4ea7\u54c1ID",
        ),
    ]


@pytest.fixture
def mi_band_entities() -> List[HAEntity]:
    return [
        _entity(
            "sensor.xiaomi_smart_band_9_pro_heart_rate",
            state="68", unit="bpm",
            friendly_name="Xiaomi Smart Band 9 Pro Heart Rate",
        ),
        _entity(
            "sensor.xiaomi_smart_band_9_pro_steps",
            state="3120", friendly_name="Xiaomi Smart Band 9 Pro Steps",
        ),
        _entity(
            "sensor.xiaomi_smart_band_9_pro_battery",
            state="78", device_class="battery", unit="%",
            friendly_name="Xiaomi Smart Band 9 Pro Battery",
        ),
    ]


@pytest.fixture
def mixed_install(
    r60abd1_radar_entities: List[HAEntity],
    mi_band_entities: List[HAEntity],
) -> List[HAEntity]:
    """A whole-house entity list with two competing HR sources + actuators."""
    extras = [
        _entity("light.bedroom_main", state="off",
                friendly_name="\u5367\u5ba4\u4e3b\u706f"),
        _entity("light.bedroom_bedside", state="off",
                friendly_name="\u5367\u5ba4\u5e8a\u5934\u706f"),
        _entity("light.living_room", state="on",
                friendly_name="\u5ba2\u5385\u706f"),
        _entity("climate.bedroom_ac", state="cool",
                friendly_name="\u5367\u5ba4\u7a7a\u8c03"),
        _entity("humidifier.bedroom_humidifier", state="on",
                friendly_name="\u5367\u5ba4\u52a0\u6e7f\u5668"),
        _entity("sensor.bedroom_temperature", state="22.5",
                device_class="temperature", unit="\u00b0C",
                friendly_name="\u5367\u5ba4\u6e29\u5ea6"),
    ]
    return r60abd1_radar_entities + mi_band_entities + extras


# ---------------------------------------------------------------------------
# Bilingual keyword recognition
# ---------------------------------------------------------------------------


class TestBilingualKeywordRecognition:
    """Default keyword lists must catch both English and Chinese / pinyin."""

    def test_default_keywords_include_chinese(self) -> None:
        assert "\u5fc3\u7387" in DEFAULT_HEART_RATE_KEYWORDS
        assert "\u8fd0\u52a8" in DEFAULT_MOVEMENT_KEYWORDS
        assert "\u547c\u5438" in DEFAULT_BREATHING_KEYWORDS

    def test_radar_chinese_friendly_name_matches_movement(
        self, r60abd1_radar_entities: List[HAEntity]
    ) -> None:
        result = DeviceDiscovery(DiscoveryConfig()).discover(
            r60abd1_radar_entities
        )
        ids = [e.entity_id for e in result.sensors.movement]
        assert "sensor.sleepradar_r60abd1_ts5_yundong_zhuangtai" in ids

    def test_radar_chinese_friendly_name_matches_breathing(
        self, r60abd1_radar_entities: List[HAEntity]
    ) -> None:
        result = DeviceDiscovery(DiscoveryConfig()).discover(
            r60abd1_radar_entities
        )
        ids = [e.entity_id for e in result.sensors.breathing]
        assert "sensor.sleepradar_r60abd1_ts6_huxi_xinxi" in ids

    def test_radar_distance_sensor_is_not_breathing_or_movement(
        self, r60abd1_radar_entities: List[HAEntity]
    ) -> None:
        """``T1D \u76ee\u68071\u8ddd\u79bb`` shouldn't be mistaken for a HR/movement signal."""
        result = DeviceDiscovery(DiscoveryConfig()).discover(
            r60abd1_radar_entities
        )
        ids_mv = [e.entity_id for e in result.sensors.movement]
        ids_hr = [e.entity_id for e in result.sensors.heart_rate]
        ids_br = [e.entity_id for e in result.sensors.breathing]
        assert "sensor.sleepradar_r60abd1_ts1_distance" not in ids_mv
        assert "sensor.sleepradar_r60abd1_ts1_distance" not in ids_hr
        assert "sensor.sleepradar_r60abd1_ts1_distance" not in ids_br

    def test_breathing_only_satisfies_minimum_sensors(
        self, r60abd1_radar_entities: List[HAEntity]
    ) -> None:
        """Without HR or movement, a breathing-only setup must still pass."""
        only_breathing = [
            e for e in r60abd1_radar_entities
            if "huxi" in e.entity_id
        ]
        result = DeviceDiscovery(DiscoveryConfig()).discover(only_breathing)
        assert result.has_minimum_sensors() is True
        assert len(result.sensors.breathing) == 1
        assert len(result.sensors.heart_rate) == 0


# ---------------------------------------------------------------------------
# Slot bindings \u2014 user explicit choices override keyword scan
# ---------------------------------------------------------------------------


class TestSlotBindings:
    """Slot bindings are authoritative; the keyword scan must defer to them."""

    def test_heart_rate_slot_pin_is_used_verbatim(
        self, mixed_install: List[HAEntity]
    ) -> None:
        cfg = DiscoveryConfig(
            slot_bindings={
                "heart_rate": ["sensor.xiaomi_smart_band_9_pro_heart_rate"],
            }
        )
        result = DeviceDiscovery(cfg).discover(mixed_install)
        assert [e.entity_id for e in result.sensors.heart_rate] == [
            "sensor.xiaomi_smart_band_9_pro_heart_rate"
        ]

    def test_slot_bound_bucket_is_frozen_against_keyword_scan(
        self, mixed_install: List[HAEntity]
    ) -> None:
        """If user pinned HR=Mi Band, the breathing keyword scan should NOT
        accidentally add the radar's HR-shaped breathing entity to the HR
        bucket.  The HR bucket has *exactly one* item: the user's choice."""
        cfg = DiscoveryConfig(
            slot_bindings={
                "heart_rate": ["sensor.xiaomi_smart_band_9_pro_heart_rate"],
            }
        )
        result = DeviceDiscovery(cfg).discover(mixed_install)
        assert len(result.sensors.heart_rate) == 1

    def test_unbound_buckets_still_use_keyword_scan(
        self, mixed_install: List[HAEntity]
    ) -> None:
        """Pinning HR shouldn't disable movement / temperature discovery."""
        cfg = DiscoveryConfig(
            slot_bindings={
                "heart_rate": ["sensor.xiaomi_smart_band_9_pro_heart_rate"],
            }
        )
        result = DeviceDiscovery(cfg).discover(mixed_install)
        # Radar movement still picked up by keyword scan
        assert any(
            "yundong" in e.entity_id
            for e in result.sensors.movement
        )
        assert len(result.sensors.temperature) >= 1

    def test_light_targets_list_pin_replaces_domain_scan(
        self, mixed_install: List[HAEntity]
    ) -> None:
        """User says 'only control these two lights'; the third (living-room)
        light must NOT end up in the lights bucket."""
        cfg = DiscoveryConfig(
            slot_bindings={
                "lights": ["light.bedroom_main", "light.bedroom_bedside"],
            }
        )
        result = DeviceDiscovery(cfg).discover(mixed_install)
        ids = sorted(e.entity_id for e in result.devices.lights)
        assert ids == ["light.bedroom_bedside", "light.bedroom_main"]

    def test_unknown_slot_key_is_warned_and_ignored(
        self,
        mixed_install: List[HAEntity],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg = DiscoveryConfig(slot_bindings={"made_up_slot": ["sensor.xyz"]})
        with caplog.at_level("WARNING"):
            DeviceDiscovery(cfg).discover(mixed_install)
        assert any("Unknown slot binding" in rec.message for rec in caplog.records)

    def test_missing_entity_in_binding_logs_warning(
        self,
        mixed_install: List[HAEntity],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg = DiscoveryConfig(
            slot_bindings={"heart_rate": ["sensor.does_not_exist"]}
        )
        with caplog.at_level("WARNING"):
            result = DeviceDiscovery(cfg).discover(mixed_install)
        # Warning explicitly mentions the bad entity_id and the slot.
        assert any(
            "sensor.does_not_exist" in rec.message
            and "heart_rate" in rec.message
            for rec in caplog.records
        )
        # The auto-discovered Mi Band HR sensor still ends up in the bucket
        # because the *binding* didn't actually fill it (HA didn't have that
        # entity).  Without this fallback the user would be locked out by
        # a typo in Configuration.
        assert any(
            e.entity_id == "sensor.xiaomi_smart_band_9_pro_heart_rate"
            for e in result.sensors.heart_rate
        )


# ---------------------------------------------------------------------------
# Soft area filter \u2014 fall back to global scan when nothing matches
# ---------------------------------------------------------------------------


class TestAreaSoftConstraint:
    """When ``area_filter`` matches nothing critical, rescan all of HA."""

    def test_area_filter_matches_nothing_then_global_scan_recovers(
        self,
        r60abd1_radar_entities: List[HAEntity],
        mi_band_entities: List[HAEntity],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        # None of the radar / band entities have ``area_id=bedroom``
        # in their attributes \u2014 this is exactly the user's reported state.
        cfg = DiscoveryConfig(area_filter="bedroom")
        with caplog.at_level("WARNING"):
            result = DeviceDiscovery(cfg).discover(
                r60abd1_radar_entities + mi_band_entities
            )
        # The fallback log must fire.
        assert any(
            "Re-scanning all HA entities" in rec.message
            for rec in caplog.records
        )
        # And we still recover sensors after the rescan.
        assert result.has_minimum_sensors() is True
        assert len(result.sensors.heart_rate) >= 1
        assert len(result.sensors.movement) >= 1

    def test_area_filter_with_match_does_not_trigger_rescan(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        entities = [
            _entity("sensor.bedroom_hr", state="70", unit="bpm",
                    friendly_name="HR", area="bedroom"),
            _entity("sensor.bedroom_motion", state="0",
                    friendly_name="Motion", area="bedroom"),
            _entity("sensor.kitchen_hr", state="80", unit="bpm",
                    friendly_name="HR", area="kitchen"),
        ]
        cfg = DiscoveryConfig(area_filter="bedroom")
        with caplog.at_level("WARNING"):
            result = DeviceDiscovery(cfg).discover(entities)
        assert not any(
            "Re-scanning" in rec.message for rec in caplog.records
        )
        # Kitchen HR is correctly excluded.
        ids = [e.entity_id for e in result.sensors.heart_rate]
        assert "sensor.bedroom_hr" in ids
        assert "sensor.kitchen_hr" not in ids


# ---------------------------------------------------------------------------
# suggest_candidates \u2014 powers the binding-help log in run_ha_smart_service
# ---------------------------------------------------------------------------


class TestSuggestCandidates:
    """The static helper used to print 'did you mean...' hints to the user."""

    def test_returns_buckets_for_each_slot(
        self, mixed_install: List[HAEntity]
    ) -> None:
        suggestions = DeviceDiscovery.suggest_candidates(mixed_install)
        for slot in (
            "heart_rate", "movement", "breathing",
            "temperature", "humidity", "illuminance",
        ):
            assert slot in suggestions, slot
        for slot in ("lights", "climates", "fans", "humidifiers", "switches"):
            assert slot in suggestions, slot

    def test_respects_limit_per_bucket(
        self, mixed_install: List[HAEntity]
    ) -> None:
        suggestions = DeviceDiscovery.suggest_candidates(
            mixed_install, limit_per_bucket=1,
        )
        for slot, ids in suggestions.items():
            assert len(ids) <= 1, slot

    def test_ignores_area_filter(
        self,
        r60abd1_radar_entities: List[HAEntity],
        mi_band_entities: List[HAEntity],
    ) -> None:
        """Suggestions must surface candidates the area filter would hide."""
        cfg = DiscoveryConfig(area_filter="bedroom")  # nothing in this area
        suggestions = DeviceDiscovery.suggest_candidates(
            r60abd1_radar_entities + mi_band_entities, cfg,
        )
        assert suggestions["heart_rate"]   # Mi Band still suggested
        assert suggestions["movement"]     # radar still suggested
        assert suggestions["breathing"]


# ---------------------------------------------------------------------------
# Real-world end-to-end fixture
# ---------------------------------------------------------------------------


def test_user_real_world_radar_only_install_works(
    r60abd1_radar_entities: List[HAEntity],
) -> None:
    """User's actual reported install: only the R60ABD1 radar in HA, nothing
    else.  Default config with no area filter must produce a usable result
    that satisfies has_minimum_sensors()."""
    cfg = DiscoveryConfig()
    result = DeviceDiscovery(cfg).discover(r60abd1_radar_entities)
    assert result.has_minimum_sensors()
    # Exactly the two physiological-relevant entities, no false positives
    # from the radar's product-info or distance sensors.
    summary = result.sensors.as_summary()
    assert summary["movement"] == [
        "sensor.sleepradar_r60abd1_ts5_yundong_zhuangtai"
    ]
    assert summary["breathing"] == [
        "sensor.sleepradar_r60abd1_ts6_huxi_xinxi"
    ]
    assert summary["heart_rate"] == []
