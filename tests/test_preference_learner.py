"""Unit tests for :mod:`src.preference_learner`."""
from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import List

import pytest

from src.data_structures import SleepStage
from src.preference_learner import (
    EnvironmentParams,
    PreferenceConfig,
    PreferenceLearner,
    SleepSession,
    compute_quality_score,
    stage_counts_from_sequence,
)


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------


class TestQualityScore:
    def test_empty_distribution_returns_zero(self):
        assert compute_quality_score({}) == 0.0
        assert compute_quality_score({"AWAKE": 0, "LIGHT": 0}) == 0.0

    def test_good_night_scores_higher_than_bad(self):
        good = {"AWAKE": 5, "LIGHT": 50, "DEEP": 15, "REM": 30}    # near ideal
        bad = {"AWAKE": 50, "LIGHT": 40, "DEEP": 5, "REM": 5}     # fragmented
        assert compute_quality_score(good) > compute_quality_score(bad)
        assert compute_quality_score(good) >= 50.0
        assert compute_quality_score(bad) <= 50.0

    def test_clamped_to_unit_interval(self):
        # All AWAKE — heavily penalised.
        s = compute_quality_score({"AWAKE": 100, "LIGHT": 0, "DEEP": 0, "REM": 0})
        assert 0.0 <= s <= 100.0
        # All DEEP — exceeds 100% before clamp but should not leak past.
        s = compute_quality_score({"AWAKE": 0, "LIGHT": 0, "DEEP": 100, "REM": 0})
        assert 0.0 <= s <= 100.0


class TestStageCountsFromSequence:
    def test_counts_match_input(self):
        seq = [SleepStage.AWAKE, SleepStage.LIGHT, SleepStage.LIGHT,
               SleepStage.DEEP, SleepStage.REM]
        counts = stage_counts_from_sequence(seq)
        assert counts == {"AWAKE": 1, "LIGHT": 2, "DEEP": 1, "REM": 1}

    def test_empty_sequence(self):
        assert stage_counts_from_sequence([]) == {
            "AWAKE": 0, "LIGHT": 0, "DEEP": 0, "REM": 0,
        }


# ---------------------------------------------------------------------------
# Helper to construct sessions
# ---------------------------------------------------------------------------


def _session(
    sid: str,
    quality: float,
    *,
    temp: float = 20.0,
    hum: float = 55.0,
    bright: float = 0.0,
    n: int = 100,
) -> SleepSession:
    return SleepSession(
        session_id=sid,
        started_at=0.0,
        ended_at=0.0,
        env_params=EnvironmentParams(
            temperature_c=temp, humidity_pct=hum, brightness_pct=bright,
        ),
        stage_counts={"AWAKE": 5, "LIGHT": 80, "DEEP": 10, "REM": 5},
        quality_score=quality,
        n_samples=n,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_records_persist_to_disk(self, tmp_path: Path):
        cfg = PreferenceConfig(history_path=str(tmp_path / "history.json"))
        learner = PreferenceLearner(cfg)
        learner.record_session(_session("a", 75.0))
        learner.record_session(_session("b", 60.0))

        assert Path(cfg.history_path).exists()
        with open(cfg.history_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        assert payload["version"] == 1
        assert len(payload["sessions"]) == 2

    def test_reload_from_disk(self, tmp_path: Path):
        cfg = PreferenceConfig(history_path=str(tmp_path / "history.json"))
        learner = PreferenceLearner(cfg)
        learner.record_session(_session("a", 80.0))

        # Second learner instance reads the same file.
        learner2 = PreferenceLearner(cfg)
        assert learner2.n_sessions() == 1
        assert learner2.status().startswith("1 session(s)")

    def test_history_caps_at_max_sessions_kept(self, tmp_path: Path):
        cfg = PreferenceConfig(
            history_path=str(tmp_path / "h.json"),
            max_sessions_kept=5,
        )
        learner = PreferenceLearner(cfg)
        for i in range(10):
            learner.record_session(_session(f"s{i}", 50.0 + i))
        assert learner.n_sessions() == 5

    def test_corrupt_file_falls_back_to_empty(self, tmp_path: Path):
        path = tmp_path / "broken.json"
        path.write_text("not valid json", encoding="utf-8")
        cfg = PreferenceConfig(history_path=str(path))
        learner = PreferenceLearner(cfg)
        assert learner.n_sessions() == 0   # should not raise


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------


class TestRecommendation:
    def test_too_few_sessions_returns_defaults(self, tmp_path: Path):
        cfg = PreferenceConfig(
            history_path=str(tmp_path / "h.json"),
            min_sessions_for_personalisation=3,
        )
        learner = PreferenceLearner(cfg)
        learner.record_session(_session("a", 80.0, temp=18.0))

        defaults = EnvironmentParams(temperature_c=21.0, humidity_pct=55.0,
                                     brightness_pct=10.0)
        rec = learner.recommend(defaults)
        # Only 1 session in history → defaults must be returned verbatim.
        assert rec == defaults

    def test_picks_median_of_top_quantile(self, tmp_path: Path):
        cfg = PreferenceConfig(
            history_path=str(tmp_path / "h.json"),
            min_sessions_for_personalisation=3,
            quality_quantile=0.7,
        )
        learner = PreferenceLearner(cfg)
        # 5 sessions; top 30% = top 2 nights, both with temp=18°C
        learner.record_session(_session("worst", 30.0, temp=24.0))
        learner.record_session(_session("bad",   40.0, temp=23.0))
        learner.record_session(_session("ok",    60.0, temp=22.0))
        learner.record_session(_session("good",  85.0, temp=18.0))
        learner.record_session(_session("best",  95.0, temp=18.0))

        rec = learner.recommend(EnvironmentParams(temperature_c=21.0))
        assert rec.temperature_c == pytest.approx(18.0)

    def test_defaults_fill_missing_fields(self, tmp_path: Path):
        cfg = PreferenceConfig(
            history_path=str(tmp_path / "h.json"),
            min_sessions_for_personalisation=2,
            quality_quantile=0.0,
        )
        learner = PreferenceLearner(cfg)
        # All sessions only have temperature recorded; brightness etc.
        # should fall back to defaults.
        for i in range(3):
            learner.record_session(SleepSession(
                session_id=f"s{i}",
                started_at=0.0,
                ended_at=0.0,
                env_params=EnvironmentParams(temperature_c=19.0),
                stage_counts={"AWAKE": 5, "LIGHT": 70, "DEEP": 15, "REM": 10},
                quality_score=80.0,
                n_samples=100,
            ))
        defaults = EnvironmentParams(
            temperature_c=21.0, humidity_pct=55.0, brightness_pct=8.0,
        )
        rec = learner.recommend(defaults)
        assert rec.temperature_c == pytest.approx(19.0)
        assert rec.humidity_pct == 55.0
        assert rec.brightness_pct == 8.0

    def test_exploration_adds_noise(self, tmp_path: Path):
        cfg = PreferenceConfig(
            history_path=str(tmp_path / "h.json"),
            min_sessions_for_personalisation=2,
            quality_quantile=0.0,
            exploration_rate=0.5,
        )
        learner = PreferenceLearner(cfg, rng=random.Random(0))
        for i in range(3):
            learner.record_session(_session(f"s{i}", 80.0, temp=19.0, hum=55.0))

        defaults = EnvironmentParams(temperature_c=19.0, humidity_pct=55.0)
        rec = learner.recommend(defaults, explore=True)
        # With non-zero noise the recommendation should differ from the
        # historical median (the deterministic random.Random seed makes this
        # reproducible).
        assert rec.temperature_c != pytest.approx(19.0)
