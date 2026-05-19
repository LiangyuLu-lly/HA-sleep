"""Property 20 (X3): 错误计数 → 自动降级状态机端到端测试。

**Validates: Requirements 1.4, 11.3, 11.6**

验证 4 个 v3 模块（BAO / CAE / EMST / PP）各注入 3 次运行时异常后，
对应 ``*_health`` sensor = ``degraded`` 且 internal flag = False（即
模块从 ``_v3_degraded`` 集合中存在）。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.data_structures import SleepStage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_minimal_config(tmp_path: Path) -> Path:
    """Write a minimal config.json enabling all 4 v3 modules in dry_run."""
    from training_config.config_loader import get_default_config

    cfg = get_default_config()
    ha = cfg.setdefault("home_assistant", {})
    ha["api"] = {
        "base_url": "http://localhost:8123",
        "access_token": "fake-token-for-test",
        "verify_ssl": False,
    }
    ha["preference_learner"] = {"enabled": False}
    ha["smart_control"] = {"enabled": True, "dry_run": True}
    ha["natural_sleep"] = {
        "profile_path": str(tmp_path / "user_profile.json"),
    }
    ha["v3"] = {
        "bayesian_optimizer_enabled": True,
        "causal_attribution_enabled": True,
        "population_prior_enabled": True,
        "stage_predictor_enabled": True,
    }
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def _make_args(config_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        config=str(config_path),
        model="models/best_model.h5",
        base_url=None,
        token=None,
        area=None,
        infer_interval=30.0,
        session_interval=1800.0,
        duration=None,
        dry_run=True,
        verbose=False,
    )


class _FakeHAClient:
    """Minimal mock HA client for SleepStatePublisher."""

    def __init__(self) -> None:
        self.published: List[Tuple[str, Any, Dict[str, Any]]] = []

    async def update_state(
        self,
        entity_id: str,
        state: Any,
        *,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.published.append((entity_id, state, attributes or {}))


class _FakeModule:
    """Minimal module mock exposing ``error_count`` and ``should_disable``."""

    def __init__(self) -> None:
        self._error_count: int = 0

    @property
    def error_count(self) -> int:
        return self._error_count

    @property
    def should_disable(self) -> bool:
        return self._error_count >= 3

    def inject_errors(self, n: int = 3) -> None:
        self._error_count += n


class _FakeController:
    """Minimal SmartEnvironmentController mock for the health check."""

    def __init__(self) -> None:
        self._provider_error_count: int = 0
        self._setpoint_provider: Any = None

    @property
    def provider_error_count(self) -> int:
        return self._provider_error_count

    def set_setpoint_provider(self, provider: Any) -> None:
        self._setpoint_provider = provider


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


async def test_property_x3_three_strikes_disables_module(
    tmp_path: Path,
) -> None:
    """P20 (X3): 对 4 模块各注入 3 次运行时异常，断言:

    1. 对应 ``*_health`` sensor = ``degraded``
    2. 模块名存在于 ``_v3_degraded`` 集合中（internal flag = False）
    """
    from scripts.run_ha_smart_service import SmartSleepService
    from src.sleep_state_publisher import SleepStatePublisher

    # ---- 构建 service 实例 ---- #
    config_path = _write_minimal_config(tmp_path)
    svc = SmartSleepService(_make_args(config_path))

    # ---- 替换 4 个模块为可控的 fake ---- #
    fake_bao = _FakeModule()
    fake_cae = _FakeModule()
    fake_emst = _FakeModule()
    fake_pp = _FakeModule()

    svc._v3_bao = fake_bao
    svc._v3_cae_engine = fake_cae
    svc._v3_predictor = fake_emst
    svc._v3_prior_repo = fake_pp

    # 确保 _v3_degraded 集合为空（未降级状态）
    svc._v3_degraded = set()

    # ---- 构建 fake publisher 来捕获发布的 sensor ---- #
    fake_ha_client = _FakeHAClient()
    publisher = SleepStatePublisher(fake_ha_client)
    publisher.set_v3_modules(
        bao=fake_bao,
        cae_engine=fake_cae,
        prior_repo=fake_pp,
        predictor=fake_emst,
    )
    svc.publisher = publisher

    # ---- 构建 fake controller ---- #
    fake_ctrl = _FakeController()

    # ---- 注入 3 次异常到所有 4 个模块 ---- #
    fake_bao.inject_errors(3)
    fake_cae.inject_errors(3)
    fake_emst.inject_errors(3)
    fake_pp.inject_errors(3)

    # 确认 should_disable = True
    assert fake_bao.should_disable is True
    assert fake_cae.should_disable is True
    assert fake_emst.should_disable is True
    assert fake_pp.should_disable is True

    # ---- 调用 health check 方法 ---- #
    await svc._v3_check_health_and_degrade(fake_ctrl)

    # ---- 断言 1: 所有 4 个模块被标记为 degraded ---- #
    assert "bao" in svc._v3_degraded, (
        "BAO should be in _v3_degraded set after 3 errors"
    )
    assert "cae" in svc._v3_degraded, (
        "CAE should be in _v3_degraded set after 3 errors"
    )
    assert "emst" in svc._v3_degraded, (
        "EMST should be in _v3_degraded set after 3 errors"
    )
    assert "pp" in svc._v3_degraded, (
        "PP should be in _v3_degraded set after 3 errors"
    )

    # ---- 断言 2: BAO 的 setpoint provider 被清除 ---- #
    assert fake_ctrl._setpoint_provider is None, (
        "BAO degradation should clear the controller's setpoint provider"
    )

    # ---- 断言 3: publisher 发布了 degraded 状态的 health sensor ---- #
    # 收集 publisher 发布的所有 entity_id + state 对
    published_states: Dict[str, Any] = {}
    for entity_id, state, attrs in fake_ha_client.published:
        published_states[entity_id] = state

    # optimizer_health should be "degraded"
    assert published_states.get(
        "sensor.sleep_classifier_optimizer_health"
    ) == "degraded", (
        f"optimizer_health should be 'degraded', got: "
        f"{published_states.get('sensor.sleep_classifier_optimizer_health')}"
    )

    # predictor_health should be "degraded"
    assert published_states.get(
        "sensor.sleep_classifier_predictor_health"
    ) == "degraded", (
        f"predictor_health should be 'degraded', got: "
        f"{published_states.get('sensor.sleep_classifier_predictor_health')}"
    )


async def test_property_x3_individual_module_degradation(
    tmp_path: Path,
) -> None:
    """Verify each module degrades independently: only the module with
    error_count >= 3 gets disabled, others remain active."""
    from scripts.run_ha_smart_service import SmartSleepService
    from src.sleep_state_publisher import SleepStatePublisher

    config_path = _write_minimal_config(tmp_path)
    svc = SmartSleepService(_make_args(config_path))

    fake_bao = _FakeModule()
    fake_cae = _FakeModule()
    fake_emst = _FakeModule()
    fake_pp = _FakeModule()

    svc._v3_bao = fake_bao
    svc._v3_cae_engine = fake_cae
    svc._v3_predictor = fake_emst
    svc._v3_prior_repo = fake_pp
    svc._v3_degraded = set()

    fake_ha_client = _FakeHAClient()
    publisher = SleepStatePublisher(fake_ha_client)
    publisher.set_v3_modules(
        bao=fake_bao,
        cae_engine=fake_cae,
        prior_repo=fake_pp,
        predictor=fake_emst,
    )
    svc.publisher = publisher
    fake_ctrl = _FakeController()

    # Only inject errors for BAO
    fake_bao.inject_errors(3)

    await svc._v3_check_health_and_degrade(fake_ctrl)

    # BAO degraded
    assert "bao" in svc._v3_degraded
    # Others not degraded
    assert "cae" not in svc._v3_degraded
    assert "emst" not in svc._v3_degraded
    assert "pp" not in svc._v3_degraded


async def test_property_x3_idempotent_after_degradation(
    tmp_path: Path,
) -> None:
    """Verify the health check is idempotent: calling it again after
    degradation doesn't cause errors or change state."""
    from scripts.run_ha_smart_service import SmartSleepService
    from src.sleep_state_publisher import SleepStatePublisher

    config_path = _write_minimal_config(tmp_path)
    svc = SmartSleepService(_make_args(config_path))

    fake_bao = _FakeModule()
    fake_cae = _FakeModule()
    fake_emst = _FakeModule()
    fake_pp = _FakeModule()

    svc._v3_bao = fake_bao
    svc._v3_cae_engine = fake_cae
    svc._v3_predictor = fake_emst
    svc._v3_prior_repo = fake_pp
    svc._v3_degraded = set()

    fake_ha_client = _FakeHAClient()
    publisher = SleepStatePublisher(fake_ha_client)
    publisher.set_v3_modules(
        bao=fake_bao,
        cae_engine=fake_cae,
        prior_repo=fake_pp,
        predictor=fake_emst,
    )
    svc.publisher = publisher
    fake_ctrl = _FakeController()

    # Inject 3 errors into all modules
    fake_bao.inject_errors(3)
    fake_cae.inject_errors(3)
    fake_emst.inject_errors(3)
    fake_pp.inject_errors(3)

    # First call
    await svc._v3_check_health_and_degrade(fake_ctrl)
    degraded_after_first = set(svc._v3_degraded)

    # Record publish count
    published_count_first = len(fake_ha_client.published)

    # Second call — should be a no-op (idempotent)
    await svc._v3_check_health_and_degrade(fake_ctrl)
    degraded_after_second = set(svc._v3_degraded)

    # Same degraded set
    assert degraded_after_first == degraded_after_second
    # No additional publishes from the second call
    assert len(fake_ha_client.published) == published_count_first


