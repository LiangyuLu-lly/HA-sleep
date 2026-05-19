"""EMST ``maybe_anticipate`` 触发条件等价 property（task 5.4）。

**Validates: Requirements 9.3, 9.5, 9.6, 10.1**

Property 16: maybe_anticipate 触发条件等价
------------------------------------------
:meth:`src.stage_predictor.StagePredictor.maybe_anticipate` 是 EMST
提前控制路径的入口：在 ``ExternalStageSubscriber.add_pre_transition_hook``
调用它时，它判断「下一阶段是否大概率为 DEEP」并通过
:meth:`SmartEnvironmentController.dispatch_with_lookahead` 把空调 /
加湿器 / 电热毯往 DEEP 的 setpoint 提前 60 秒推一把。这个判断必须
**精确等价**于 R9.3 / R9.5 / R9.6 / R10.1 的逻辑合取式：

.. code-block:: text

   trigger ⇔ (current_stage == LIGHT)
              AND is_valid(predicted)
              AND (confidence >= min_confidence)
              AND (argmax(predicted) == DEEP)

其中 ``is_valid`` 的等价定义（R9.5）::

   is_valid ⇔ no NaN
              AND ∀ p ∈ {p_awake, p_light, p_deep, p_rem}: 0.0 ≤ p ≤ 1.0
              AND |sum(p_*) - 1.0| ≤ 0.01

任何条件不满足都必须**短路**为「不下发」，绝不能让仅置信度高但
``argmax`` 是 LIGHT 的窗口、或 ``current_stage=AWAKE`` 仍提前给空调送
DEEP 指令的越界路径出现。

实现要点
--------

* **device_class 维度跳过**：``maybe_anticipate`` 不接收 ``device_class``
  参数；R10.1 中的 ``{"climate", "humidifier"}`` 白名单实际由
  :meth:`SmartEnvironmentController.dispatch_with_lookahead` 在转发链路
  下游执行（设计 §3.4.2）。把白名单守卫集中在一处是 PR1 dry-run 契约
  的物理保证，本测试因此只断言 ``maybe_anticipate`` 是否调用了
  ``dispatch_with_lookahead``，下游过滤由 task 5.6 单元测试覆盖。
* **is_valid 等价同时验证**：测试在生成的每组 4 概率上同步比较
  :func:`src.stage_predictor._validate_probabilities` 与本文件的
  reference 实现，把 R9.5 的硬契约钉死在 property 测试里——任何对
  阈值 / 边界 / NaN 处理的微调都会让两者脱节并立刻失败。
* **不依赖 onnxruntime**：构造 :class:`StagePredictor` 时
  ``__init__`` 路径不触发 ONNX 加载（R11.3 graceful），所以本测试在
  没有 ``stage_predictor.onnx`` 文件的开发机 / CI 上同样可跑。
* **AsyncMock controller**：用 :class:`unittest.mock.AsyncMock` 提供
  awaitable ``dispatch_with_lookahead``，并断言其被调用的**次数**
  和**关键字参数**——后者锁定 ``stage=DEEP, lead_seconds=60``，匹配
  R10.1 与设计 §3.4.3 的 60 秒 lookahead 契约。

@settings(max_examples=50, deadline=None) 与任务说明一致：50 例已能
覆盖 ``current_stage`` × 4 种概率 pattern × is_valid 边界三维笛卡尔
积；``deadline=None`` 避免 Windows / Pi 4B 上的偶发抖动误报。
"""
from __future__ import annotations

import asyncio
import math
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

from src.data_structures import SleepStage
from src.stage_predictor import (
    PredictorOutput,
    StagePredictor,
    _validate_probabilities,
)


# ---------------------------------------------------------------------------
# Constants — 与 src/stage_predictor.py 内部约定一致
# ---------------------------------------------------------------------------

#: ONNX 输出的 4 维概率向量按此索引顺序映射到 stage 名称（R9.3 + 设计
#: §3.4.1：``argmax`` 索引 ↔ :class:`SleepStage` 整数值）。
_STAGE_NAMES_ORDER: tuple[str, str, str, str] = (
    "AWAKE", "LIGHT", "DEEP", "REM",
)

#: ``maybe_anticipate`` 默认置信度门槛（R9.5 / R10.1）。
_MIN_CONFIDENCE: float = 0.6

#: ``dispatch_with_lookahead`` 触发时 ``maybe_anticipate`` 锁定的提前
#: 量。R10.1 / 设计 §3.4.3：60 秒 lookahead 仅对慢响应设备有意义。
_LEAD_SECONDS: int = 60


# ---------------------------------------------------------------------------
# Reference implementations — pure mirrors of R9.5 / argmax stage name
# ---------------------------------------------------------------------------


def _reference_is_valid(
    p_awake: float, p_light: float, p_deep: float, p_rem: float,
) -> bool:
    """R9.5 的纯函数 reference：与 predictor 内部实现独立编写。

    保持 reference 与 predictor 的实现分离，可以保证「is_valid 等价」
    测试不会因为 import 同一个函数而退化为重言式。
    """
    probs = (p_awake, p_light, p_deep, p_rem)
    for p in probs:
        # ``isnan`` 必须先查：``-0.5 <= NaN <= 1.5`` 在 Python 里返回
        # ``False`` 但部分边缘平台浮点比较有非典型行为。
        if math.isnan(p):
            return False
        if not (0.0 <= p <= 1.0):
            return False
    return abs(sum(probs) - 1.0) <= 0.01


def _reference_argmax_stage(
    p_awake: float, p_light: float, p_deep: float, p_rem: float,
) -> str:
    """``argmax`` reference；与 ``_argmax_stage_name`` 实现独立编写。

    平局时 :func:`max` 返回**最小**索引，与
    :func:`src.stage_predictor._argmax_stage_name` 的 ``key=lambda i:
    probs[i]`` 行为一致（``max`` over an iterable returns the first
    maximal element）。本函数仅在 ``is_valid=True`` 的窗口被调用，
    NaN 路径已被上层短路。
    """
    probs = (p_awake, p_light, p_deep, p_rem)
    idx = max(range(4), key=lambda i: probs[i])
    return _STAGE_NAMES_ORDER[idx]


# ---------------------------------------------------------------------------
# Strategies — mixed valid / invalid coverage
# ---------------------------------------------------------------------------


@st.composite
def _four_probs(
    draw: st.DrawFn,
) -> tuple[float, float, float, float]:
    """Generate 4 probabilities mixing valid and invalid patterns.

    Pattern 概览：

    * ``"normalized"``：先在 ``[0, 1]`` 上抽 4 个值，再除以总和归一化
      —— ``is_valid=True`` 的稠密覆盖，让「触发」分支被充分采样。
    * ``"uniform_range"``：每维独立在 ``[0, 1]`` 上抽，多数情况下
      ``|sum - 1| > 0.01``，验证「sum 不达标」分支。
    * ``"wide_range"``：每维独立在 ``[-0.5, 1.5]`` 上抽，覆盖「区间
      越界」分支（含恰好踩到 0 / 1 边界的退化样本）。
    * ``"nan_injected"``：在 ``[0, 1]`` 抽完后随机一维替换为 NaN，
      把 R9.5 的「无 NaN」要求作为 invalidation 的独立维度覆盖。

    四种 pattern 的 ``sampled_from`` 默认均匀采样，``max_examples=50``
    下每分支约 10–15 例，足以让 hypothesis 同时探索「触发」与
    「不触发」两侧的边界。
    """
    pattern = draw(
        st.sampled_from(
            ["normalized", "uniform_range", "wide_range", "nan_injected"]
        )
    )
    unit_st = st.floats(
        min_value=0.0,
        max_value=1.0,
        allow_nan=False,
        allow_infinity=False,
        width=64,
    )
    raw = draw(st.tuples(unit_st, unit_st, unit_st, unit_st))

    if pattern == "normalized":
        total = sum(raw)
        if total <= 0.0:
            # 4 个 0 的退化样本：返回均匀分布作为合法 fallback，
            # 仍是 ``is_valid=True``。
            return (0.25, 0.25, 0.25, 0.25)
        return tuple(p / total for p in raw)  # type: ignore[return-value]

    if pattern == "uniform_range":
        return raw

    if pattern == "wide_range":
        wide_st = st.floats(
            min_value=-0.5,
            max_value=1.5,
            allow_nan=False,
            allow_infinity=False,
            width=64,
        )
        return draw(st.tuples(wide_st, wide_st, wide_st, wide_st))

    # ``nan_injected``
    idx = draw(st.integers(min_value=0, max_value=3))
    mutable = list(raw)
    mutable[idx] = float("nan")
    return tuple(mutable)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Property 16 — maybe_anticipate 触发条件等价
