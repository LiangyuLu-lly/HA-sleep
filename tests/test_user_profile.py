"""Tests for :mod:`src.user_profile` — age cohorts and Bayesian update."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.user_profile import (
    AgeCohort,
    UserProfile,
    UserProfileStore,
    cohort_for_age,
    cohort_recommendation,
)


# ---------------------------------------------------------------------------
# Cohort table — verify breakpoints match NSF / AAP 2015-2016 consensus
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("age,expected", [
    (0.0,   AgeCohort.NEWBORN),
    (0.1,   AgeCohort.NEWBORN),
    (0.5,   AgeCohort.INFANT),
    (2.0,   AgeCohort.TODDLER),
    (5.0,   AgeCohort.PRESCHOOL),
    (10.0,  AgeCohort.SCHOOL_AGE),
    (15.0,  AgeCohort.TEEN),
    (20.0,  AgeCohort.YOUNG_ADULT),
    (40.0,  AgeCohort.ADULT),
    (70.0,  AgeCohort.SENIOR),
])
def test_cohort_breakpoints(age: float, expected: AgeCohort) -> None:
    assert cohort_for_age(age) is expected


def test_cohort_for_age_rejects_negative() -> None:
    with pytest.raises(ValueError):
        cohort_for_age(-1.0)


def test_recommendation_table_has_low_target_high() -> None:
    for cohort in AgeCohort:
        low, target, high = cohort_recommendation(cohort)
        assert 0 < low <= target <= high
        # Sanity: even adults shouldn't be told to sleep < 6 h.
        if cohort in (AgeCohort.ADULT, AgeCohort.YOUNG_ADULT):
            assert low >= 7.0


# ---------------------------------------------------------------------------
# Bayesian update behaviour
# ---------------------------------------------------------------------------


class TestBayesianUpdate:
    def test_no_evidence_returns_cohort_target(self) -> None:
        p = UserProfile(birth_year=1995)   # adult cohort
        assert p.recommended_total_sleep_hours() == p.cohort_target_hours()

    def test_consistent_user_pulls_personal_estimate(self) -> None:
        p = UserProfile(birth_year=1995)
        # User consistently feels great on 7.5 h.
        for _ in range(20):
            p.record_quality_session(7.5, objective_score=85, subjective_score=4.5)
        rec = p.recommended_total_sleep_hours()
        # After 20 nights the personal estimate should dominate the
        # cohort default (8.0).
        assert 7.5 <= rec < 8.0

    def test_low_quality_session_does_not_update_posterior(self) -> None:
        p = UserProfile(birth_year=1995)
        before = p.posterior_count
        p.record_quality_session(5.0, objective_score=40)   # bad night
        assert p.posterior_count == before

    def test_negative_subjective_blocks_update(self) -> None:
        p = UserProfile(birth_year=1995)
        before = p.posterior_count
        p.record_quality_session(7.5, objective_score=85, subjective_score=2.0)
        assert p.posterior_count == before

    def test_recommendation_clamped_to_cohort_bounds(self) -> None:
        """Even a strong personal posterior never escapes the cohort range."""
        p = UserProfile(birth_year=1995)   # adult: bounds (7, 9)
        # Try to pull the posterior outside the clamp.
        for _ in range(50):
            p.record_quality_session(11.0, objective_score=85)
        rec = p.recommended_total_sleep_hours()
        low, high = p.cohort_bounds_hours()
        assert low <= rec <= high


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestStore:
    def test_round_trip_preserves_posterior(self, tmp_path: Path) -> None:
        store = UserProfileStore(tmp_path / "profile.json")
        p = UserProfile(user_id="alice", birth_year=1990)
        p.record_quality_session(7.8, objective_score=80)
        store.save(p)
        # Fresh store, fresh load.
        loaded = UserProfileStore(tmp_path / "profile.json").load("alice")
        assert loaded.user_id == "alice"
        assert loaded.birth_year == 1990
        assert loaded.posterior_count == p.posterior_count
        assert abs(loaded.posterior_mean_hours - 7.8) < 1e-9

    def test_unknown_user_returns_blank(self, tmp_path: Path) -> None:
        store = UserProfileStore(tmp_path / "profile.json")
        p = UserProfile(user_id="alice", birth_year=1990)
        store.save(p)
        bob = store.load("bob")
        assert bob.user_id == "bob"
        assert bob.posterior_count == 0

    def test_corrupt_file_does_not_raise(self, tmp_path: Path) -> None:
        path = tmp_path / "profile.json"
        path.write_text("not json", encoding="utf-8")
        store = UserProfileStore(path)
        p = store.load("alice")
        assert p.user_id == "alice"
        assert p.posterior_count == 0

    def test_list_users(self, tmp_path: Path) -> None:
        store = UserProfileStore(tmp_path / "profile.json")
        store.save(UserProfile(user_id="alice", birth_year=1990))
        store.save(UserProfile(user_id="bob", birth_year=1985))
        assert store.list_users() == ["alice", "bob"]
