"""Tests for :mod:`src.apnea_detector` (v1.6.0 PoC).

The detector is deliberately gated behind consent + calibration in
production wiring (v1.7).  These tests cover the algorithm only — the
exact thing the medical-disclaimer policy *prevents* us from
exercising in the field, which is precisely why we want a paranoid
test suite around it.
"""
from __future__ import annotations

from typing import List

import pytest

from src.apnea_detector import (
    ApneaDetectorConfig,
    ApneaEvent,
    ApneaTrend,
    BreathingSample,
    NightSummary,
    UserBaseline,
    compute_baseline,
    detect_events,
    summarise_night,
    trend_for,
)


# ---------------------------------------------------------------------------
# Fixtures / synth helpers
# ---------------------------------------------------------------------------


def _seconds(start: float, count: int, dt: float = 1.0) -> List[float]:
    """A linspace of unix-second timestamps."""
    return [start + i * dt for i in range(count)]


def _normal_breathing(n: int = 600) -> List[BreathingSample]:
    """10 minutes (n=600 samples at 1 Hz) of healthy 14 bpm breathing."""
    return [
        BreathingSample(timestamp=t, rate_bpm=14.0, amplitude=0.5)
        for t in _seconds(1_700_000_000.0, n)
    ]


def _normal_baseline() -> UserBaseline:
    """The baseline a healthy user would have after calibration."""
    return UserBaseline(
        rate_bpm_median=14.0,
        amplitude_median=0.5,
        nights_observed=7,
    )


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------


class TestComputeBaseline:
    def test_empty_samples_returns_none(self) -> None:
        # No data → can't establish a baseline → caller stays in
        # CALIBRATING.  Returning None is the explicit contract.
        assert compute_baseline([]) is None

    def test_all_none_returns_none(self) -> None:
        # Every sample missing both signals (sensor on the fritz) →
        # also unusable.  Don't fabricate a baseline of zeros.
        samples = [
            BreathingSample(timestamp=float(i)) for i in range(10)
        ]
        assert compute_baseline(samples) is None

    def test_median_of_clean_data(self) -> None:
        samples = _normal_breathing(50)
        baseline = compute_baseline(samples)
        assert baseline is not None
        assert baseline.rate_bpm_median == pytest.approx(14.0)
        assert baseline.amplitude_median == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Event detection
# ---------------------------------------------------------------------------