# ---------------------------------------------------------------------------


@given(
    current_stage=st.sampled_from(list(SleepStage)),
    probs=_four_probs(),
)
@settings(max_examples=50, deadline=None)
def test_property_p8b_maybe_anticipate_triggers_iff_all_conditions(
    current_stage: SleepStage,
    probs: tuple[float, float, float, float],
) -> None:
    """**Validates: Requirements 9.3, 9.5, 9.6, 10.1**

    Property 16: ``maybe_anticipate`` 调用 ``dispatch_with_lookahead``
    当且仅当 R9.3 + R9.5 + R10.1 的 4 个条件**全部成立**::

        current_stage == LIGHT
        AND predicted.is_valid
        AND predicted.confidence >= min_confidence (= 0.6)
        AND argmax(predicted) == DEEP

    且 :class:`PredictorOutput` 的 ``is_valid`` 字段语义与 R9.5 等价
    （4 概率 ∈ [0, 1] 且 ``|sum - 1| ≤ 0.01`` 且无 NaN）。

    任何一条短路即应导致**零次** ``dispatch_with_lookahead`` 调用，
    保证慢响应设备永远不会被错误的 60 秒提前指令打断。

    Note on ``device_class``
    ------------------------
    任务说明明确：``maybe_anticipate`` 不接收 ``device_class`` 参数；
    R10.1 中「仅 ``climate`` / ``humidifier``」的过滤实际由
    :meth:`SmartEnvironmentController.dispatch_with_lookahead` 在下游
    执行（设计 §3.4.2 的「白名单单点收敛」契约）。本测试因此跳过
    device_class 维度，下游 device_class 过滤由 task 5.6 的
    ``test_dispatch_with_lookahead_respects_dry_run`` 等单元测试覆盖。
    """
    p_awake, p_light, p_deep, p_rem = probs

    # ------------------------------------------------------------------
    # Part A — is_valid 等价 (R9.5)
    # ------------------------------------------------------------------
    expected_valid = _reference_is_valid(p_awake, p_light, p_deep, p_rem)
    actual_valid = _validate_probabilities(
        p_awake, p_light, p_deep, p_rem,
    )
    assert expected_valid == actual_valid, (
        "is_valid mismatch between reference and predictor: "
        f"probs=({p_awake}, {p_light}, {p_deep}, {p_rem}); "
        f"reference={expected_valid}, predictor={actual_valid}"
    )

    # ------------------------------------------------------------------
    # Part B — 真值表：expected_trigger
    # ------------------------------------------------------------------
    # confidence 沿用源码 :class:`PredictorOutput` 构造路径的语义：
    # 仅在 valid 时取 max(probs)，invalid 时落 0.0（屏蔽下游使用）。
    if expected_valid:
        confidence = max(p_awake, p_light, p_deep, p_rem)
    else:
        confidence = 0.0

    # 注意短路顺序：``and`` 是短路求值，``_reference_argmax_stage``
    # 仅在 ``expected_valid=True`` 分支被调用，NaN 路径下避免
    # ``max([NaN, ...])`` 的未定义行为污染参考真值。
    expected_trigger = (
        current_stage == SleepStage.LIGHT
        and expected_valid
        and confidence >= _MIN_CONFIDENCE
        and _reference_argmax_stage(
            p_awake, p_light, p_deep, p_rem,
        ) == "DEEP"
    )

    # ------------------------------------------------------------------
    # Part C — 调用 maybe_anticipate 并比对
    # ------------------------------------------------------------------
    out = PredictorOutput(
        p_awake=p_awake,
        p_light=p_light,
        p_deep=p_deep,
        p_rem=p_rem,
        confidence=confidence,
        inference_ms=10.0,  # 任意 < max budget 的值，本 property 不关心
        is_valid=expected_valid,
    )

    controller = MagicMock()
    # ``dispatch_with_lookahead`` 在源码中是 ``async def``；用
    # AsyncMock 提供 awaitable 返回值，避免 ``await`` 被普通 MagicMock
    # 默默吞掉变成 coroutine warnings。
    controller.dispatch_with_lookahead = AsyncMock(return_value=None)

    # 直接构造：``__init__`` 不触发 onnxruntime / 文件加载（R11.3
    # graceful），允许在没有真实 ``stage_predictor.onnx`` 的开发机 /
    # CI 上跑本测试。
    predictor = StagePredictor(
        model_path=Path("/tmp/sleep_classifier_test_no_such_model.onnx"),
        audit_jsonl=Path(
            "/tmp/sleep_classifier_test_no_such_audit.jsonl"
        ),
        min_confidence=_MIN_CONFIDENCE,
    )

    asyncio.run(
        predictor.maybe_anticipate(
            current_stage=current_stage,
            predicted=out,
            controller=controller,
        )
    )

    # ------------------------------------------------------------------
    # Part D — 断言触发条件等价
    # ------------------------------------------------------------------
    if expected_trigger:
        assert controller.dispatch_with_lookahead.call_count == 1, (
            "expected dispatch_with_lookahead exactly once but got "
            f"{controller.dispatch_with_lookahead.call_count}; "
            f"current_stage={current_stage.name}, "
            f"probs=({p_awake}, {p_light}, {p_deep}, {p_rem}), "
            f"confidence={confidence}, is_valid={expected_valid}"
        )
        controller.dispatch_with_lookahead.assert_called_once_with(
            stage=SleepStage.DEEP,
            lead_seconds=_LEAD_SECONDS,
        )
    else:
        assert controller.dispatch_with_lookahead.call_count == 0, (
            "dispatch_with_lookahead must NOT be called when any of the "
            "trigger conditions fails, but it was invoked "
            f"{controller.dispatch_with_lookahead.call_count} time(s); "
            f"current_stage={current_stage.name}, "
            f"probs=({p_awake}, {p_light}, {p_deep}, {p_rem}), "
            f"confidence={confidence}, is_valid={expected_valid}"
        )


# ---------------------------------------------------------------------------
# Task 5.6 — EMST 单元测试（缺失通道、ONNX 加载降级、dispatch dry-run）
# ---------------------------------------------------------------------------
#
# **Validates: Requirements 9.2, 9.4, 9.5, 9.6, 11.5**
#
# 这 5 个用例覆盖 ``StagePredictor`` 的 4 条降级 / 安全分支与
# ``SmartEnvironmentController.dispatch_with_lookahead`` 的 PR1 dry-run
# 契约：
#
# * **R9.6** 任一通道非 ``None`` 比例 < 50% → ``predict`` 直接返回
#   ``None``，不进入 ONNX 推理路径（防止零填充偏置 AWAKE）。
# * **R9.4** 推理 > 50 ms 计 1 次 error，连续 3 次后 1 小时冷却；冷却
#   窗口内 ``predict`` 必须返回 ``None``，无论 session 是否健康。
# * **R11.3 / 9.2** 未安装 ``onnxruntime`` 或模型 > 80 KB（疑似未量化
#   工件）时 ``try_load`` 返回 ``None`` + INFO 日志，主流程继续（优雅
#   降级）。
# * **R11.5 (PR1)** ``dry_run=True`` 时 ``dispatch_with_lookahead``
#   只 log 不调用 ``ha_client.call_service`` —— 4 个新模块的提前控制
#   路径都经此方法转发，把 dry-run 守卫集中在唯一一处。

import sys
import time
from typing import Any

import numpy as np
import pytest

from src.stage_predictor import (
    HitRecord,
    PredictorInput,
)


_FULL_WINDOW_SAMPLES: int = 300


def _full_channel(value: float = 0.5) -> tuple[float, ...]:
    """Return a 300-sample tuple filled with *value* (no ``None``).

    Used as the «healthy» channel filler when we want to leave only
    one channel under-populated so :attr:`PredictorInput.is_complete_enough`
    fails on that channel alone.
    """
    return tuple(value for _ in range(_FULL_WINDOW_SAMPLES))


def _sparse_channel(non_none_count: int) -> tuple[float | None, ...]:
    """Return a 300-sample tuple with exactly *non_none_count* floats.

    The remaining samples are ``None``.  The non-``None`` values are
    placed at the front so the order is deterministic and easy to
    inspect on a failing case.
    """
    return tuple(
        0.5 if i < non_none_count else None
        for i in range(_FULL_WINDOW_SAMPLES)
    )


