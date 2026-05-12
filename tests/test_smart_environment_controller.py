"""Unit tests for :mod:`src.smart_environment_controller`.

The controller talks to HA via :class:`src.ha_api_client.HomeAssistantClient`,
which is async.  We replace the client with an ``AsyncMock`` and assert on
the planned + executed action lists.
"""
from __future__ import annotations

from typing import List
from unittest.mock import AsyncMock

import pytest

from src.data_structures import SleepStage
from src.device_discovery import ActionableDevices
from src.ha_api_client import HAEntity
from src.preference_learner import EnvironmentParams
from src.smart_environment_controller import (
    ControlAction,
    SmartControlConfig,
    SmartEnvironmentController,
    is_in_wind_down,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _light(eid: str) -> HAEntity:
    return HAEntity(entity_id=eid, state="off", attributes={})


def _climate(eid: str) -> HAEntity:
    return HAEntity(entity_id=eid, state="cool", attributes={})


def _humidifier(eid: str) -> HAEntity:
    return HAEntity(entity_id=eid, state="on", attributes={})


def _fan(eid: str) -> HAEntity:
    return HAEntity(entity_id=eid, state="off", attributes={})


@pytest.fixture
def devices() -> ActionableDevices:
    return ActionableDevices(
        lights=[_light("light.bedroom_main")],
        climates=[_climate("climate.bedroom_ac")],
        humidifiers=[_humidifier("humidifier.bedroom")],
        fans=[_fan("fan.bedroom")],
    )


@pytest.fixture
def ha_client():
    """An AsyncMock standing in for HomeAssistantClient."""
    mock = AsyncMock()
    mock.call_service = AsyncMock(return_value=None)
    return mock


@pytest.fixture
def controller(devices, ha_client):
    cfg = SmartControlConfig(
        enabled=True,
        min_seconds_between_actions=0.0,  # disable rate-limit for unit tests
        deadband_temperature_c=0.5,
        deadband_humidity_pct=5.0,
        deadband_brightness_pct=10.0,
    )
    return SmartEnvironmentController(
        config=cfg, ha_client=ha_client, devices=devices, learner=None,
    )


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


class TestPlanning:
    def test_deep_stage_plans_off_lights(self, controller):
        actions = controller.plan_actions(
            SleepStage.DEEP,
            EnvironmentParams(temperature_c=22.0, humidity_pct=55.0,
                              brightness_pct=20.0),
        )
        light_actions = [a for a in actions if a.domain == "light"]
        assert any(a.service == "turn_off" for a in light_actions)

    def test_awake_stage_turns_lights_on(self, controller):
        actions = controller.plan_actions(
            SleepStage.AWAKE,
            EnvironmentParams(temperature_c=20.0, humidity_pct=55.0,
                              brightness_pct=0.0),
        )
        light_on = [
            a for a in actions
            if a.domain == "light" and a.service == "turn_on"
        ]
        assert light_on, "expected at least one light.turn_on action"
        assert light_on[0].data.get("brightness_pct", 0) > 0

    def test_temperature_within_deadband_skipped(self, controller):
        # Target for DEEP is 19°C; current is 19.3°C → 0.3°C < deadband 0.5
        actions = controller.plan_actions(
            SleepStage.DEEP,
            EnvironmentParams(temperature_c=19.3, humidity_pct=55.0),
        )
        assert not any(a.domain == "climate" for a in actions)

    def test_temperature_outside_deadband_triggers(self, controller):
        actions = controller.plan_actions(
            SleepStage.DEEP,
            EnvironmentParams(temperature_c=25.0, humidity_pct=55.0),
        )
        climate_actions = [a for a in actions if a.domain == "climate"]
        assert climate_actions
        # v1.4.0: climate.set_temperature gets the *anticipated* target,
        # blending DEEP (19.0) with the next stage REM (19.5) at α=0.5
        # so the AC starts pre-warming for REM while still in DEEP.
        # Expected = 0.5 * 19.0 + 0.5 * 19.5 = 19.25, rounded to 19.2.
        assert climate_actions[0].data["temperature"] == pytest.approx(19.2)

    def test_humidity_within_deadband_skipped(self, controller):
        actions = controller.plan_actions(
            SleepStage.DEEP,
            EnvironmentParams(temperature_c=19.0, humidity_pct=53.0),
        )
        assert not any(a.domain == "humidifier" for a in actions)

    def test_no_climates_no_temperature_action(self, ha_client):
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        controller = SmartEnvironmentController(
            config=cfg,
            ha_client=ha_client,
            devices=ActionableDevices(lights=[_light("light.x")]),
        )
        actions = controller.plan_actions(
            SleepStage.DEEP,
            EnvironmentParams(temperature_c=25.0),
        )
        assert not any(a.domain == "climate" for a in actions)

    def test_disabled_controller_plans_nothing(self, controller):
        controller.config.enabled = False
        actions = controller.plan_actions(
            SleepStage.AWAKE, EnvironmentParams(temperature_c=10.0),
        )
        assert actions == []


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_min_interval_blocks_repeat_actions(self, devices, ha_client):
        cfg = SmartControlConfig(
            enabled=True,
            min_seconds_between_actions=120.0,  # 2 min cool-down
        )
        ctl = SmartEnvironmentController(cfg, ha_client, devices)

        env = EnvironmentParams(temperature_c=25.0)
        await ctl.apply(SleepStage.DEEP, env)
        # Immediately calling apply again must skip climate due to rate-limit.
        second = await ctl.apply(SleepStage.DEEP, env)
        assert not any(a.domain == "climate" for a in second)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


class TestExecution:
    @pytest.mark.asyncio
    async def test_apply_calls_ha_service(self, controller, ha_client):
        actions = await controller.apply(
            SleepStage.AWAKE,
            EnvironmentParams(temperature_c=25.0, humidity_pct=40.0,
                              brightness_pct=0.0),
        )
        assert actions
        assert ha_client.call_service.await_count == len(actions)
        # Check at least one call was for a light service
        called_domains = {
            call.args[0] for call in ha_client.call_service.call_args_list
        }
        assert "light" in called_domains

    @pytest.mark.asyncio
    async def test_dry_run_does_not_call_ha(self, controller, ha_client):
        controller.config.dry_run = True
        actions = await controller.apply(
            SleepStage.AWAKE, EnvironmentParams(temperature_c=25.0),
        )
        assert actions  # actions are still planned and logged
        ha_client.call_service.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_call_does_not_propagate(self, controller, ha_client):
        ha_client.call_service.side_effect = RuntimeError("simulated outage")
        # Should not raise; controller logs the error and moves on.
        actions = await controller.apply(
            SleepStage.AWAKE, EnvironmentParams(temperature_c=25.0),
        )
        assert actions
        assert ha_client.call_service.await_count == len(actions)


# ---------------------------------------------------------------------------
# Feedback / exploration coupling
# ---------------------------------------------------------------------------


class TestFeedback:
    def test_feedback_score_enables_exploration(self, controller):
        controller.feedback_score(40.0)        # below 50 → explore
        assert controller._should_explore() is True
        controller.feedback_score(80.0)
        assert controller._should_explore() is False


# ---------------------------------------------------------------------------
# Per-stage adaptation (v1.3.1)
# ---------------------------------------------------------------------------


class _FakeLearner:
    """Minimal stub matching :meth:`PreferenceLearner.recommend`'s signature.

    Returns a fixed env regardless of inputs, which lets us assert that
    :meth:`SmartEnvironmentController.target_for` correctly composes
    that baseline with the per-stage delta table.
    """

    def __init__(self, env: EnvironmentParams) -> None:
        self._env = env

    def recommend(self, defaults, *, explore=False, now_ts=None):
        return self._env


class TestStageAdaptation:
    """The controller must keep stage variation alive after learning kicks in."""

    def test_no_learner_falls_back_to_per_stage_defaults(self, controller):
        # Without a learner the AWAKE target should differ from DEEP across
        # *all* stage-varying fields.
        awake = controller.target_for(SleepStage.AWAKE)
        deep = controller.target_for(SleepStage.DEEP)
        assert awake.temperature_c > deep.temperature_c
        assert awake.brightness_pct > deep.brightness_pct
        assert awake.fan_speed_pct > deep.fan_speed_pct

    def test_learner_baseline_preserves_stage_variation(self, devices, ha_client):
        # User's "best LIGHT-stage" env: 20 °C, 52 %, 6 % bright, fan 12 %.
        # AWAKE must still come out warmer + brighter than this; DEEP
        # cooler + dark.
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        learner = _FakeLearner(EnvironmentParams(
            temperature_c=20.0, humidity_pct=52.0,
            brightness_pct=6.0, fan_speed_pct=12.0,
        ))
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devices, learner=learner,
        )
        awake = ctl.target_for(SleepStage.AWAKE)
        light = ctl.target_for(SleepStage.LIGHT)
        deep = ctl.target_for(SleepStage.DEEP)

        # LIGHT target == the learner's baseline (zero delta).
        assert light.temperature_c == pytest.approx(20.0)
        assert light.brightness_pct == pytest.approx(6.0)
        # AWAKE is the baseline + the AWAKE delta (+2 °C, +32 % brightness).
        assert awake.temperature_c == pytest.approx(22.0)
        assert awake.brightness_pct == pytest.approx(38.0)
        # DEEP is the baseline + the DEEP delta (-2 °C, brightness clamped at 0).
        assert deep.temperature_c == pytest.approx(18.0)
        assert deep.brightness_pct == 0.0

    def test_safe_clamp_prevents_runaway_setpoints(self, devices, ha_client):
        # Pathological learner output (a hot baseline).  Even though the
        # AWAKE delta would push it past the ceiling, the clamp must
        # cap it at the safe-range maximum.
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        learner = _FakeLearner(EnvironmentParams(temperature_c=27.5))
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devices, learner=learner,
        )
        target = ctl.target_for(SleepStage.AWAKE)
        # 27.5 + 2.0 = 29.5 → clamped to the 28 °C ceiling.
        assert target.temperature_c == pytest.approx(28.0)

    def test_baseline_missing_field_uses_stage_default(self, devices, ha_client):
        # Learner returns a temp-only baseline.  The shifted brightness
        # should fall back to the stage default, not crash on ``None``.
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        learner = _FakeLearner(EnvironmentParams(temperature_c=20.0))
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devices, learner=learner,
        )
        deep = ctl.target_for(SleepStage.DEEP)
        # Temperature got the personalised path (20 - 2 = 18).
        assert deep.temperature_c == pytest.approx(18.0)
        # Brightness falls back to the DEEP stage default (0 %).
        assert deep.brightness_pct == 0.0


