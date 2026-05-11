"""Tests for :mod:`src.feedback_input` — subjective rating listener."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import pytest

from src.feedback_input import SubjectiveFeedbackListener


# ---------------------------------------------------------------------------
# Minimal stand-in for ``StateChangeEvent`` so the listener stays
# decoupled from src.ha_api_client.
# ---------------------------------------------------------------------------


@dataclass
class _FakeNewState:
    state: str


@dataclass
class _FakeEvent:
    entity_id: str
    new_state: Optional[_FakeNewState]


def _ev(eid: str, state: str) -> _FakeEvent:
    return _FakeEvent(entity_id=eid, new_state=_FakeNewState(state=state))


# ---------------------------------------------------------------------------
# Acceptance + filtering
# ---------------------------------------------------------------------------


class TestAcceptance:
    def test_accepts_in_range_value(self) -> None:
        l = SubjectiveFeedbackListener("input_number.sleep_rating", scale=5)
        snap = l.on_state_change(_ev("input_number.sleep_rating", "4"))
        assert snap is not None
        assert snap.score == 4.0

    def test_ignores_other_entities(self) -> None:
        l = SubjectiveFeedbackListener("input_number.sleep_rating")
        snap = l.on_state_change(_ev("light.bedroom", "on"))
        assert snap is None
        assert l.peek() is None

    def test_ignores_unknown_state(self) -> None:
        l = SubjectiveFeedbackListener("input_number.sleep_rating")
        for v in ("unknown", "unavailable", ""):
            assert l.on_state_change(_ev("input_number.sleep_rating", v)) is None

    def test_ignores_non_numeric(self, caplog: pytest.LogCaptureFixture) -> None:
        l = SubjectiveFeedbackListener("input_number.sleep_rating")
        with caplog.at_level("WARNING"):
            l.on_state_change(_ev("input_number.sleep_rating", "great"))
        assert any("non-numeric" in r.message for r in caplog.records)

    def test_clamps_overshoot_to_scale(self) -> None:
        l = SubjectiveFeedbackListener("input_number.sleep_rating", scale=5)
        snap = l.on_state_change(_ev("input_number.sleep_rating", "6"))
        assert snap is not None
        assert snap.score == 5.0   # clamped down

    def test_warns_only_once_per_bad_value(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        l = SubjectiveFeedbackListener("input_number.sleep_rating")
        with caplog.at_level("WARNING"):
            l.on_state_change(_ev("input_number.sleep_rating", "junk"))
            l.on_state_change(_ev("input_number.sleep_rating", "junk"))
        warns = [r for r in caplog.records if "non-numeric" in r.message]
        assert len(warns) == 1


# ---------------------------------------------------------------------------
# Consume / freshness
# ---------------------------------------------------------------------------


class TestConsume:
    def test_consume_returns_then_none(self) -> None:
        l = SubjectiveFeedbackListener("input_number.sleep_rating")
        l.on_state_change(_ev("input_number.sleep_rating", "4"))
        first = l.consume()
        assert first is not None
        # Same rating must not be applied to a second session.
        assert l.consume() is None

    def test_new_rating_replaces_consumed(self) -> None:
        l = SubjectiveFeedbackListener("input_number.sleep_rating")
        l.on_state_change(_ev("input_number.sleep_rating", "4"))
        l.consume()
        l.on_state_change(_ev("input_number.sleep_rating", "5"))
        snap = l.consume()
        assert snap is not None
        assert snap.score == 5.0

    def test_stale_rating_is_dropped(self) -> None:
        l = SubjectiveFeedbackListener("input_number.sleep_rating",
                                       max_age_hours=0.5)
        l.on_state_change(_ev("input_number.sleep_rating", "4"))
        # Forge an old timestamp.
        assert l._latest is not None
        l._latest.received_at = time.time() - 3600   # 1 hour ago
        assert l.peek() is None
        assert l.consume() is None


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def test_status_starts_empty() -> None:
    l = SubjectiveFeedbackListener("input_number.sleep_rating")
    s = l.status()
    assert s["entity_id"] == "input_number.sleep_rating"
    assert s["latest"] is None


def test_status_after_event() -> None:
    l = SubjectiveFeedbackListener("input_number.sleep_rating")
    l.on_state_change(_ev("input_number.sleep_rating", "3"))
    s = l.status()
    assert s["latest_score"] == 3.0
    assert s["is_fresh"] is True
    assert s["consumed"] is False
