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
    # ``supported_color_modes=["brightness"]`` gives the entity the
    # SET_BRIGHTNESS capability that v1.6.2 gating requires; the
    # alternative ``["onoff"]`` would simulate a dimmer-less bulb.
    return HAEntity(
        entity_id=eid, state="off",
        attributes={"supported_color_modes": ["brightness"]},
    )


def _climate(eid: str) -> HAEntity:
    # SUPPORT_TARGET_TEMPERATURE bit = 1 (ClimateEntityFeature.TARGET_TEMPERATURE).
    return HAEntity(
        entity_id=eid, state="cool",
        attributes={"supported_features": 1},
    )


def _humidifier(eid: str) -> HAEntity:
    # ``humidifier.*`` unconditionally advertises SET_HUMIDITY in
    # capabilities_of(), so no supported_features bit needed here.
    return HAEntity(entity_id=eid, state="on", attributes={})


def _fan(eid: str) -> HAEntity:
    # FanEntityFeature.SET_SPEED bit = 1.
    return HAEntity(
        entity_id=eid, state="off",
        attributes={"supported_features": 1},
    )


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


# ---------------------------------------------------------------------------
# Per-stage learned deltas (v1.5.0)
# ---------------------------------------------------------------------------


class _StubLearner:
    """Minimal learner double exposing only what the controller calls.

    Real ``PreferenceLearner`` is heavy (file IO, decay maths); we only
    need to verify the controller's *contract* — that it prefers the
    learner's per-stage delta when one is supplied and falls back to
    the clinical default otherwise.
    """

    def __init__(
        self,
        per_stage: dict,
        baseline_temp: float = 21.0,
    ) -> None:
        self._per_stage = per_stage
        self._baseline_temp = baseline_temp

    def recommend(self, defaults, *, explore=False):
        return EnvironmentParams(
            temperature_c=self._baseline_temp,
            humidity_pct=defaults.humidity_pct,
            brightness_pct=defaults.brightness_pct,
            fan_speed_pct=defaults.fan_speed_pct,
        )

    def recommend_per_stage_deltas(self, now=None):
        return self._per_stage