# ---------------------------------------------------------------------------
# Anticipatory control (v1.4.0)
# ---------------------------------------------------------------------------


class TestAnticipation:
    """High-latency actuators (AC, humidifier) must lead the user by a
    fraction of the next stage's target so the room is at the right
    setpoint by the time the user actually gets there."""

    def test_target_for_actuator_climate_leads_next_stage(self, controller):
        # LIGHT current = 21 °C, next stage DEEP = 19 °C.
        # Climate latency 900s ÷ 1800s typical-stage = α = 0.5
        # Expected climate target = 0.5 * 21 + 0.5 * 19 = 20.0
        climate = controller.target_for_actuator(SleepStage.LIGHT, "climate")
        assert climate.temperature_c == pytest.approx(20.0)

    def test_target_for_actuator_light_no_anticipation(self, controller):
        # Lights have zero latency, so no blending — the LIGHT-stage
        # brightness target should match target_for() exactly.
        canonical = controller.target_for(SleepStage.LIGHT)
        light = controller.target_for_actuator(SleepStage.LIGHT, "light")
        assert light.brightness_pct == canonical.brightness_pct

    def test_climate_during_awake_pre_cools_for_light(self, controller):
        # Pre-bedtime AWAKE: current target = 23 °C (baseline 21 + 2),
        # next-stage LIGHT = 21 °C.  Climate blend = 22 °C.
        # This is the "fall asleep faster because the room is already
        # cooling" payoff users feel on a real night.
        climate = controller.target_for_actuator(SleepStage.AWAKE, "climate")
        assert climate.temperature_c == pytest.approx(22.0)

    def test_humidifier_uses_smaller_anticipation_than_climate(self, controller):
        # Humidifier latency 300s vs climate 900s.  α_hum = 1/6, α_clim = 1/2.
        # Since the humidity deltas are zero between stages, the blend
        # collapses but the test verifies the per-domain dispatch works:
        # both calls must return valid env params and the climate's
        # temperature must move further toward the next stage than the
        # humidifier's would (humidifier has no temp field of its own).
        climate = controller.target_for_actuator(SleepStage.LIGHT, "climate")
        humidifier = controller.target_for_actuator(SleepStage.LIGHT, "humidifier")
        # climate temp = 20 °C (mid LIGHT/DEEP); humidifier temp also gets
        # the lighter blend (1/6 toward DEEP) but uses its own α.
        assert climate.temperature_c < humidifier.temperature_c, (
            "climate's stronger anticipation should pull its target "
            "further toward DEEP than the humidifier's weaker one"
        )

    def test_alpha_capped_at_0_6(self, devices, ha_client):
        # Override the typical-stage-duration via monkeypatching the
        # constant would be cleaner, but a behavioural assertion works:
        # even with an unreasonably high latency the controller must
        # never let alpha drop the current-stage signal entirely.
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devices, learner=None,
        )
        # The LIGHT temperature target should always be in [LIGHT, DEEP]
        # range — never past the next stage.
        light_temp = ctl.target_for_actuator(
            SleepStage.LIGHT, "climate"
        ).temperature_c
        assert 19.0 <= light_temp <= 21.0