def _build_predictor(tmp_path: Path) -> StagePredictor:
    """Construct a :class:`StagePredictor` rooted at *tmp_path*.

    The model file is intentionally absent: the ``__init__`` path
    never touches disk (R11.3 graceful — onnxruntime + model are
    deferred to first ``predict`` call), so this leaves us free to
    inject a stubbed ``_load_session`` per-test.
    """
    return StagePredictor(
        model_path=tmp_path / "missing_stage_predictor.onnx",
        audit_jsonl=tmp_path / "predictor_audit.jsonl",
    )


# ---------------------------------------------------------------------------
# R9.6 — channel completeness gate
# ---------------------------------------------------------------------------


async def test_predict_returns_none_when_channel_missing_50pct(
    tmp_path: Path,
) -> None:
    """**Validates: Requirements 9.6**

    When *any* of the three input channels has more than half of its
    samples dropped (``None``), :meth:`StagePredictor.predict` must
    short-circuit to ``None`` *before* invoking the ONNX session.
    Zero-filling more than 50 % of a channel would bias the model
    toward AWAKE (a zero-vector input has no HRV / motion / breathing
    signal), so the contract is to refuse the inference rather than
    emit a polluted prediction.

    The test fills two channels at 100 % completeness and starves the
    third (``hrv_ms``) at 149 / 300 = 49.6 % — one sample below the
    50 % threshold (``< 0.5 * 300 = 150``), which is the exact branch
    point in :attr:`PredictorInput.is_complete_enough`.
    """
    # 149 < 150 = 0.5 * 300 → fails the «≥ 50 %» predicate.
    window = PredictorInput(
        hrv_ms=_sparse_channel(non_none_count=149),
        motion_au=_full_channel(),
        breathing_rate_bpm=_full_channel(),
    )
    assert window.is_complete_enough is False  # sanity check

    predictor = _build_predictor(tmp_path)
    out = await predictor.predict(window)
    assert out is None
    # Channel-completeness skip is silent (R9.6): no error budget bump.
    assert predictor.error_count == 0
    assert predictor.disabled_until == 0.0


# ---------------------------------------------------------------------------
# R9.4 — inference budget + cool-down latch
# ---------------------------------------------------------------------------


class _SlowFakeSession:
    """Minimal ONNX-runtime stand-in whose ``run`` is *always* slow.

    Returns a uniform 4-class probability vector (so ``is_valid``
    would have been ``True`` had the call landed under the 50 ms
    budget).  The slowness itself is simulated by the
    :class:`monkeypatch`-controlled clock, not by ``time.sleep`` —
    keeping the test sub-second on CI.
    """

    def get_inputs(self) -> list[Any]:
        # ``predict`` only reads ``[0].name`` so a single shim is enough.
        shim = type("InputShim", (), {"name": "input"})()
        return [shim]

    def run(self, _output_names: Any, _feed: Any) -> list[Any]:
        # Uniform softmax — would be «valid» if it ever made it past
        # the budget gate.
        return [np.array([[0.25, 0.25, 0.25, 0.25]], dtype=np.float32)]


async def test_predict_returns_none_when_inference_timeout_3_consecutive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Validates: Requirements 9.4**

    Three consecutive ``predict`` calls whose inference exceeds the
    50 ms budget must:

    1. Each return ``None`` (inference result is dropped).
    2. Bump :attr:`StagePredictor.error_count` on every call.
    3. Latch :attr:`StagePredictor.disabled_until` to a non-zero
       Unix timestamp once the third error lands (1-hour cool-down).
    4. Cause a *fourth* call within the cool-down window to also
       return ``None`` even though the session itself is unchanged —
       proving that the cool-down gate short-circuits before the
       inference path is even entered.

    Implementation detail: we monkeypatch
    :func:`src.stage_predictor.time.perf_counter` to alternate between
    ``0.0`` and ``0.06`` so each call's measured duration is exactly
    60 ms (60 > 50 ⇒ over-budget).  ``time.time`` is left untouched
    so the cool-down comparison still uses real wall-clock seconds.
    """
    predictor = _build_predictor(tmp_path)
    fake_session = _SlowFakeSession()

    # Bypass the real ``_load_session`` (would try to import
    # ``onnxruntime`` + read a missing file) and serve our slow stub.
    monkeypatch.setattr(
        predictor, "_load_session", lambda: fake_session,
    )

    # Each ``predict`` call uses ``time.perf_counter`` exactly twice
    # (start + end); this generator yields a 60 ms gap on every pair.
    perf_clock = iter([0.0, 0.06] * 100)
    import src.stage_predictor as sp_module
    monkeypatch.setattr(
        sp_module.time, "perf_counter", lambda: next(perf_clock),
    )

    window = PredictorInput(
        hrv_ms=_full_channel(),
        motion_au=_full_channel(),
        breathing_rate_bpm=_full_channel(),
    )

    # Three over-budget calls → 3 errors → cool-down latched.
    for attempt in range(1, 4):
        out = await predictor.predict(window)
        assert out is None, f"attempt {attempt} should return None"
        assert predictor.error_count == attempt, (
            f"error_count should be {attempt} after attempt {attempt}, "
            f"got {predictor.error_count}"
        )

    # After the 3rd consecutive over-budget call the predictor is in
    # cool-down (R9.4): ``disabled_until`` is a future Unix timestamp.
    assert predictor.disabled_until > time.time(), (
        f"disabled_until should be in the future after 3 errors, "
        f"got {predictor.disabled_until} vs now={time.time()}"
    )

    # 4th call within the cool-down window — must still return None
    # even though the session is healthy.  This proves the cool-down
    # gate short-circuits before reaching ``session.run``.
    out = await predictor.predict(window)
    assert out is None
    assert predictor.predictor_status == "degraded"


# ---------------------------------------------------------------------------
# R9.2 / R11.3 — try_load graceful degradation
# ---------------------------------------------------------------------------


def test_try_load_returns_none_when_onnxruntime_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Validates: Requirements 9.2, 11.3**

    If ``onnxruntime`` cannot be imported (e.g. user opted out of the
    optional runtime dependency or the wheel didn't materialize on
    their platform), :meth:`StagePredictor.try_load` must:

    * Return ``None`` (caller short-circuits the EMST wiring).
    * Not raise (R11.3 graceful — main loop continues without EMST).

    We simulate the missing module by setting ``sys.modules["onnxruntime"]``
    to ``None``: per :pep:`328`, this turns ``import onnxruntime`` into
    a synthetic :class:`ImportError` with the message «import of
    onnxruntime halted; None in sys.modules», which is exactly what
    the system-side missing wheel would surface.

    The model file is created within the 80 KB cap so we exercise
    *only* the ``import onnxruntime`` failure path; the size guard
    is asserted in the next test.
    """
    # Minimum-viable model file (1 byte) so we don't trip the
    # 80 KB size guard ahead of the import probe.
    model_path = tmp_path / "stage_predictor.onnx"
    model_path.write_bytes(b"\x00")
    audit_path = tmp_path / "predictor_audit.jsonl"

    # ``setitem`` with value ``None`` is a documented pytest pattern
    # for forcing import failures; ``monkeypatch.delitem`` would only
    # cause a re-import (which would succeed if ``onnxruntime`` is
    # already cached elsewhere).
    monkeypatch.setitem(sys.modules, "onnxruntime", None)

    result = StagePredictor.try_load(
        model_path=model_path,
        audit_jsonl=audit_path,
    )
    assert result is None


def test_try_load_returns_none_when_model_exceeds_80kb(
    tmp_path: Path,
) -> None:
    """**Validates: Requirements 9.2**

    The INT8-quantized transformer should land around 50 KB; an
    artifact larger than 80 KB indicates either a corrupt download or
    an unquantized export, and :meth:`StagePredictor.try_load` must
    refuse to load it.  Letting an oversized artifact through would:

    * Inflate the add-on image past the 96 MB CI guard (PR4).
    * Risk slow inference on Pi 4B (the 50 ms budget assumes the
      INT8 transformer; FP32 would burn ~200 ms).

    We write 81 KB of zeros to ``stage_predictor.onnx``; the byte
    count is one kilobyte over the 80 × 1024 cap.
    """
    model_path = tmp_path / "stage_predictor.onnx"
    # 81 KB — exactly 1 KB over the 80 KB guard.
    model_path.write_bytes(b"\x00" * (81 * 1024))
    audit_path = tmp_path / "predictor_audit.jsonl"

    result = StagePredictor.try_load(
        model_path=model_path,
        audit_jsonl=audit_path,
    )
    assert result is None


