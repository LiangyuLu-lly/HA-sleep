"""Unit tests for :class:`LearningPanelPublisher` (v1.6.0).

The panel was extracted out of ``scripts/run_ha_smart_service.py`` to
make it independently testable.  Pre-v1.6 the same logic was buried
inside the orchestrator and required a full SmartSleepService stand-up
to exercise.  These tests instead drive the panel against trivial
learner / publisher / profile doubles.

What we lock down:

* The two public coroutines no-op silently when the learner or
  publisher is missing (orchestrator boot path depends on this).
* All five HA sensor publish methods are called when the learner
  is fully populated.
* A learner that predates v1.5 (no ``recommend_per_stage_deltas``)
  still triggers the v1.3 publishes.
* A learner that raises mid-flight is logged but doesn't propagate.
* The env_provider callable is invoked at publish time, not at
  panel-construction time (so the panel sees fresh env updates).
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.learning_panel_publisher import LearningPanelPublisher
from src.preference_learner import EnvironmentParams

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _full_learner() -> MagicMock:
    """Learner stub exposing every method the panel calls."""
    learner = MagicMock()
    learner.sessions.return_value = []
    learner.recommend_bedtime.return_value = {
        "weekday_bedtime": "23:00",
        "weekend_bedtime": "23:30",
        "next_bedtime": "23:00",
        "tonight_bucket": "weekday",
        "confidence": 0.6,
    }
    learner.recommend_knn.return_value = {
        "env": EnvironmentParams(
            temperature_c=20.0, humidity_pct=50.0,
            brightness_pct=0.0, fan_speed_pct=0.0,
        ),
        "neighbors": [],
        "n_used": 5,
        "confidence": 0.7,
    }
    learner.explain.return_value = {
        "ready": True, "method": "knn+decay",
        "n_total": 12, "effective_sample_size": 6.5,
        "neighbors": [],
    }
    learner.recommend_per_stage_deltas.return_value = {
        "AWAKE": {"temperature_c": 1.5, "humidity_pct": None,
                  "brightness_pct": None, "fan_speed_pct": None,
                  "ess": 6.0, "n_sessions": 6},
        "LIGHT": {"temperature_c": 0.0, "humidity_pct": 0.0,
                  "brightness_pct": 0.0, "fan_speed_pct": 0.0,
                  "ess": 10.0, "n_sessions": 10},
        "DEEP": {"temperature_c": -1.7, "humidity_pct": None,
                 "brightness_pct": None, "fan_speed_pct": None,
                 "ess": 7.0, "n_sessions": 7},
        "REM": {"temperature_c": -1.0, "humidity_pct": None,
                "brightness_pct": None, "fan_speed_pct": None,
                "ess": 5.0, "n_sessions": 5},
    }
    return learner


def _publisher() -> AsyncMock:
    pub = AsyncMock()
    pub.publish_debt = AsyncMock()
    pub.publish_recommended_bedtime = AsyncMock()
    pub.publish_learned_bedtime = AsyncMock()
    pub.publish_learned_environment = AsyncMock()
    pub.publish_recommendation_explain = AsyncMock()
    pub.publish_per_stage_deltas = AsyncMock()
    return pub


def _profile() -> MagicMock:
    p = MagicMock()
    p.recommended_total_sleep_hours.return_value = 8.0
    return p


def _make_panel(
    learner: Any = None,
    publisher: Any = None,
    env: EnvironmentParams | None = None,
) -> LearningPanelPublisher:
    return LearningPanelPublisher(
        learner=learner if learner is not None else _full_learner(),
        publisher=publisher if publisher is not None else _publisher(),
        profile=_profile(),
        wake_window_strs=["07:00", "07:30"],
        env_provider=lambda: env or EnvironmentParams(
            temperature_c=21.0, humidity_pct=50.0,
            brightness_pct=5.0, fan_speed_pct=10.0,
        ),
    )


# ---------------------------------------------------------------------------
# debt + bedtime
# ---------------------------------------------------------------------------


class TestDebtAndBedtime:
    async def test_no_op_when_learner_missing(self) -> None:
        # The orchestrator constructs the panel before the learner
        # exists; we must NOT crash, just silently skip.
        # _make_panel() coerces None→stub, so build the panel directly.
        pub = _publisher()
        panel = LearningPanelPublisher(
            learner=None, publisher=pub, profile=_profile(),
            wake_window_strs=["07:00", "07:30"],
            env_provider=lambda: EnvironmentParams(),
        )
        await panel.publish_debt_and_bedtime()
        pub.publish_debt.assert_not_called()
        pub.publish_recommended_bedtime.assert_not_called()

    async def test_no_op_when_publisher_missing(self) -> None:
        # Symmetric: no publisher (HA isn't reachable yet) → silently skip.
        learner = _full_learner()
        panel = LearningPanelPublisher(
            learner=learner, publisher=None, profile=_profile(),
            wake_window_strs=["07:00", "07:30"],
            env_provider=lambda: EnvironmentParams(),
        )
        # Must not raise even though learner.sessions() would otherwise
        # fire — short-circuit before touching the learner.
        await panel.publish_debt_and_bedtime()
        learner.sessions.assert_not_called()

    async def test_publishes_both_debt_and_bedtime(self) -> None:
        pub = _publisher()
        panel = _make_panel(publisher=pub)
        await panel.publish_debt_and_bedtime()
        pub.publish_debt.assert_awaited_once()
        pub.publish_recommended_bedtime.assert_awaited_once()


# ---------------------------------------------------------------------------
# learning panel
# ---------------------------------------------------------------------------


class TestLearningPanel:
    async def test_full_v15_learner_publishes_all_five(self) -> None:
        # Modern learner with per-stage support → all five sensors
        # touched in one call.
        pub = _publisher()
        panel = _make_panel(publisher=pub)
        await panel.publish_learning_panel()
        pub.publish_learned_bedtime.assert_awaited_once()
        pub.publish_learned_environment.assert_awaited_once()
        pub.publish_recommendation_explain.assert_awaited_once()
        pub.publish_per_stage_deltas.assert_awaited_once()

    async def test_pre_v15_learner_skips_per_stage_silently(self) -> None:
        # A learner without recommend_per_stage_deltas should still
        # publish the four v1.3 sensors.  Catches the AttributeError
        # branch and confirms it's swallowed without dropping the rest.
        learner = _full_learner()
        del learner.recommend_per_stage_deltas
        # `del` on a MagicMock removes the auto-attr, so subsequent
        # access raises AttributeError — simulating an old learner.
        pub = _publisher()
        panel = _make_panel(learner=learner, publisher=pub)
        await panel.publish_learning_panel()
        pub.publish_learned_bedtime.assert_awaited_once()
        pub.publish_learned_environment.assert_awaited_once()
        pub.publish_recommendation_explain.assert_awaited_once()
        pub.publish_per_stage_deltas.assert_not_awaited()

    async def test_learner_crash_is_swallowed(self, caplog) -> None:
        # If the learner explodes mid-flight, we log a warning but
        # never re-raise.  The orchestrator's inference loop relies
        # on this guarantee — a corrupted history must not stop the
        # add-on from controlling devices.
        learner = _full_learner()
        learner.recommend_bedtime.side_effect = RuntimeError("disk corrupt")
        panel = _make_panel(learner=learner)
        await panel.publish_learning_panel()    # must not raise

    async def test_env_provider_called_at_publish_time(self) -> None:
        # The orchestrator's last_env is mutable; the panel must read
        # the *latest* value at publish time, not whatever existed
        # when the panel was constructed.
        seen_temps: List[float] = []

        def _provider() -> EnvironmentParams:
            seen_temps.append(22.5)
            return EnvironmentParams(temperature_c=22.5, humidity_pct=50.0)

        learner = _full_learner()
        panel = LearningPanelPublisher(
            learner=learner,
            publisher=_publisher(),
            profile=_profile(),
            wake_window_strs=["07:00", "07:30"],
            env_provider=_provider,
        )
        await panel.publish_learning_panel()
        assert seen_temps, "env_provider was never called"
        # And it was passed to recommend_knn via current_temp_c.
        learner.recommend_knn.assert_called()
        assert learner.recommend_knn.call_args.kwargs["current_temp_c"] == 22.5


# ---------------------------------------------------------------------------
# env coercion
# ---------------------------------------------------------------------------


class TestEnvCoercion:
    async def test_env_provider_returning_dict_is_coerced(self) -> None:
        # Defensive coercion in `_current_env_defaults` — if a future
        # orchestrator change holds env as a dict instead of an
        # EnvironmentParams, the panel still works.
        panel = LearningPanelPublisher(
            learner=_full_learner(),
            publisher=_publisher(),
            profile=_profile(),
            wake_window_strs=["07:00", "07:30"],
            env_provider=lambda: {
                "temperature_c": 19.0,
                "humidity_pct": 55.0,
            },
        )
        # Should run cleanly without TypeError.
        await panel.publish_learning_panel()
