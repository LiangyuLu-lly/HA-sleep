"""Smoke test for ``SmartSleepService.__init__`` natural-sleep wiring.

Why this exists
---------------
The service ``__init__`` got 80+ lines of new code in v1.2.0 that
constructs five optional natural-sleep components (UserProfile,
SmartWakePlanner trigger config, WhiteNoiseMatcher, SubjectiveFeedback
listener, SleepDebtTracker plumbing).  Each of those is *opt-in*: an
empty config field must leave the corresponding member as ``None`` and
must never crash.

These tests only exercise ``__init__`` — no asyncio, no live HA — so
they're cheap and they catch the most common regression I'm worried
about: someone tightens a config check and accidentally requires the
user to fill, say, the wake-window before they can use the add-on at
all.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest


def _write_config(tmp_path: Path, natural: dict) -> Path:
    """Build a config.json that passes ``validate_config`` + carries our
    ``home_assistant.natural_sleep`` overrides.

    We start from the bundled defaults so we don't have to duplicate the
    schema; then we splice in our test-specific natural-sleep config.
    """
    from config.config_loader import get_default_config
    cfg = get_default_config()
    ha = cfg.setdefault("home_assistant", {})
    ha["api"] = {
        "base_url": "http://localhost:8123",
        "access_token": "fake-token-for-test",
        "verify_ssl": False,
    }
    ha["preference_learner"] = {"enabled": False}
    ha["smart_control"] = {"enabled": True, "dry_run": True}
    # Force the user-profile JSON into tmp_path so successive tests
    # never inherit each other's posterior or birth_year.
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
        base_url=None,
        token=None,
        area=None,
        infer_interval=30.0,
        session_interval=1800.0,
        duration=None,
        dry_run=True,
        verbose=False,
    )


@pytest.fixture
def service_cls():
    """Lazy-import the service class to avoid pulling TF at module level."""
    from scripts.run_ha_smart_service import SmartSleepService
    return SmartSleepService


# ---------------------------------------------------------------------------
# Empty natural_sleep — every optional member must be None
# ---------------------------------------------------------------------------


class TestEmptyNaturalSleep:
    def test_all_modules_disabled_by_default(
        self, service_cls, tmp_path: Path,
    ) -> None:
        cfg = _write_config(tmp_path, natural={})
        svc = service_cls(_args(cfg))
        assert svc.wake_planner is None
        assert svc.sound_matcher is None
        assert svc.feedback is None
        # Profile is always created (cohort defaults), just no posterior.
        assert svc.profile is not None
        assert svc.profile.posterior_count == 0
        # Wake light targets default to empty list.
        assert svc._wake_light_targets == []

    def test_wake_window_strs_none_when_partial(
        self, service_cls, tmp_path: Path,
    ) -> None:
        """Filling only one of (start, end) must NOT arm smart-wake."""
        cfg = _write_config(tmp_path, natural={
            "wake_window_start": "07:00",
            # missing end — half-configured shouldn't arm the planner
        })
        svc = service_cls(_args(cfg))
        assert svc._wake_window_strs is None


# ---------------------------------------------------------------------------
# Each module engages exactly when its field is filled
# ---------------------------------------------------------------------------


class TestSelectiveEngagement:
    def test_birth_year_routed_to_profile(
        self, service_cls, tmp_path: Path,
    ) -> None:
        cfg = _write_config(tmp_path, natural={"birth_year": 1995})
        svc = service_cls(_args(cfg))
        assert svc.profile.birth_year == 1995

    def test_invalid_birth_year_warns_but_does_not_crash(
        self, service_cls, tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg = _write_config(tmp_path, natural={"birth_year": "not-a-year"})
        with caplog.at_level("WARNING"):
            svc = service_cls(_args(cfg))
        assert svc.profile.birth_year is None
        assert any("birth_year" in r.message for r in caplog.records)

    def test_wake_window_pair_arms_planner_lazily(
        self, service_cls, tmp_path: Path,
    ) -> None:
        cfg = _write_config(tmp_path, natural={
            "wake_window_start": "07:00",
            "wake_window_end": "07:30",
            "wake_light_targets": ["light.bedroom_main"],
        })
        svc = service_cls(_args(cfg))
        # Stored as strings; planner builds itself in _ensure_wake_planner.
        assert svc._wake_window_strs == ("07:00", "07:30")
        assert svc._wake_light_targets == ["light.bedroom_main"]
        # The planner instance itself is not built until the first tick.
        assert svc.wake_planner is None

    def test_whitenoise_target_builds_matcher(
        self, service_cls, tmp_path: Path,
    ) -> None:
        cfg = _write_config(tmp_path, natural={
            "whitenoise_target": "media_player.bedroom_speaker",
            "whitenoise_volume_scale": 0.7,
            "whitenoise_track_overrides": {
                "pink_noise": "http://nas/pink.mp3",
            },
        })
        svc = service_cls(_args(cfg))
        assert svc.sound_matcher is not None
        assert svc.sound_matcher.media_player_entity == "media_player.bedroom_speaker"
        # Track override propagates through to the matcher catalogue.
        from src.whitenoise_matcher import Soundscape
        assert svc.sound_matcher.media_url(Soundscape.PINK_NOISE) == "http://nas/pink.mp3"

    def test_feedback_entity_builds_listener(
        self, service_cls, tmp_path: Path,
    ) -> None:
        cfg = _write_config(tmp_path, natural={
            "feedback_entity": "input_number.sleep_rating",
            "feedback_scale": 10,
        })
        svc = service_cls(_args(cfg))
        assert svc.feedback is not None
        assert svc.feedback.entity_id == "input_number.sleep_rating"
        assert svc.feedback.scale == 10

    def test_chronotype_propagates(
        self, service_cls, tmp_path: Path,
    ) -> None:
        cfg = _write_config(tmp_path, natural={"chronotype": "evening"})
        svc = service_cls(_args(cfg))
        assert svc.profile.chronotype == "evening"


# ---------------------------------------------------------------------------
# _is_pre_wake — sound matcher's hook into the wake planner state
# ---------------------------------------------------------------------------


class TestIsPreWakeHook:
    def test_returns_false_without_planner(
        self, service_cls, tmp_path: Path,
    ) -> None:
        from datetime import datetime
        cfg = _write_config(tmp_path, natural={})
        svc = service_cls(_args(cfg))
        assert svc._is_pre_wake(datetime(2026, 5, 12, 6, 50)) is False

    def test_returns_true_inside_ramp(
        self, service_cls, tmp_path: Path,
    ) -> None:
        from datetime import datetime, timedelta
        from src.smart_wake import SmartWakePlanner, WakeWindow
        cfg = _write_config(tmp_path, natural={
            "wake_window_start": "07:00",
            "wake_window_end": "07:30",
        })
        svc = service_cls(_args(cfg))
        # Manually arm a planner anchored on a known date.
        end = datetime(2026, 5, 12, 7, 30)
        svc.wake_planner = SmartWakePlanner(WakeWindow(
            start=end - timedelta(minutes=30), end=end,
        ))
        # ramp_start = end - 30 (default light_ramp_min) = 07:00
        assert svc._is_pre_wake(end - timedelta(minutes=10)) is True
        assert svc._is_pre_wake(end - timedelta(hours=2)) is False