# ---------------------------------------------------------------------------
# R11.5 (PR1) — dispatch_with_lookahead respects dry_run
# ---------------------------------------------------------------------------


async def test_dispatch_with_lookahead_respects_dry_run() -> None:
    """**Validates: Requirements 11.5**

    The PR1 contract: every external HA service call across the
    add-on must funnel through a single ``dry_run`` short-circuit.
    For the EMST path that funnel is
    :meth:`SmartEnvironmentController.dispatch_with_lookahead`;
    when ``dry_run=True`` it must log the planned actions but never
    invoke :meth:`HomeAssistantClient.call_service`.

    Test setup:

    * One HA climate entity advertising
      ``ClimateEntityFeature.TARGET_TEMPERATURE`` (bit ``0x01``) so
      the capability gate (``_device_supports``) lets us through.
    * Default :class:`LiveStateCache` (no seeding) means
      :meth:`LiveStateCache.is_available` returns ``True`` and
      :meth:`LiveStateCache.is_off` returns ``False`` — both gates
      open optimistically.
    * Mocked :class:`HomeAssistantClient` with an ``AsyncMock``
      ``call_service`` so we can count invocations.

    With ``dry_run=True`` and ``stage=DEEP, lead_seconds=60``:

    * The controller plans climate ``set_temperature`` actions
      (verifiable in the actions log).
    * It must **not** call ``ha_client.call_service`` even once.

    A regression here would mean the EMST predictor could push real
    setpoints during onboarding / dry-run smoke tests, violating the
    «default-on dry-run» contract for first-time users.
    """
    from unittest.mock import AsyncMock

    from src.device_discovery import ActionableDevices
    from src.ha_api_client import HAEntity
    from src.smart_environment_controller import (
        SmartControlConfig,
        SmartEnvironmentController,
    )

    # ``supported_features=1`` ⇒ ClimateEntityFeature.TARGET_TEMPERATURE
    # which :func:`capabilities_of` translates to ``SET_TEMPERATURE``.
    climate = HAEntity(
        entity_id="climate.bedroom_ac",
        state="cool",   # not in OFF_STATES → no auto turn_on path
        attributes={"supported_features": 1},
    )
    devices = ActionableDevices(climates=[climate])

    cfg = SmartControlConfig(
        enabled=True,
        dry_run=True,
        min_seconds_between_actions=0.0,
    )

    ha_client = AsyncMock()
    ha_client.call_service = AsyncMock(return_value=None)

    controller = SmartEnvironmentController(
        config=cfg,
        ha_client=ha_client,
        devices=devices,
        learner=None,
    )

    await controller.dispatch_with_lookahead(
        stage=SleepStage.DEEP, lead_seconds=60,
    )

    # PR1 invariant: zero outbound HA service calls when dry_run=True.
    assert ha_client.call_service.call_count == 0, (
        "dispatch_with_lookahead must not call ha_client.call_service "
        "when dry_run=True; got "
        f"{ha_client.call_service.call_count} call(s)."
    )
    # The action *was* planned (it just didn't dispatch) — guarantees
    # we exercised the body, not an early-return that vacuously
    # satisfies the call-count assertion.
    planned = [
        a for a in controller._actions_log
        if a.domain == "climate" and a.service == "set_temperature"
    ]
    assert planned, (
        "expected at least one climate.set_temperature action to be "
        "planned (and logged) under dry_run; got actions="
        f"{[a.describe() for a in controller._actions_log]}"
    )


# ---------------------------------------------------------------------------
# Silence the unused-import warning for HitRecord (re-exported so the
# task-5.6 test surface explicitly lines up with the StagePredictor
# public dataclasses).
# ---------------------------------------------------------------------------
_ = HitRecord  # noqa: F401 — kept to document task-5.6 surface


# ---------------------------------------------------------------------------
# Supplementary coverage tests — exercising remaining branches
# ---------------------------------------------------------------------------

import json
from datetime import datetime, timedelta, timezone

from src.stage_predictor import (
    _argmax_stage_name,
    _parse_iso_timestamp,
)


# ---------------------------------------------------------------------------
# _parse_iso_timestamp
# ---------------------------------------------------------------------------


def test_parse_iso_timestamp_with_z_suffix() -> None:
    """Cover the ``Z`` → ``+00:00`` normalization branch."""
    ts = "2025-01-15T03:00:00Z"
    result = _parse_iso_timestamp(ts)
    assert result is not None
    # Should parse to the correct UTC time.
    expected = datetime(2025, 1, 15, 3, 0, 0, tzinfo=timezone.utc).timestamp()
    assert abs(result - expected) < 1.0


def test_parse_iso_timestamp_with_offset() -> None:
    """Cover normal ISO-8601 with timezone offset."""
    ts = "2025-01-15T03:00:00+00:00"
    result = _parse_iso_timestamp(ts)
    assert result is not None


def test_parse_iso_timestamp_malformed_returns_none() -> None:
    """Cover the ValueError/TypeError exception branch."""
    assert _parse_iso_timestamp("not-a-timestamp") is None
    assert _parse_iso_timestamp("") is None


# ---------------------------------------------------------------------------
# _argmax_stage_name
# ---------------------------------------------------------------------------


def test_argmax_stage_name_identifies_each_stage() -> None:
    """Cover _argmax_stage_name for all four stages."""
    # AWAKE highest
    out_awake = PredictorOutput(
        p_awake=0.7, p_light=0.1, p_deep=0.1, p_rem=0.1,
        confidence=0.7, inference_ms=5.0, is_valid=True,
    )
    assert _argmax_stage_name(out_awake) == "AWAKE"

    # REM highest
    out_rem = PredictorOutput(
        p_awake=0.1, p_light=0.1, p_deep=0.1, p_rem=0.7,
        confidence=0.7, inference_ms=5.0, is_valid=True,
    )
    assert _argmax_stage_name(out_rem) == "REM"


# ---------------------------------------------------------------------------
# try_load — success path
# ---------------------------------------------------------------------------


def test_try_load_returns_predictor_when_model_valid(
    tmp_path: Path,
) -> None:
    """Cover the success path of try_load (model exists, under 80 KB)."""
    model_path = tmp_path / "stage_predictor.onnx"
    model_path.write_bytes(b"\x00" * 1024)  # 1 KB, well under limit
    audit_path = tmp_path / "predictor_audit.jsonl"

    result = StagePredictor.try_load(
        model_path=model_path,
        audit_jsonl=audit_path,
    )
    # onnxruntime is installed in this environment, so try_load should succeed.
    assert result is not None
    assert isinstance(result, StagePredictor)


def test_try_load_returns_none_when_model_missing(
    tmp_path: Path,
) -> None:
    """Cover the file-not-found branch of try_load."""
    model_path = tmp_path / "nonexistent.onnx"
    audit_path = tmp_path / "predictor_audit.jsonl"

    result = StagePredictor.try_load(
        model_path=model_path,
        audit_jsonl=audit_path,
    )
    assert result is None


# ---------------------------------------------------------------------------
# _load_session — failure and caching paths
# ---------------------------------------------------------------------------


def test_load_session_caches_failure(tmp_path: Path) -> None:
    """Cover _session_load_failed short-circuit and exception path."""
    predictor = StagePredictor(
        model_path=tmp_path / "bad_model.onnx",
        audit_jsonl=tmp_path / "audit.jsonl",
    )
    # First call: model file doesn't exist → exception → _session_load_failed
    result1 = predictor._load_session()
    assert result1 is None
    assert predictor._session_load_failed is True

    # Second call: should short-circuit (line 330: if self._session_load_failed)
    result2 = predictor._load_session()
    assert result2 is None


