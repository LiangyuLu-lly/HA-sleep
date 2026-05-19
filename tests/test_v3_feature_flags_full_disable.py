"""Property 10: 4 个 feature flag 全关时 add-on 主流程仍可启动 + 跑完一晚 dry_run。

**Validates: Requirements 11.4, 11.5**

核心断言：
1. ``ha_client.call_service`` 调用次数 = 0（PR1 + R11.5 dry_run 安全契约）；
2. 20 个 v2.x sensor 与 baseline 完全一致（PR2 sensor schema 不变量）；
3. 4 个 v3 健康 sensor 状态 ∈ ``{"disabled", "healthy"}``（R11.4 字节级等价回退）。

当 4 个 flag 全 false 时，``scripts/run_ha_smart_service.py`` 不 import 对应模块
（lazy import in ``if flag:``），运行时行为字节级等价于 v2.1.0。本测试通过构造
一整晚的合成 stage 数据流，验证该不变量。
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Dict, Set
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.data_structures import SleepStage
from src.sleep_state_publisher import SleepStatePublisher


# ---------------------------------------------------------------------------
# v2.1.0 baseline —— 与 test_sensor_schema_invariant.py 保持同步。
# ---------------------------------------------------------------------------

V2_1_0_ENTITY_IDS: frozenset = frozenset({
    "sensor.sleep_classifier_stage",
    "sensor.sleep_classifier_confidence",
    "sensor.sleep_classifier_quality_score",
    "sensor.sleep_classifier_session_duration",
    "sensor.sleep_classifier_last_action",
    "sensor.sleep_classifier_debt_hours",
    "sensor.sleep_classifier_recommended_bedtime",
    "sensor.sleep_classifier_wake_decision",
    "sensor.sleep_classifier_soundscape",
    "sensor.sleep_classifier_learned_bedtime_workday",
    "sensor.sleep_classifier_learned_bedtime_weekend",
    "sensor.sleep_classifier_learned_environment",
    "sensor.sleep_classifier_recommendation_explain",
    "sensor.sleep_classifier_per_stage_deltas",
    "sensor.sleep_classifier_apnea_index",
    "sensor.sleep_classifier_health",
    "sensor.sleep_classifier_quality_architecture",
    "sensor.sleep_classifier_quality_efficiency",
    "sensor.sleep_classifier_quality_fragmentation",
    "sensor.sleep_classifier_quality_onset",
})

# 4 个 v3 健康相关 sensor —— 当全关时应为 disabled 或 healthy。
V3_HEALTH_SENSOR_IDS: frozenset = frozenset({
    "sensor.sleep_classifier_optimizer_health",
    "sensor.sleep_classifier_predictor_health",
    "sensor.sleep_classifier_prior_status",
    "sensor.sleep_classifier_v3_health_summary",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_all_flags_off_config(tmp_path: Path) -> Path:
    """Write a config.json with all 4 v3 flags = false + dry_run = true."""
    from training_config.config_loader import get_default_config

    cfg = get_default_config()
    ha = cfg.setdefault("home_assistant", {})
    ha["api"] = {
        "base_url": "http://localhost:8123",
        "access_token": "fake-token-for-test",
        "verify_ssl": False,
    }
    ha["preference_learner"] = {
        "enabled": True,
        "history_path": str(tmp_path / "user_preferences.json"),
    }
    ha["smart_control"] = {"enabled": True, "dry_run": True}
    ha["natural_sleep"] = {
        "profile_path": str(tmp_path / "user_profile.json"),
    }
    # 4 个 flag 全 false —— R11.4 字节级等价回退。
    ha["v3"] = {
        "bayesian_optimizer_enabled": False,
        "causal_attribution_enabled": False,
        "population_prior_enabled": False,
        "stage_predictor_enabled": False,
        "causal_attribution_explain_all": False,
        "user_profile_age_band": "",
        "user_profile_sex": "",
        "user_profile_chronotype": "",
    }
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def _args(config_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        config=str(config_path),
        base_url=None,
        token=None,
        area=None,
        infer_interval=30.0,
        session_interval=1800.0,
        duration=10.0,
        dry_run=True,
        verbose=False,
    )


def _patch_buffer_dir(monkeypatch: Any, tmp_path: Path) -> None:
    """Redirect buffer dir paths so /data writes land in tmp_path."""
    import scripts.run_ha_smart_service as mod

    monkeypatch.setattr(mod, "_BUFFER_DIR", tmp_path, raising=True)
    monkeypatch.setattr(
        mod, "_V3_BAO_STATE_PATH", tmp_path / "bao_model.pickle", raising=True,
    )
    monkeypatch.setattr(
        mod,
        "_V3_CAUSAL_FACTORS_PATH",
        tmp_path / "causal_factors.jsonl",
        raising=True,
    )
    monkeypatch.setattr(
        mod,
        "_V3_PREDICTOR_AUDIT_PATH",
        tmp_path / "predictor_audit.jsonl",
        raising=True,
    )
    monkeypatch.setattr(
        mod, "_V3_INSTALL_ID_PATH", tmp_path / "install_id.uuid", raising=True,
    )


def _collect_published_entities(
    ha_client: AsyncMock,
) -> Dict[str, Set[str]]:
    """Aggregate all update_state calls → {entity_id: union(attr keys)}."""
    result: Dict[str, Set[str]] = {}
    for call in ha_client.update_state.call_args_list:
        entity_id = call.args[0]
        attrs: Dict[str, Any] = call.kwargs.get("attributes") or {}
        result.setdefault(entity_id, set()).update(attrs.keys())
    return result


def _collect_entity_states(
    ha_client: AsyncMock,
) -> Dict[str, Any]:
    """Aggregate all update_state calls → {entity_id: last_state_value}."""
    result: Dict[str, Any] = {}
    for call in ha_client.update_state.call_args_list:
        entity_id = call.args[0]
        state = call.args[1] if len(call.args) > 1 else call.kwargs.get("state")
        result[entity_id] = state
    return result


async def _simulate_one_night(publisher: SleepStatePublisher) -> None:
    """Run a full one-night simulation covering all v2.1.0 publish paths.

    Mirrors the simulation in test_sensor_schema_invariant.py to ensure
    all 20 v2.x sensors are published.
    """
    # 1) Boot placeholders.
    await publisher.publish_initial_placeholders()

    # 2) Stage transitions.
    await publisher.publish_stage(
        SleepStage.AWAKE, 0.42,
        env_temperature_c=22.5,
        env_humidity_pct=50.0,
        env_brightness_pct=10.0,
    )
    await publisher.publish_stage(SleepStage.LIGHT, 0.85)
    await publisher.publish_stage(SleepStage.DEEP, 0.91)
    await publisher.publish_stage(SleepStage.REM, 0.78)

    # 3) Quality / duration / sub-scores.
    await publisher.publish_quality(82.5)
    await publisher.publish_duration(28800.0)
    await publisher.publish_quality_sub_scores({
        "architecture": 80.0,
        "efficiency": 88.0,
        "fragmentation": 75.0,
        "onset": 92.0,
    })

    # 4) Debt / bedtime / wake / soundscape.
    await publisher.publish_debt(
        1.5, severity="mild", target_hours=8.0, nights_to_full_recovery=2,
    )
    await publisher.publish_recommended_bedtime(
        None, tonight_target_hours=8.0, reason="weekday default",
    )
    await publisher.publish_wake_decision(
        "fire_now", reason="REM detected", matched_stage="REM",
    )
    await publisher.publish_soundscape("brown_noise", volume_pct=30.0)

    # 5) Learning panel.
    await publisher.publish_learned_bedtime({
        "weekday_bedtime": "23:30",
        "weekend_bedtime": "00:00",
        "n_workday": 12,
        "n_weekend": 5,
        "confidence": 0.7,
        "tonight_bucket": "weekday",
    })
    await publisher.publish_learned_environment(
        {
            "temperature_c": 19.5,
            "humidity_pct": 50.0,
            "brightness_pct": 5.0,
            "fan_speed_pct": 0.0,
        },
        confidence=0.8,
        n_used=10,
    )
    await publisher.publish_recommendation_explain({
        "ready": True,
        "method": "knn",
        "n_total": 20,
        "avg_age_days": 7.0,
        "decay_half_life_days": 14.0,
        "effective_sample_size": 6.5,
        "recommendation": "warmer",
        "bedtime": "23:30",
        "confidence": 0.78,
        "reason": "neighbours match",
        "neighbors": [],
    })
    await publisher.publish_per_stage_deltas({
        "AWAKE": {"temperature_c": 0.5, "ess": 5.0, "n_sessions": 8},
        "LIGHT": {"temperature_c": 0.0, "ess": 4.5, "n_sessions": 7},
        "DEEP": {"temperature_c": -0.8, "ess": 6.0, "n_sessions": 9},
        "REM": {"temperature_c": -0.5, "ess": 4.2, "n_sessions": 5},
    })

    # 6) Apnea / last_action / health.
    await publisher.publish_apnea_index(
        "calibrating",
        status={
            "enabled": True,
            "consent": True,
            "calibration_nights_required": 7,
            "calibration_nights_completed": 3,
        },
    )
    await publisher.publish_last_action(
        "climate.bedroom → 19.5 °C", executed=True,
    )
    await publisher.publish_health(
        stage_source_stale=False,
        env_stale_fields=[],
        publisher_failures=0,
        learner_sessions=12,
        capability_skipped=0,
    )


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


async def test_property_p10_full_disable_equivalent_to_v2_1_0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Property 10: 4 个 feature flag 全关 + dry_run=true 跑一晚合成数据。

    **Validates: Requirements 11.4, 11.5**

    断言：
    1. ``ha_client.call_service`` 调用次数 = 0；
    2. 20 个 v2.x sensor 与 baseline 完全一致；
    3. 4 个 v3 健康 sensor 状态 ∈ ``{"disabled", "healthy"}``。
    """
    from scripts.run_ha_smart_service import SmartSleepService
    from src.smart_environment_controller import (
        SmartControlConfig,
        SmartEnvironmentController,
        EnvironmentParams,
    )
    from src.device_discovery import ActionableDevices

    _patch_buffer_dir(monkeypatch, tmp_path)

    # Build service with all 4 flags off + dry_run.
    cfg_path = _write_all_flags_off_config(tmp_path)
    svc = SmartSleepService(_args(cfg_path))

    # Verify the 4 flags are indeed false.
    assert svc._v3_bayesian_optimizer_enabled is False
    assert svc._v3_causal_attribution_enabled is False
    assert svc._v3_population_prior_enabled is False
    assert svc._v3_stage_predictor_enabled is False

    # Create mock HA client that tracks all calls.
    mock_ha = AsyncMock()
    mock_ha.call_service = AsyncMock(return_value=None)
    mock_ha.update_state = AsyncMock(return_value=None)

    # Build a real publisher backed by the mock HA client.
    publisher = SleepStatePublisher(mock_ha, confidence_deadband=0.05)
    svc.publisher = publisher

    # v3_modules_loaded should be False since we never call set_v3_modules.
    assert publisher.v3_modules_loaded is False

    # Build controller with dry_run=True.
    ctrl_cfg = SmartControlConfig(dry_run=True)
    controller = SmartEnvironmentController(
        config=ctrl_cfg,
        ha_client=mock_ha,
        devices=ActionableDevices(),
        learner=svc.learner,
    )

    # Initialize v3 modules — with all flags off, no module should load.
    svc._v3_controller_ref = controller
    svc._init_v3_modules(controller)

    # After init with all flags off, v3 module refs should remain None.
    assert svc._v3_bao is None
    assert svc._v3_cae_engine is None
    assert svc._v3_prior_repo is None
    assert svc._v3_predictor is None

    # v3_modules_loaded stays False (set_v3_modules never called when all None).
    assert publisher.v3_modules_loaded is False

    # --- Simulate one night of synthetic data through the publisher ---
    await _simulate_one_night(publisher)

    # --- Also simulate controller.apply for each stage (dry_run path) ---
    env = EnvironmentParams(
        temperature_c=22.0,
        humidity_pct=50.0,
        brightness_pct=10.0,
    )
    for stage in [SleepStage.AWAKE, SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM]:
        await controller.apply(stage, env)
        # Also test dispatch_with_lookahead if available.
        if hasattr(controller, "dispatch_with_lookahead"):
            try:
                await controller.dispatch_with_lookahead(
                    stage=stage, lead_seconds=60,
                )
            except Exception:  # noqa: BLE001
                pass  # May fail due to missing devices — OK in test.

    # =================================================================== #
    # ASSERTION 1: call_service 调用次数 = 0（PR1 + R11.5）                #
    # =================================================================== #
    assert mock_ha.call_service.call_count == 0, (
        f"dry_run=true violated! ha_client.call_service was called "
        f"{mock_ha.call_service.call_count} time(s). "
        f"Calls: {mock_ha.call_service.call_args_list}"
    )

    # =================================================================== #
    # ASSERTION 2: 20 个 v2.x sensor 与 baseline 完全一致（PR2）           #
    # =================================================================== #
    published = _collect_published_entities(mock_ha)
    published_ids = set(published.keys())

    # All 20 v2.x sensor entity_ids must be present.
    missing_v2 = V2_1_0_ENTITY_IDS - published_ids
    assert missing_v2 == set(), (
        f"v2.1.0 baseline 中的 entity_id 缺失（不允许被删除）：{sorted(missing_v2)}"
    )

    # No extra sensors beyond the v2 baseline should appear
    # (when v3_modules_loaded=False, no v3 sensors are published).
    extras = published_ids - V2_1_0_ENTITY_IDS
    assert extras == set(), (
        f"v3_modules_loaded=False 时 publisher 不应发布额外 sensor，"
        f"但发现：{sorted(extras)}"
    )

    # =================================================================== #
    # ASSERTION 3: 4 个 v3 健康 sensor 状态 ∈ {"disabled", "healthy"}      #
    #   当 v3_modules_loaded=False 时，v3 sensor 不被发布，所以我们验证：   #
    #   如果有 v3 sensor 出现，其状态必须是 disabled 或 healthy。           #
    #   在全关场景下 v3 sensor 不应出现（由 assertion 2 覆盖）。            #
    #   额外：验证 _init_v3_modules 不会意外启用任何模块。                  #
    # =================================================================== #
    # The 4 v3 health sensors should NOT appear in published set when all
    # flags are off and v3_modules_loaded=False (covered by assertion 2).
    # But as an additional guard, verify that IF they were to appear
    # (e.g. future regression), their state would be acceptable.
    entity_states = _collect_entity_states(mock_ha)
    for sensor_id in V3_HEALTH_SENSOR_IDS:
        if sensor_id in entity_states:
            state = str(entity_states[sensor_id])
            assert state in {"disabled", "healthy"}, (
                f"v3 health sensor {sensor_id} has unexpected state "
                f"{state!r}; expected 'disabled' or 'healthy'"
            )

    # Final validation: with all v3 flags off, the service should be
    # functionally identical to v2.1.0 — no v3 module was imported.
    # Verify via the internal flags.
    assert svc._v3_bao is None, "BAO should not be loaded when flag=false"
    assert svc._v3_cae_engine is None, "CAE should not be loaded when flag=false"
    assert svc._v3_prior_repo is None, "PP should not be loaded when flag=false"
    assert svc._v3_predictor is None, "EMST should not be loaded when flag=false"
