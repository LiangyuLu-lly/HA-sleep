"""Tests for :mod:`src.sleep_state_publisher`.

Two failure modes we care about:

1.  HA is briefly down — every ``update_state`` call raises.  The
    publisher must swallow the error so the inference loop keeps running.
2.  The model emits a sleep stage every 30 s but its confidence wobbles
    by 0.1 % between calls.  The publisher should de-dupe these no-ops
    so we don't spam HA's recorder with hundreds of identical writes.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.data_structures import SleepStage
from src.sleep_state_publisher import (
    ENTITY_CONFIDENCE,
    ENTITY_DEBT,
    ENTITY_DURATION,
    ENTITY_LAST_ACTION,
    ENTITY_LEARNED_BEDTIME_WEEKEND,
    ENTITY_LEARNED_BEDTIME_WORKDAY,
    ENTITY_LEARNED_ENVIRONMENT,
    ENTITY_PER_STAGE_DELTAS,
    ENTITY_QUALITY,
    ENTITY_RECOMMENDATION_EXPLAIN,
    ENTITY_RECOMMENDED_BEDTIME,
    ENTITY_SOUNDSCAPE,
    ENTITY_STAGE,
    ENTITY_WAKE_DECISION,
    SleepStatePublisher,
)


@pytest.fixture
def ha_client() -> AsyncMock:
    """Stand-in HomeAssistantClient with the methods the publisher calls."""
    client = AsyncMock()
    client.update_state = AsyncMock(return_value=None)
    return client


@pytest.fixture
def publisher(ha_client: AsyncMock) -> SleepStatePublisher:
    return SleepStatePublisher(ha_client, confidence_deadband=0.05)


# ---------------------------------------------------------------------------
# Stage publishing
# ---------------------------------------------------------------------------


class TestPublishStage:
    async def test_first_call_publishes_stage_and_confidence(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_stage(SleepStage.DEEP, 0.91)
        # First publish always writes both entities.
        called_ids = [c.args[0] for c in ha_client.update_state.call_args_list]
        assert ENTITY_STAGE in called_ids
        assert ENTITY_CONFIDENCE in called_ids
        assert ha_client.update_state.await_count == 2

    async def test_repeat_call_with_same_state_skips_writes(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_stage(SleepStage.DEEP, 0.91)
        ha_client.update_state.reset_mock()
        # Same stage, confidence within deadband → no writes
        await publisher.publish_stage(SleepStage.DEEP, 0.92)
        assert ha_client.update_state.await_count == 0

    async def test_stage_change_always_publishes(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_stage(SleepStage.DEEP, 0.91)
        ha_client.update_state.reset_mock()
        await publisher.publish_stage(SleepStage.REM, 0.91)
        # New stage forces both writes again, even with same confidence.
        called_ids = [c.args[0] for c in ha_client.update_state.call_args_list]
        assert ENTITY_STAGE in called_ids

    async def test_confidence_jump_outside_deadband_publishes(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_stage(SleepStage.LIGHT, 0.50)
        ha_client.update_state.reset_mock()
        await publisher.publish_stage(SleepStage.LIGHT, 0.80)
        assert ha_client.update_state.await_count >= 1

    async def test_environment_attached_to_stage_attributes(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_stage(
            SleepStage.DEEP, 0.91,
            env_temperature_c=22.5, env_humidity_pct=48.0,
        )
        # Find the call that wrote the stage entity and inspect its attrs.
        stage_calls = [
            c for c in ha_client.update_state.call_args_list
            if c.args[0] == ENTITY_STAGE
        ]
        assert stage_calls, "stage entity was not written"
        attrs = stage_calls[0].kwargs["attributes"]
        assert attrs["temperature_c"] == 22.5
        assert attrs["humidity_pct"] == 48.0
        assert attrs["confidence_pct"] == 91.0


# ---------------------------------------------------------------------------
# Robustness: HA-side transient errors must not break the inference loop
# ---------------------------------------------------------------------------


class TestRobustness:
    async def test_ha_outage_does_not_raise(
        self, ha_client: AsyncMock,
    ) -> None:
        ha_client.update_state.side_effect = RuntimeError("HA went away")
        publisher = SleepStatePublisher(ha_client)
        # Must not raise.
        await publisher.publish_stage(SleepStage.AWAKE, 0.7)
        assert publisher.stats.failures >= 1
        assert publisher.stats.publishes == 0

    async def test_repeated_failures_only_warn_once(
        self, ha_client: AsyncMock, caplog: pytest.LogCaptureFixture,
    ) -> None:
        ha_client.update_state.side_effect = RuntimeError("HA went away")
        publisher = SleepStatePublisher(ha_client)
        with caplog.at_level("WARNING"):
            for _ in range(5):
                await publisher.publish_stage(SleepStage.AWAKE, 0.7)
                # Bump stage so each publish_stage actually attempts a write.
                publisher.stats.last_stage = None
                publisher.stats.last_conf = None
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        # First failure logs WARNING, subsequent ones drop to DEBUG so we
        # don't drown the user's HA log if their network blips for an hour.
        assert len(warnings) == 1


# ---------------------------------------------------------------------------
# The auxiliary publish_* helpers
# ---------------------------------------------------------------------------


class TestAuxiliaryPublishers:
    async def test_publish_quality_writes_quality_entity(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_quality(82.5)
        call = next(
            c for c in ha_client.update_state.call_args_list
            if c.args[0] == ENTITY_QUALITY
        )
        assert call.args[1] == 82.5

    async def test_publish_duration_writes_duration_entity(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_duration(3600.7)
        # Float duration is stored as int seconds (HA sensor convention).
        call = next(
            c for c in ha_client.update_state.call_args_list
            if c.args[0] == ENTITY_DURATION
        )
        assert call.args[1] == 3600

    async def test_publish_last_action_truncates_long_summaries(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        long_summary = "x" * 1000
        await publisher.publish_last_action(long_summary, executed=True)
        call = next(
            c for c in ha_client.update_state.call_args_list
            if c.args[0] == ENTITY_LAST_ACTION
        )
        assert len(call.args[1]) == 255
        assert call.kwargs["attributes"]["executed"] is True

    async def test_publish_last_action_dry_run_marker(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_last_action("light.x → off", executed=False)
        call = next(
            c for c in ha_client.update_state.call_args_list
            if c.args[0] == ENTITY_LAST_ACTION
        )
        assert call.kwargs["attributes"]["executed"] is False


# ---------------------------------------------------------------------------
# Natural-sleep entities (v1.2.0)
# ---------------------------------------------------------------------------


class TestNaturalSleepPublishers:
    """publish_debt / publish_recommended_bedtime / publish_wake_decision /
    publish_soundscape — added in v1.2.0 alongside the 6 new modules.
    """

    async def test_publish_debt_writes_value_and_severity(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_debt(
            3.4, severity="moderate",
            target_hours=8.0, nights_to_full_recovery=2,
        )
        call = next(
            c for c in ha_client.update_state.call_args_list
            if c.args[0] == ENTITY_DEBT
        )
        assert call.args[1] == 3.4
        attrs = call.kwargs["attributes"]
        assert attrs["severity"] == "moderate"
        assert attrs["nightly_target_hours"] == 8.0
        assert attrs["nights_to_full_recovery"] == 2

    async def test_publish_recommended_bedtime_iso(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        from datetime import datetime
        bedtime = datetime(2026, 5, 12, 23, 35)
        await publisher.publish_recommended_bedtime(
            bedtime, tonight_target_hours=7.5, reason="MILD",
        )
        call = next(
            c for c in ha_client.update_state.call_args_list
            if c.args[0] == ENTITY_RECOMMENDED_BEDTIME
        )
        # HA's timestamp device_class needs an ISO string.
        assert call.args[1] == "2026-05-12T23:35:00"
        attrs = call.kwargs["attributes"]
        assert attrs["tonight_target_hours"] == 7.5
        assert attrs["reason"] == "MILD"

    async def test_publish_recommended_bedtime_none_writes_unknown(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_recommended_bedtime(None)
        call = next(
            c for c in ha_client.update_state.call_args_list
            if c.args[0] == ENTITY_RECOMMENDED_BEDTIME
        )
        assert call.args[1] == "unknown"

    async def test_publish_wake_decision_includes_alarm_iso(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        from datetime import datetime
        await publisher.publish_wake_decision(
            "fire_now",
            reason="post-REM transition into LIGHT",
            alarm_time=datetime(2026, 5, 12, 7, 22),
            light_ramp_start=datetime(2026, 5, 12, 7, 0),
            matched_stage="LIGHT",
        )
        call = next(
            c for c in ha_client.update_state.call_args_list
            if c.args[0] == ENTITY_WAKE_DECISION
        )
        assert call.args[1] == "fire_now"
        attrs = call.kwargs["attributes"]
        assert attrs["alarm_time"] == "2026-05-12T07:22:00"
        assert attrs["light_ramp_start"] == "2026-05-12T07:00:00"
        assert attrs["matched_stage"] == "LIGHT"

    async def test_publish_soundscape_writes_volume(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_soundscape(
            "brown_noise", volume_pct=18.5, reason="DEEP / SWA support",
        )
        call = next(
            c for c in ha_client.update_state.call_args_list
            if c.args[0] == ENTITY_SOUNDSCAPE
        )
        assert call.args[1] == "brown_noise"
        attrs = call.kwargs["attributes"]
        assert attrs["volume_pct"] == 18.5
        assert attrs["reason"] == "DEEP / SWA support"


class TestLearningPanelPublishers:
    """v1.3.0 PreferenceLearner-driven entities."""

    async def test_publish_learned_bedtime_writes_both_buckets(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_learned_bedtime({
            "weekday_bedtime": "23:00",
            "weekend_bedtime": "00:15",
            "tonight_bucket": "workday",
            "n_workday": 12,
            "n_weekend": 4,
            "confidence": 0.78,
        })
        calls = {c.args[0]: c for c in ha_client.update_state.call_args_list}
        assert calls[ENTITY_LEARNED_BEDTIME_WORKDAY].args[1] == "23:00"
        assert calls[ENTITY_LEARNED_BEDTIME_WEEKEND].args[1] == "00:15"
        wd_attrs = calls[ENTITY_LEARNED_BEDTIME_WORKDAY].kwargs["attributes"]
        assert wd_attrs["n_samples"] == 12
        assert wd_attrs["confidence"] == 0.78
        assert wd_attrs["tonight_bucket"] == "workday"

    async def test_publish_learned_bedtime_unknown_when_none(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_learned_bedtime({
            "weekday_bedtime": None,
            "weekend_bedtime": None,
            "n_workday": 0,
            "n_weekend": 0,
            "confidence": 0.0,
        })
        calls = {c.args[0]: c for c in ha_client.update_state.call_args_list}
        assert calls[ENTITY_LEARNED_BEDTIME_WORKDAY].args[1] == "unknown"
        assert calls[ENTITY_LEARNED_BEDTIME_WEEKEND].args[1] == "unknown"

    async def test_publish_learned_environment_formats_headline(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_learned_environment(
            {"temperature_c": 19.4, "humidity_pct": 52.0, "brightness_pct": 5.0},
            confidence=0.91,
            n_used=4,
        )
        c = ha_client.update_state.call_args_list[0]
        assert c.args[0] == ENTITY_LEARNED_ENVIRONMENT
        # State must be human-readable; per-field values still in attrs.
        assert "19.4" in c.args[1] and "°C" in c.args[1]
        attrs = c.kwargs["attributes"]
        assert attrs["temperature_c"] == 19.4
        assert attrs["humidity_pct"] == 52.0
        assert attrs["confidence"] == 0.91
        assert attrs["n_used"] == 4

    async def test_publish_learned_environment_dash_when_missing(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_learned_environment({})
        c = ha_client.update_state.call_args_list[0]
        # All three fields render as "—" → state is "— / — / —".
        assert c.args[1].count("—") == 3

    async def test_publish_recommendation_explain_ready(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        payload = {
            "ready": True,
            "method": "knn+decay",
            "n_total": 12,
            "recommendation": {"temperature_c": 19.5},
            "neighbors": [{"session_id": f"s{i}", "weight": 0.5} for i in range(10)],
            "confidence": 0.8,
        }
        await publisher.publish_recommendation_explain(payload)
        c = ha_client.update_state.call_args_list[0]
        assert c.args[0] == ENTITY_RECOMMENDATION_EXPLAIN
        assert c.args[1] == "ready"
        attrs = c.kwargs["attributes"]
        assert attrs["method"] == "knn+decay"
        assert attrs["n_total"] == 12
        # Neighbor list must be capped at 5 to stay under HA's 16 KB limit.
        assert len(attrs["neighbors"]) == 5

    async def test_publish_recommendation_explain_not_ready(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_recommendation_explain({
            "ready": False,
            "reason": "need 3 sessions, have 1",
            "n_total": 1,
        })
        c = ha_client.update_state.call_args_list[0]
        assert c.args[1] == "not_ready"
        assert c.kwargs["attributes"]["reason"] == "need 3 sessions, have 1"
        assert c.kwargs["attributes"]["n_total"] == 1


class TestInitialPlaceholders:
    """Boot-time seeding: every owned entity must get a state immediately."""

    async def test_publishes_all_fourteen_entities(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        await publisher.publish_initial_placeholders()
        called = {c.args[0] for c in ha_client.update_state.call_args_list}
        # v1.5.0: 5 legacy + 4 natural-sleep + 4 learning +
        # 1 per-stage-deltas = 14 entities.
        expected = {
            ENTITY_STAGE, ENTITY_CONFIDENCE, ENTITY_QUALITY, ENTITY_DURATION,
            ENTITY_LAST_ACTION, ENTITY_DEBT, ENTITY_RECOMMENDED_BEDTIME,
            ENTITY_WAKE_DECISION, ENTITY_SOUNDSCAPE,
            ENTITY_LEARNED_BEDTIME_WORKDAY, ENTITY_LEARNED_BEDTIME_WEEKEND,
            ENTITY_LEARNED_ENVIRONMENT, ENTITY_RECOMMENDATION_EXPLAIN,
            ENTITY_PER_STAGE_DELTAS,
        }
        assert called == expected

    async def test_failures_do_not_propagate(
        self, ha_client: AsyncMock,
    ) -> None:
        ha_client.update_state.side_effect = RuntimeError("HA is down")
        publisher = SleepStatePublisher(ha_client)
        # Must not raise, even though every single update fails.
        await publisher.publish_initial_placeholders()
        # v1.5.0: 14 entities seeded → 14 failures recorded.
        assert publisher.stats.failures == 14


# ---------------------------------------------------------------------------
# v1.5.0 — Per-stage deltas publishing
# ---------------------------------------------------------------------------


class TestPublishPerStageDeltas:
    """The per-stage-deltas sensor's state must accurately reflect how
    much of the controller's policy is *learned* vs *clinical default*."""

    async def test_empty_payload_publishes_clinical_state(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        # ``deltas={}`` mimics a learner that hasn't computed anything
        # yet (or one that pre-dates v1.5.0).  Sensor must default to
        # ``clinical`` so users see "controller using defaults" at a
        # glance rather than a stale value or "unknown".
        await publisher.publish_per_stage_deltas({})
        assert ha_client.update_state.call_count == 1
        c = ha_client.update_state.call_args_list[0]
        assert c.args[0] == ENTITY_PER_STAGE_DELTAS
        assert c.args[1] == "clinical"

    async def test_state_is_learning_when_ess_below_threshold(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        # Some samples accumulated but no stage crossed ESS=4 yet —
        # users should see "learning" so they know it's progressing.
        deltas = {
            "AWAKE": {"temperature_c": None, "humidity_pct": None,
                      "brightness_pct": None, "fan_speed_pct": None,
                      "ess": 2.0, "n_sessions": 2},
            "LIGHT": {"temperature_c": 0.0, "humidity_pct": 0.0,
                      "brightness_pct": 0.0, "fan_speed_pct": 0.0,
                      "ess": 2.0, "n_sessions": 2},
            "DEEP": {"temperature_c": None, "humidity_pct": None,
                     "brightness_pct": None, "fan_speed_pct": None,
                     "ess": 2.0, "n_sessions": 2},
            "REM": {"temperature_c": None, "humidity_pct": None,
                    "brightness_pct": None, "fan_speed_pct": None,
                    "ess": 1.0, "n_sessions": 1},
        }
        await publisher.publish_per_stage_deltas(deltas)
        c = ha_client.update_state.call_args_list[0]
        assert c.args[1] == "learning"

    async def test_state_is_personalised_when_any_stage_active(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        # DEEP has a real learned delta + sufficient ESS → the
        # controller is now using a personalised policy.  Other
        # stages still being learned shouldn't downgrade the state.
        deltas = {
            "AWAKE": {"temperature_c": None, "humidity_pct": None,
                      "brightness_pct": None, "fan_speed_pct": None,
                      "ess": 2.0, "n_sessions": 2},
            "LIGHT": {"temperature_c": 0.0, "humidity_pct": 0.0,
                      "brightness_pct": 0.0, "fan_speed_pct": 0.0,
                      "ess": 10.0, "n_sessions": 10},
            "DEEP": {"temperature_c": -1.7, "humidity_pct": None,
                     "brightness_pct": None, "fan_speed_pct": None,
                     "ess": 8.0, "n_sessions": 8},
            "REM": {"temperature_c": None, "humidity_pct": None,
                    "brightness_pct": None, "fan_speed_pct": None,
                    "ess": 0.0, "n_sessions": 0},
        }
        await publisher.publish_per_stage_deltas(deltas)
        c = ha_client.update_state.call_args_list[0]
        assert c.args[1] == "personalised"

    async def test_attributes_flatten_for_lovelace(
        self, publisher: SleepStatePublisher, ha_client: AsyncMock,
    ) -> None:
        # HA frontends choke on nested dicts in attributes — verify
        # we surface the per-stage data as flat ``deep_temperature_c_delta``
        # / ``deep_ess`` keys.
        deltas = {
            "DEEP": {"temperature_c": -1.7, "humidity_pct": None,
                     "brightness_pct": None, "fan_speed_pct": None,
                     "ess": 8.0, "n_sessions": 8},
            "LIGHT": {"temperature_c": 0.0, "humidity_pct": 0.0,
                      "brightness_pct": 0.0, "fan_speed_pct": 0.0,
                      "ess": 10.0, "n_sessions": 10},
        }
        await publisher.publish_per_stage_deltas(deltas)
        attrs = ha_client.update_state.call_args_list[0].kwargs["attributes"]
        assert attrs["deep_temperature_c_delta"] == pytest.approx(-1.7)
        assert attrs["deep_ess"] == pytest.approx(8.0)
        assert attrs["deep_n_sessions"] == 8
        # Humidity was None → must not appear in attributes (HA
        # rendering rules prefer "missing" over "null").
        assert "deep_humidity_pct_delta" not in attrs
        # ess_threshold also surfaced so dashboards can render a
        # progress bar without hard-coding 4.
        assert attrs["ess_threshold"] == pytest.approx(4.0)