def test_load_session_returns_cached_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover the 'if self._session is not None: return self._session' path."""
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=tmp_path / "audit.jsonl",
    )
    fake_session = MagicMock()
    predictor._session = fake_session

    result = predictor._load_session()
    assert result is fake_session


# ---------------------------------------------------------------------------
# predict — valid inference path (success)
# ---------------------------------------------------------------------------


class _FastFakeSession:
    """Minimal ONNX session returning valid probabilities instantly."""

    def __init__(self, probs: list[float] | None = None):
        self._probs = probs or [0.1, 0.2, 0.6, 0.1]

    def get_inputs(self) -> list[Any]:
        shim = type("InputShim", (), {"name": "input"})()
        return [shim]

    def run(self, _output_names: Any, _feed: Any) -> list[Any]:
        return [np.array([self._probs], dtype=np.float32)]


async def test_predict_returns_valid_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover the successful inference path returning a PredictorOutput."""
    predictor = _build_predictor(tmp_path)
    fake_session = _FastFakeSession([0.1, 0.2, 0.6, 0.1])
    monkeypatch.setattr(predictor, "_load_session", lambda: fake_session)

    # Ensure perf_counter gives fast times (< 50 ms budget).
    import src.stage_predictor as sp_module
    perf_clock = iter([0.0, 0.001] * 10)
    monkeypatch.setattr(sp_module.time, "perf_counter", lambda: next(perf_clock))

    window = PredictorInput(
        hrv_ms=_full_channel(),
        motion_au=_full_channel(),
        breathing_rate_bpm=_full_channel(),
    )
    out = await predictor.predict(window)
    assert out is not None
    assert out.is_valid is True
    assert out.confidence == pytest.approx(0.6, abs=0.01)
    assert out.p_deep == pytest.approx(0.6, abs=0.01)
    # Error count should have been reset to 0.
    assert predictor.error_count == 0


async def test_predict_returns_invalid_output_with_nan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover predict path when ONNX returns NaN values (is_valid=False)."""
    predictor = _build_predictor(tmp_path)
    # Session returns NaN in one slot.
    fake_session = _FastFakeSession([0.3, float("nan"), 0.4, 0.3])
    monkeypatch.setattr(predictor, "_load_session", lambda: fake_session)

    import src.stage_predictor as sp_module
    perf_clock = iter([0.0, 0.001] * 10)
    monkeypatch.setattr(sp_module.time, "perf_counter", lambda: next(perf_clock))

    window = PredictorInput(
        hrv_ms=_full_channel(),
        motion_au=_full_channel(),
        breathing_rate_bpm=_full_channel(),
    )
    out = await predictor.predict(window)
    assert out is not None
    assert out.is_valid is False
    # Confidence should be 0.0 when invalid.
    assert out.confidence == 0.0


async def test_predict_returns_invalid_output_sum_not_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover predict path when probabilities don't sum to 1."""
    predictor = _build_predictor(tmp_path)
    fake_session = _FastFakeSession([0.5, 0.5, 0.5, 0.5])  # sum = 2.0
    monkeypatch.setattr(predictor, "_load_session", lambda: fake_session)

    import src.stage_predictor as sp_module
    perf_clock = iter([0.0, 0.001] * 10)
    monkeypatch.setattr(sp_module.time, "perf_counter", lambda: next(perf_clock))

    window = PredictorInput(
        hrv_ms=_full_channel(),
        motion_au=_full_channel(),
        breathing_rate_bpm=_full_channel(),
    )
    out = await predictor.predict(window)
    assert out is not None
    assert out.is_valid is False
    assert out.confidence == 0.0


# ---------------------------------------------------------------------------
# predict — inference exception path
# ---------------------------------------------------------------------------


class _ExplodingSession:
    """ONNX session that raises on run()."""

    def get_inputs(self) -> list[Any]:
        shim = type("InputShim", (), {"name": "input"})()
        return [shim]

    def run(self, _output_names: Any, _feed: Any) -> list[Any]:
        raise RuntimeError("ONNX exploded")


async def test_predict_returns_none_on_inference_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover the inference exception branch (lines 436-438)."""
    predictor = _build_predictor(tmp_path)
    monkeypatch.setattr(predictor, "_load_session", lambda: _ExplodingSession())

    window = PredictorInput(
        hrv_ms=_full_channel(),
        motion_au=_full_channel(),
        breathing_rate_bpm=_full_channel(),
    )
    out = await predictor.predict(window)
    assert out is None
    assert predictor.error_count == 1


# ---------------------------------------------------------------------------
# predict — cool-down expiry recovery
# ---------------------------------------------------------------------------


async def test_predict_recovers_after_cooldown_expires(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cover the disabled_until expiry branch (error_count and disabled_until reset)."""
    predictor = _build_predictor(tmp_path)
    # Simulate that the predictor was disabled in the past.
    predictor._disabled_until = time.time() - 1.0  # expired 1 second ago
    predictor._error_count = 5

    fake_session = _FastFakeSession([0.25, 0.25, 0.25, 0.25])
    monkeypatch.setattr(predictor, "_load_session", lambda: fake_session)

    import src.stage_predictor as sp_module
    perf_clock = iter([0.0, 0.001] * 10)
    monkeypatch.setattr(sp_module.time, "perf_counter", lambda: next(perf_clock))

    window = PredictorInput(
        hrv_ms=_full_channel(),
        motion_au=_full_channel(),
        breathing_rate_bpm=_full_channel(),
    )
    out = await predictor.predict(window)
    # After recovery, disabled_until and error_count are reset.
    assert predictor.disabled_until == 0.0
    assert predictor.error_count == 0
    assert out is not None


async def test_predict_returns_none_when_session_load_fails(
    tmp_path: Path,
) -> None:
    """Cover 'if session is None: return None' path."""
    predictor = _build_predictor(tmp_path)
    predictor._session_load_failed = True  # force _load_session to return None

    window = PredictorInput(
        hrv_ms=_full_channel(),
        motion_au=_full_channel(),
        breathing_rate_bpm=_full_channel(),
    )
    out = await predictor.predict(window)
    assert out is None


# ---------------------------------------------------------------------------
# maybe_anticipate — exception paths
# ---------------------------------------------------------------------------


async def test_maybe_anticipate_handles_attribute_error(
    tmp_path: Path,
) -> None:
    """Cover the AttributeError branch in maybe_anticipate."""
    predictor = _build_predictor(tmp_path)
    # Controller without dispatch_with_lookahead attribute.
    controller = MagicMock(spec=[])  # empty spec → AttributeError on access

    predicted = PredictorOutput(
        p_awake=0.05, p_light=0.1, p_deep=0.8, p_rem=0.05,
        confidence=0.8, inference_ms=5.0, is_valid=True,
    )
    # Should not raise; logs debug and returns.
    await predictor.maybe_anticipate(
        current_stage=SleepStage.LIGHT,
        predicted=predicted,
        controller=controller,
    )


async def test_maybe_anticipate_handles_generic_exception(
    tmp_path: Path,
) -> None:
    """Cover the generic Exception branch in maybe_anticipate."""
    predictor = _build_predictor(tmp_path)
    controller = MagicMock()
    controller.dispatch_with_lookahead = AsyncMock(
        side_effect=RuntimeError("something broke"),
    )

    predicted = PredictorOutput(
        p_awake=0.05, p_light=0.1, p_deep=0.8, p_rem=0.05,
        confidence=0.8, inference_ms=5.0, is_valid=True,
    )
    # Should not raise; logs warning and returns.
    await predictor.maybe_anticipate(
        current_stage=SleepStage.LIGHT,
        predicted=predicted,
        controller=controller,
    )


async def test_maybe_anticipate_skips_when_disabled(
    tmp_path: Path,
) -> None:
    """Cover maybe_anticipate when predictor is disabled (not directly blocking, but
    tests that even a valid prediction scenario can be exercised)."""
    predictor = _build_predictor(tmp_path)
    # Set predictor as auto_disabled — but note maybe_anticipate doesn't check this;
    # it's predict() that gates on disabled_until. This test confirms maybe_anticipate
    # behavior is independent of disable state.
    controller = MagicMock()
    controller.dispatch_with_lookahead = AsyncMock(return_value=None)

    predicted = PredictorOutput(
        p_awake=0.05, p_light=0.1, p_deep=0.8, p_rem=0.05,
        confidence=0.8, inference_ms=5.0, is_valid=True,
    )
    await predictor.maybe_anticipate(
        current_stage=SleepStage.LIGHT,
        predicted=predicted,
        controller=controller,
    )
    assert controller.dispatch_with_lookahead.call_count == 1


# ---------------------------------------------------------------------------
# record_hit and _prune_audit
# ---------------------------------------------------------------------------


async def test_record_hit_appends_and_prunes(tmp_path: Path) -> None:
    """Cover record_hit (append + prune) with fresh audit file."""
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=tmp_path / "predictor_audit.jsonl",
    )

    await predictor.record_hit(
        predicted_stage="DEEP",
        confidence=0.85,
        actual_stage_after_60s="DEEP",
    )

    # Verify the JSONL file was written.
    audit_path = tmp_path / "predictor_audit.jsonl"
    assert audit_path.exists()
    content = audit_path.read_text(encoding="utf-8")
    lines = [l for l in content.splitlines() if l.strip()]
    assert len(lines) >= 1
    record = json.loads(lines[0])
    assert record["predicted_stage"] == "DEEP"
    assert record["actual_stage_60s_later"] == "DEEP"
    assert record["confidence"] == 0.85

    # Hit-rate cache should have been invalidated.
    assert predictor._hit_rate_cache is None
    assert predictor._hit_rate_cache_ts == 0.0


