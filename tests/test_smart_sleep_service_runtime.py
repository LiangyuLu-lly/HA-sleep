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
import asyncio
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
    from training_config.config_loader import get_default_config
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


# ---------------------------------------------------------------------------
# _task_inference_loop — last_action formatting regression
# ---------------------------------------------------------------------------


class TestLastActionFormatting:
    """``actions`` is a list of :class:`ControlAction` dataclasses.

    Pre-fix the orchestrator called ``first.get('domain', '?')`` on it,
    which raises ``AttributeError`` on every non-empty plan.  A test
    that asserts ``publish_last_action`` is awaited with the expected
    string both locks in the fix and guards against future refactors
    that might switch the dataclass to a dict (or vice versa) without
    updating the formatter.
    """

    async def test_control_action_is_formatted_by_attribute_access(
        self, service_cls, tmp_path, ha_client, stage_enum,
    ) -> None:
        from src.smart_environment_controller import ControlAction
        import asyncio

        cfg = _write_config(tmp_path, natural={})
        svc = service_cls(_args(cfg))

        # Wire in a publisher that records what we'd have pushed to HA.
        svc.publisher = MagicMock()
        svc.publisher.publish_stage = AsyncMock()
        svc.publisher.publish_duration = AsyncMock()
        svc.publisher.publish_last_action = AsyncMock()

        # Stub engine + controller so the loop runs exactly one tick
        # before stop_event fires.  Controller returns a real
        # ControlAction so we exercise the dataclass formatting path.
        engine = MagicMock()
        engine.infer = MagicMock(
            return_value=(stage_enum.LIGHT, 0.9),
        )
        # v1.6.3 — the inference loop skips everything when the stage
        # source goes stale, so we must force it to live for this
        # test's happy-path assertions to fire.  capability_stats is
        # also consulted now (v1.6.2), so stub it too.
        engine.is_stale = MagicMock(return_value=False)
        action = ControlAction(
            domain="climate",
            service="set_temperature",
            entity_id="climate.bedroom_ac",
            data={"temperature": 20.0},
            reason="test",
        )
        controller = MagicMock()
        controller.apply = AsyncMock(return_value=[action])
        controller.capability_stats = MagicMock(return_value={})

        # Short infer_interval so the wait_for returns quickly via
        # stop_event rather than sleeping the whole 30 s default.
        svc.args.infer_interval = 0.05

        async def _stop_soon():
            # Give the loop one tick, then release it.
            await asyncio.sleep(0.01)
            svc.stop_event.set()

        await asyncio.gather(
            svc._task_inference_loop(engine, controller, ha_client),
            _stop_soon(),
        )

        svc.publisher.publish_last_action.assert_awaited()
        call = svc.publisher.publish_last_action.await_args
        # Positional arg is the formatted summary string.
        assert call.args[0] == "climate.set_temperature → climate.bedroom_ac"
        assert call.kwargs["executed"] is True


