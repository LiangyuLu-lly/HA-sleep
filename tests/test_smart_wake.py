"""Tests for :mod:`src.smart_wake` — wake-window planner."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.data_structures import SleepStage
from src.smart_wake import (
    SmartWakePlanner,
    WakeDecision,
    WakeWindow,
    light_ramp_brightness,
)


# ---------------------------------------------------------------------------
# WakeWindow
# ---------------------------------------------------------------------------


class TestWakeWindow:
    def test_from_strings_chooses_tomorrow_if_past(self) -> None:
        ref = datetime(2026, 5, 11, 23, 0)   # 11 PM
        w = WakeWindow.from_strings("07:00", "07:30", ref=ref)
        # 07:00 is in the past relative to 11 PM, so the window must be
        # anchored on the next day.
        assert w.start.date() == datetime(2026, 5, 12).date()

    def test_from_strings_chooses_today_if_future(self) -> None:
        ref = datetime(2026, 5, 11, 4, 0)    # 4 AM
        w = WakeWindow.from_strings("07:00", "07:30", ref=ref)
        # 07:00 is still ahead of us, so the window is today.
        assert w.start.date() == datetime(2026, 5, 11).date()

    def test_invalid_window_rejected(self) -> None:
        s = datetime(2026, 5, 12, 7, 30)
        e = datetime(2026, 5, 12, 7, 0)
        with pytest.raises(ValueError):
            WakeWindow(start=s, end=e)


# ---------------------------------------------------------------------------
# Planner phases
# ---------------------------------------------------------------------------


def _planner_at(
    window_end: datetime,
    *,
    window_minutes: int = 30,
    ramp_min: int = 30,
) -> SmartWakePlanner:
    """Build a planner whose wake window ends at ``window_end``.

    Default window is 30 min, matching a real-world ``07:00-07:30``
    use case.  Tests that need a non-empty PRE_RAMP phase override the
    pair so that ``ramp_min > window_minutes``.
    """
    window = WakeWindow(
        start=window_end - timedelta(minutes=window_minutes),
        end=window_end,
    )
    return SmartWakePlanner(window, light_ramp_min=ramp_min)


class TestPhases:
    def test_too_early_holds(self) -> None:
        end = datetime(2026, 5, 12, 7, 30)
        planner = _planner_at(end)
        plan = planner.tick(now=end - timedelta(hours=2))
        assert plan.decision == WakeDecision.HOLD

    def test_pre_window_starts_light_ramp(self) -> None:
        # Short 10-min window + 30-min ramp → ramp_start = end - 30 =
        # 07:00, window_start = end - 10 = 07:20.  The PRE_RAMP phase
        # is therefore 07:00–07:20.
        end = datetime(2026, 5, 12, 7, 30)
        planner = _planner_at(end, window_minutes=10, ramp_min=30)
        plan = planner.tick(now=end - timedelta(minutes=25))   # 07:05
        assert plan.decision == WakeDecision.PRE_RAMP

    def test_in_window_friendly_stage_fires_now(self) -> None:
        end = datetime(2026, 5, 12, 7, 30)
        planner = _planner_at(end)
        # Feed an unambiguous LIGHT history at high confidence.
        for _ in range(5):
            planner.observe_stage(SleepStage.LIGHT, confidence=0.9)
        plan = planner.tick(now=end - timedelta(minutes=10))
        assert plan.decision == WakeDecision.FIRE_NOW
        assert plan.matched_stage == "LIGHT"

    def test_deep_keeps_open_window(self) -> None:
        end = datetime(2026, 5, 12, 7, 30)
        planner = _planner_at(end)
        for _ in range(5):
            planner.observe_stage(SleepStage.DEEP, confidence=0.9)
        plan = planner.tick(now=end - timedelta(minutes=20))
        assert plan.decision == WakeDecision.OPEN_WINDOW
        assert plan.matched_stage == "DEEP"

    def test_safety_margin_fires_even_in_deep(self) -> None:
        end = datetime(2026, 5, 12, 7, 30)
        planner = _planner_at(end)
        for _ in range(5):
            planner.observe_stage(SleepStage.DEEP, confidence=0.9)
        # Inside the safety margin (last 60 s).
        plan = planner.tick(now=end - timedelta(seconds=30))
        assert plan.decision == WakeDecision.FIRE_NOW
        assert "safety" in plan.reason.lower()

    def test_low_confidence_keeps_window_open(self) -> None:
        end = datetime(2026, 5, 12, 7, 30)
        planner = _planner_at(end)
        for _ in range(5):
            planner.observe_stage(SleepStage.LIGHT, confidence=0.4)
        plan = planner.tick(now=end - timedelta(minutes=15))
        assert plan.decision == WakeDecision.OPEN_WINDOW

    def test_post_rem_transition_recognised(self) -> None:
        end = datetime(2026, 5, 12, 7, 30)
        planner = _planner_at(end)
        # REM → LIGHT signal: ideal moment per Trotter 2018.
        for stage in [SleepStage.REM, SleepStage.REM, SleepStage.LIGHT]:
            planner.observe_stage(stage, confidence=0.9)
        plan = planner.tick(now=end - timedelta(minutes=15))
        assert plan.decision == WakeDecision.FIRE_NOW
        assert "post-REM" in plan.reason

    def test_marked_woken_returns_post_wake(self) -> None:
        end = datetime(2026, 5, 12, 7, 30)
        planner = _planner_at(end)
        planner.mark_woken()
        plan = planner.tick(now=end - timedelta(minutes=10))
        assert plan.decision == WakeDecision.POST_WAKE


# ---------------------------------------------------------------------------
# Light ramp
# ---------------------------------------------------------------------------


class TestLightRamp:
    def test_ramp_zero_before_start(self) -> None:
        s = datetime(2026, 5, 12, 7, 0)
        e = datetime(2026, 5, 12, 7, 30)
        b = light_ramp_brightness(now=s - timedelta(minutes=1),
                                  ramp_start=s, ramp_end=e)
        assert b == 0.0

    def test_ramp_full_after_end(self) -> None:
        s = datetime(2026, 5, 12, 7, 0)
        e = datetime(2026, 5, 12, 7, 30)
        b = light_ramp_brightness(now=e + timedelta(minutes=1),
                                  ramp_start=s, ramp_end=e)
        assert b == 100.0

    def test_ramp_monotonic(self) -> None:
        s = datetime(2026, 5, 12, 7, 0)
        e = datetime(2026, 5, 12, 7, 30)
        prev = -1.0
        for f in [0.1, 0.3, 0.5, 0.8, 0.99]:
            b = light_ramp_brightness(
                now=s + (e - s) * f, ramp_start=s, ramp_end=e,
            )
            assert b >= prev   # non-decreasing
            prev = b

    def test_exp_curve_slower_at_start_than_linear(self) -> None:
        s = datetime(2026, 5, 12, 7, 0)
        e = datetime(2026, 5, 12, 7, 30)
        mid = s + (e - s) * 0.3
        b_lin = light_ramp_brightness(
            now=mid, ramp_start=s, ramp_end=e, curve="linear",
        )
        b_exp = light_ramp_brightness(
            now=mid, ramp_start=s, ramp_end=e, curve="exp",
        )
        assert b_exp < b_lin   # exp curve is below the line at 30 % progress