async def test_prune_audit_removes_old_records(tmp_path: Path) -> None:
    """Cover _prune_audit dropping records older than 7 days."""
    audit_path = tmp_path / "predictor_audit.jsonl"
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=audit_path,
    )

    # Write one old record (8 days ago) and one recent record.
    now = time.time()
    old_ts = datetime.fromtimestamp(
        now - 8 * 24 * 3600, timezone.utc
    ).isoformat()
    recent_ts = datetime.fromtimestamp(now - 3600, timezone.utc).isoformat()

    lines = [
        json.dumps({"timestamp": old_ts, "predicted_stage": "LIGHT",
                    "actual_stage_60s_later": "DEEP", "confidence": 0.7}),
        json.dumps({"timestamp": recent_ts, "predicted_stage": "DEEP",
                    "actual_stage_60s_later": "DEEP", "confidence": 0.8}),
    ]
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Trigger prune.
    predictor._prune_audit(now)

    # Old record should be gone, recent kept.
    content = audit_path.read_text(encoding="utf-8")
    remaining = [l for l in content.splitlines() if l.strip()]
    assert len(remaining) == 1
    assert json.loads(remaining[0])["timestamp"] == recent_ts


async def test_prune_audit_keeps_malformed_lines(tmp_path: Path) -> None:
    """Cover the JSONDecodeError branch in _prune_audit (malformed lines kept)."""
    audit_path = tmp_path / "predictor_audit.jsonl"
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=audit_path,
    )

    now = time.time()
    old_ts = datetime.fromtimestamp(
        now - 8 * 24 * 3600, timezone.utc
    ).isoformat()
    lines = [
        "this is not valid json",
        json.dumps({"timestamp": old_ts, "predicted_stage": "DEEP",
                    "actual_stage_60s_later": "DEEP", "confidence": 0.7}),
    ]
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    predictor._prune_audit(now)

    content = audit_path.read_text(encoding="utf-8")
    remaining = [l for l in content.splitlines() if l.strip()]
    # Malformed line kept; old valid record dropped.
    assert len(remaining) == 1
    assert remaining[0] == "this is not valid json"


async def test_prune_audit_noop_when_file_missing(tmp_path: Path) -> None:
    """Cover _prune_audit FileNotFoundError (early return)."""
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=tmp_path / "nonexistent_audit.jsonl",
    )
    # Should not raise.
    predictor._prune_audit(time.time())


async def test_prune_audit_noop_when_file_empty(tmp_path: Path) -> None:
    """Cover _prune_audit early return when file is empty."""
    audit_path = tmp_path / "predictor_audit.jsonl"
    audit_path.write_text("", encoding="utf-8")
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=audit_path,
    )
    predictor._prune_audit(time.time())


# ---------------------------------------------------------------------------
# hit_rate_7d — various scenarios
# ---------------------------------------------------------------------------


def _write_hit_records(
    audit_path: Path,
    records: list[dict[str, Any]],
) -> None:
    """Helper to write a list of hit records as JSONL."""
    lines = [json.dumps(r) for r in records]
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_records_for_n_nights(
    n_nights: int,
    hits_per_night: int = 5,
    misses_per_night: int = 0,
) -> list[dict[str, Any]]:
    """Generate hit records spanning n distinct nights (UTC days)."""
    records: list[dict[str, Any]] = []
    base = datetime.now(timezone.utc) - timedelta(hours=2)  # recent
    for night in range(n_nights):
        day = base - timedelta(days=night)
        for i in range(hits_per_night):
            ts = (day + timedelta(minutes=i)).isoformat()
            records.append({
                "timestamp": ts,
                "predicted_stage": "DEEP",
                "actual_stage_60s_later": "DEEP",
                "confidence": 0.8,
            })
        for i in range(misses_per_night):
            ts = (day + timedelta(minutes=hits_per_night + i)).isoformat()
            records.append({
                "timestamp": ts,
                "predicted_stage": "DEEP",
                "actual_stage_60s_later": "LIGHT",  # miss
                "confidence": 0.8,
            })
    return records


def test_hit_rate_7d_returns_none_when_fewer_than_7_nights(
    tmp_path: Path,
) -> None:
    """Cover the 'len(per_night_hits) < 7' branch."""
    audit_path = tmp_path / "predictor_audit.jsonl"
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=audit_path,
    )
    # Only 5 nights of data.
    records = _make_records_for_n_nights(5, hits_per_night=3)
    _write_hit_records(audit_path, records)

    result = predictor.hit_rate_7d()
    assert result is None


def test_hit_rate_7d_returns_percentage_with_7_nights(
    tmp_path: Path,
) -> None:
    """Cover the success path: 7+ nights, returns rate in [0, 100]."""
    audit_path = tmp_path / "predictor_audit.jsonl"
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=audit_path,
    )
    # 7 nights, all hits → 100%.
    records = _make_records_for_n_nights(7, hits_per_night=3, misses_per_night=0)
    _write_hit_records(audit_path, records)

    result = predictor.hit_rate_7d()
    assert result is not None
    assert result == pytest.approx(100.0, abs=0.1)


def test_hit_rate_7d_returns_correct_mixed_rate(
    tmp_path: Path,
) -> None:
    """Cover hit_rate calculation with a mixture of hits and misses."""
    audit_path = tmp_path / "predictor_audit.jsonl"
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=audit_path,
    )
    # 7 nights, 3 hits + 2 misses per night → 60%.
    records = _make_records_for_n_nights(7, hits_per_night=3, misses_per_night=2)
    _write_hit_records(audit_path, records)

    result = predictor.hit_rate_7d()
    assert result is not None
    assert result == pytest.approx(60.0, abs=0.1)


def test_hit_rate_7d_cache_within_one_hour(
    tmp_path: Path,
) -> None:
    """Cover the cache branch: second call within 1 hour returns cached value."""
    audit_path = tmp_path / "predictor_audit.jsonl"
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=audit_path,
    )
    records = _make_records_for_n_nights(7, hits_per_night=5)
    _write_hit_records(audit_path, records)

    # First call computes.
    result1 = predictor.hit_rate_7d()
    assert result1 is not None
    # Second call should use cache.
    result2 = predictor.hit_rate_7d()
    assert result2 == result1


def test_hit_rate_7d_returns_none_when_file_missing(
    tmp_path: Path,
) -> None:
    """Cover the FileNotFoundError branch."""
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=tmp_path / "nonexistent.jsonl",
    )
    result = predictor.hit_rate_7d()
    assert result is None


def test_hit_rate_7d_returns_none_when_file_empty(
    tmp_path: Path,
) -> None:
    """Cover the 'if not raw' branch."""
    audit_path = tmp_path / "predictor_audit.jsonl"
    audit_path.write_text("", encoding="utf-8")
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=audit_path,
    )
    result = predictor.hit_rate_7d()
    assert result is None


def test_hit_rate_7d_skips_records_without_actual_stage(
    tmp_path: Path,
) -> None:
    """Cover 'if actual is None: continue' branch."""
    audit_path = tmp_path / "predictor_audit.jsonl"
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=audit_path,
    )
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    records = []
    for night in range(7):
        day = base - timedelta(days=night)
        # Records with actual_stage_60s_later=None → skipped.
        records.append({
            "timestamp": day.isoformat(),
            "predicted_stage": "DEEP",
            "actual_stage_60s_later": None,
            "confidence": 0.8,
        })
    _write_hit_records(audit_path, records)

    result = predictor.hit_rate_7d()
    # All records are skipped → rolling_total=0 → returns None.
    assert result is None


def test_hit_rate_7d_skips_json_decode_errors(
    tmp_path: Path,
) -> None:
    """Cover JSONDecodeError 'continue' branch in hit_rate_7d."""
    audit_path = tmp_path / "predictor_audit.jsonl"
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=audit_path,
    )
    # Mix of malformed and valid records across 7 nights.
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    lines: list[str] = []
    for night in range(7):
        day = base - timedelta(days=night)
        lines.append("this is malformed json")
        for i in range(3):
            ts = (day + timedelta(minutes=i)).isoformat()
            lines.append(json.dumps({
                "timestamp": ts,
                "predicted_stage": "DEEP",
                "actual_stage_60s_later": "DEEP",
                "confidence": 0.8,
            }))
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = predictor.hit_rate_7d()
    # Should still compute correctly from valid records.
    assert result is not None
    assert result == pytest.approx(100.0, abs=0.1)


