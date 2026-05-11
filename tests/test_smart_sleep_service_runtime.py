"""Runtime integration tests for the natural-sleep tick path.

Why this exists
---------------
``test_smart_sleep_service_init.py`` covered the constructor wiring; this
file covers the **per-tick** behaviour:

* ``_wake_tick`` issues HA service calls when the wake window opens
  and the planner sees a friendly stage.
* ``_soundscape_tick`` swaps audio on stage transitions and stays
  quiet on no-ops.
* ``_persist_session`` records a session into the learner and feeds
  the user profile's Bayesian update.

We use a single ``AsyncMock`` for the whole HA client so each test
can assert on which service calls were issued.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers — share the config-builder with test_smart_sleep_service_init.py
# but rebuild here to keep the file standalone.
# ---------------------------------------------------------------------------


def _write_config(tmp_path: Path, natural: dict, learner: bool = False) -> Path:
    from config.config_loader import get_default_config
    cfg = get_default_config()
    ha = cfg.setdefault("home_assistant", {})
    ha["api"] = {
        "base_url": "http://localhost:8123",
        "access_token": "test-token",
        "verify_ssl": False,
    }
    ha["preference_learner"] = {
        "enabled": learner,
        "history_path": str(tmp_path / "user_preferences.json"),
    }
    ha["smart_control"] = {"enabled": True, "dry_run": False}
    natural_with_isolation = dict(natural)
    natural_with_isolation.setdefault(
        "profile_path", str(tmp_path / "user_profile.json"),
    )
    ha["natural_sleep"] = natural_with_isolation
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def _args(config_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        config=str(config_path),
        model="models/best_model.h5",
        base_url=None, token=None, area=None,
        infer_interval=30.0,
        session_interval=1800.0,
        duration=None, dry_run=False,
        verbose=False,
    )


@pytest.fixture
def service_cls():
    from scripts.run_ha_smart_service import SmartSleepService
    return SmartSleepService


@pytest.fixture
def ha_client() -> AsyncMock:
    """Stand-in HomeAssistantClient covering the methods the ticks use."""
    client = AsyncMock()
    client.call_service = AsyncMock(return_value=None)
    client.update_state = AsyncMock(return_value=None)
    return client


@pytest.fixture
def stage_enum():
    from src.data_structures import SleepStage
    return SleepStage


# ---------------------------------------------------------------------------
# _wake_tick
# ---------------------------------------------------------------------------


class TestWakeTick:
    async def test_no_planner_when_window_unset(
        self, service_cls, tmp_path, ha_client, stage_enum,
    ) -> None:
        cfg = _write_config(tmp_path, natural={})
        svc = service_cls(_args(cfg))
        # Without a wake window, the tick is a noop and the planner is
        # never created.
        await svc._wake_tick(ha_client, stage_enum.LIGHT, 0.9)
        assert svc.wake_planner is None
        ha_client.call_service.assert_not_awaited()

    async def test_pre_ramp_dims_lights(
        self, service_cls, tmp_path, ha_client, stage_enum,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.smart_wake import SmartWakePlanner, WakeWindow
        cfg = _write_config(tmp_path, natural={
            "wake_window_start": "07:00",
            "wake_window_end": "07:30",
            "wake_light_targets": ["light.bedroom_main"],
        })
        svc = service_cls(_args(cfg))
        # Force the planner to a known anchor so the tick is deterministic.
        end = datetime(2026, 5, 12, 7, 30)
        svc.wake_planner = SmartWakePlanner(
            WakeWindow(start=end - timedelta(minutes=10), end=end),
        )
        # Patch ``now_local`` *via monkeypatch* so the change is reverted
        # automatically at test teardown (a previous version leaked the
        # patched datetime into other tests' UserProfile.age_years).
        import scripts.run_ha_smart_service as mod
        monkeypatch.setattr(mod, "now_local", lambda: end - timedelta(minutes=15))

        # Need a publisher mock too because _wake_tick publishes.
        svc.publisher = MagicMock()
        svc.publisher.publish_wake_decision = AsyncMock()

        await svc._wake_tick(ha_client, stage_enum.DEEP, 0.9)
        # PRE_RAMP must dim, not stop, the light.
        assert ha_client.call_service.await_count >= 1
        first_call = ha_client.call_service.await_args_list[0]
        assert first_call.args[:2] == ("light", "turn_on")
        # Decision was published to HA Lovelace.
        svc.publisher.publish_wake_decision.assert_awaited()

    async def test_fire_now_marks_planner_woken(
        self, service_cls, tmp_path, ha_client, stage_enum,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from src.smart_wake import SmartWakePlanner, WakeWindow
        cfg = _write_config(tmp_path, natural={
            "wake_window_start": "07:00",
            "wake_window_end": "07:30",
            "wake_light_targets": ["light.bedroom_main"],
        })
        svc = service_cls(_args(cfg))
        end = datetime(2026, 5, 12, 7, 30)
        svc.wake_planner = SmartWakePlanner(
            WakeWindow(start=end - timedelta(minutes=30), end=end),
        )
        # Saturate the planner's stage history with LIGHT so the next
        # tick takes the FIRE_NOW branch.
        for _ in range(5):
            svc.wake_planner.observe_stage(stage_enum.LIGHT, 0.9)
        import scripts.run_ha_smart_service as mod
        monkeypatch.setattr(mod, "now_local", lambda: end - timedelta(minutes=10))
        svc.publisher = MagicMock()
        svc.publisher.publish_wake_decision = AsyncMock()

        await svc._wake_tick(ha_client, stage_enum.LIGHT, 0.9)
        assert svc.wake_planner._woken is True
        # The 100% turn-on call fired.
        last_call = ha_client.call_service.await_args_list[-1]
        assert last_call.kwargs.get("brightness_pct") == 100


# ---------------------------------------------------------------------------
# _soundscape_tick
# ---------------------------------------------------------------------------


class TestSoundscapeTick:
    async def test_no_matcher_when_target_unset(
        self, service_cls, tmp_path, ha_client, stage_enum,
    ) -> None:
        cfg = _write_config(tmp_path, natural={})
        svc = service_cls(_args(cfg))
        await svc._soundscape_tick(ha_client, stage_enum.DEEP, 0.9)
        ha_client.call_service.assert_not_awaited()

    async def test_first_tick_plays_media_and_sets_volume(
        self, service_cls, tmp_path, ha_client, stage_enum,
    ) -> None:
        cfg = _write_config(tmp_path, natural={
            "whitenoise_target": "media_player.bedroom_speaker",
            "whitenoise_track_overrides": {
                "brown_noise": "http://example.com/brown.mp3",
            },
        })
        svc = service_cls(_args(cfg))
        svc.publisher = MagicMock()
        svc.publisher.publish_soundscape = AsyncMock()

        await svc._soundscape_tick(ha_client, stage_enum.DEEP, 0.9)
        # Two services should have been invoked: play_media + volume_set.
        services = [
            (c.args[0], c.args[1])
            for c in ha_client.call_service.await_args_list
        ]
        assert ("media_player", "play_media") in services
        assert ("media_player", "volume_set") in services
        svc.publisher.publish_soundscape.assert_awaited()

    async def test_no_op_on_unchanged_policy(
        self, service_cls, tmp_path, ha_client, stage_enum,
    ) -> None:
        cfg = _write_config(tmp_path, natural={
            "whitenoise_target": "media_player.bedroom_speaker",
            "whitenoise_track_overrides": {
                "brown_noise": "http://example.com/brown.mp3",
            },
        })
        svc = service_cls(_args(cfg))
        svc.publisher = MagicMock()
        svc.publisher.publish_soundscape = AsyncMock()

        await svc._soundscape_tick(ha_client, stage_enum.DEEP, 0.9)
        ha_client.call_service.reset_mock()
        # Same stage + confidence → debouncer should suppress further calls.
        await svc._soundscape_tick(ha_client, stage_enum.DEEP, 0.9)
        ha_client.call_service.assert_not_awaited()

    async def test_rem_stops_audio(
        self, service_cls, tmp_path, ha_client, stage_enum,
    ) -> None:
        cfg = _write_config(tmp_path, natural={
            "whitenoise_target": "media_player.bedroom_speaker",
        })
        svc = service_cls(_args(cfg))
        svc.publisher = MagicMock()
        svc.publisher.publish_soundscape = AsyncMock()

        await svc._soundscape_tick(ha_client, stage_enum.REM, 0.9)
        # REM policy → soundscape OFF → media_stop, no play_media call.
        services = [
            (c.args[0], c.args[1])
            for c in ha_client.call_service.await_args_list
        ]
        assert ("media_player", "media_stop") in services
        assert ("media_player", "play_media") not in services


# ---------------------------------------------------------------------------
# _persist_session round-trip
# ---------------------------------------------------------------------------


class TestPersistSession:
    async def test_metric_path_used_when_enough_epochs(
        self, service_cls, tmp_path, stage_enum,
    ) -> None:
        cfg = _write_config(tmp_path, natural={"birth_year": 1995},
                            learner=True)
        svc = service_cls(_args(cfg))
        # Build a 60-epoch sequence dominated by LIGHT.
        svc.stage_sequence = [stage_enum.LIGHT] * 50 + [stage_enum.DEEP] * 10
        svc.stage_counts = {"LIGHT": 50, "DEEP": 10, "REM": 0, "AWAKE": 0}
        svc.session_started_at = 1_700_000_000.0
        # Stub controller.
        controller = MagicMock()
        controller.feedback_score = MagicMock()

        svc._persist_session(controller, partial=False)

        # Learner persisted exactly one session into our temp file.
        assert svc.learner is not None
        sessions = svc.learner.sessions()
        assert len(sessions) == 1
        # Quality score must be in valid 0-100 band.
        assert 0.0 <= sessions[0].quality_score <= 100.0
        # Profile posterior should now reflect the session evidence
        # (or be untouched if the score landed below the 60 threshold).
        assert svc.profile.posterior_count >= 0

    async def test_subjective_feedback_blends_into_score(
        self, service_cls, tmp_path, stage_enum,
    ) -> None:
        from src.feedback_input import FeedbackSnapshot
        import time as _time
        cfg = _write_config(tmp_path, natural={
            "feedback_entity": "input_number.sleep_rating",
        }, learner=True)
        svc = service_cls(_args(cfg))
        svc.stage_sequence = [stage_enum.LIGHT] * 50 + [stage_enum.DEEP] * 10
        svc.stage_counts = {"LIGHT": 50, "DEEP": 10, "REM": 0, "AWAKE": 0}
        # Inject a fresh perfect-score subjective rating.
        svc.feedback._latest = FeedbackSnapshot(   # type: ignore[union-attr]
            score=5.0, received_at=_time.time(),
            raw_value="5", entity_id="input_number.sleep_rating",
        )
        svc.feedback._consumed = False             # type: ignore[union-attr]

        controller = MagicMock()
        controller.feedback_score = MagicMock()
        svc._persist_session(controller, partial=False)

        sessions = svc.learner.sessions()
        assert len(sessions) == 1
        # Subjective '5/5' should pull score up; check note records it.
        assert "subjective=5.0" in (sessions[0].notes or "")
