"""v3.0.0 SIGTERM 优雅退出端到端测试 —— Property 19 (X2): PR5 优雅退出契约。

**Validates: Requirements 11.3, 11.6**

验证 ``SmartSleepService._v3_drain_pending_tasks`` 在 4 模块任意子集
∈ {空, 单, 任意, 全开} 启动后发出 SIGTERM 信号（模拟设置 shutdown
event + 调用 drain 方法），断言：

1. ``_v3_tasks`` 全部 done/cancelled
2. 总耗时 ≤ 10 秒

设计说明
--------
由于真实 OS 信号在 Windows CI 上行为不稳定，测试不发送 ``SIGTERM``；
而是直接调用 ``_v3_drain_pending_tasks()``（该方法内部负责 set
``_v3_shutdown_event`` 并 gather 所有 pending task）。这精确覆盖了
PR5 契约的核心语义：所有 v3 后台 task 在主入口触发关闭时能在 10 秒
内结束。

测试用 hypothesis 在 4 个 flag 的 powerset（16 种组合）上抽样，每种
组合在服务初始化后注入若干异步 sleep task 模拟后台活动，验证 drain
方法的正确性。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


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
) -> Path:
    """Write a minimal config.json with the 4 v3 feature flags."""
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
        duration=None,
        dry_run=True,
        verbose=False,
    )


@pytest.fixture
def patched_buffer_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect persistent data paths to tmp_path for isolation."""
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
    controller.dispatch_with_lookahead = MagicMock()
    return controller


# ---------------------------------------------------------------------------
# Property 19 (X2): PR5 优雅退出契约
# ---------------------------------------------------------------------------


# Strategy: any subset of 4 modules
_flags_strategy = st.fixed_dictionaries({
    "bao": st.booleans(),
    "cae": st.booleans(),
    "pp": st.booleans(),
    "emst": st.booleans(),
})

# Number of simulated background tasks to inject
_n_tasks_strategy = st.integers(min_value=0, max_value=8)

# How long each simulated task sleeps (seconds) — kept short for test speed
_task_duration_strategy = st.floats(min_value=0.0, max_value=2.0)