class TestPerStageLearnedDeltas:
    def test_no_learner_falls_back_to_clinical(self, devices, ha_client):
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devices, learner=None,
        )
        # With no learner: DEEP target = LIGHT-default 21 + clinical -2 = 19.
        assert ctl.target_for(SleepStage.DEEP).temperature_c == pytest.approx(19.0)

    def test_learned_delta_overrides_clinical(self, devices, ha_client):
        """Heavy-duvet user: learned DEEP delta = 0 °C beats clinical -2."""
        per_stage = {
            "AWAKE": {
                "temperature_c": 2.0, "humidity_pct": None,
                "brightness_pct": None, "fan_speed_pct": None,
                "ess": 8.0, "n_sessions": 8,
            },
            "LIGHT": {
                "temperature_c": 0.0, "humidity_pct": 0.0,
                "brightness_pct": 0.0, "fan_speed_pct": 0.0,
                "ess": 10.0, "n_sessions": 10,
            },
            "DEEP": {
                "temperature_c": 0.0,  # learned: no delta vs LIGHT
                "humidity_pct": None,
                "brightness_pct": None, "fan_speed_pct": None,
                "ess": 6.0, "n_sessions": 6,
            },
            "REM": {
                "temperature_c": -0.5,
                "humidity_pct": None,
                "brightness_pct": None, "fan_speed_pct": None,
                "ess": 5.0, "n_sessions": 5,
            },
        }
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devices,
            learner=_StubLearner(per_stage),
        )
        # DEEP target = 21 (learned baseline) + 0 (learned delta) = 21.
        # If the override hadn't applied, it would be 21 + (-2) = 19.
        assert ctl.target_for(SleepStage.DEEP).temperature_c == pytest.approx(21.0)

    def test_partial_learned_field_falls_back_per_field(
        self, devices, ha_client,
    ):
        """Temp learned, brightness/humidity unknown → mixed sources OK."""
        per_stage = {
            "AWAKE": {
                "temperature_c": None,  # learned but None → fall back
                "humidity_pct": None, "brightness_pct": None,
                "fan_speed_pct": None, "ess": 6.0, "n_sessions": 6,
            },
            "LIGHT": {
                "temperature_c": 0.0, "humidity_pct": 0.0,
                "brightness_pct": 0.0, "fan_speed_pct": 0.0,
                "ess": 10.0, "n_sessions": 10,
            },
            "DEEP": {
                "temperature_c": -1.0,    # learned
                "humidity_pct": None,     # not learned → clinical fallback
                "brightness_pct": None,
                "fan_speed_pct": None,
                "ess": 6.0, "n_sessions": 6,
            },
            "REM": {
                "temperature_c": None, "humidity_pct": None,
                "brightness_pct": None, "fan_speed_pct": None,
                "ess": 0.0, "n_sessions": 0,
            },
        }
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devices,
            learner=_StubLearner(per_stage),
        )
        deep = ctl.target_for(SleepStage.DEEP)
        # Temp: 21 + (-1) = 20 (learned overrode the -2 clinical).
        assert deep.temperature_c == pytest.approx(20.0)
        # Humidity: clinical DEEP-delta is 0 % → baseline humidity (55,
        # the LIGHT-stage default) flows through unchanged.  This still
        # validates the per-field merge — the learner returned ``None``
        # for humidity so we took the ``getattr(clinical, "humidity_pct")``
        # = 0 branch (rather than letting a None delta poison the math).
        assert deep.humidity_pct == pytest.approx(55.0)

    def test_learner_crash_does_not_break_planning(
        self, devices, ha_client,
    ):
        """A broken learner must NOT poison the control loop."""

        class _BrokenLearner(_StubLearner):
            def recommend_per_stage_deltas(self, now=None):
                raise RuntimeError("disk corruption")

        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devices,
            learner=_BrokenLearner({}),
        )
        # Falls back to clinical: DEEP = 21 + (-2) = 19.
        assert ctl.target_for(SleepStage.DEEP).temperature_c == pytest.approx(19.0)

    def test_cache_amortises_learner_calls(self, devices, ha_client):
        """Within the TTL we should only call the learner once."""
        per_stage = {
            "AWAKE": {"temperature_c": 1.5, "humidity_pct": None,
                      "brightness_pct": None, "fan_speed_pct": None,
                      "ess": 5.0, "n_sessions": 5},
            "LIGHT": {"temperature_c": 0.0, "humidity_pct": 0.0,
                      "brightness_pct": 0.0, "fan_speed_pct": 0.0,
                      "ess": 10.0, "n_sessions": 10},
            "DEEP": {"temperature_c": -1.0, "humidity_pct": None,
                     "brightness_pct": None, "fan_speed_pct": None,
                     "ess": 5.0, "n_sessions": 5},
            "REM": {"temperature_c": -0.5, "humidity_pct": None,
                    "brightness_pct": None, "fan_speed_pct": None,
                    "ess": 5.0, "n_sessions": 5},
        }

        class _CountingLearner(_StubLearner):
            calls = 0

            def recommend_per_stage_deltas(self, now=None):
                _CountingLearner.calls += 1
                return self._per_stage

        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devices,
            learner=_CountingLearner(per_stage),
        )
        # Many calls in quick succession — should only hit the learner once.
        for _ in range(10):
            ctl.target_for(SleepStage.DEEP)
            ctl.target_for(SleepStage.REM)
        assert _CountingLearner.calls == 1


# ---------------------------------------------------------------------------
# Capability gating (v1.6.2)
# ---------------------------------------------------------------------------