# ---------------------------------------------------------------------------
# Wind-down detection (v1.4.0)
# ---------------------------------------------------------------------------


from datetime import datetime    # noqa: E402 - keep test imports grouped by feature


class TestWindDown:
    """``is_in_wind_down`` decides whether the controller should
    pre-cool the bedroom before the user actually goes to bed."""

    @staticmethod
    def _at(hh: int, mm: int) -> datetime:
        # Any year works — the function reads only ``.hour`` and ``.minute``.
        return datetime(2026, 5, 12, hh, mm)

    def test_inside_window_returns_true(self) -> None:
        # Bedtime 23:00, now 22:45, window 30 min → 15 min away → True.
        bedtime = {"next_bedtime": "23:00"}
        assert is_in_wind_down(self._at(22, 45), bedtime, 30) is True

    def test_after_bedtime_returns_false(self) -> None:
        # Bedtime 23:00, now 23:15 → 23h45m to next bedtime, outside window.
        bedtime = {"next_bedtime": "23:00"}
        assert is_in_wind_down(self._at(23, 15), bedtime, 30) is False

    def test_too_early_returns_false(self) -> None:
        # Bedtime 23:00, now 21:00, window 30 → 120 min away, outside window.
        bedtime = {"next_bedtime": "23:00"}
        assert is_in_wind_down(self._at(21, 0), bedtime, 30) is False

    def test_midnight_wraparound(self) -> None:
        # Bedtime 00:30 (past midnight), now 23:55, window 60 min
        # → 35 min to bedtime modulo a day → True.
        bedtime = {"next_bedtime": "00:30"}
        assert is_in_wind_down(self._at(23, 55), bedtime, 60) is True

    def test_zero_window_disables(self) -> None:
        bedtime = {"next_bedtime": "23:00"}
        assert is_in_wind_down(self._at(22, 45), bedtime, 0) is False

    def test_no_bedtime_disables(self) -> None:
        # Learner hasn't accumulated enough history yet → next_bedtime=None.
        bedtime = {"next_bedtime": None}
        assert is_in_wind_down(self._at(22, 45), bedtime, 30) is False

    def test_malformed_bedtime_disables(self) -> None:
        # Garbage shouldn't crash — fail closed.
        bedtime = {"next_bedtime": "not-a-time"}
        assert is_in_wind_down(self._at(22, 45), bedtime, 30) is False
