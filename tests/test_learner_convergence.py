"""7-day continuous synthetic data learner convergence test (Sprint 4A).

Verifies that after 7 sessions with consistent environment parameters,
the learner's recommendations converge to the true values.
"""
from __future__ import annotations

import random
import time
from datetime import datetime
from pathlib import Path

import pytest

from src.preference_learner import (
    EnvironmentParams,
    PreferenceConfig,
    PreferenceLearner,
    SleepSession,
)


def _make_session(
    idx: int,
    *,
    rng: random.Random,
    base_ts: float,
) -> SleepSession:
    """Create a synthetic session with fixed env and random quality 70-90."""
    started_at = base_ts + idx * 86400 + 23 * 3600  # 23:00 each night
    ended_at = started_at + 7.5 * 3600  # 7.5 h sleep
    return SleepSession(
        session_id=f"convergence_{idx}",
        started_at=started_at,
        ended_at=ended_at,
        env_params=EnvironmentParams(
            temperature_c=20.0,
            humidity_pct=50.0,
            brightness_pct=5.0,
            fan_speed_pct=10.0,
        ),
        stage_counts={"AWAKE": 10, "LIGHT": 150, "DEEP": 60, "REM": 40},
        quality_score=rng.uniform(70.0, 90.0),
        n_samples=260,
        recorded_at=ended_at,
    )


class TestLearnerConvergence:
    """After 7 consistent nights the learner should converge."""

    def test_temperature_converges_to_true_value(self, tmp_path: Path) -> None:
        """recommend().temperature_c should land in [19.5, 20.5]."""
        cfg = PreferenceConfig(
            history_path=str(tmp_path / "h.json"),
            min_sessions_for_personalisation=3,
            quality_quantile=0.5,
        )
        learner = PreferenceLearner(cfg)
        rng = random.Random(42)
        base_ts = datetime(2024, 6, 1, 0, 0, 0).timestamp()

        for i in range(7):
            learner.record_session(_make_session(i, rng=rng, base_ts=base_ts))

        defaults = EnvironmentParams(
            temperature_c=21.0, humidity_pct=55.0,
            brightness_pct=10.0, fan_speed_pct=15.0,
        )
        rec = learner.recommend(defaults, now_ts=base_ts + 8 * 86400)
        assert rec.temperature_c is not None
        assert 19.5 <= rec.temperature_c <= 20.5, (
            f"Expected temperature_c in [19.5, 20.5], got {rec.temperature_c}"
        )

    def test_recommend_bedtime_returns_weekday(self, tmp_path: Path) -> None:
        """After 7 sessions recommend_bedtime should return a non-None weekday_bedtime."""
        cfg = PreferenceConfig(
            history_path=str(tmp_path / "h.json"),
            min_sessions_for_personalisation=3,
        )
        learner = PreferenceLearner(cfg)
        rng = random.Random(42)
        base_ts = datetime(2024, 6, 3, 0, 0, 0).timestamp()  # Monday

        for i in range(7):
            learner.record_session(_make_session(i, rng=rng, base_ts=base_ts))

        now = datetime(2024, 6, 11, 20, 0, 0)  # Tuesday evening
        result = learner.recommend_bedtime(now=now)
        assert result["weekday_bedtime"] is not None

    def test_recommend_knn_confidence_above_threshold(
        self, tmp_path: Path,
    ) -> None:
        """After 7 sessions recommend_knn confidence should exceed 0.5."""
        cfg = PreferenceConfig(
            history_path=str(tmp_path / "h.json"),
            min_sessions_for_personalisation=3,
            knn_k=5,
        )
        learner = PreferenceLearner(cfg)
        rng = random.Random(42)
        base_ts = datetime(2024, 6, 3, 0, 0, 0).timestamp()

        for i in range(7):
            learner.record_session(_make_session(i, rng=rng, base_ts=base_ts))

        defaults = EnvironmentParams(
            temperature_c=21.0, humidity_pct=55.0,
            brightness_pct=10.0, fan_speed_pct=15.0,
        )
        now = datetime(2024, 6, 11, 23, 0, 0)
        result = learner.recommend_knn(
            defaults, now=now, current_temp_c=20.0,
        )
        assert result["confidence"] > 0.5, (
            f"Expected confidence > 0.5, got {result['confidence']}"
        )