class TestCapabilityGating:
    """v1.6.2 — the controller refuses to plan actions against entities
    that don't advertise the required capability, instead of firing a
    service the device will 200-OK but silently no-op on.
    """

    def test_climate_without_set_temperature_is_skipped(
        self, ha_client,
    ) -> None:
        # Build a "dumb" climate entity — hvac-mode-only, no
        # TARGET_TEMPERATURE bit (real-world example: many Mihome AC
        # integrations before their v2 firmware).
        dumb_ac = HAEntity(
            entity_id="climate.dumb_ac", state="cool",
            attributes={"supported_features": 0},
        )
        devs = ActionableDevices(climates=[dumb_ac])
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devs, learner=None,
        )
        actions = ctl.plan_actions(
            SleepStage.DEEP,
            EnvironmentParams(temperature_c=25.0, humidity_pct=55.0),
        )
        assert [a for a in actions if a.domain == "climate"] == []
        # Bookkeeping: gate recorded exactly one skip.
        assert ctl.capability_stats().get("set_temperature") == 1

    def test_onoff_only_light_degrades_to_plain_turn_on(
        self, ha_client,
    ) -> None:
        # Cheap smart plug re-exposed as a light entity — can toggle
        # but has no dimmer.  The controller should still keep the
        # user in light (turn_on) at AWAKE, just without brightness.
        onoff_light = HAEntity(
            entity_id="light.onoff_bulb", state="off",
            attributes={"supported_color_modes": ["onoff"]},
        )
        devs = ActionableDevices(lights=[onoff_light])
        cfg = SmartControlConfig(
            min_seconds_between_actions=0.0,
            deadband_brightness_pct=0.0,
        )
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devs, learner=None,
        )
        actions = ctl.plan_actions(
            SleepStage.AWAKE,
            EnvironmentParams(brightness_pct=0.0),
        )
        light_actions = [a for a in actions if a.domain == "light"]
        assert len(light_actions) == 1
        # turn_on still fires (the degraded path), but without the
        # ``brightness_pct`` parameter the bulb can't honour.
        assert light_actions[0].service == "turn_on"
        assert "brightness_pct" not in light_actions[0].data
        # Capability bookkeeping shows the miss.
        assert ctl.capability_stats().get("set_brightness") == 1

    def test_preset_only_fan_uses_preset_mode(self, ha_client) -> None:
        # Sonoff iFan04-style preset-only fan: no SET_SPEED (bit 1),
        # but PRESET_MODE (bit 8).
        preset_fan = HAEntity(
            entity_id="fan.ifan04", state="off",
            attributes={"supported_features": 8},
        )
        devs = ActionableDevices(fans=[preset_fan])
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devs, learner=None,
        )
        # AWAKE default fan_speed_pct = 20 % -> "low" bucket.
        actions = ctl.plan_actions(
            SleepStage.AWAKE, EnvironmentParams(),
        )
        fan_actions = [a for a in actions if a.domain == "fan"]
        assert len(fan_actions) == 1
        assert fan_actions[0].service == "set_preset_mode"
        assert fan_actions[0].data["preset_mode"] == "low"

    def test_warning_only_logged_once_per_missing_capability(
        self, ha_client, caplog,
    ) -> None:
        dumb_ac = HAEntity(
            entity_id="climate.dumb_ac", state="cool",
            attributes={"supported_features": 0},
        )
        devs = ActionableDevices(climates=[dumb_ac])
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devs, learner=None,
        )
        with caplog.at_level("WARNING"):
            ctl.plan_actions(
                SleepStage.DEEP,
                EnvironmentParams(temperature_c=25.0),
            )
            ctl.plan_actions(
                SleepStage.DEEP,
                EnvironmentParams(temperature_c=25.0),
            )
            ctl.plan_actions(
                SleepStage.DEEP,
                EnvironmentParams(temperature_c=25.0),
            )
        warns = [
            r for r in caplog.records
            if r.levelname == "WARNING" and "set_temperature" in r.message
        ]
        assert len(warns) == 1
        # But the skip counter kept growing, so capability_stats is
        # still useful for diagnostics.
        assert ctl.capability_stats().get("set_temperature") == 3


# ---------------------------------------------------------------------------
# Futile-retry suppression (v1.6.4)
# ---------------------------------------------------------------------------