class TestDetectEvents:
    def test_normal_breathing_yields_no_events(self) -> None:
        # Sanity: a healthy 14 bpm trace must produce ZERO events.
        # If it doesn't, our amplitude-floor or rate thresholds are
        # mis-tuned and would cry wolf for every user.
        events = detect_events(_normal_breathing(), _normal_baseline())
        assert events == []

    def test_apnea_run_below_4_bpm(self) -> None:
        # 30 s at 14 bpm, 15 s at 1 bpm (apnea), 30 s at 14 bpm.
        # The middle window crosses the 10 s minimum so it MUST be
        # flagged.  This is the simplest realistic apnea pattern.
        ts = _seconds(1_700_000_000.0, 75)
        rates = [14.0] * 30 + [1.0] * 15 + [14.0] * 30
        samples = [
            BreathingSample(timestamp=t, rate_bpm=r, amplitude=0.5)
            for t, r in zip(ts, rates)
        ]
        events = detect_events(samples, _normal_baseline())
        apneas = [e for e in events if e.kind == "apnea"]
        assert len(apneas) == 1
        # Duration = 14 s (15 samples - 1 endpoint), within tolerance.
        assert 13.0 <= apneas[0].duration_s <= 16.0

    def test_short_dip_does_not_trigger(self) -> None:
        # A 5 s dip at 1 bpm is below the min_event_seconds=10 floor.
        # MUST NOT trigger.  This is the property that filters out
        # benign sigh patterns where the user holds their breath for
        # a couple of seconds.
        ts = _seconds(1_700_000_000.0, 65)
        rates = [14.0] * 30 + [1.0] * 5 + [14.0] * 30
        samples = [
            BreathingSample(timestamp=t, rate_bpm=r, amplitude=0.5)
            for t, r in zip(ts, rates)
        ]
        events = detect_events(samples, _normal_baseline())
        assert events == []

    def test_hypopnea_requires_both_rate_and_amplitude_drop(self) -> None:
        # 15 s at 6 bpm (rate = 6/14 ≈ 43 % ≤ 50 % ratio) BUT amp = 0.5
        # (no amplitude dip) → NOT hypopnea.  Confirms the AND-gate.
        ts = _seconds(1_700_000_000.0, 75)
        rates = [14.0] * 30 + [6.0] * 15 + [14.0] * 30
        samples = [
            BreathingSample(timestamp=t, rate_bpm=r, amplitude=0.5)
            for t, r in zip(ts, rates)
        ]
        events = detect_events(samples, _normal_baseline())
        assert events == [], (
            "hypopnea fired without an amplitude dip — both gates "
            "must trigger for the event to count"
        )

    def test_hypopnea_full_pattern(self) -> None:
        # 15 s where rate=6 (~43 % of 14) AND amp=0.3 (60 % of 0.5).
        # Both ratios below their thresholds → hypopnea fires.
        ts = _seconds(1_700_000_000.0, 75)
        samples: List[BreathingSample] = []
        for i, t in enumerate(ts):
            if 30 <= i < 45:
                samples.append(BreathingSample(t, rate_bpm=6.0, amplitude=0.3))
            else:
                samples.append(BreathingSample(t, rate_bpm=14.0, amplitude=0.5))
        events = detect_events(samples, _normal_baseline())
        hypos = [e for e in events if e.kind == "hypopnea"]
        assert len(hypos) == 1

    def test_apnea_overrides_hypopnea(self) -> None:
        # When BOTH conditions match (rate < 4 AND rate < 50 % of
        # baseline AND amp < 70 % of baseline), the event is
        # classified as apnea, not hypopnea.  Apnea is the more
        # severe category and should never be misreported as the
        # milder hypopnea — that would mask real respiratory events.
        ts = _seconds(1_700_000_000.0, 75)
        samples = []
        for i, t in enumerate(ts):
            if 30 <= i < 45:
                samples.append(BreathingSample(t, rate_bpm=2.0, amplitude=0.1))
            else:
                samples.append(BreathingSample(t, rate_bpm=14.0, amplitude=0.5))
        events = detect_events(samples, _normal_baseline())
        assert all(e.kind == "apnea" for e in events)
        assert len(events) == 1

    def test_dropout_does_not_fabricate_event(self) -> None:
        # 15 s of None readings (sensor briefly lost the user).
        # The detector treats None as "below threshold = False" so
        # NO event is fabricated from a sensor dropout.  Critical
        # for users who flip onto their stomach mid-night and lose
        # the chest channel for a minute.
        ts = _seconds(1_700_000_000.0, 75)
        samples = []
        for i, t in enumerate(ts):
            if 30 <= i < 45:
                samples.append(BreathingSample(t, rate_bpm=None, amplitude=None))
            else:
                samples.append(BreathingSample(t, rate_bpm=14.0, amplitude=0.5))
        events = detect_events(samples, _normal_baseline())
        assert events == []


# ---------------------------------------------------------------------------
# Per-night summary + trend bucket
# ---------------------------------------------------------------------------


class TestSummariseNight:
    def test_events_per_hour_normalises_by_duration(self) -> None:
        # 1 event in 30 minutes → 2 events/hour, NOT 1.  A 4-hour
        # nap with 1 apnea is very different from an 8-hour night
        # with 1 apnea — the trend bucket needs the rate.
        baseline = _normal_baseline()
        ts = _seconds(1_700_000_000.0, 1801)   # 30 min at 1 Hz
        samples = []
        for i, t in enumerate(ts):
            if 600 <= i < 615:
                samples.append(BreathingSample(t, rate_bpm=1.0, amplitude=0.5))
            else:
                samples.append(BreathingSample(t, rate_bpm=14.0, amplitude=0.5))
        summary = summarise_night(samples, baseline)
        assert len(summary.events) == 1
        assert summary.events_per_hour == pytest.approx(2.0, abs=0.05)

    def test_signal_coverage_drops_with_dropouts(self) -> None:
        # Half the samples are None → coverage = 0.5.  Used by
        # trend_for to demote noisy nights to CALIBRATING.
        ts = _seconds(1_700_000_000.0, 100)
        samples = []
        for i, t in enumerate(ts):
            if i < 50:
                samples.append(BreathingSample(t, rate_bpm=14.0, amplitude=0.5))
            else:
                samples.append(BreathingSample(t))
        summary = summarise_night(samples, _normal_baseline())
        assert summary.signal_coverage_fraction == pytest.approx(0.5)