async def test_property_x3_below_threshold_no_degradation(
    tmp_path: Path,
) -> None:
    """Modules with error_count < 3 should NOT be degraded."""
    from scripts.run_ha_smart_service import SmartSleepService
    from src.sleep_state_publisher import SleepStatePublisher

    config_path = _write_minimal_config(tmp_path)
    svc = SmartSleepService(_make_args(config_path))

    fake_bao = _FakeModule()
    fake_cae = _FakeModule()
    fake_emst = _FakeModule()
    fake_pp = _FakeModule()

    svc._v3_bao = fake_bao
    svc._v3_cae_engine = fake_cae
    svc._v3_predictor = fake_emst
    svc._v3_prior_repo = fake_pp
    svc._v3_degraded = set()

    fake_ha_client = _FakeHAClient()
    publisher = SleepStatePublisher(fake_ha_client)
    publisher.set_v3_modules(
        bao=fake_bao,
        cae_engine=fake_cae,
        prior_repo=fake_pp,
        predictor=fake_emst,
    )
    svc.publisher = publisher
    fake_ctrl = _FakeController()

    # Inject only 2 errors (below threshold)
    fake_bao.inject_errors(2)
    fake_cae.inject_errors(2)
    fake_emst.inject_errors(2)
    fake_pp.inject_errors(2)

    await svc._v3_check_health_and_degrade(fake_ctrl)

    # None should be degraded at error_count = 2
    assert "bao" not in svc._v3_degraded
    assert "cae" not in svc._v3_degraded
    assert "emst" not in svc._v3_degraded
    assert "pp" not in svc._v3_degraded