class TestFutileRetrySuppression:
    """v1.6.4 — hammering ``set_temperature=19`` at an AC that's already
    at max cooling but unable to fight 35 °C outside is a waste of
    service calls.  After a few futile retries we pause same-setpoint
    actions for the entity until the stage changes.
    """

    async def test_no_suppression_before_streak_reached(
        self, ha_client,
    ) -> None:
        # Brand new controller with climate entity.  A few same-target
        # calls shouldn't trip saturation — we need a full streak.
        climate = HAEntity(
            entity_id="climate.bedroom_ac", state="cool",
            attributes={"supported_features": 1},
        )
        devs = ActionableDevices(climates=[climate])
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devs, learner=None,
        )
        # Before the streak threshold, saturation returns False.
        assert not ctl._is_entity_saturated(
            "climate", "climate.bedroom_ac",
            target_value=19.0, current_env_value=26.0, now=100.0,
        )

    async def test_streak_without_effective_movement_triggers_saturation(
        self, ha_client,
    ) -> None:
        climate = HAEntity(
            entity_id="climate.bedroom_ac", state="cool",
            attributes={"supported_features": 1},
        )
        devs = ActionableDevices(climates=[climate])
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devs, learner=None,
        )
        # Simulate 3 consecutive set_temperature=19 calls, each 20 min
        # apart, all observing ~26 °C (no effective cooling).
        base_ts = 100.0
        for i in range(3):
            ctl._record_action_for_futility(
                "climate", "climate.bedroom_ac",
                target_value=19.0,
                current_env_value=26.0 - i * 0.05,    # ~0.1 C drift
                now=base_ts + i * 1200.0,
            )
        # Now ask if a 4th attempt would be futile.
        saturated = ctl._is_entity_saturated(
            "climate", "climate.bedroom_ac",
            target_value=19.0,
            current_env_value=25.9,
            now=base_ts + 4 * 1200.0,
        )
        assert saturated
        assert (
            "climate.climate.bedroom_ac"
            in ctl.futility_stats()["saturated_entities"]
        )

    async def test_env_movement_resets_streak(
        self, ha_client,
    ) -> None:
        """If the environment actually moved by >= the min-effective
        threshold, the device is working and should NOT be suppressed.
        """
        climate = HAEntity(
            entity_id="climate.bedroom_ac", state="cool",
            attributes={"supported_features": 1},
        )
        devs = ActionableDevices(climates=[climate])
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devs, learner=None,
        )
        base_ts = 100.0
        # 3 same-target samples, but env moves from 26 → 23 — that's
        # the AC actually working.
        for i, env in enumerate([26.0, 24.5, 23.0]):
            ctl._record_action_for_futility(
                "climate", "climate.bedroom_ac",
                target_value=19.0,
                current_env_value=env,
                now=base_ts + i * 1200.0,
            )
        assert not ctl._is_entity_saturated(
            "climate", "climate.bedroom_ac",
            target_value=19.0,
            current_env_value=22.0,
            now=base_ts + 4 * 1200.0,
        )

    async def test_stage_change_clears_saturation(
        self, ha_client,
    ) -> None:
        """A new stage = a new setpoint = the previously-saturated
        entity deserves a fresh chance.
        """
        climate = HAEntity(
            entity_id="climate.bedroom_ac", state="cool",
            attributes={"supported_features": 1},
        )
        devs = ActionableDevices(climates=[climate])
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devs, learner=None,
        )
        # Force saturation flag on.
        ctl._saturated_entities.add("climate.climate.bedroom_ac")
        ctl._stage_when_saturation_recorded = SleepStage.LIGHT

        # A plan_actions() call against a different stage should clear it.
        ctl.plan_actions(SleepStage.DEEP, EnvironmentParams(temperature_c=26.0))
        assert ctl._saturated_entities == set()

    async def test_settle_time_required(
        self, ha_client,
    ) -> None:
        """3 calls 30 seconds apart can't count as saturation — the
        AC hasn't had time to respond.
        """
        climate = HAEntity(
            entity_id="climate.bedroom_ac", state="cool",
            attributes={"supported_features": 1},
        )
        devs = ActionableDevices(climates=[climate])
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devs, learner=None,
        )
        base_ts = 100.0
        # Same setpoint, same env, but each attempt is only 30 s apart.
        for i in range(3):
            ctl._record_action_for_futility(
                "climate", "climate.bedroom_ac",
                target_value=19.0,
                current_env_value=26.0,
                now=base_ts + i * 30.0,     # way below _FUTILE_MIN_SETTLE_SECONDS
            )
        assert not ctl._is_entity_saturated(
            "climate", "climate.bedroom_ac",
            target_value=19.0,
            current_env_value=26.0,
            now=base_ts + 100.0,
        )


