"""Tests for :mod:`src.sleep_debt` — debt accumulation + recovery planning."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.sleep_debt import (
    DebtSeverity,
    NightRecord,
    SleepDebtTracker,
    MAX_SAME_NIGHT_RECOVERY_H,
)
from src.user_profile import UserProfile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def adult_profile() -> UserProfile:
    """Adult user with 8.0 h target (no personal posterior yet)."""
    return UserProfile(birth_year=1995)


def _make_night(date: str, slept: float, target: float) -> NightRecord:
    return NightRecord(
        date=date, target_hours=target, actual_hours=slept, quality_score=70.0,
    )


# ---------------------------------------------------------------------------
# Debt computation
# ---------------------------------------------------------------------------


class TestDebtComputation:
    def test_no_history_zero_debt(self, adult_profile: UserProfile) -> None:
        tracker = SleepDebtTracker(adult_profile)
        assert tracker.current_debt_hours() == 0.0

    def test_one_night_short_one_hour(self, adult_profile: UserProfile) -> None:
        tracker = SleepDebtTracker(adult_profile)
        tracker.add_night(_make_night("2026-05-10", slept=7.0, target=8.0))
        debt = tracker.current_debt_hours(now=datetime(2026, 5, 11))
        assert abs(debt - 1.0) < 1e-6

    def test_oversleep_creates_credit(self, adult_profile: UserProfile) -> None:
        tracker = SleepDebtTracker(adult_profile)
        tracker.add_night(_make_night("2026-05-10", slept=9.5, target=8.0))
        debt = tracker.current_debt_hours(now=datetime(2026, 5, 11))
        assert debt < 0      # credit, not debt

    def test_acute_window_full_weight(self, adult_profile: UserProfile) -> None:
        tracker = SleepDebtTracker(adult_profile)
        # 7 nights of -1 h within the acute window
        for d in range(1, 8):
            tracker.add_night(_make_night(
                f"2026-05-{10 - d:02d}", slept=7.0, target=8.0,
            ))
        debt = tracker.current_debt_hours(now=datetime(2026, 5, 10))
        assert abs(debt - 7.0) < 1e-6

    def test_old_nights_decay(self, adult_profile: UserProfile) -> None:
        tracker = SleepDebtTracker(adult_profile)
        # A single 14-night-old shortfall should be discounted heavily.
        old_night = _make_night("2026-04-26", slept=4.0, target=8.0)  # -4 h
        tracker.add_night(old_night)
        debt = tracker.current_debt_hours(now=datetime(2026, 5, 10))
        assert 0 < debt < 4.0   # decayed

    def test_severity_bands(self, adult_profile: UserProfile) -> None:
        tracker = SleepDebtTracker(adult_profile)
        # 5 h slept against 8 h target = 3 h debt → lands inside [2, 4) MODERATE.
        tracker.add_night(_make_night("2026-05-10", slept=5.0, target=8.0))
        sev = tracker.severity(now=datetime(2026, 5, 11))
        assert sev == DebtSeverity.MODERATE


# ---------------------------------------------------------------------------
# Recovery planning
# ---------------------------------------------------------------------------


class TestRecovery:
    def test_no_debt_just_sleep_target(self, adult_profile: UserProfile) -> None:
        tracker = SleepDebtTracker(adult_profile)
        plan = tracker.plan_recovery(now=datetime(2026, 5, 11))
        assert plan.severity == DebtSeverity.NONE
        assert plan.tonight_target_hours == adult_profile.cohort_target_hours()
        assert plan.nights_to_full_recovery == 0

    def test_small_debt_single_night_recovery(
        self, adult_profile: UserProfile,
    ) -> None:
        tracker = SleepDebtTracker(adult_profile)
        tracker.add_night(_make_night("2026-05-10", slept=6.5, target=8.0))   # -1.5 h
        plan = tracker.plan_recovery(now=datetime(2026, 5, 11))
        assert plan.severity == DebtSeverity.MILD
        assert plan.tonight_target_hours > 8.0    # extra sleep prescribed
        # Should fully recover in 1-2 nights.
        assert plan.nights_to_full_recovery <= 2

    def test_large_debt_multi_night_paydown(
        self, adult_profile: UserProfile,
    ) -> None:
        tracker = SleepDebtTracker(adult_profile)
        # 3 nights of -3 h = 9 h debt, way over the cap.
        for d in range(1, 4):
            tracker.add_night(_make_night(
                f"2026-05-{10 - d:02d}", slept=5.0, target=8.0,
            ))
        plan = tracker.plan_recovery(now=datetime(2026, 5, 10))
        assert plan.severity in (DebtSeverity.SEVERE, DebtSeverity.CHRONIC)
        # Should not try to dump it all in one night.
        assert plan.tonight_target_hours <= 11.0
        assert plan.nights_to_full_recovery >= 2
        assert "1 night" not in plan.message.lower()

    def test_recovery_caps_at_max_night_total(
        self, adult_profile: UserProfile,
    ) -> None:
        tracker = SleepDebtTracker(adult_profile)
        # 20 h of debt — physiologically impossible to clear in one night.
        for d in range(1, 8):
            tracker.add_night(_make_night(
                f"2026-05-{10 - d:02d}", slept=4.0, target=8.0,
            ))
        plan = tracker.plan_recovery(now=datetime(2026, 5, 10))
        assert plan.tonight_target_hours <= 11.0   # MAX_NIGHT_TOTAL_HOURS

    def test_plan_with_wake_window_computes_bedtime(
        self, adult_profile: UserProfile,
    ) -> None:
        tracker = SleepDebtTracker(adult_profile)
        tracker.add_night(_make_night("2026-05-10", slept=6.0, target=8.0))
        plan = tracker.plan_recovery(
            wake_window=("07:00", "07:30"),
            now=datetime(2026, 5, 10, 22, 0),
        )
        assert plan.tonight_bedtime is not None
        assert plan.wake_target is not None
        # Wake = end of window the next morning at 07:30.
        assert plan.wake_target.hour == 7 and plan.wake_target.minute == 30
        # Bedtime ≈ wake - tonight_target_hours
        delta_h = (plan.wake_target - plan.tonight_bedtime).total_seconds() / 3600
        assert abs(delta_h - plan.tonight_target_hours) < 1e-3


# ---------------------------------------------------------------------------
# Recovery efficiency physiology
# ---------------------------------------------------------------------------


class TestRecoveryPhysiology:
    def test_efficiency_zero_at_zero(self, adult_profile: UserProfile) -> None:
        tracker = SleepDebtTracker(adult_profile)
        assert tracker.recovery_efficiency(0.0) == 0.0

    def test_efficiency_saturates(self, adult_profile: UserProfile) -> None:
        tracker = SleepDebtTracker(adult_profile)
        # η(2 h) ≈ 0.74 ; η(4 h) ≈ 0.93 — calibration point in the docstring.
        eta_2 = tracker.recovery_efficiency(2.0)
        eta_4 = tracker.recovery_efficiency(4.0)
        assert 0.7 < eta_2 < 0.8
        assert 0.9 < eta_4 < 0.97
        assert eta_4 > eta_2

    def test_invert_recovery_inverse_of_effective(
        self, adult_profile: UserProfile,
    ) -> None:
        tracker = SleepDebtTracker(adult_profile)
        for target in [0.5, 1.0, 1.8]:
            extra = tracker._invert_recovery(target)
            actual = tracker.effective_debt_reduction(extra)
            assert abs(actual - target) < 0.05