@settings(
    max_examples=30,
    deadline=30_000,  # 30 seconds per example (generous for slow CI)
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    flags=_flags_strategy,
    n_tasks=_n_tasks_strategy,
    task_duration=_task_duration_strategy,
)
async def test_property_x2_sigterm_drains_all_v3_tasks_within_10s(
    flags: dict,
    n_tasks: int,
    task_duration: float,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR5 优雅退出契约：SIGTERM 时 _v3_tasks 全部 done/cancelled 且 ≤ 10s。

    **Validates: Requirements 11.3, 11.6**

    用 hypothesis 在 4 模块任意子集（16 种组合）上抽样，每种组合在服务
    初始化后注入若干后台 task（模拟 BAO persist / CAE listener / EMST
    health check 等 fire-and-forget 活动），随后调用
    ``_v3_drain_pending_tasks()`` 模拟 SIGTERM 关闭，断言：
      1. 所有 ``_v3_tasks`` 项都变为 done 或 cancelled
      2. drain 总耗时 ≤ 10 秒
    """
    import scripts.run_ha_smart_service as mod

    # Each hypothesis example gets its own temp directory
    tmp_path = tmp_path_factory.mktemp("shutdown")

    # Redirect buffer paths
    monkeypatch.setattr(mod, "_BUFFER_DIR", tmp_path, raising=True)
    monkeypatch.setattr(
        mod, "_V3_BAO_STATE_PATH", tmp_path / "bao_model.pickle", raising=True,
    )
    monkeypatch.setattr(
        mod, "_V3_CAUSAL_FACTORS_PATH",
        tmp_path / "causal_factors.jsonl", raising=True,
    )
    monkeypatch.setattr(
        mod, "_V3_PREDICTOR_AUDIT_PATH",
        tmp_path / "predictor_audit.jsonl", raising=True,
    )
    monkeypatch.setattr(
        mod, "_V3_INSTALL_ID_PATH", tmp_path / "install_id.uuid", raising=True,
    )

    from scripts.run_ha_smart_service import SmartSleepService

    # Build the service with given flags
    cfg_path = _write_v3_config(
        tmp_path,
        bao=flags["bao"],
        cae=flags["cae"],
        pp=flags["pp"],
        emst=flags["emst"],
    )
    svc = SmartSleepService(_args(cfg_path))

    # Mock out the publisher and controller
    controller = _make_controller_mock()
    svc.publisher = MagicMock()
    svc.publisher.set_v3_modules = MagicMock()
    svc._v3_controller_ref = controller

    # Initialize v3 modules (some will succeed, some won't depending on
    # artifact availability — this is fine, we test the drain contract)
    try:
        svc._init_v3_modules(controller)
    except Exception:
        pass  # graceful degrade is expected

    # Set up the shutdown event
    svc._v3_shutdown_event = asyncio.Event()

    # Inject simulated background tasks into _v3_tasks
    async def _simulated_background_task(duration: float) -> None:
        """Simulates a fire-and-forget v3 task that respects shutdown."""
        try:
            if svc._v3_shutdown_event is not None:
                # Cooperative: wait until shutdown event or duration elapsed
                try:
                    await asyncio.wait_for(
                        svc._v3_shutdown_event.wait(),
                        timeout=duration,
                    )
                except asyncio.TimeoutError:
                    pass  # Duration expired naturally
            else:
                await asyncio.sleep(duration)
        except asyncio.CancelledError:
            pass  # Cancellation is acceptable

    for _ in range(n_tasks):
        task = asyncio.create_task(
            _simulated_background_task(task_duration),
        )
        svc._v3_tasks.append(task)

    # Also inject tasks via BAO pending_persist_tasks if BAO is active
    if svc._v3_bao is not None and hasattr(svc._v3_bao, "pending_persist_tasks"):
        # The real BAO tracks its own persist tasks; for this test the
        # main contract is that _v3_drain_pending_tasks collects them.
        pass

    # Record all tasks before drain (including any created by init)
    all_tasks_before: List[asyncio.Task] = list(svc._v3_tasks)

    # Measure shutdown time
    t0 = time.monotonic()
    await svc._v3_drain_pending_tasks(timeout=10.0)
    elapsed = time.monotonic() - t0

    # --- Assertions ---

    # 1. All tasks in _v3_tasks must be done or cancelled
    for task in all_tasks_before:
        assert task.done(), (
            f"Task {task.get_name()} not done after drain "
            f"(flags={flags}, n_tasks={n_tasks})"
        )

    # 2. Total drain time must be ≤ 10 seconds
    assert elapsed <= 10.0, (
        f"Drain took {elapsed:.2f}s > 10s budget "
        f"(flags={flags}, n_tasks={n_tasks}, task_duration={task_duration})"
    )

    # 3. Shutdown event must have been set (if it was initialized)
    if svc._v3_shutdown_event is not None:
        assert svc._v3_shutdown_event.is_set(), (
            "_v3_shutdown_event was not set by _v3_drain_pending_tasks"
        )


# ---------------------------------------------------------------------------
# Supplementary example-based tests for specific subsets
# ---------------------------------------------------------------------------


async def test_drain_with_no_modules_and_no_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty subset (all disabled, no tasks) — drain completes instantly."""
    import scripts.run_ha_smart_service as mod

    monkeypatch.setattr(mod, "_BUFFER_DIR", tmp_path, raising=True)
    monkeypatch.setattr(
        mod, "_V3_BAO_STATE_PATH", tmp_path / "bao_model.pickle", raising=True,
    )
    monkeypatch.setattr(
        mod, "_V3_CAUSAL_FACTORS_PATH",
        tmp_path / "causal_factors.jsonl", raising=True,
    )
    monkeypatch.setattr(
        mod, "_V3_PREDICTOR_AUDIT_PATH",
        tmp_path / "predictor_audit.jsonl", raising=True,
    )
    monkeypatch.setattr(
        mod, "_V3_INSTALL_ID_PATH", tmp_path / "install_id.uuid", raising=True,
    )

    from scripts.run_ha_smart_service import SmartSleepService

    cfg_path = _write_v3_config(
        tmp_path, bao=False, cae=False, pp=False, emst=False,
    )
    svc = SmartSleepService(_args(cfg_path))
    svc.publisher = MagicMock()
    svc._v3_controller_ref = _make_controller_mock()
    svc._init_v3_modules(_make_controller_mock())
    svc._v3_shutdown_event = asyncio.Event()

    t0 = time.monotonic()
    await svc._v3_drain_pending_tasks(timeout=10.0)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, "Empty drain should be near-instant"
    assert svc._v3_shutdown_event.is_set()


async def test_drain_cancels_stuck_tasks_after_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tasks that ignore shutdown event get cancelled after timeout."""
    import scripts.run_ha_smart_service as mod

    monkeypatch.setattr(mod, "_BUFFER_DIR", tmp_path, raising=True)
    monkeypatch.setattr(
        mod, "_V3_BAO_STATE_PATH", tmp_path / "bao_model.pickle", raising=True,
    )
    monkeypatch.setattr(
        mod, "_V3_CAUSAL_FACTORS_PATH",
        tmp_path / "causal_factors.jsonl", raising=True,
    )
    monkeypatch.setattr(
        mod, "_V3_PREDICTOR_AUDIT_PATH",
        tmp_path / "predictor_audit.jsonl", raising=True,
    )
    monkeypatch.setattr(
        mod, "_V3_INSTALL_ID_PATH", tmp_path / "install_id.uuid", raising=True,
    )

    from scripts.run_ha_smart_service import SmartSleepService

    cfg_path = _write_v3_config(tmp_path, bao=True, cae=False, pp=False, emst=False)
    svc = SmartSleepService(_args(cfg_path))
    svc.publisher = MagicMock()
    svc.publisher.set_v3_modules = MagicMock()
    controller = _make_controller_mock()
    svc._v3_controller_ref = controller
    svc._init_v3_modules(controller)
    svc._v3_shutdown_event = asyncio.Event()

    # Inject a "stuck" task that does NOT respect the shutdown event
    # and does NOT catch CancelledError — so cancellation propagates.
    async def _stuck_task() -> None:
        await asyncio.sleep(999)  # Will never finish on its own

    stuck = asyncio.create_task(_stuck_task())
    svc._v3_tasks.append(stuck)

    # Use a very short timeout to verify cancellation behavior
    t0 = time.monotonic()
    await svc._v3_drain_pending_tasks(timeout=1.0)
    elapsed = time.monotonic() - t0

    # Stuck task should be done after drain (cancelled by the timeout path)
    assert stuck.done(), "Stuck task should be done after drain"
    assert stuck.cancelled(), "Stuck task should have been cancelled"
    # Total time should be close to timeout (1s) + cancel grace (2s) at most
    assert elapsed < 5.0, f"Drain took {elapsed:.2f}s, expected < 5s"


async def test_drain_with_all_modules_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All 4 modules enabled — drain still completes within budget."""
    import scripts.run_ha_smart_service as mod

    monkeypatch.setattr(mod, "_BUFFER_DIR", tmp_path, raising=True)
    monkeypatch.setattr(
        mod, "_V3_BAO_STATE_PATH", tmp_path / "bao_model.pickle", raising=True,
    )
    monkeypatch.setattr(
        mod, "_V3_CAUSAL_FACTORS_PATH",
        tmp_path / "causal_factors.jsonl", raising=True,
    )
    monkeypatch.setattr(
        mod, "_V3_PREDICTOR_AUDIT_PATH",
        tmp_path / "predictor_audit.jsonl", raising=True,
    )
    monkeypatch.setattr(
        mod, "_V3_INSTALL_ID_PATH", tmp_path / "install_id.uuid", raising=True,
    )

    from scripts.run_ha_smart_service import SmartSleepService

    cfg_path = _write_v3_config(tmp_path, bao=True, cae=True, pp=True, emst=True)
    svc = SmartSleepService(_args(cfg_path))
    svc.publisher = MagicMock()
    svc.publisher.set_v3_modules = MagicMock()
    controller = _make_controller_mock()
    svc._v3_controller_ref = controller
    svc._init_v3_modules(controller)
    svc._v3_shutdown_event = asyncio.Event()

    # Inject cooperative tasks that will finish once shutdown event is set
    async def _cooperative_task() -> None:
        try:
            await svc._v3_shutdown_event.wait()
        except asyncio.CancelledError:
            pass

    for _ in range(5):
        task = asyncio.create_task(_cooperative_task())
        svc._v3_tasks.append(task)

    t0 = time.monotonic()
    await svc._v3_drain_pending_tasks(timeout=10.0)
    elapsed = time.monotonic() - t0

    # All tasks should be done
    for task in svc._v3_tasks:
        assert task.done()

    # Should complete almost instantly since cooperative tasks exit on event
    assert elapsed < 2.0, f"Cooperative drain took {elapsed:.2f}s, expected < 2s"
    assert svc._v3_shutdown_event.is_set()