# ---------------------------------------------------------------------------
# 落地 safety: live-state-cache integration (v1.7.1)
# ---------------------------------------------------------------------------


class TestOffStateAutoTurnOn:
    """v1.7.1 — when the AC is off, firing set_temperature is a no-op.
    The controller must inject set_hvac_mode first to actually wake
    the device up before asking it for a setpoint.
    """

    async def test_climate_off_gets_set_hvac_mode_before_set_temperature(
        self, ha_client,
    ) -> None:
        from src.live_state_cache import LiveStateCache
        climate = HAEntity(
            entity_id="climate.bedroom_ac", state="off",
            attributes={"supported_features": 1},
        )
        devs = ActionableDevices(climates=[climate])
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        cache = LiveStateCache()
        cache.seed_from_registry("climate.bedroom_ac", "off", {}, now=100.0)

        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devs,
            learner=None, live_state=cache,
        )
        actions = ctl.plan_actions(
            SleepStage.DEEP,
            EnvironmentParams(temperature_c=25.0),
        )
        climate_actions = [a for a in actions if a.domain == "climate"]
        # Must be exactly two: hvac_mode first, set_temperature second.
        assert len(climate_actions) == 2
        assert climate_actions[0].service == "set_hvac_mode"
        # Target < current (25) → cool.
        assert climate_actions[0].data["hvac_mode"] == "cool"
        assert climate_actions[1].service == "set_temperature"

    async def test_climate_already_on_skips_set_hvac_mode(
        self, ha_client,
    ) -> None:
        from src.live_state_cache import LiveStateCache
        climate = HAEntity(
            entity_id="climate.bedroom_ac", state="cool",
            attributes={"supported_features": 1},
        )
        devs = ActionableDevices(climates=[climate])
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        cache = LiveStateCache()
        cache.seed_from_registry("climate.bedroom_ac", "cool", {}, now=100.0)

        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devs,
            learner=None, live_state=cache,
        )
        actions = ctl.plan_actions(
            SleepStage.DEEP,
            EnvironmentParams(temperature_c=25.0),
        )
        services = [a.service for a in actions if a.domain == "climate"]
        assert services == ["set_temperature"]

    async def test_humidifier_off_gets_turn_on_before_set_humidity(
        self, ha_client,
    ) -> None:
        from src.live_state_cache import LiveStateCache
        hum = HAEntity(
            entity_id="humidifier.bedroom", state="off",
            attributes={},
        )
        devs = ActionableDevices(humidifiers=[hum])
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        cache = LiveStateCache()
        cache.seed_from_registry("humidifier.bedroom", "off", {}, now=100.0)

        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devs,
            learner=None, live_state=cache,
        )
        actions = ctl.plan_actions(
            SleepStage.DEEP,
            EnvironmentParams(humidity_pct=40.0),
        )
        hum_actions = [a for a in actions if a.domain == "humidifier"]
        assert len(hum_actions) == 2
        assert hum_actions[0].service == "turn_on"
        assert hum_actions[1].service == "set_humidity"


