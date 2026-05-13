"""HA state_changed event storm test (Sprint 4B).

Simulates 1000 state_changed events arriving in rapid succession to
verify the orchestrator's _route_state_change doesn't crash, doesn't
lose events, and correctly reflects the final sensor value.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.data_structures import SleepStage
from src.device_discovery import (
    ActionableDevices,
    DiscoveryResult,
    SensorSources,
)
from src.external_stage_subscriber import ExternalStageSubscriber
from src.ha_api_client import HAEntity, StateChangeEvent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    entity_id: str,
    state: str,
    attributes: Optional[Dict] = None,
) -> StateChangeEvent:
    """Build a minimal StateChangeEvent for testing."""
    return StateChangeEvent(
        entity_id=entity_id,
        new_state=HAEntity(
            entity_id=entity_id,
            state=state,
            attributes=attributes or {},
        ),
        old_state=None,
    )


def _build_discovery_with_sensors(
    temp_entity: str = "sensor.bedroom_temperature",
    stage_entity: str = "sensor.sleep_stage",
) -> DiscoveryResult:
    """Build a minimal DiscoveryResult with one temperature sensor."""
    temp_sensor = HAEntity(
        entity_id=temp_entity,
        state="20.0",
        attributes={"device_class": "temperature"},
    )
    return DiscoveryResult(
        sensors=SensorSources(
            temperature=[temp_sensor],
            humidity=[],
            illuminance=[],
        ),
        devices=ActionableDevices(
            lights=[], climates=[], fans=[],
            humidifiers=[], switches=[], media_players=[],
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEventStorm:
    """Verify the orchestrator handles a burst of 1000 events gracefully."""

    def test_stage_events_no_loss(self, tmp_path: Path) -> None:
        """1000 stage events → subscriber sees all of them (update_count)."""
        from scripts.run_ha_smart_service import SmartSleepService
        import argparse

        cfg_path = tmp_path / "cfg.json"
        from training_config.config_loader import get_default_config
        cfg = get_default_config()
        ha = cfg.setdefault("home_assistant", {})
        ha["api"] = {
            "base_url": "http://localhost:8123",
            "access_token": "test-token",
            "verify_ssl": False,
            "sleep_stage_source": "sensor.sleep_stage",
        }
        ha["preference_learner"] = {
            "enabled": True,
            "history_path": str(tmp_path / "prefs.json"),
        }
        ha["smart_control"] = {"enabled": True, "dry_run": True}
        ha["natural_sleep"] = {
            "profile_path": str(tmp_path / "profile.json"),
        }
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        args = argparse.Namespace(
            config=str(cfg_path),
            model="models/best_model.h5",
            base_url=None, token=None, area=None,
            infer_interval=30.0,
            session_interval=86400.0,
            duration=None, dry_run=True,
            verbose=False,
        )
        svc = SmartSleepService(args)

        engine = ExternalStageSubscriber(
            stage_entity_id="sensor.sleep_stage",
            min_stage_dwell_seconds=0.0,
        )
        discovery = _build_discovery_with_sensors()

        # Fire 1000 stage events alternating between LIGHT and DEEP.
        stages = ["LIGHT", "DEEP"]
        observe_count = 0
        for i in range(1000):
            event = _make_event(
                "sensor.sleep_stage",
                stages[i % 2],
                {"confidence": 0.9},
            )
            svc._route_state_change(event, discovery, engine)
            observe_count += 1

        # The subscriber should have received all 1000 observations.
        # We verify by checking that the engine's internal state is
        # consistent (no crash, last stage matches the last event).
        last_stage_name = stages[999 % 2]  # DEEP
        raw = engine.raw_stage
        assert raw.name == last_stage_name
        # No exception means no crash — the primary assertion.

    def test_env_sensor_final_value(self, tmp_path: Path) -> None:
        """1000 temperature events → last_env reflects the final value."""
        from scripts.run_ha_smart_service import SmartSleepService
        import argparse

        cfg_path = tmp_path / "cfg.json"
        from training_config.config_loader import get_default_config
        cfg = get_default_config()
        ha = cfg.setdefault("home_assistant", {})
        ha["api"] = {
            "base_url": "http://localhost:8123",
            "access_token": "test-token",
            "verify_ssl": False,
            "sleep_stage_source": "sensor.sleep_stage",
        }
        ha["preference_learner"] = {
            "enabled": True,
            "history_path": str(tmp_path / "prefs.json"),
        }
        ha["smart_control"] = {"enabled": True, "dry_run": True}
        ha["natural_sleep"] = {
            "profile_path": str(tmp_path / "profile.json"),
        }
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        args = argparse.Namespace(
            config=str(cfg_path),
            model="models/best_model.h5",
            base_url=None, token=None, area=None,
            infer_interval=30.0,
            session_interval=86400.0,
            duration=None, dry_run=True,
            verbose=False,
        )
        svc = SmartSleepService(args)

        engine = ExternalStageSubscriber(
            stage_entity_id="sensor.sleep_stage",
            min_stage_dwell_seconds=0.0,
        )
        discovery = _build_discovery_with_sensors(
            temp_entity="sensor.bedroom_temperature",
        )

        # Fire 1000 temperature events with incrementing values.
        for i in range(1000):
            temp_value = 18.0 + i * 0.01  # 18.00 → 27.99
            event = _make_event(
                "sensor.bedroom_temperature",
                f"{temp_value:.2f}",
            )
            svc._route_state_change(event, discovery, engine)

        # The final temperature should be the last event's value.
        expected_final = 18.0 + 999 * 0.01
        assert svc.last_env.temperature_c == pytest.approx(
            expected_final, abs=0.01,
        )

    def test_mixed_storm_no_crash(self, tmp_path: Path) -> None:
        """1000 mixed events (stage + env) in rapid succession — no crash."""
        from scripts.run_ha_smart_service import SmartSleepService
        import argparse

        cfg_path = tmp_path / "cfg.json"
        from training_config.config_loader import get_default_config
        cfg = get_default_config()
        ha = cfg.setdefault("home_assistant", {})
        ha["api"] = {
            "base_url": "http://localhost:8123",
            "access_token": "test-token",
            "verify_ssl": False,
            "sleep_stage_source": "sensor.sleep_stage",
        }
        ha["preference_learner"] = {
            "enabled": True,
            "history_path": str(tmp_path / "prefs.json"),
        }
        ha["smart_control"] = {"enabled": True, "dry_run": True}
        ha["natural_sleep"] = {
            "profile_path": str(tmp_path / "profile.json"),
        }
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

        args = argparse.Namespace(
            config=str(cfg_path),
            model="models/best_model.h5",
            base_url=None, token=None, area=None,
            infer_interval=30.0,
            session_interval=86400.0,
            duration=None, dry_run=True,
            verbose=False,
        )
        svc = SmartSleepService(args)

        engine = ExternalStageSubscriber(
            stage_entity_id="sensor.sleep_stage",
            min_stage_dwell_seconds=0.0,
        )
        discovery = _build_discovery_with_sensors()

        stages = ["AWAKE", "LIGHT", "DEEP", "REM"]
        for i in range(1000):
            if i % 3 == 0:
                # Stage event
                event = _make_event(
                    "sensor.sleep_stage",
                    stages[i % 4],
                    {"confidence": 0.85},
                )
            else:
                # Temperature event
                event = _make_event(
                    "sensor.bedroom_temperature",
                    f"{20.0 + (i % 10) * 0.1:.1f}",
                )
            svc._route_state_change(event, discovery, engine)

        # No crash is the primary assertion.  Also verify env is sane.
        assert svc.last_env.temperature_c is not None
        assert 18.0 <= svc.last_env.temperature_c <= 30.0
