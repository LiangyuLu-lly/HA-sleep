"""Unit tests for ``SmartSleepService._init_v3_modules`` (task 8.1).

这一组测试验证 v3.0.0 4 个算法模块在 ``scripts/run_ha_smart_service.py``
里的接入合约：

* 启动序列遵循 design §2.5（PP → BAO → EMST → CAE）。
* 任一模块的 import / 加载失败仅 log INFO + sensor 置 ``disabled``，主
  流程继续（R11.3 graceful degrade）。
* 4 个 flag 全 false 时**不 import** 对应模块（lazy import in
  ``if flag:``），实现 R11.4 字节级等价回退。
* 注册的 3 个 hook 都通过既有公开 API（``set_setpoint_provider`` /
  ``add_session_listener`` / ``add_pre_transition_hook``）落到正确的目标
  模块上。
* 启动期一次性打印 design §6.2 v3 status banner（仅一次）。
* SIGTERM 时的 ``_v3_drain_pending_tasks`` 在 10 秒内 await
  ``pending_persist_tasks`` + ``pending_listener_tasks``。

测试用 ``unittest.mock`` 替代真实 BAO / CAE / EMST 模块的实例，避免
依赖 numpy / scipy / onnxruntime 在 CI 上的可用性。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _write_v3_config(
    tmp_path: Path,
    *,
    bao: bool = True,
    cae: bool = True,
    pp: bool = True,
    emst: bool = True,
    age_band: str = "",
    sex: str = "",
    chronotype: str = "",
) -> Path:
    """Write a config.json that carries the 4 v3 flags + user_profile fields."""
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
        "user_profile_age_band": age_band,
        "user_profile_sex": sex,
        "user_profile_chronotype": chronotype,
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
        duration=None,
        dry_run=True,
        verbose=False,
    )


@pytest.fixture
def service_cls():
    from scripts.run_ha_smart_service import SmartSleepService
    return SmartSleepService


@pytest.fixture
def patched_buffer_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    return tmp_path


def _make_controller_mock() -> MagicMock:
    controller = MagicMock(name="SmartEnvironmentController")
    controller.set_setpoint_provider = MagicMock()
    controller.dispatch_with_lookahead = AsyncMock()
    return controller


def _make_engine_mock() -> MagicMock:
    engine = MagicMock(name="ExternalStageSubscriber")
    engine.add_pre_transition_hook = MagicMock()
    return engine


# ---------------------------------------------------------------------------
# Module loading & graceful degrade
# ---------------------------------------------------------------------------


class TestV3InitGracefulDegrade:
    """任一模块加载失败 → INFO + sensor 置 disabled，主流程继续（R11.3）."""

    def test_init_loads_all_4_modules_when_artifacts_missing(
        self, service_cls, patched_buffer_dir: Path, tmp_path: Path,
    ) -> None:
        """开发机 / CI 上 prior + onnx artifact 都不存在 → 仍然成功 init。

        BAO + CAE 不依赖任何 artifact，应 init 成功；PP + EMST 因 artifact
        缺失返回 None；publisher.set_v3_modules 被调用一次。
        """
        cfg = _write_v3_config(tmp_path)
        svc = service_cls(_args(cfg))
        controller = _make_controller_mock()
        svc.publisher = MagicMock()
        svc.publisher.set_v3_modules = MagicMock()
        svc._v3_controller_ref = controller

        svc._init_v3_modules(controller)

        # BAO + CAE 应该 init 成功（不依赖 prior / onnx 文件）。
        assert svc._v3_bao is not None
        assert svc._v3_cae_engine is not None
        # PP + EMST artifact 不存在 → None。
        assert svc._v3_prior_repo is None
        assert svc._v3_predictor is None
        # set_setpoint_provider 被调用（BAO 接入）。
        assert controller.set_setpoint_provider.called
        # publisher.set_v3_modules 被调用一次。
        assert svc.publisher.set_v3_modules.called
        # banner 日志一次性 latch。
        assert svc._v3_status_banner_logged is True

    def test_init_handles_bao_import_failure(
        self,
        service_cls,
        patched_buffer_dir: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """BayesianOptimizer.load_or_init raise → BAO=None，主流程继续。"""
        cfg = _write_v3_config(tmp_path)
        svc = service_cls(_args(cfg))
        controller = _make_controller_mock()
        svc.publisher = MagicMock()
        svc._v3_controller_ref = controller

        with patch(
            "src.bayesian_optimizer.BayesianOptimizer.load_or_init",
            side_effect=RuntimeError("simulated numpy missing"),
        ):
            svc._init_v3_modules(controller)

        assert svc._v3_bao is None
        # CAE / PP / EMST 不受 BAO 失败牵连。
        assert svc._v3_cae_engine is not None
        # set_setpoint_provider 被显式 reset 到 None（fallback to v2.x）。
        # 收到的最后一次调用应是 None（因为 BAO 失败时 except 分支调用了
        # set_setpoint_provider(None)）。
        last_call = controller.set_setpoint_provider.call_args
        assert last_call is not None
        assert last_call.args[0] is None

    def test_init_handles_cae_init_failure(
        self,
        service_cls,
        patched_buffer_dir: Path,
        tmp_path: Path,
    ) -> None:
        """CausalAttributionEngine __init__ raise → CAE=None，PL 不挂 listener。"""
        cfg = _write_v3_config(tmp_path)
        svc = service_cls(_args(cfg))
        # 给 service 一个 learner 让 add_session_listener 路径有意义。
        learner = MagicMock()
        learner.add_session_listener = MagicMock()
        svc.learner = learner
        controller = _make_controller_mock()
        svc.publisher = MagicMock()
        svc._v3_controller_ref = controller

        with patch(
            "src.causal_attribution.CausalAttributionEngine",
            side_effect=ValueError("simulated bad config"),
        ):
            svc._init_v3_modules(controller)

        assert svc._v3_cae_engine is None
        # CAE 失败 → learner.add_session_listener 不应被调用。
        assert not learner.add_session_listener.called


# ---------------------------------------------------------------------------
# Lazy import & R11.4 byte-equivalent fallback
# ---------------------------------------------------------------------------


class TestV3LazyImportFallback:
    """4 个 flag 全 false 时不 import 任何 v3 模块（R11.4）."""

    def test_all_flags_false_does_not_import_v3_modules(
        self,
        service_cls,
        patched_buffer_dir: Path,
        tmp_path: Path,
    ) -> None:
        """4 个 flag 全 false → 没有 v3 模块被 import 进 sys.modules（如果未
        提前 import 过）。若已经被其他测试 import 过，至少不应**新**导入。"""
        cfg = _write_v3_config(
            tmp_path, bao=False, cae=False, pp=False, emst=False,
        )
        svc = service_cls(_args(cfg))
        controller = _make_controller_mock()
        svc.publisher = MagicMock()
        svc.publisher.set_v3_modules = MagicMock()
        svc._v3_controller_ref = controller

        # 记录 init 之前的 v3 模块 import 状态。
        v3_module_names = {
            "src.bayesian_optimizer",
            "src.causal_attribution",
            "src.population_prior",
            "src.stage_predictor",
        }
        loaded_before = {m for m in v3_module_names if m in sys.modules}

        svc._init_v3_modules(controller)

        # init 后既有 4 个引用都应保持 None。
        assert svc._v3_bao is None
        assert svc._v3_cae_engine is None
        assert svc._v3_prior_repo is None
        assert svc._v3_predictor is None
        # set_setpoint_provider 不被调用（BAO 未启用）。
        assert not controller.set_setpoint_provider.called
        # publisher.set_v3_modules 不被调用（4 个引用都是 None）。
        assert not svc.publisher.set_v3_modules.called

        # lazy import 不变量：任何**之前未导入**的 v3 模块在本次 init 后
        # 仍未导入。已经被其他测试加载过的模块继续保留是允许的。
        loaded_after = {m for m in v3_module_names if m in sys.modules}
        newly_loaded = loaded_after - loaded_before
        assert newly_loaded == set(), (
            f"R11.4 violated: 4 flags all false but newly imported "
            f"{newly_loaded}"
        )


# ---------------------------------------------------------------------------
# Hook registration
# ---------------------------------------------------------------------------


class TestV3HookRegistration:
    """3 个 hook 都通过既有公开 API 落到正确的目标模块上."""

    def test_setpoint_provider_closure_returns_recommendation(
        self,
        service_cls,
        patched_buffer_dir: Path,
        tmp_path: Path,
    ) -> None:
        """set_setpoint_provider 收到的是一个 zero-arg closure，调用之返回
        BAO.recommend 的结果。"""
        cfg = _write_v3_config(tmp_path)
        svc = service_cls(_args(cfg))
        controller = _make_controller_mock()
        svc.publisher = MagicMock()
        svc._v3_controller_ref = controller

        svc._init_v3_modules(controller)

        assert controller.set_setpoint_provider.called
        provider = controller.set_setpoint_provider.call_args.args[0]
        assert callable(provider)

        # 调用 closure 应返回一个 GPRecommendation-like 对象（不抛异常）。
        rec = provider()
        assert hasattr(rec, "temperature_c")
        assert hasattr(rec, "humidity_pct")
        assert hasattr(rec, "brightness_pct")

    def test_session_listener_registered_when_cae_loaded(
        self,
        service_cls,
        patched_buffer_dir: Path,
        tmp_path: Path,
    ) -> None:
        """CAE 加载成功 + learner 暴露 add_session_listener → listener 被注册。"""
        cfg = _write_v3_config(tmp_path)
        svc = service_cls(_args(cfg))
        learner = MagicMock()
        learner.add_session_listener = MagicMock()
        svc.learner = learner
        controller = _make_controller_mock()
        svc.publisher = MagicMock()
        svc._v3_controller_ref = controller

        svc._init_v3_modules(controller)

        assert learner.add_session_listener.called
        listener = learner.add_session_listener.call_args.args[0]
        # listener 必须是 awaitable（async function）。
        assert asyncio.iscoroutinefunction(listener)

    def test_pre_transition_hook_skipped_when_predictor_none(
        self,
        service_cls,
        patched_buffer_dir: Path,
        tmp_path: Path,
    ) -> None:
        """EMST artifact 不存在 → predictor=None → 不注册 pre_transition_hook。"""
        cfg = _write_v3_config(tmp_path)
        svc = service_cls(_args(cfg))
        controller = _make_controller_mock()
        engine = _make_engine_mock()
        svc.publisher = MagicMock()
        svc._v3_controller_ref = controller

        svc._init_v3_modules(controller)
        assert svc._v3_predictor is None
        svc._v3_register_pre_transition_hook(engine)

        # predictor=None → 不注册 hook。
        assert not engine.add_pre_transition_hook.called

    def test_pre_transition_hook_registered_when_predictor_loaded(
        self,
        service_cls,
        patched_buffer_dir: Path,
        tmp_path: Path,
    ) -> None:
        """EMST 加载成功 → engine.add_pre_transition_hook 被调用一次。"""
        cfg = _write_v3_config(tmp_path)
        svc = service_cls(_args(cfg))
        # 直接把一个假的 predictor 注入。
        svc._v3_predictor = MagicMock()
        engine = _make_engine_mock()

        svc._v3_register_pre_transition_hook(engine)

        assert engine.add_pre_transition_hook.called
        hook = engine.add_pre_transition_hook.call_args.args[0]
        assert asyncio.iscoroutinefunction(hook)


# ---------------------------------------------------------------------------
# Status banner
# ---------------------------------------------------------------------------


class TestV3StatusBanner:
    """启动期一次性打印 design §6.2 v3 status INFO 日志."""

    def test_status_banner_logged_once(
        self,
        service_cls,
        patched_buffer_dir: Path,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        cfg = _write_v3_config(tmp_path)
        svc = service_cls(_args(cfg))
        controller = _make_controller_mock()
        svc.publisher = MagicMock()
        svc._v3_controller_ref = controller

        with caplog.at_level(logging.INFO, logger="smart_service"):
            svc._init_v3_modules(controller)
            # 第二次调用不应再次打印 banner（latch 不变）。
            svc._v3_log_status_banner()

        banners = [
            r for r in caplog.records
            if "v3.0.0 algorithmic moat status" in r.getMessage()
        ]
        assert len(banners) == 1


# ---------------------------------------------------------------------------
# Graceful shutdown drain (PR5)
# ---------------------------------------------------------------------------


async def test_drain_pending_tasks_within_budget(
    service_cls,
    patched_buffer_dir: Path,
    tmp_path: Path,
) -> None:
    """``_v3_drain_pending_tasks`` 等待 BAO + PL 的后台 task 在 10 秒内完成。"""
    cfg = _write_v3_config(tmp_path)
    svc = service_cls(_args(cfg))

    # 构造两个会快速完成的 dummy task。
    completed_count = 0

    async def _quick_task() -> None:
        nonlocal completed_count
        await asyncio.sleep(0.01)
        completed_count += 1

    bao_task = asyncio.create_task(_quick_task())
    pl_task = asyncio.create_task(_quick_task())

    bao = MagicMock()
    bao.pending_persist_tasks = MagicMock(return_value=(bao_task,))
    learner = MagicMock()
    learner.pending_listener_tasks = MagicMock(return_value=(pl_task,))
    svc._v3_bao = bao
    svc.learner = learner

    await svc._v3_drain_pending_tasks(timeout=5.0)

    assert completed_count == 2
    assert bao_task.done()
    assert pl_task.done()


async def test_drain_pending_tasks_cancels_on_timeout(
    service_cls,
    patched_buffer_dir: Path,
    tmp_path: Path,
) -> None:
    """单个 task 超时 → ``_v3_drain_pending_tasks`` 在内层 cancel 它。"""
    cfg = _write_v3_config(tmp_path)
    svc = service_cls(_args(cfg))

    async def _slow_task() -> None:
        await asyncio.sleep(60.0)

    slow = asyncio.create_task(_slow_task())
    bao = MagicMock()
    bao.pending_persist_tasks = MagicMock(return_value=(slow,))
    svc._v3_bao = bao

    await svc._v3_drain_pending_tasks(timeout=0.05)

    # 超时后应被 cancel。
    assert slow.cancelled() or slow.done()