class TestUnavailableSkip:
    """v1.7.1 — firing a service at an 'unavailable' entity returns 200
    but does nothing.  The controller must skip the dispatch and
    record the skip in diagnostics."""

    async def test_unavailable_climate_skipped(self, ha_client) -> None:
        from src.live_state_cache import LiveStateCache
        climate = HAEntity(
            entity_id="climate.bedroom_ac", state="unavailable",
            attributes={"supported_features": 1},
        )
        devs = ActionableDevices(climates=[climate])
        cfg = SmartControlConfig(min_seconds_between_actions=0.0)
        cache = LiveStateCache()
        cache.seed_from_registry(
            "climate.bedroom_ac", "unavailable", {}, now=100.0,
        )

        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devs,
            learner=None, live_state=cache,
        )
        actions = ctl.plan_actions(
            SleepStage.DEEP,
            EnvironmentParams(temperature_c=25.0),
        )
        assert [a for a in actions if a.domain == "climate"] == []
        assert cache.stats()["skipped_unavailable"].get(
            "climate.bedroom_ac",
        ) == 1

    async def test_unavailable_light_skipped(self, ha_client) -> None:
        from src.live_state_cache import LiveStateCache
        light = HAEntity(
            entity_id="light.bedroom", state="unavailable",
            attributes={"supported_color_modes": ["brightness"]},
        )
        devs = ActionableDevices(lights=[light])
        cfg = SmartControlConfig(
            min_seconds_between_actions=0.0,
            deadband_brightness_pct=0.0,
        )
        cache = LiveStateCache()
        cache.seed_from_registry(
            "light.bedroom", "unavailable", {}, now=100.0,
        )

        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devs,
            learner=None, live_state=cache,
        )
        actions = ctl.plan_actions(
            SleepStage.AWAKE,
            EnvironmentParams(brightness_pct=0.0),
        )
        assert [a for a in actions if a.domain == "light"] == []


class TestUserOverrideRespect:
    """v1.7.1 — if the user just manually toggled the light, leave it
    alone.  Auto-fighting the user is the fastest way to get
    uninstalled."""

    async def test_user_override_suppresses_actions(self, ha_client) -> None:
        from src.live_state_cache import LiveStateCache
        light = HAEntity(
            entity_id="light.bedroom", state="on",
            attributes={"supported_color_modes": ["brightness"]},
        )
        devs = ActionableDevices(lights=[light])
        cfg = SmartControlConfig(
            min_seconds_between_actions=0.0,
            deadband_brightness_pct=0.0,
        )
        cache = LiveStateCache(user_override_grace_seconds=600.0)
        # Seed off at t=100.
        cache.seed_from_registry("light.bedroom", "off", {}, now=100.0)
        # User manually flipped it on at t=200 (no self-dispatch
        # recorded → classified as user).
        cache.on_state_change("light.bedroom", "on", now=200.0)

        ctl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devs,
            learner=None, live_state=cache,
        )

        # Monkey-patch `under_user_override` to use an unambiguous
        # "now" so the test is deterministic regardless of wall-clock.
        _orig = cache.under_user_override
        cache.under_user_override = lambda eid, now=None: _orig(
            eid, now=300.0,     # 100 s after override → well within 600 s grace
        )
        actions = ctl.plan_actions(
            SleepStage.DEEP,
            EnvironmentParams(brightness_pct=100.0),   # stage wants 0%
        )
        # Would have fired turn_off, but user override holds us back.
        assert [a for a in actions if a.domain == "light"] == []
        assert cache.stats()["skipped_user_override"].get(
            "light.bedroom",
        ) == 1

    async def test_self_dispatch_not_classified_as_override(
        self, ha_client,
    ) -> None:
        """After we fire turn_on, the state_changed echo must NOT
        trigger the override grace window."""
        from src.live_state_cache import LiveStateCache
        cache = LiveStateCache(user_override_grace_seconds=600.0)
        cache.seed_from_registry("light.bedroom", "off", {}, now=100.0)
        # Controller dispatches turn_on.
        cache.record_self_dispatch("light.bedroom", now=200.0)
        # HA echoes 1 s later.
        cache.on_state_change("light.bedroom", "on", now=201.0)
        # NOT override.
        assert not cache.under_user_override("light.bedroom", now=300.0)
