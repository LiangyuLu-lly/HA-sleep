"""Unit tests for the v1.3.0 PreferenceLearner additions.

Covers:
  * ``recorded_at`` field round-trips through to_dict / from_dict.
  * Exponential decay weighting of session quality.
  * Weighted-median tie-breaker.
  * ``recommend_bedtime`` weekday/weekend split.
  * ``recommend_knn`` neighbour selection and confidence reporting.
  * ``explain`` payload shape suitable for an HA attribute panel.

These tests are deliberately independent of the legacy ones in
``test_preference_learner.py`` so the older suite still asserts the
pre-v1.3 invariants (median-of-quantile, persistence layout, etc.).
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pytest

from src.preference_learner import (
    EnvironmentParams,
    PreferenceConfig,
    PreferenceLearner,
    SleepSession,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _session(
    sid: str,
    quality: float,
    *,
    temp: Optional[float] = None,
    hum: Optional[float] = None,
    bright: Optional[float] = None,
    started_at: float = 0.0,
    ended_at: Optional[float] = None,
    recorded_at: float = 0.0,
) -> SleepSession:
    """Compact session-builder for the v1.3 test cases."""
    return SleepSession(
        session_id=sid,
        started_at=started_at,
        ended_at=ended_at if ended_at is not None else started_at + 7 * 3600,
        env_params=EnvironmentParams(
            temperature_c=temp, humidity_pct=hum, brightness_pct=bright,
        ),
        stage_counts={"AWAKE": 5, "LIGHT": 80, "DEEP": 10, "REM": 5},
        quality_score=quality,
        n_samples=100,
        recorded_at=recorded_at,
    )


@pytest.fixture
def learner(tmp_path: Path) -> PreferenceLearner:
    cfg = PreferenceConfig(
        history_path=str(tmp_path / "v13.json"),
        min_sessions_for_personalisation=3,
        decay_half_life_days=7.0,    # 1-week half life keeps tests sharp
        knn_k=3,
    )
    return PreferenceLearner(cfg)


# ---------------------------------------------------------------------------
# recorded_at backfill
# ---------------------------------------------------------------------------


class TestRecordedAtBackfill:
    def test_default_falls_back_to_ended_at(self) -> None:
        s = _session("a", 80.0, started_at=1_700_000_000.0, ended_at=1_700_025_200.0)
        assert s.recorded_at == 1_700_025_200.0

    def test_explicit_value_preserved(self) -> None:
        s = _session("b", 80.0, started_at=1.0, recorded_at=2.0)
        assert s.recorded_at == 2.0

    def test_roundtrip_through_json(self) -> None:
        s = _session("c", 90.0, started_at=1_700_000_000.0, recorded_at=1_700_500_000.0)
        rt = SleepSession.from_dict(s.to_dict())
        assert rt.recorded_at == 1_700_500_000.0

    def test_from_dict_without_recorded_at_falls_back(self) -> None:
        raw = {
            "session_id": "old",
            "started_at": 1_700_000_000.0,
            "ended_at": 1_700_025_200.0,
            "env_params": {},
            "stage_counts": {},
            "quality_score": 80.0,
            "n_samples": 0,
        }
        rt = SleepSession.from_dict(raw)
        # No recorded_at in payload → __post_init__ should backfill it.
        assert rt.recorded_at == 1_700_025_200.0


# ---------------------------------------------------------------------------
# Decay weighting + weighted median
# ---------------------------------------------------------------------------


class TestDecayWeight:
    def test_fresh_session_has_full_weight(self, learner: PreferenceLearner) -> None:
        now_ts = time.time()
        s = _session("fresh", 80.0, recorded_at=now_ts)
        assert learner._decay_weight(s, now_ts) == pytest.approx(1.0)

    def test_half_life_gives_half_weight(self, learner: PreferenceLearner) -> None:
        now_ts = time.time()
        # Half-life is 7 days from the fixture.
        s = _session("old", 80.0, recorded_at=now_ts - 7 * 86400)
        assert learner._decay_weight(s, now_ts) == pytest.approx(0.5, rel=1e-6)

    def test_future_session_clamped_to_one(self, learner: PreferenceLearner) -> None:
        now_ts = time.time()
        s = _session("clock_skew", 80.0, recorded_at=now_ts + 86400)
        assert learner._decay_weight(s, now_ts) == pytest.approx(1.0)

    def test_recent_outranks_old_with_same_quality(
        self, learner: PreferenceLearner,
    ) -> None:
        now_ts = time.time()
        old = _session("old", 90.0, temp=18.0, recorded_at=now_ts - 30 * 86400)
        new = _session("new", 90.0, temp=22.0, recorded_at=now_ts - 1 * 86400)
        mid = _session("mid", 50.0, temp=20.0, recorded_at=now_ts - 5 * 86400)
        for s in (old, new, mid):
            learner.record_session(s)

        # Recommendation should prefer the new session's temperature.
        rec = learner.recommend(
            EnvironmentParams(temperature_c=30.0),
            now_ts=now_ts,
        )
        assert rec.temperature_c == pytest.approx(22.0)


class TestWeightedMedian:
    def test_equal_weights_match_plain_median(self) -> None:
        v = [10.0, 20.0, 30.0]
        w = [1.0, 1.0, 1.0]
        assert PreferenceLearner._weighted_median(v, w) == 20.0

    def test_heavy_weight_dominates(self) -> None:
        v = [10.0, 20.0, 30.0]
        w = [0.01, 10.0, 0.01]
        assert PreferenceLearner._weighted_median(v, w) == 20.0

    def test_zero_weights_fall_back_to_plain_median(self) -> None:
        v = [10.0, 20.0, 30.0]
        w = [0.0, 0.0, 0.0]
        assert PreferenceLearner._weighted_median(v, w) == 20.0

    def test_drops_none_values(self) -> None:
        # None is filtered, leaving [20, 30] with equal weights.  The
        # cumulative weight first crosses half-of-total at the 20 bin,
        # so the weighted-median walker correctly returns 20.0.
        v = [None, 20.0, 30.0]
        w = [1.0, 1.0, 1.0]
        assert PreferenceLearner._weighted_median(v, w) == 20.0

    def test_empty_returns_none(self) -> None:
        assert PreferenceLearner._weighted_median([], []) is None


# ---------------------------------------------------------------------------
# recommend_bedtime
# ---------------------------------------------------------------------------


class TestRecommendBedtime:
    def _ts_at(self, days_ago: int, hh: int, mm: int = 0) -> float:
        """Return a unix ts for ``days_ago`` calendar days before now."""
        d = datetime.now() - timedelta(days=days_ago)
        d = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
        return d.timestamp()

    def test_returns_none_when_buckets_too_small(
        self, learner: PreferenceLearner,
    ) -> None:
        # Only one workday sample, no weekend samples.
        learner.record_session(_session(
            "a", 80.0, started_at=self._ts_at(2, 23, 30),
        ))
        out = learner.recommend_bedtime(min_per_bucket=2)
        assert out["weekday_bedtime"] is None
        assert out["weekend_bedtime"] is None
        assert out["next_bedtime"] is None

    def test_splits_weekend_and_workday(
        self, learner: PreferenceLearner,
    ) -> None:
        # Find the most recent Saturday + Wednesday so we can plant
        # synthetic bedtimes on known weekdays.
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        sat = today - timedelta(days=(today.weekday() - 5) % 7)
        wed = today - timedelta(days=(today.weekday() - 2) % 7)

        for offset in range(3):
            workday_ts = (wed - timedelta(weeks=offset)).replace(hour=22, minute=30).timestamp()
            weekend_ts = (sat - timedelta(weeks=offset)).replace(hour=23, minute=45).timestamp()
            learner.record_session(_session(
                f"wk{offset}", 80.0, started_at=workday_ts,
            ))
            learner.record_session(_session(
                f"we{offset}", 80.0, started_at=weekend_ts,
            ))

        out = learner.recommend_bedtime(min_per_bucket=2)
        assert out["weekday_bedtime"] == "22:30"
        assert out["weekend_bedtime"] == "23:45"
        assert out["n_workday"] == 3
        assert out["n_weekend"] == 3
        assert 0.0 < out["confidence"] <= 1.0


# ---------------------------------------------------------------------------
# recommend_knn
# ---------------------------------------------------------------------------


class TestRecommendKnn:
    def test_cold_start_returns_defaults(self, learner: PreferenceLearner) -> None:
        defaults = EnvironmentParams(temperature_c=21.0)
        out = learner.recommend_knn(defaults)
        assert out["env"] == defaults
        assert out["n_used"] == 0
        assert out["confidence"] == 0.0
        assert out["neighbors"] == []

    def test_selects_top_k_by_combined_weight(
        self, learner: PreferenceLearner,
    ) -> None:
        # Five sessions, only three should appear (knn_k=3 from fixture).
        now_ts = time.time()
        # All recent so decay doesn't dominate; differentiate via quality.
        learner.record_session(_session(
            "best", 95.0, temp=18.0, recorded_at=now_ts - 86400,
            started_at=now_ts - 86400,
        ))
        learner.record_session(_session(
            "good", 80.0, temp=19.0, recorded_at=now_ts - 86400,
            started_at=now_ts - 86400,
        ))
        learner.record_session(_session(
            "ok",   60.0, temp=20.0, recorded_at=now_ts - 86400,
            started_at=now_ts - 86400,
        ))
        learner.record_session(_session(
            "bad",  20.0, temp=23.0, recorded_at=now_ts - 86400,
            started_at=now_ts - 86400,
        ))
        learner.record_session(_session(
            "worst", 5.0, temp=25.0, recorded_at=now_ts - 86400,
            started_at=now_ts - 86400,
        ))

        defaults = EnvironmentParams(temperature_c=21.0)
        out = learner.recommend_knn(defaults)
        assert out["n_used"] == 3
        ids = {n["session_id"] for n in out["neighbors"]}
        assert "best" in ids
        assert "good" in ids
        assert "worst" not in ids

    def test_temperature_kernel_pulls_recommendation_toward_query(
        self, learner: PreferenceLearner,
    ) -> None:
        now_ts = time.time()
        # Two equally good clusters; the temperature kernel must break
        # the tie in favour of the cluster centred on the query temp.
        for i in range(3):
            learner.record_session(_session(
                f"cool{i}", 80.0, temp=18.0, recorded_at=now_ts - i * 86400,
                started_at=now_ts - i * 86400,
            ))
        for i in range(3):
            learner.record_session(_session(
                f"warm{i}", 80.0, temp=26.0, recorded_at=now_ts - i * 86400,
                started_at=now_ts - i * 86400,
            ))
        defaults = EnvironmentParams(temperature_c=22.0)
        out = learner.recommend_knn(defaults, current_temp_c=25.0)
        assert out["env"].temperature_c == pytest.approx(26.0)

        out2 = learner.recommend_knn(defaults, current_temp_c=17.0)
        assert out2["env"].temperature_c == pytest.approx(18.0)


# ---------------------------------------------------------------------------
# explain
# ---------------------------------------------------------------------------


class TestExplain:
    def test_cold_start_marks_not_ready(self, learner: PreferenceLearner) -> None:
        out = learner.explain(EnvironmentParams(temperature_c=21.0))
        assert out["ready"] is False
        assert "need" in out["reason"]
        assert out["n_total"] == 0
        assert out["recommendation"]["temperature_c"] == 21.0

    def test_ready_payload_has_expected_shape(
        self, learner: PreferenceLearner,
    ) -> None:
        now_ts = time.time()
        for i in range(5):
            learner.record_session(_session(
                f"s{i}", 80.0, temp=19.0 + i, recorded_at=now_ts - i * 86400,
                started_at=now_ts - i * 86400,
            ))
        out = learner.explain(EnvironmentParams(temperature_c=21.0))
        assert out["ready"] is True
        assert out["method"] == "knn+decay"
        assert out["n_total"] == 5
        assert isinstance(out["recommendation"], dict)
        assert "temperature_c" in out["recommendation"]
        assert isinstance(out["neighbors"], list)
        assert "bedtime" in out
        assert 0.0 <= out["confidence"] <= 1.0
        # effective_sample_size must never exceed the actual sample count.
        assert out["effective_sample_size"] <= out["n_total"]

    def test_attribute_panel_is_json_serialisable(
        self, learner: PreferenceLearner,
    ) -> None:
        """HA's WebSocket layer rejects attribute dicts that aren't pure JSON."""
        import json
        now_ts = time.time()
        for i in range(4):
            learner.record_session(_session(
                f"s{i}", 75.0, temp=20.0, recorded_at=now_ts - i * 86400,
                started_at=now_ts - i * 86400,
            ))
        out = learner.explain(EnvironmentParams(temperature_c=21.0))
        # Should round-trip through json without raising.
        as_text = json.dumps(out)
        assert "recommendation" in as_text