class TestTrendFor:
    def test_no_consent_dominates(self) -> None:
        # Even with a rip-roaring red night of data, no-consent
        # MUST mask everything.  The medical-disclaimer policy
        # depends on this gate firing first.
        summary = NightSummary(
            events=[
                ApneaEvent("apnea", t, 12.0, 0.9)
                for t in range(0, 3600, 120)
            ],
            recorded_seconds=3600.0,
            signal_coverage_fraction=1.0,
        )
        result = trend_for(summary, _normal_baseline(), consent_given=False)
        assert result is ApneaTrend.PENDING_CONSENT

    def test_calibrating_when_baseline_missing(self) -> None:
        result = trend_for(
            NightSummary(),
            baseline=None,
            consent_given=True,
        )
        assert result is ApneaTrend.CALIBRATING

    def test_calibrating_when_not_enough_nights(self) -> None:
        # 7 calibration nights required; 3 isn't enough.
        baseline = UserBaseline(
            rate_bpm_median=14.0, amplitude_median=0.5, nights_observed=3,
        )
        result = trend_for(
            NightSummary(recorded_seconds=3600.0, signal_coverage_fraction=1.0),
            baseline,
            consent_given=True,
        )
        assert result is ApneaTrend.CALIBRATING

    def test_low_coverage_demotes_to_calibrating(self) -> None:
        # Stomach-sleeping night with 20 % coverage.  Must NOT go red.
        result = trend_for(
            NightSummary(
                events=[
                    ApneaEvent("apnea", t, 12.0, 0.9)
                    for t in range(0, 3600, 100)
                ],
                recorded_seconds=3600.0,
                signal_coverage_fraction=0.2,
            ),
            _normal_baseline(),
            consent_given=True,
        )
        assert result is ApneaTrend.CALIBRATING

    def test_green_for_healthy_night(self) -> None:
        # 0 events / hour → green.
        result = trend_for(
            NightSummary(
                recorded_seconds=8 * 3600.0,
                signal_coverage_fraction=0.95,
            ),
            _normal_baseline(),
            consent_given=True,
        )
        assert result is ApneaTrend.GREEN

    def test_amber_at_clinical_mild_threshold(self) -> None:
        # 8 events in 1 hour → 8 events/hour, in the AASM mild range.
        result = trend_for(
            NightSummary(
                events=[ApneaEvent("apnea", float(i), 12.0, 0.9)
                        for i in range(8)],
                recorded_seconds=3600.0,
                signal_coverage_fraction=0.95,
            ),
            _normal_baseline(),
            consent_given=True,
        )
        assert result is ApneaTrend.AMBER

    def test_red_at_clinical_moderate_threshold(self) -> None:
        result = trend_for(
            NightSummary(
                events=[ApneaEvent("apnea", float(i), 12.0, 0.9)
                        for i in range(20)],
                recorded_seconds=3600.0,
                signal_coverage_fraction=0.95,
            ),
            _normal_baseline(),
            consent_given=True,
        )
        assert result is ApneaTrend.RED

    def test_custom_thresholds_respected(self) -> None:
        # A future setting could let advanced users tighten the
        # thresholds.  Verify the pipeline reads from cfg, not
        # hard-coded constants.
        cfg = ApneaDetectorConfig(amber_threshold=2.0, red_threshold=4.0)
        result = trend_for(
            NightSummary(
                events=[ApneaEvent("apnea", float(i), 12.0, 0.9)
                        for i in range(3)],
                recorded_seconds=3600.0,
                signal_coverage_fraction=0.95,
            ),
            _normal_baseline(),
            consent_given=True,
            config=cfg,
        )
        assert result is ApneaTrend.AMBER