# ---------------------------------------------------------------------------
# Session lifecycle + stale-source guard (v1.6.3)
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    """v1.6.3 — a 'session' now has proper onset / wake detection.

    Before this release, stage_counts / stage_sequence / env_by_stage
    were initialised once in ``__init__`` and never reset, so an
    add-on running for a month reported one 30-day 'session' with a
    meaningless quality score.  These tests lock in the new semantics.
    """

    async def test_reset_rotates_session_id_and_zeros_counts(
        self, service_cls, tmp_path, stage_enum,
    ) -> None:
        cfg = _write_config(tmp_path, natural={}, learner=True)
        svc = service_cls(_args(cfg))

        svc.stage_counts = {"AWAKE": 5, "LIGHT": 30, "DEEP": 12, "REM": 8}
        svc.stage_sequence = [stage_enum.LIGHT] * 30
        svc.env_by_stage = {"LIGHT": object()}
        svc._in_session = True
        svc._consecutive_awake_ticks = 7
        original_id = svc.session_id

        svc._reset_session_state()

        assert svc.session_id != original_id
        assert svc.stage_counts == {
            "AWAKE": 0, "LIGHT": 0, "DEEP": 0, "REM": 0,
        }
        assert svc.stage_sequence == []
        assert svc.env_by_stage == {}
        assert svc._in_session is False
        assert svc._consecutive_awake_ticks == 0

    async def test_onset_requires_sustained_non_awake(
        self, service_cls, tmp_path, stage_enum,
    ) -> None:
        cfg = _write_config(tmp_path, natural={}, learner=True)
        svc = service_cls(_args(cfg))
        svc.args.infer_interval = 60.0
        svc._session_onset_dwell_seconds = 300.0      # = 5 ticks
        controller = MagicMock()

        # 3 ticks of LIGHT isn't enough.
        for _ in range(3):
            svc._maybe_advance_session_lifecycle(
                stage_enum.LIGHT, controller,
            )
        assert svc._in_session is False

        # Two more and we cross the 5-tick threshold.
        for _ in range(2):
            svc._maybe_advance_session_lifecycle(
                stage_enum.LIGHT, controller,
            )
        assert svc._in_session is True

    async def test_brief_awake_stir_does_not_close_session(
        self, service_cls, tmp_path, stage_enum,
    ) -> None:
        cfg = _write_config(tmp_path, natural={}, learner=True)
        svc = service_cls(_args(cfg))
        svc.args.infer_interval = 60.0
        svc._session_wake_dwell_seconds = 600.0       # = 10 ticks
        # Pretend a session is already open.
        svc._in_session = True
        controller = MagicMock()

        # 5 ticks AWAKE (under threshold) → still in session.
        for _ in range(5):
            svc._maybe_advance_session_lifecycle(
                stage_enum.AWAKE, controller,
            )
        assert svc._in_session is True

        # A LIGHT tick resets the AWAKE counter.
        svc._maybe_advance_session_lifecycle(
            stage_enum.LIGHT, controller,
        )
        assert svc._consecutive_awake_ticks == 0
        # Even another 9 AWAKE ticks (not 10) doesn't close.
        for _ in range(9):
            svc._maybe_advance_session_lifecycle(
                stage_enum.AWAKE, controller,
            )
        assert svc._in_session is True

    async def test_sustained_awake_persists_and_resets_session(
        self, service_cls, tmp_path, stage_enum,
    ) -> None:
        cfg = _write_config(tmp_path, natural={}, learner=True)
        svc = service_cls(_args(cfg))
        svc.args.infer_interval = 60.0
        svc._session_wake_dwell_seconds = 600.0       # = 10 ticks
        svc._in_session = True
        svc.stage_counts = {"AWAKE": 1, "LIGHT": 50, "DEEP": 10, "REM": 5}
        svc.stage_sequence = [stage_enum.LIGHT] * 50
        controller = MagicMock()
        controller.feedback_score = MagicMock()
        original_id = svc.session_id

        # 10 AWAKE ticks → triggers wake-up.
        for _ in range(10):
            svc._maybe_advance_session_lifecycle(
                stage_enum.AWAKE, controller,
            )

        # Session was persisted (learner records it) + state reset.
        assert svc.learner is not None
        assert len(svc.learner.sessions()) == 1
        assert svc._in_session is False
        assert svc.session_id != original_id
        assert svc.stage_counts == {
            "AWAKE": 0, "LIGHT": 0, "DEEP": 0, "REM": 0,
        }


