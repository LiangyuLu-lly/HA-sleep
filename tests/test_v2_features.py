"""Tests for v2.0.0 features: bilingual logs, whitenoise volume feedback, diagnostic export."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, natural: dict | None = None) -> Path:
    from training_config.config_loader import get_default_config

    cfg = get_default_config()
    ha = cfg.setdefault("home_assistant", {})
    ha["api"] = {
        "base_url": "http://localhost:8123",
        "access_token": "test-token",
        "verify_ssl": False,
    }
    ha["preference_learner"] = {
        "enabled": False,
        "history_path": str(tmp_path / "user_preferences.json"),
    }
    ha["smart_control"] = {"enabled": True, "dry_run": True}
    natural_cfg = natural or {}
    natural_cfg.setdefault("profile_path", str(tmp_path / "user_profile.json"))
    ha["natural_sleep"] = natural_cfg
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def _args(config_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        config=str(config_path),
        model="models/best_model.h5",
        base_url=None,
        token=None,
        area=None,
        infer_interval=30.0,
        session_interval=1800.0,
        duration=None,
        dry_run=True,
        verbose=False,
    )


# ---------------------------------------------------------------------------
# _L bilingual helper
# ---------------------------------------------------------------------------


class TestBilingualHelper:
    """Test the _L() function for bilingual log messages."""

    def test_returns_english_when_lang_unset(self):
        from scripts.run_ha_smart_service import _L

        with patch.dict(os.environ, {}, clear=True):
            # Remove LANG if present
            os.environ.pop("LANG", None)
            result = _L("Session started", "睡眠会话已开始")
            assert result == "Session started"

    def test_returns_english_when_lang_is_en(self):
        from scripts.run_ha_smart_service import _L

        with patch.dict(os.environ, {"LANG": "en_US.UTF-8"}):
            result = _L("Session started", "睡眠会话已开始")
            assert result == "Session started"

    def test_returns_chinese_when_lang_contains_zh(self):
        from scripts.run_ha_smart_service import _L

        with patch.dict(os.environ, {"LANG": "zh_CN.UTF-8"}):
            result = _L("Session started", "睡眠会话已开始")
            assert result == "睡眠会话已开始"

    def test_returns_chinese_when_lang_is_zh_tw(self):
        from scripts.run_ha_smart_service import _L

        with patch.dict(os.environ, {"LANG": "zh_TW.UTF-8"}):
            result = _L("Session started", "睡眠会话已开始")
            assert result == "睡眠会话已开始"

    def test_case_insensitive(self):
        from scripts.run_ha_smart_service import _L

        with patch.dict(os.environ, {"LANG": "ZH_CN"}):
            result = _L("hello", "你好")
            assert result == "你好"


# ---------------------------------------------------------------------------
# Whitenoise volume feedback
# ---------------------------------------------------------------------------


class TestWhitenoiseVolumeFeedback:
    """Test the whitenoise volume one-click feedback feature."""

    def test_volume_feedback_entity_stored(self, tmp_path: Path):
        from scripts.run_ha_smart_service import SmartSleepService

        cfg_path = _write_config(tmp_path, natural={
            "whitenoise_target": "media_player.bedroom",
            "whitenoise_volume_feedback_entity": "input_button.sleep_classifier_too_loud",
        })
        svc = SmartSleepService(_args(cfg_path))
        assert svc._whitenoise_volume_feedback_entity == "input_button.sleep_classifier_too_loud"

    def test_volume_feedback_empty_by_default(self, tmp_path: Path):
        from scripts.run_ha_smart_service import SmartSleepService

        cfg_path = _write_config(tmp_path)
        svc = SmartSleepService(_args(cfg_path))
        assert svc._whitenoise_volume_feedback_entity == ""

    def test_volume_feedback_reduces_scale(self, tmp_path: Path):
        from scripts.run_ha_smart_service import SmartSleepService
        from src.ha_api_client import HAEntity, StateChangeEvent

        cfg_path = _write_config(tmp_path, natural={
            "whitenoise_target": "media_player.bedroom",
            "whitenoise_volume_feedback_entity": "input_button.too_loud",
            "whitenoise_volume_scale": 1.0,
        })
        svc = SmartSleepService(_args(cfg_path))
        assert svc.sound_matcher is not None
        original_scale = svc.sound_matcher.volume_scale

        # Simulate a state_changed event for the feedback button
        new_state = MagicMock()
        new_state.state = "2026-05-16T10:00:00"
        new_state.attributes = {}
        new_state.numeric_state = MagicMock(return_value=None)
        event = StateChangeEvent(
            entity_id="input_button.too_loud",
            old_state=None,
            new_state=new_state,
        )

        # Create mock discovery and engine
        discovery = MagicMock()
        engine = MagicMock()

        svc._route_state_change(event, discovery, engine)

        assert svc.sound_matcher.volume_scale == pytest.approx(original_scale * 0.7)

    def test_volume_feedback_no_crash_without_matcher(self, tmp_path: Path):
        from scripts.run_ha_smart_service import SmartSleepService
        from src.ha_api_client import StateChangeEvent

        cfg_path = _write_config(tmp_path, natural={
            "whitenoise_volume_feedback_entity": "input_button.too_loud",
        })
        svc = SmartSleepService(_args(cfg_path))
        assert svc.sound_matcher is None

        new_state = MagicMock()
        new_state.state = "2026-05-16T10:00:00"
        new_state.attributes = {}
        new_state.numeric_state = MagicMock(return_value=None)
        event = StateChangeEvent(
            entity_id="input_button.too_loud",
            old_state=None,
            new_state=new_state,
        )
        discovery = MagicMock()
        engine = MagicMock()

        # Should not raise
        svc._route_state_change(event, discovery, engine)


# ---------------------------------------------------------------------------
# Diagnostic export
# ---------------------------------------------------------------------------


class TestDiagnosticExport:
    """Test the diagnostic_export.py script."""

    def test_export_with_no_data(self, tmp_path: Path):
        from scripts.diagnostic_export import (
            _extract_apnea,
            _extract_config,
            _extract_preferences,
            _load_json,
        )

        result = _extract_preferences(None)
        assert result["n_sessions"] == 0
        assert result["last_session_at"] is None
        assert result["learner_status"] == "no_data"

    def test_export_with_sessions(self, tmp_path: Path):
        import time

        from scripts.diagnostic_export import _extract_preferences

        now = time.time()
        data = {
            "sessions": [
                {"ended_at": now - 86400},
                {"ended_at": now - 43200},
                {"ended_at": now},
            ],
        }
        result = _extract_preferences(data)
        assert result["n_sessions"] == 3
        assert result["last_session_at"] is not None
        assert result["learner_status"] == "learning"

    def test_export_personalised_status(self):
        from scripts.diagnostic_export import _extract_preferences

        data = {"sessions": [{"ended_at": 1000000 + i * 86400} for i in range(20)]}
        result = _extract_preferences(data)
        assert result["n_sessions"] == 20
        assert result["learner_status"] == "personalised"

    def test_extract_apnea_none(self):
        from scripts.diagnostic_export import _extract_apnea

        assert _extract_apnea(None) is None

    def test_extract_apnea_with_data(self):
        from scripts.diagnostic_export import _extract_apnea

        data = {"calibration_nights": 7, "status": "green", "raw_samples": [1, 2, 3]}
        result = _extract_apnea(data)
        assert result == {"calibration_nights": 7, "status": "green"}
        assert "raw_samples" not in result

    def test_extract_config_safe_fields(self):
        from scripts.diagnostic_export import _extract_config

        data = {
            "home_assistant": {
                "api": {
                    "area": "bedroom",
                    "sleep_stage_source": "sensor.mi_band",
                    "access_token": "SECRET_TOKEN_DO_NOT_EXPOSE",
                },
                "smart_control": {
                    "dry_run": False,
                    "wind_down_minutes": 25,
                },
            },
        }
        result = _extract_config(data)
        assert result["area"] == "bedroom"
        assert result["sleep_stage_source"] == "sensor.mi_band"
        assert result["dry_run"] is False
        assert result["wind_down_minutes"] == 25
        # Must NOT contain the token
        assert "SECRET_TOKEN_DO_NOT_EXPOSE" not in json.dumps(result)

    def test_load_json_missing_file(self, tmp_path: Path):
        from scripts.diagnostic_export import _load_json

        result = _load_json(tmp_path / "nonexistent.json")
        assert result is None

    def test_load_json_valid_file(self, tmp_path: Path):
        from scripts.diagnostic_export import _load_json

        path = tmp_path / "test.json"
        path.write_text('{"key": "value"}', encoding="utf-8")
        result = _load_json(path)
        assert result == {"key": "value"}

    def test_main_outputs_valid_json(self, tmp_path: Path, capsys):
        """Test that main() produces valid JSON output."""
        from scripts import diagnostic_export

        # Patch _DATA_DIR to use tmp_path
        with patch.object(diagnostic_export, "_DATA_DIR", tmp_path):
            # Create minimal data files
            prefs = {"sessions": [{"ended_at": 1700000000}]}
            (tmp_path / "user_preferences.json").write_text(
                json.dumps(prefs), encoding="utf-8",
            )
            (tmp_path / "effective_config.json").write_text(
                json.dumps({"home_assistant": {"api": {"area": "bedroom"}}}),
                encoding="utf-8",
            )

            ret = diagnostic_export.main()
            assert ret == 0

            captured = capsys.readouterr()
            output = json.loads(captured.out)
            assert output["version"] == "2.0.0"
            assert output["n_sessions"] == 1
            assert output["last_session_at"] is not None
            assert "apnea_baseline" in output
            assert "config_summary" in output
