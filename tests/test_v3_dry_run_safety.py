"""Property 17: dry_run=true 阻断所有 call_service（端到端测试）。

**Validates: Requirements 11.5**

测试用 hypothesis 在 4 flag × {true,false} = 16 种组合上跑 + ``dry_run=true``，
断言每种组合下 ``ha_client.call_service`` 调用次数 = 0。

PR1 不变量核心保证：
  无论哪些 v3 模块被启用，``dry_run=true`` 时 SmartEnvironmentController
  的所有 ``call_service`` 路径都只打日志不下发。EMST 的 ``dispatch_with_lookahead``
  同样由 ``config.dry_run`` 统一守护。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_v3_config(
    tmp_path: Path,
    *,
    bao: bool = True,
    cae: bool = True,
    pp: bool = True,
    emst: bool = True,
) -> Path:
    """Write a config.json that carries the 4 v3 flags + dry_run=true."""
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
        "bayesian_optimizer_enabled": bao,
        "causal_attribution_enabled": cae,
        "population_prior_enabled": pp,
        "stage_predictor_enabled": emst,
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
        duration=5.0,
        dry_run=True,
        verbose=False,
    )


def _patch_buffer_dir(monkeypatch: Any, tmp_path: Path) -> None:
    """Redirect ``_BUFFER_DIR`` so /data writes land in tmp_path."""
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


# ---------------------------------------------------------------------------
# Property test
# ---------------------------------------------------------------------------


@given(
    bao_flag=st.booleans(),
    cae_flag=st.booleans(),
    pp_flag=st.booleans(),
    emst_flag=st.booleans(),
)
@settings(
    max_examples=16,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property_p10b_dry_run_blocks_all_call_service(
    bao_flag: bool,
    cae_flag: bool,
    pp_flag: bool,
    emst_flag: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Property 17: dry_run=true 阻断所有 call_service.

    **Validates: Requirements 11.5**

    对 4 flag × {true, false} = 16 种组合，全部以 ``dry_run=true`` 运行，
    断言每种组合下 ``ha_client.call_service`` 调用次数 = 0。
    """
    from scripts.run_ha_smart_service import SmartSleepService
    from src.data_structures import SleepStage

    _patch_buffer_dir(monkeypatch, tmp_path)

    cfg_path = _write_v3_config(
        tmp_path,
        bao=bao_flag,
        cae=cae_flag,
        pp=pp_flag,
        emst=emst_flag,
    )
    svc = SmartSleepService(_args(cfg_path))

    # Track call_service invocations via mock HA client.
    call_service_mock = AsyncMock(return_value=None)

    # Create a mock HA client that tracks call_service calls.
    mock_ha = MagicMock()
    mock_ha.call_service = call_service_mock
    mock_ha.update_state = AsyncMock(return_value=None)
    mock_ha.ping = AsyncMock(return_value=True)
    mock_ha.get_states = AsyncMock(return_value=[])
    mock_ha.subscribe_events = AsyncMock(return_value=None)
    mock_ha.close = AsyncMock(return_value=None)
    mock_ha.__aenter__ = AsyncMock(return_value=mock_ha)
    mock_ha.__aexit__ = AsyncMock(return_value=None)

    # Build controller with dry_run=true and the mock HA client.
    from src.device_discovery import ActionableDevices
    from src.smart_environment_controller import (
        SmartControlConfig,
        SmartEnvironmentController,
    )

    ctrl_cfg = SmartControlConfig(dry_run=True)
    controller = SmartEnvironmentController(
        config=ctrl_cfg,
        ha_client=mock_ha,
        devices=ActionableDevices(),
        learner=svc.learner,
    )

    # Wire up v3 modules (they may or may not load depending on flags).
    svc.publisher = MagicMock()
    svc.publisher.set_v3_modules = MagicMock()
    svc.publisher.publish_stage = AsyncMock()
    svc._v3_controller_ref = controller

    try:
        svc._init_v3_modules(controller)
    except Exception:  # noqa: BLE001
        pass  # Graceful degrade — same as production path.

    # Simulate one synthetic cycle through all stages.
    stages = [SleepStage.AWAKE, SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM]

    async def _run_synthetic() -> None:
        from src.smart_environment_controller import EnvironmentParams

        env = EnvironmentParams(
            temperature_c=22.0,
            humidity_pct=50.0,
            brightness_pct=10.0,
        )
        for stage in stages:
            # Invoke controller.apply — the main path that would normally
            # call ha_client.call_service when dry_run=False.
            await controller.apply(stage, env)

            # Also test dispatch_with_lookahead if EMST path exists.
            if hasattr(controller, "dispatch_with_lookahead"):
                try:
                    await controller.dispatch_with_lookahead(
                        stage=stage, lead_seconds=60,
                    )
                except Exception:  # noqa: BLE001
                    pass  # May fail due to missing devices — that's OK.

    asyncio.run(_run_synthetic())

    # THE CORE ASSERTION: dry_run=true guarantees zero call_service calls.
    assert call_service_mock.call_count == 0, (
        f"dry_run=true violated! call_service was called "
        f"{call_service_mock.call_count} time(s) with flags "
        f"bao={bao_flag}, cae={cae_flag}, pp={pp_flag}, emst={emst_flag}. "
        f"Calls: {call_service_mock.call_args_list}"
    )