class TestStaleStageSourceGuard:
    """v1.6.3 — a dead wearable must NOT lock the bedroom into the
    last-known stage's setpoints forever.
    """

    async def test_stale_skips_controller_apply(
        self, service_cls, tmp_path, ha_client, stage_enum,
    ) -> None:
        cfg = _write_config(tmp_path, natural={}, learner=False)
        svc = service_cls(_args(cfg))
        svc.args.infer_interval = 0.05

        svc.publisher = MagicMock()
        svc.publisher.publish_stage = AsyncMock()
        svc.publisher.publish_duration = AsyncMock()
        svc.publisher.publish_last_action = AsyncMock()

        engine = MagicMock()
        engine.infer = MagicMock(
            return_value=(stage_enum.DEEP, 0.9),
        )
        engine.is_stale = MagicMock(return_value=True)
        engine.stage_entity_id = "sensor.watch"
        engine._stale_after = 1800.0

        controller = MagicMock()
        controller.apply = AsyncMock(return_value=[])
        controller.capability_stats = MagicMock(return_value={})

        async def _stop_soon():
            await asyncio.sleep(0.02)
            svc.stop_event.set()

        await asyncio.gather(
            svc._task_inference_loop(engine, controller, ha_client),
            _stop_soon(),
        )

        # Controller.apply was NEVER awaited because the loop bailed
        # out of every tick at the stale-guard branch.
        controller.apply.assert_not_awaited()
        # But we did publish the stage sensor so the user can see
        # "stale" on their Lovelace dashboard.
        svc.publisher.publish_stage.assert_awaited()
        # last_action must NOT have been re-awaited with stale data.
        svc.publisher.publish_last_action.assert_not_awaited()

    async def test_live_source_resumes_after_stale(
        self, service_cls, tmp_path, ha_client, stage_enum,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg = _write_config(tmp_path, natural={}, learner=False)
        svc = service_cls(_args(cfg))
        svc.args.infer_interval = 0.05
        svc._stage_source_was_stale = True    # pretend we just recovered

        svc.publisher = MagicMock()
        svc.publisher.publish_stage = AsyncMock()
        svc.publisher.publish_duration = AsyncMock()
        svc.publisher.publish_last_action = AsyncMock()

        engine = MagicMock()
        engine.infer = MagicMock(
            return_value=(stage_enum.LIGHT, 0.9),
        )
        engine.is_stale = MagicMock(return_value=False)
        engine.stage_entity_id = "sensor.watch"

        controller = MagicMock()
        controller.apply = AsyncMock(return_value=[])
        controller.capability_stats = MagicMock(return_value={})

        async def _stop_soon():
            await asyncio.sleep(0.02)
            svc.stop_event.set()

        with caplog.at_level("INFO"):
            await asyncio.gather(
                svc._task_inference_loop(engine, controller, ha_client),
                _stop_soon(),
            )

        # Recovery transition was logged exactly once.
        recovery_logs = [
            r for r in caplog.records
            if "live again" in r.message
        ]
        assert len(recovery_logs) == 1
        assert svc._stage_source_was_stale is False


# ---------------------------------------------------------------------------
# Env freshness (v1.6.4 P1)
# ---------------------------------------------------------------------------


class TestEnvFreshness:
    """v1.6.4 — stale environment sensor readings must not contaminate
    the controller's deadband / anticipation logic.
    """

    async def test_fresh_reading_passes_through(
        self, service_cls, tmp_path,
    ) -> None:
        cfg = _write_config(tmp_path, natural={}, learner=False)
        svc = service_cls(_args(cfg))
        svc._env_freshness_window_seconds = 900.0
        now = 1_700_000_000.0

        svc.last_env.temperature_c = 22.5
        svc._env_ts["temperature_c"] = now - 60.0    # 1 min ago

        safe = svc._safe_last_env(now=now)
        assert safe.temperature_c == 22.5
        assert "temperature_c" not in svc._env_stale_fields

    async def test_stale_reading_is_masked(
        self, service_cls, tmp_path,
    ) -> None:
        cfg = _write_config(tmp_path, natural={}, learner=False)
        svc = service_cls(_args(cfg))
        svc._env_freshness_window_seconds = 900.0
        now = 1_700_000_000.0

        svc.last_env.temperature_c = 22.5
        svc._env_ts["temperature_c"] = now - 2000.0   # 33 min ago

        safe = svc._safe_last_env(now=now)
        assert safe.temperature_c is None
        assert "temperature_c" in svc._env_stale_fields

    async def test_never_observed_stays_none_not_stale(
        self, service_cls, tmp_path,
    ) -> None:
        """Uninitialised field (ts=0) is semantically different from
        stale — the controller falls back to stage default, but we
        don't claim the sensor died.
        """
        cfg = _write_config(tmp_path, natural={}, learner=False)
        svc = service_cls(_args(cfg))
        now = 1_700_000_000.0

        svc.last_env.humidity_pct = None
        svc._env_ts["humidity_pct"] = 0.0

        safe = svc._safe_last_env(now=now)
        assert safe.humidity_pct is None
        assert "humidity_pct" not in svc._env_stale_fields

    async def test_mixed_fresh_and_stale_fields(
        self, service_cls, tmp_path,
    ) -> None:
        cfg = _write_config(tmp_path, natural={}, learner=False)
        svc = service_cls(_args(cfg))
        svc._env_freshness_window_seconds = 900.0
        now = 1_700_000_000.0

        svc.last_env.temperature_c = 22.5
        svc.last_env.humidity_pct = 55.0
        svc.last_env.brightness_pct = 3.0
        svc._env_ts["temperature_c"] = now - 60.0     # fresh
        svc._env_ts["humidity_pct"] = now - 2000.0    # stale
        svc._env_ts["brightness_pct"] = now - 120.0   # fresh

        safe = svc._safe_last_env(now=now)
        assert safe.temperature_c == 22.5
        assert safe.humidity_pct is None
        assert safe.brightness_pct == 3.0
        assert svc._env_stale_fields == {"humidity_pct"}