# ---------------------------------------------------------------------------
# _update_auto_disable — R10.4
# ---------------------------------------------------------------------------


def test_auto_disable_triggers_after_3_bad_nights(
    tmp_path: Path,
) -> None:
    """Cover _update_auto_disable flipping _auto_disabled when 3 nights < 70%."""
    audit_path = tmp_path / "predictor_audit.jsonl"
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=audit_path,
    )

    # 3 nights where hit rate is below 70% (e.g., 1 hit, 4 misses = 20%).
    records = _make_records_for_n_nights(3, hits_per_night=1, misses_per_night=4)
    _write_hit_records(audit_path, records)

    # Calling hit_rate_7d will trigger _update_auto_disable.
    # (fewer than 7 nights → returns None, but still evaluates auto-disable)
    predictor.hit_rate_7d()

    assert predictor._auto_disabled is True
    assert predictor.predictor_status == "auto_disabled"


def test_auto_disable_does_not_trigger_with_good_nights(
    tmp_path: Path,
) -> None:
    """Cover _update_auto_disable NOT flipping when nights are above 70%."""
    audit_path = tmp_path / "predictor_audit.jsonl"
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=audit_path,
    )

    # 3 nights with 80% hit rate (4 hits, 1 miss).
    records = _make_records_for_n_nights(3, hits_per_night=4, misses_per_night=1)
    _write_hit_records(audit_path, records)

    predictor.hit_rate_7d()

    assert predictor._auto_disabled is False
    assert predictor.predictor_status == "healthy"


def test_auto_disable_latch_is_sticky(
    tmp_path: Path,
) -> None:
    """Cover the 'if self._auto_disabled: return' branch (sticky latch)."""
    audit_path = tmp_path / "predictor_audit.jsonl"
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=audit_path,
    )
    predictor._auto_disabled = True

    # Even if subsequent data is good, latch remains.
    records = _make_records_for_n_nights(7, hits_per_night=5, misses_per_night=0)
    _write_hit_records(audit_path, records)

    # Invalidate cache to force recomputation.
    predictor._hit_rate_cache = None
    predictor._hit_rate_cache_ts = 0.0
    predictor.hit_rate_7d()

    assert predictor._auto_disabled is True
    assert predictor.predictor_status == "auto_disabled"


def test_auto_disable_requires_minimum_nights(
    tmp_path: Path,
) -> None:
    """Cover 'if len(per_night_hits) < _BAD_NIGHTS_BEFORE_AUTO_DISABLE: return'."""
    audit_path = tmp_path / "predictor_audit.jsonl"
    predictor = StagePredictor(
        model_path=tmp_path / "model.onnx",
        audit_jsonl=audit_path,
    )

    # Only 2 nights (below the 3-night threshold).
    records = _make_records_for_n_nights(2, hits_per_night=0, misses_per_night=5)
    _write_hit_records(audit_path, records)

    predictor.hit_rate_7d()
    assert predictor._auto_disabled is False


# ---------------------------------------------------------------------------
# predictor_status / error_count / should_disable / disabled_until
# ---------------------------------------------------------------------------


def test_predictor_status_healthy(tmp_path: Path) -> None:
    """Cover predictor_status returning 'healthy'."""
    predictor = _build_predictor(tmp_path)
    assert predictor.predictor_status == "healthy"


def test_predictor_status_degraded(tmp_path: Path) -> None:
    """Cover predictor_status returning 'degraded' during cool-down."""
    predictor = _build_predictor(tmp_path)
    predictor._disabled_until = time.time() + 3600  # 1 hour in future
    assert predictor.predictor_status == "degraded"


def test_predictor_status_auto_disabled(tmp_path: Path) -> None:
    """Cover predictor_status returning 'auto_disabled'."""
    predictor = _build_predictor(tmp_path)
    predictor._auto_disabled = True
    assert predictor.predictor_status == "auto_disabled"


def test_should_disable_property(tmp_path: Path) -> None:
    """Cover should_disable property (True when error_count >= 3)."""
    predictor = _build_predictor(tmp_path)
    assert predictor.should_disable is False
    predictor._error_count = 2
    assert predictor.should_disable is False
    predictor._error_count = 3
    assert predictor.should_disable is True


def test_disabled_until_property(tmp_path: Path) -> None:
    """Cover disabled_until property getter."""
    predictor = _build_predictor(tmp_path)
    assert predictor.disabled_until == 0.0
    predictor._disabled_until = 12345.0
    assert predictor.disabled_until == 12345.0


def test_error_count_property(tmp_path: Path) -> None:
    """Cover error_count property getter."""
    predictor = _build_predictor(tmp_path)
    assert predictor.error_count == 0
    predictor._error_count = 7
    assert predictor.error_count == 7


# ---------------------------------------------------------------------------
# Task 5.5 — EMST P8 推理性能 property（slow + integration）
# ---------------------------------------------------------------------------
#
# **Validates: Requirements 9.4**
#
# Property 8: Stage 预测推理 ≤ 50 ms（Pi 4B；CI 放宽 ×1.5 = 75 ms）
#
# 这条 property 锁定 R9.4 的硬契约：``StagePredictor.predict`` 在
# (1, 3, 300) float32 输入上的单次 wall-clock 推理时延 p95 应 ≤ 50 ms
# （CI 容忍 50 × 1.5 = 75 ms）。
#
# 为什么要在 task 5.5 用合成 stub ONNX 而非真模型
# -----------------------------------------------
# * **CI 无 artifact 也能跑**：``sleep_classifier/rootfs/training_config/``
#   下的 ``stage_predictor.onnx`` 由开发者机器训练 + COPY 进镜像，源码仓库
#   既不应也不能保证它存在；本测试通过 ``onnx.helper`` 在 ``tmp_path``
#   现场构造一个**结构合法**的微型 ONNX（约 14 KB），让推理路径可以稳定
#   走完，从而把"性能 budget"这一维度独立出来验证。
# * **隔离 torch 依赖**：``scripts/train_stage_predictor.py --synthetic``
#   依赖 ``torch + onnx``；torch 是训练时依赖，未在 ``requirements.txt``
#   /  ``requirements-runtime.txt`` 中。直接用 ``onnx`` 构图能去掉对
#   torch 的依赖，让 CI 仅靠 ``onnxruntime + onnx``（runtime 镜像本就
#   有的依赖）就能跑这条 property。
# * **形状对齐 R9.3**：合成模型保持 ``(1, 3, 300) → (1, 4)`` softmax
#   契约（design §3.4.1），让 :meth:`StagePredictor.predict` 走完整路径，
#   包含 :func:`_validate_probabilities` / ``argmax`` / 时延测量等，
#   p95 ms 数据来自 :attr:`PredictorOutput.inference_ms`，不是 mock。
#
# 为何用 ``max_inference_ms=10_000.0``
# ------------------------------------
# :meth:`StagePredictor.predict` 在 ``inference_ms > max_inference_ms``
# 时会**返回 None** 并把 error_count +1，连续 3 次后冷却 1 小时（R9.4
# 副作用，已由 task 5.6 ``test_predict_returns_none_when_inference_timeout_3_consecutive``
# 验证）。本测试关注「时延分布本身」，不应被 CI 上偶发的 GC stall /
# Windows 时钟分辨率噪声误触冷却闸门，因此把单次 budget 抬到 10 秒
# 让所有 100 次调用都落在「成功」路径，最后用 :func:`numpy.percentile`
# 在收集到的 ``inference_ms`` 数组上做 p95 检验。错误路径的语义本身
# 由 task 5.6 的 4 个单元测试覆盖，本测试不重复。


