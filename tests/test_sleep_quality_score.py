"""Tests for :mod:`src.sleep_quality_score` — SE / WASO / SOL composite."""
from __future__ import annotations

from typing import List

import pytest

from src.data_structures import SleepStage
from src.sleep_quality_score import (
    SleepMetrics,
    blend_subjective,
    compute_metrics,
    compute_objective_quality,
    compute_quality_score,
)


# ---------------------------------------------------------------------------
# compute_metrics
# ---------------------------------------------------------------------------


def _hypnogram(*, sol: int, sleep: int, waso: int, post: int) -> List[SleepStage]:
    """Synthetic stage sequence: sol AWAKE epochs, then 'sleep' epochs of LIGHT
    with ``waso`` AWAKE epochs in the middle, then ``post`` final AWAKE."""
    seq: List[SleepStage] = []
    seq.extend([SleepStage.AWAKE] * sol)
    half = sleep // 2
    seq.extend([SleepStage.LIGHT] * half)
    seq.extend([SleepStage.AWAKE] * waso)
    seq.extend([SleepStage.LIGHT] * (sleep - half))
    seq.extend([SleepStage.AWAKE] * post)
    return seq


class TestComputeMetrics:
    def test_empty_returns_zeros(self) -> None:
        m = compute_metrics([])
        assert m.tib_min == 0.0
        assert m.tst_min == 0.0
        assert m.sleep_efficiency_pct == 0.0

    def test_pure_sleep_full_efficiency(self) -> None:
        # 60 epochs of LIGHT @ 30 s = 30 min TST, 30 min TIB, 100 % SE.
        m = compute_metrics([SleepStage.LIGHT] * 60, epoch_seconds=30.0)
        assert m.tst_min == pytest.approx(30.0)
        assert m.tib_min == pytest.approx(30.0)
        assert m.sleep_efficiency_pct == pytest.approx(100.0)
        assert m.waso_min == 0.0
        assert m.sol_min == 0.0

    def test_sol_extracted_from_leading_awake(self) -> None:
        # 10 epochs (= 5 min) of AWAKE then sleep
        m = compute_metrics(_hypnogram(sol=10, sleep=40, waso=0, post=0))
        assert m.sol_min == pytest.approx(5.0)
        # TIB = 50 epochs * 0.5 min = 25 min ; TST = 40 * 0.5 = 20 min
        assert m.tib_min == pytest.approx(25.0)
        assert m.tst_min == pytest.approx(20.0)

    def test_waso_excludes_sol_and_final(self) -> None:
        # SOL=4, sleep=20, mid-WASO=6, final=4 → WASO should be 3 min (6 epochs).
        m = compute_metrics(_hypnogram(sol=4, sleep=20, waso=6, post=4))
        assert m.waso_min == pytest.approx(3.0)
        assert m.sol_min == pytest.approx(2.0)

    def test_awakening_count(self) -> None:
        # Two distinct AWAKE bouts inside the sleep period.
        seq = (
            [SleepStage.AWAKE] * 2
            + [SleepStage.LIGHT] * 4
            + [SleepStage.AWAKE] * 2     # awakening 1
            + [SleepStage.LIGHT] * 4
            + [SleepStage.AWAKE] * 2     # awakening 2
            + [SleepStage.LIGHT] * 4
            + [SleepStage.AWAKE] * 2
        )
        m = compute_metrics(seq)
        assert m.n_awakenings == 2


# ---------------------------------------------------------------------------
# Sub-scores
# ---------------------------------------------------------------------------


class TestSubscores:
    def test_high_efficiency_high_score(self) -> None:
        m = SleepMetrics(tst_min=480, tib_min=500, waso_min=10, sol_min=10,
                         n_awakenings=1, stage_counts={"LIGHT": 800, "DEEP": 80,
                                                       "REM": 100, "AWAKE": 20})
        scores = compute_objective_quality(m)
        assert scores["efficiency"] >= 80
        assert scores["composite"] > 50

    def test_low_efficiency_low_score(self) -> None:
        m = SleepMetrics(tst_min=300, tib_min=600, waso_min=180, sol_min=120,
                         n_awakenings=10, stage_counts={"LIGHT": 500, "DEEP": 50,
                                                        "REM": 50, "AWAKE": 600})
        scores = compute_objective_quality(m)
        assert scores["efficiency"] < 50
        assert scores["fragmentation"] < 50
        assert scores["onset"] < 50
        assert scores["composite"] < 50

    def test_sub_5_min_sol_not_full_score(self) -> None:
        # SOL < 5 min suggests sleep deprivation, so onset should NOT
        # be at its peak.
        m = SleepMetrics(tst_min=480, tib_min=500, waso_min=5, sol_min=2,
                         n_awakenings=1, stage_counts={"LIGHT": 800, "DEEP": 80,
                                                       "REM": 100, "AWAKE": 10})
        scores = compute_objective_quality(m)
        assert scores["onset"] < 90.0

    def test_optimal_sol_around_10min(self) -> None:
        m = SleepMetrics(tst_min=480, tib_min=500, waso_min=5, sol_min=10,
                         n_awakenings=1, stage_counts={"LIGHT": 800, "DEEP": 80,
                                                       "REM": 100, "AWAKE": 10})
        scores = compute_objective_quality(m)
        assert scores["onset"] >= 90.0


# ---------------------------------------------------------------------------
# Subjective blending
# ---------------------------------------------------------------------------


class TestBlend:
    def test_no_subjective_returns_objective(self) -> None:
        assert blend_subjective(70.0, None) == 70.0

    def test_subjective_5_pulls_score_up(self) -> None:
        out = blend_subjective(60.0, 5, subjective_weight=0.5)
        assert out > 60.0

    def test_subjective_1_pulls_score_down(self) -> None:
        out = blend_subjective(80.0, 1, subjective_weight=0.5)
        assert out < 80.0

    def test_clamps_subjective_to_scale(self) -> None:
        # An out-of-range subjective is clamped, not rejected.
        out = blend_subjective(50.0, 99, subjective_weight=1.0)
        assert out == 100.0


# ---------------------------------------------------------------------------
# Backwards compatibility
# ---------------------------------------------------------------------------


class TestBackwardsCompat:
    def test_legacy_signature_still_works(self) -> None:
        # The original 1-arg signature must keep returning a 0-100 score.
        score = compute_quality_score({"LIGHT": 50, "DEEP": 10, "REM": 15, "AWAKE": 5})
        assert 0.0 <= score <= 100.0

    def test_with_metrics_returns_composite(self) -> None:
        m = SleepMetrics(tst_min=480, tib_min=500, waso_min=10, sol_min=10,
                         n_awakenings=1, stage_counts={"LIGHT": 800, "DEEP": 80,
                                                       "REM": 100, "AWAKE": 20})
        score = compute_quality_score(m.stage_counts, metrics=m)
        # Must be inside the [0,100] band and reflect the high efficiency.
        assert 50 < score <= 100
