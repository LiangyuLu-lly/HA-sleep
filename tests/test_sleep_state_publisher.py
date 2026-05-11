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
    ENTITY_DURATION,
    ENTITY_LAST_ACTION,
    ENTITY_QUALITY,
    ENTITY_STAGE,
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