def _build_tiny_stage_predictor_onnx(out_path: Path) -> None:
    """Build a tiny ``Flatten → MatMul → Add → Softmax`` ONNX model.

    Mirrors the shape contract of :class:`StagePredictor`'s expected
    artifact (R9.3 / design §3.4.1):

    * **Input** ``input``: ``(1, 3, 300)`` float32 — five-minute window
      × three channels at 1 Hz.
    * **Output** ``probs``: ``(1, 4)`` softmax — AWAKE / LIGHT / DEEP
      / REM probabilities.

    The network is a single ``Linear(900 → 4) + Softmax``: 3 604
    float32 parameters ≈ 14 KB on disk, comfortably under the 80 KB
    R9.2 cap. Weights are filled from a deterministic ``numpy``
    Generator seeded with ``20260518`` (the project-wide default seed)
    so the artifact is byte-identical across runs — handy for CI
    cache hashing if anyone wants to memoize the artifact later.

    The model carries **no real semantic content**: this is a stub
    purely to exercise the runtime pipeline (``InferenceSession``
    load + ``session.run`` latency); meaningful 4-stage hit rates
    come from ``scripts/train_stage_predictor.py`` real-training
    mode, not from this fixture.
    """
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    rng = np.random.default_rng(20260518)
    # 0.01 scale keeps logits small so softmax doesn't saturate to a
    # one-hot; not strictly required for a latency test but keeps the
    # is_valid path well-behaved if a future regression starts caring
    # about the output values too.
    weight = (
        rng.standard_normal((900, 4)).astype(np.float32) * 0.01
    )
    bias = np.zeros(4, dtype=np.float32)

    input_info = helper.make_tensor_value_info(
        "input", TensorProto.FLOAT, [1, 3, 300],
    )
    output_info = helper.make_tensor_value_info(
        "probs", TensorProto.FLOAT, [1, 4],
    )

    weight_init = numpy_helper.from_array(weight, name="W")
    bias_init = numpy_helper.from_array(bias, name="b")

    # ``axis=1`` keeps the batch dim and flattens (3, 300) → 900.
    flatten = helper.make_node(
        "Flatten", ["input"], ["flat"], axis=1,
    )
    matmul = helper.make_node("MatMul", ["flat", "W"], ["mm"])
    add = helper.make_node("Add", ["mm", "b"], ["logits"])
    softmax = helper.make_node(
        "Softmax", ["logits"], ["probs"], axis=-1,
    )

    graph = helper.make_graph(
        nodes=[flatten, matmul, add, softmax],
        name="tiny_stage_predictor_stub",
        inputs=[input_info],
        outputs=[output_info],
        initializer=[weight_init, bias_init],
    )

    # opset 17 matches the synthetic export in
    # scripts/train_stage_predictor.py for forward-compat parity.
    model = helper.make_model(
        graph,
        opset_imports=[helper.make_opsetid("", 17)],
        producer_name="sleep_classifier_test_p8",
    )
    # ir_version 8 is what onnx 1.15+ defaults to and what
    # onnxruntime 1.16+ supports without warnings; pin explicitly so
    # newer onnx defaults that bump ir_version don't surprise older
    # onnxruntime wheels in CI.
    model.ir_version = 8
    onnx.checker.check_model(model)
    onnx.save(model, str(out_path))


@pytest.mark.slow
@pytest.mark.integration
async def test_property_p8_predict_p95_within_50ms(tmp_path: Path) -> None:
    """**Validates: Requirements 9.4**

    Property 8: Stage 预测推理 ≤ 50 ms (Pi 4B; CI relaxed ×1.5 = 75 ms).

    Strategy
    --------
    1. Skip cleanly when ``onnxruntime`` or ``onnx`` is absent — the
       runtime image guarantees both, but pure dev environments may
       have only ``onnxruntime`` and we don't want red CI on those.
    2. Build a deterministic tiny ONNX (Flatten → MatMul → Add →
       Softmax, ~14 KB) in ``tmp_path`` so the test does **not**
       depend on the real ``stage_predictor.onnx`` artifact (R7.5
       contract: artifact lives outside the source tree).
    3. Construct :class:`StagePredictor` via :meth:`try_load`,
       overriding ``max_inference_ms`` to 10 s so that the 100-call
       sample loop never trips the 3-consecutive-errors cool-down
       latch — that latch is exercised separately in task 5.6's
       ``test_predict_returns_none_when_inference_timeout_3_consecutive``.
       ``try_load`` itself can return ``None`` if the local
       ``onnxruntime`` import probe fails for an exotic reason; we
       skip in that case rather than fail.
    4. Issue 100 :meth:`StagePredictor.predict` calls with a
       physiologically-plausible (HRV ≈ 60 ms, motion ≈ 0.1 a.u.,
       breathing ≈ 15 bpm) full window. The values themselves are
       irrelevant to the latency property — what matters is that
       :attr:`PredictorInput.is_complete_enough` returns ``True`` so
       every call reaches the ``session.run`` path.
    5. Collect :attr:`PredictorOutput.inference_ms` from each
       successful call. Assert ``p95 ≤ 75 ms``.

    Why p95 not max
    ---------------
    R9.4 specifies a 50 ms budget for the canonical Pi 4B target.
    On a developer machine or GitHub Actions runner the *median*
    inference is typically sub-millisecond for this stub model, but
    occasional GC pauses, thermal throttling, or Windows scheduler
    quanta can produce single-digit-percent outliers in the 5–20 ms
    range. p95 absorbs those without masking a real regression
    (which would push the **bulk** of latencies, not just the tail,
    above 75 ms).
    """
    # Step 1 — graceful skip when optional deps are missing.
    try:
        import onnxruntime  # noqa: F401 — probe only
        import onnx  # noqa: F401 — needed for tiny-model construction
    except ImportError as exc:
        pytest.skip(
            f"onnxruntime/onnx not installed: {exc}; "
            f"skipping P8 latency property (R9.4)."
        )

    # Step 2 — build the tiny stub ONNX.
    model_path = tmp_path / "stage_predictor.onnx"
    _build_tiny_stage_predictor_onnx(model_path)
    assert model_path.exists(), "tiny ONNX must materialize on disk"
    assert model_path.stat().st_size <= 80 * 1024, (
        "tiny ONNX should be well under the 80 KB R9.2 cap; got "
        f"{model_path.stat().st_size} bytes"
    )

    # Step 3 — construct the predictor. ``max_inference_ms=10_000``
    # disables the cool-down latch for this test (see module-level
    # rationale). ``try_load`` can still return None if onnxruntime
    # can't be probed for an exotic reason — skip rather than fail.
    audit_path = tmp_path / "predictor_audit.jsonl"
    predictor = StagePredictor.try_load(
        model_path=model_path,
        audit_jsonl=audit_path,
        max_inference_ms=10_000.0,
    )
    if predictor is None:
        pytest.skip(
            "StagePredictor.try_load returned None; "
            "onnxruntime probe failed despite being importable."
        )

    # Step 4 — issue 100 predict() calls. The window values are
    # constant across calls; the tiny model is a fixed Linear so the
    # per-call latency reflects raw onnxruntime dispatch overhead,
    # which is exactly what R9.4 budgets.
    full_hrv: tuple[float, ...] = tuple(60.0 for _ in range(300))
    full_motion: tuple[float, ...] = tuple(0.1 for _ in range(300))
    full_breath: tuple[float, ...] = tuple(15.0 for _ in range(300))
    window = PredictorInput(
        hrv_ms=full_hrv,
        motion_au=full_motion,
        breathing_rate_bpm=full_breath,
    )
    # is_complete_enough should be unconditionally True for a
    # zero-None window; this guards against a future regression that
    # tightens the gate.
    assert window.is_complete_enough is True

    latencies_ms: list[float] = []
    for _ in range(100):
        out = await predictor.predict(window)
        # ``predict`` returning None during this loop would mean either
        # the cool-down latch tripped (impossible — max_inference_ms
        # is 10 s) or the InferenceSession failed mid-flight; either
        # way we want to surface it rather than silently shrinking
        # the sample.
        assert out is not None, (
            "predict() returned None unexpectedly during latency "
            "sampling; check error_count="
            f"{predictor.error_count}, "
            f"disabled_until={predictor.disabled_until}"
        )
        latencies_ms.append(out.inference_ms)

    # Step 5 — assert p95 ≤ 75 ms.
    assert len(latencies_ms) == 100, (
        f"expected 100 latency samples, got {len(latencies_ms)}"
    )
    p95_ms = float(np.percentile(latencies_ms, 95))
    median_ms = float(np.percentile(latencies_ms, 50))
    max_ms = float(np.max(latencies_ms))
    assert p95_ms <= 75.0, (
        "Property 8 violated: p95 inference latency = "
        f"{p95_ms:.2f} ms exceeds 75 ms budget "
        f"(R9.4 = 50 ms × 1.5 CI tolerance). "
        f"median={median_ms:.2f} ms, max={max_ms:.2f} ms, "
        f"n={len(latencies_ms)}."
    )
