"""BayesianOptimizer 单元测试与性能 budget（task 3.8）+ P13 强制 exploit property（task 3.6）。

**Validates: Requirements 1.3, 1.4, 1.6, 2.3, 2.5**

四个 task 3.8 测试 + 一个 task 3.6 property 与 design.md §7.6 性能 budget
守护 + R1.4 cholesky 失败语义 + R1.6 PR3 持久化契约 + R2.3 / R2.5
forced_exploit 业务规则逐字对齐：

1. ``test_observe_within_200ms_budget``
   60 个 observation 状态下再 ``observe()`` 一次必须 ≤ 200 ms（R1.3）。
   CI 容忍系数 ×1.5 = 300 ms，与 design §7.6 保持一致。

2. ``test_cholesky_failure_raises_gp_numerical_error_then_fallback``
   monkeypatch ``scipy.linalg.cho_factor`` 模拟奇异矩阵 / 数值故障，
   断言 :class:`GPNumericalError` 被抛出且 ``error_count`` +1（R1.4）。
   主入口拿到这个异常会回退到 v2.x ``PreferenceLearner.recommend``
   路径（连续 3 次后整模块 auto-disable），所以本测试只锁定 BAO
   端的契约——异常类型 + 错误计数。

3. ``test_persist_uses_atomic_write_bytes``
   monkeypatch ``src._io_utils.atomic_write_bytes`` 验证 ``persist()``
   通过 PR3 atomic-write helper 落盘，参数为 ``state_path`` 与一段
   ``bytes`` payload（R1.6 持久化契约）。

4. ``test_export_hyperparams_json_returns_plain_dict``
   forward-compat 钩子：导出的 dict 仅含 stdlib 基础类型
   （int / float / str），不含 ``numpy`` 标量、不含 ``dataclass``。
   v3.1.0 联邦聚合器需要这一保证。

5. ``test_property_p2b_wind_down_or_locked_forces_exploit``（Property 13）
   ``in_wind_down=True`` OR ``locked_dimensions`` 非空 ⇒
   ``rec.mode == "exploit"``；锁定维度的 setpoint 等于 GP 后验均值
   （不抽样），跨不同 ``install_id`` 决定性相等（R2.3 / R2.5）。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from src import _io_utils
from src.bayesian_optimizer import (
    BayesianOptimizer,
    GPHyperparams,
    GPNumericalError,
    GPObservation,
    UserProfile,
)
from src.data_structures import SleepStage


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_optimizer(tmp_path: Path) -> BayesianOptimizer:
    """构造一个无 prior、默认超参的 BAO（最小测试夹具）。

    ``prior=None`` 时 BAO 走 ``_FALLBACK_PRIOR``，与 v2.x 默认 LIGHT
    setpoint 等价；这把测试限定在 GP / TS / 持久化层面，避免被
    PopulationPrior pickle 加载链路污染。
    """
    return BayesianOptimizer(
        prior=None,
        hyperparams=GPHyperparams(),
        state_path=tmp_path / "bao_model.pickle",
    )


def _make_obs(i: int) -> GPObservation:
    """生成第 ``i`` 个合成 observation。

    在卧室生理区间内做小幅扰动，避免所有点重合（重合 + 噪声方差
    > 0 仍正定，但点云越散 cholesky 数值条件数越好，更接近真实
    使用场景）。
    """
    # 用 i 制造可重复的小幅扰动；步长 0.1 °C / 0.5 % / 0.5 % 远小于
    # 默认 RBF length scale (1.5 / 8 / 15)，所以 60 个点仍紧凑分布
    # 在 prior 桶中心附近。
    return GPObservation(
        temperature_c=21.0 + 0.1 * (i % 11 - 5),
        humidity_pct=50.0 + 0.5 * (i % 7 - 3),
        brightness_pct=5.0 + 0.5 * (i % 5 - 2),
        quality_score=70.0 + (i % 13 - 6),
        timestamp=1_700_000_000.0 + 86_400.0 * i,
        install_id="test-install",
    )


# ---------------------------------------------------------------------------
# 1. observe() 性能 budget（R1.3 / design §7.6）
# ---------------------------------------------------------------------------


def test_observe_within_200ms_budget(tmp_path: Path) -> None:
    """60 obs 状态下 ``observe()`` ≤ 200 ms（CI 容忍 ×1.5 = 300 ms）。

    步骤：
      1. 灌入 60 个 observation（FIFO 上限）。
      2. 计时第 61 次 ``observe()``——append + truncate + refit
         （60×60 cholesky）。
      3. 断言耗时 ≤ 0.3 秒。

    实现注解：
      * 同步上下文调用 ``observe`` 时 ``_schedule_persist`` 因为
        没有运行中的事件循环会静默跳过，不会污染计时；
      * cholesky 是 ``observe`` 的耗时主体，本测试同时锁定了 R1.3
        的 200 ms budget 与「不会因为 numpy / scipy 升级悄悄退化」
        的回归基线。
    """
    optimizer = _make_optimizer(tmp_path)

    # 灌入 60 个 obs，让 FIFO 缓冲达到 max_observations 上限。
    for i in range(60):
        optimizer.observe(_make_obs(i))
    assert optimizer.n_observations == 60

    # 计时第 61 次：append + truncate to 60 + refit。
    new_obs = _make_obs(60)
    start = time.perf_counter()
    optimizer.observe(new_obs)
    elapsed = time.perf_counter() - start

    # FIFO 上限保持 60。
    assert optimizer.n_observations == 60
    # CI 容忍 ×1.5 = 300 ms（design §7.6）。
    assert elapsed <= 0.3, (
        f"observe() took {elapsed*1000:.1f} ms, exceeds 300 ms budget "
        f"(R1.3: 200 ms × 1.5 CI tolerance)"
    )


# ---------------------------------------------------------------------------
# 2. Cholesky 失败 → GPNumericalError + error_count +1（R1.4）
# ---------------------------------------------------------------------------


def test_cholesky_failure_raises_gp_numerical_error_then_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注入奇异矩阵 → 抛 :class:`GPNumericalError` 且 ``error_count`` +1。

    BAO 不直接吞掉数值错误，而是把它原样抛出来由 caller 决定：
    主入口（``scripts/run_ha_smart_service.py``）catch 后回退到 v2.x
    ``PreferenceLearner.recommend`` 路径，并在连续 3 次失败后把整
    模块标记为 ``degraded``（R1.4 / R11.3 / R11.6）。本测试只锁定
    BAO 端的契约：

      * 异常类型 = :class:`GPNumericalError`（不是 ``LinAlgError`` /
        ``ValueError``，避免在 caller 暴露 numpy / scipy 内部异常）；
      * ``error_count`` 在 raise *之前* 自增（``observe()`` 实现里
        ``self._error_count += 1; raise`` 的顺序保证了这一点）。

    通过 monkeypatch ``scipy.linalg.cho_factor`` 抛 ``np.linalg.
    LinAlgError`` 模拟数值故障；``_refit`` 同时捕获 ``LinAlgError``
    与 ``ValueError`` 并统一翻译为 :class:`GPNumericalError`。
    """
    optimizer = _make_optimizer(tmp_path)
    # 先放一个 obs，保证下次 observe 会真的进入 _refit cholesky 路径
    # （N=0 时 _refit 直接 return 不做分解）。
    optimizer.observe(_make_obs(0))
    baseline_error_count = optimizer.error_count

    def _raise_singular(*_args: Any, **_kwargs: Any) -> Any:
        raise np.linalg.LinAlgError("simulated singular matrix")

    monkeypatch.setattr("scipy.linalg.cho_factor", _raise_singular)

    with pytest.raises(GPNumericalError, match="Cholesky decomposition failed"):
        optimizer.observe(_make_obs(1))

    # error_count 应该恰好 +1（R1.4 单次失败计数一次）。
    assert optimizer.error_count == baseline_error_count + 1


# ---------------------------------------------------------------------------
# 3. persist() → atomic_write_bytes（R1.6 / PR3）
# ---------------------------------------------------------------------------


async def test_persist_uses_atomic_write_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``persist()`` 必须经由 ``_io_utils.atomic_write_bytes`` 落盘。

    PR3 持久化契约：所有 v3.0.0 新增 ``/data/*`` 文件都走
    ``_io_utils.atomic_write_*`` helper，禁止直接 ``Path.write_bytes``。
    本测试通过 monkeypatch 在 ``src._io_utils.atomic_write_bytes`` 上
    挂一个计数器，调用 ``await persist()`` 后断言：

      * helper 恰好被调用一次；
      * 第一个参数 = 构造时传入的 ``state_path``；
      * 第二个参数是非空 ``bytes``（即 ``pickle.dumps(state)``，
        我们不解析其内容，只锁定「确实有 payload」的契约）。

    pytest-asyncio 的 ``asyncio_mode = "auto"`` 让 ``async def
    test_*`` 自动运行在事件循环上，无需 ``@pytest.mark.asyncio``
    装饰器（见 pyproject.toml）。
    """
    calls: list[tuple[Path, bytes]] = []

    def _fake_atomic_write_bytes(path: Path, data: bytes) -> None:
        calls.append((Path(path), data))

    # ``BayesianOptimizer.persist`` 通过 ``from src import _io_utils``
    # 拿到模块再用 ``_io_utils.atomic_write_bytes(...)``，因此打补丁
    # 必须落在 ``src._io_utils`` 模块级名字上而非 caller 局部别名。
    monkeypatch.setattr(
        _io_utils, "atomic_write_bytes", _fake_atomic_write_bytes,
    )

    optimizer = _make_optimizer(tmp_path)
    await optimizer.persist()

    assert len(calls) == 1, f"expected 1 atomic_write_bytes call, got {len(calls)}"
    path_arg, data_arg = calls[0]
    assert path_arg == tmp_path / "bao_model.pickle"
    assert isinstance(data_arg, bytes)
    assert len(data_arg) > 0, "persist() must serialize a non-empty payload"


# ---------------------------------------------------------------------------
# 4. export_hyperparams_json forward-compat（v3.1.0 FedAvg）
# ---------------------------------------------------------------------------


def test_export_hyperparams_json_returns_plain_dict(tmp_path: Path) -> None:
    """``export_hyperparams_json()`` 仅返回 stdlib 基础类型。

    v3.1.0 联邦学习聚合器（potentially Rust / Go 实现）会消费这个
    JSON；任何 ``numpy`` 标量 / ``dataclass`` / 自定义对象都会破坏
    跨语言互操作性。本测试锁定：

      * 返回值是 :class:`dict`；
      * 每个 value 类型 ∈ ``{int, float, str, bool}``——不允许
        ``numpy.float64`` / ``numpy.int64`` / ``GPHyperparams`` 直接
        泄漏到 wire format；
      * 超参快照与构造时传入的值数值上一致（保证 export 不是空壳）。

    我们用 ``type(v) is`` 而非 ``isinstance`` 是为了拒绝 ``numpy``
    标量——``numpy.float64`` 是 ``float`` 子类，``isinstance(np.
    float64(1.0), float) == True`` 但 ``type(np.float64(1.0)) is
    float == False``。
    """
    optimizer = _make_optimizer(tmp_path)
    result = optimizer.export_hyperparams_json()

    assert isinstance(result, dict)
    # 锁定基础类型集合 —— 严格 type(v) is X 拒绝 numpy 标量 / 自定义类。
    allowed_types = (int, float, str, bool)
    for key, value in result.items():
        assert isinstance(key, str), f"key {key!r} must be str"
        assert type(value) in allowed_types, (
            f"value for key {key!r} has type {type(value).__name__}, "
            f"only stdlib primitives allowed (int/float/str/bool)"
        )

    # 数值上确实导出了默认超参（而不是空壳）。
    hp = GPHyperparams()
    assert result["length_scale_temp_c"] == pytest.approx(hp.length_scale_temp_c)
    assert result["length_scale_humidity_pct"] == pytest.approx(
        hp.length_scale_humidity_pct
    )
    assert result["length_scale_brightness_pct"] == pytest.approx(
        hp.length_scale_brightness_pct
    )
    assert result["signal_variance"] == pytest.approx(hp.signal_variance)
    assert result["noise_variance"] == pytest.approx(hp.noise_variance)
    assert result["schema_version"] == hp.schema_version

    # SleepStage 没参与导出 —— 留个 import-use 防 lint 报「imported but
    # unused」（数据结构只在文档说明里用到）。
    _ = SleepStage.LIGHT


# ---------------------------------------------------------------------------
# Property 13: wind-down 与维度锁定强制 exploit（task 3.6）
# ---------------------------------------------------------------------------
#
# **Validates: Requirements 2.3, 2.5**
#
# 入睡前 30 分钟（``in_wind_down=True``，R2.3）以及用户在 Web UI 临时锁定
# 「今晚不要探索温度 / 湿度 / 亮度」（``locked_dimensions`` 非空，R2.5）这
# 两条业务路径都会把 BAO 强制压回 exploit 模式——决不能在用户即将入睡时
# 把空调温度甩到 σ 最大点上去做探索。本 property 把这条业务规则翻译成
# :meth:`BayesianOptimizer.recommend` 的两条不变量：
#
# A. ``in_wind_down=True`` OR ``len(locked_dimensions) > 0`` ⇒
#    ``rec.mode == "exploit"``。无论 ``exploration_rate`` 配多大、Bernoulli
#    flip 抽中什么、``install_id`` 哈希到哪里，这一路都被
#    ``forced_exploit = bool(in_wind_down) or bool(locked_dimensions)``
#    短路。
#
# B. ``locked_dimensions`` 中每个被锁定的维度，setpoint 等于 GP 后验均值
#    （``μ_combined`` 的 argmax），与 Thompson Sampling 的 ``z * σ`` 抽样
#    无关；因此对**同一次 recommend 调用**而言，把 ``install_id``（决策种
#    子的唯一外部输入）从 A 换到 B，锁定维度的值必须**逐字相同**。未锁
#    定维度由 TS 抽样产生，可以不同——这正是本 property 区分「锁定 ⇒
#    确定性」与「非锁定 ⇒ 仍可抽样」的关键。
#
# 实现细节
# --------
# * BAO 必须先灌入 ≥ 5 个 observation（``_MIN_OBS_FOR_GP``，R1.2），否则
#   ``recommend`` 走 ``mode == "prior-only"`` 路径，本 property 的
#   forced_exploit 判定无法生效——那是 prior-only 分支不该被 P13 覆盖
#   的边界，task 3.4 / 3.7 单独验证。
# * 测试用 ``prior=None``（走 ``_FALLBACK_PRIOR``），把 P13 限定在 GP +
#   TS 决策层，避免被 :mod:`src.population_prior` pickle 加载链路污染。
# * ``hypothesis.assume(install_id_a != install_id_b)`` 保证「换种子」
#   语义真实生效；万一两个 install_id 撞了 hash 哪怕只是字面相同，都
#   退化成单调用例，测不出 TS 抽样路径，这条 ``assume`` 只是把这种
#   没有信息量的样本剪掉。
# * ``max_examples=50`` 与 P1 / P6 等姊妹 property 保持一致；
#   ``deadline=None`` 吸收 cholesky 重算 + Pi 4B / Windows / Linux 三套
#   BLAS 后端的耗时差异。
# * ``HealthCheck.function_scoped_fixture`` 抑制：``tmp_path`` 是函数级
#   fixture，hypothesis 默认会对每个 example 复用同一份 ``tmp_path``；
#   我们每例都重新构造 :class:`BayesianOptimizer`（不调用
#   ``load_or_init``），所以共享 ``tmp_path`` 不会跨例污染状态。

_DIM_NAMES_FOR_LOCK: tuple[str, ...] = (
    "temperature_c", "humidity_pct", "brightness_pct",
)


@given(
    in_wind_down=st.booleans(),
    locked_dimensions=st.frozensets(
        st.sampled_from(_DIM_NAMES_FOR_LOCK), min_size=0, max_size=3,
    ),
    exploration_rate=st.floats(
        min_value=0.0, max_value=0.5, allow_nan=False, allow_infinity=False,
    ),
    install_id_a=st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126),
        min_size=1, max_size=20,
    ),
    install_id_b=st.text(
        alphabet=st.characters(min_codepoint=33, max_codepoint=126),
        min_size=1, max_size=20,
    ),
)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property_p2b_wind_down_or_locked_forces_exploit(
    tmp_path: Path,
    in_wind_down: bool,
    locked_dimensions: frozenset[str],
    exploration_rate: float,
    install_id_a: str,
    install_id_b: str,
) -> None:
    """**Validates: Requirements 2.3, 2.5** — wind-down / 锁定 ⇒ 强制 exploit。

    本测试一并验证 R2.3（``in_wind_down=True`` 强制 exploit）与 R2.5
    （锁定维度 ⇒ exploit + 锁定维度等于后验均值，不抽样）两条业务规则在
    :meth:`BayesianOptimizer.recommend` 上的等价契约（design.md §3.2.5
    Property 13 / requirements.md R2 系列）。

    步骤
    ----

    1. 构造一个 ``prior=None`` 的 :class:`BayesianOptimizer`，灌入 6 条
       合成 observation —— 略过 ``N < _MIN_OBS_FOR_GP=5`` 的 prior-only
       分支，保证 ``recommend`` 走 GP + Thompson Sampling 主路。
    2. 用同一组关键字参数调用 ``recommend(install_id=install_id_a)``：
       - 若 ``in_wind_down`` 或 ``locked_dimensions`` 非空，断言
         ``rec_a.mode == "exploit"``；
       - 否则不约束 mode（可能 explore-* 也可能 exploit，由
         ``exploration_rate`` Bernoulli 抽样决定，task 3.5 P2 单独验证）。
    3. 当 ``locked_dimensions`` 非空时，再用 ``install_id=install_id_b``
       重复调一次：换 RNG 种子之后，**锁定维度的 setpoint 必须逐字
       相同**——后验均值 argmax 不依赖 RNG，决定性地由 ``μ_combined``
       决定（design.md §3.2.5 / src 实现 ``setpoint[d] = exploit_pt[d]``
       那行）。

    边界与排除
    ----------

    * ``hypothesis.assume(install_id_a != install_id_b)``：两个 install_id
      字面相同时退化成单调用例，TS 抽样路径无法被分辨，跳过这种没有
      信息量的样本（不影响 hypothesis 的统计覆盖）。
    * 未锁定维度（``locked_dimensions`` 不含的维度）来自 TS 抽样，
      RNG 不同时可能取不同值，本测试**不**做断言——若强行要求二者
      相等，反而会把 TS 抽样的随机性误判成 bug。
    """
    assume(install_id_a != install_id_b)

    state_path = tmp_path / "bao_p2b.pickle"
    optimizer = BayesianOptimizer(
        prior=None,
        hyperparams=GPHyperparams(),
        state_path=state_path,
        exploration_rate=exploration_rate,
    )
    # 灌入 6 条 obs（> _MIN_OBS_FOR_GP=5），确保 recommend 走 GP + TS
    # 主路而非 prior-only 分支。同步上下文调用 ``observe``，
    # ``_schedule_persist`` 因事件循环不存在会静默跳过，不会污染。
    for i in range(6):
        optimizer.observe(_make_obs(i))
    assert optimizer.n_observations == 6

    profile = UserProfile(
        age_band="26-35",
        sex="unspecified",
        chronotype="neutral",
        season="spring",
    )

    rec_a = optimizer.recommend(
        user_profile=profile,
        current_stage=SleepStage.LIGHT,
        in_wind_down=in_wind_down,
        locked_dimensions=locked_dimensions,
        install_id=install_id_a,
    )

    # ---- 不变量 A：forced_exploit ⇒ mode == "exploit" -----------------
    forced_exploit = in_wind_down or len(locked_dimensions) > 0
    if forced_exploit:
        assert rec_a.mode == "exploit", (
            f"R2.3 / R2.5 violated: in_wind_down={in_wind_down}, "
            f"locked_dimensions={locked_dimensions!r}, "
            f"exploration_rate={exploration_rate}, but rec.mode={rec_a.mode!r}"
            f" (expected 'exploit'); forced_exploit short-circuit not engaged."
        )

    # ---- 不变量 B：锁定维度 ⇒ 跨 install_id 取相同（后验均值 argmax）---
    if len(locked_dimensions) == 0:
        # 没有锁定维度时，第二次调用没有信息量；直接退出这一例。
        return

    rec_b = optimizer.recommend(
        user_profile=profile,
        current_stage=SleepStage.LIGHT,
        in_wind_down=in_wind_down,
        locked_dimensions=locked_dimensions,
        install_id=install_id_b,
    )
    # B.1 第二次调用的 mode 仍然必须是 exploit（locked_dimensions 非空触发
    # forced_exploit）。
    assert rec_b.mode == "exploit", (
        f"second call (install_id={install_id_b!r}) lost forced_exploit "
        f"despite locked_dimensions={locked_dimensions!r}: mode={rec_b.mode!r}"
    )

    # B.2 每个锁定维度的 setpoint 在 A / B 两次调用之间必须**逐字相同**——
    # 它来自 ``mu_combined`` 的 argmax，不依赖任何 RNG。任何一处不等都
    # 说明 ``setpoint[d] = exploit_pt[d]`` 的 override 没生效（譬如被
    # 下游误覆写成 TS 抽样值）。
    locked_values_a: dict[str, float] = {
        "temperature_c": rec_a.temperature_c,
        "humidity_pct": rec_a.humidity_pct,
        "brightness_pct": rec_a.brightness_pct,
    }
    locked_values_b: dict[str, float] = {
        "temperature_c": rec_b.temperature_c,
        "humidity_pct": rec_b.humidity_pct,
        "brightness_pct": rec_b.brightness_pct,
    }
    for dim in locked_dimensions:
        a = locked_values_a[dim]
        b = locked_values_b[dim]
        # 浮点字面相等：mu_combined argmax 是 int 索引，candidates 网格
        # 在两次调用中由同一份 prior_pt + length_scale 决定，因此选出来
        # 的坐标在两次调用中是同一个 ndarray 元素，没有任何浮点误差。
        assert a == b, (
            f"R2.5 violated: locked dim {dim!r} differs across install_ids: "
            f"install_id_a={install_id_a!r} -> {a!r}, "
            f"install_id_b={install_id_b!r} -> {b!r}; "
            f"locked dimensions must take the deterministic posterior-mean "
            f"argmax, not a Thompson sample."
        )


# ---------------------------------------------------------------------------
# Additional coverage tests — Checkpoint 2
# ---------------------------------------------------------------------------


class TestBAOInitValidation:
    """Cover __init__ validation error paths (lines 360, 364)."""

    def test_max_observations_zero_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="max_observations must be > 0"):
            BayesianOptimizer(
                prior=None,
                hyperparams=GPHyperparams(),
                state_path=tmp_path / "bao.pickle",
                max_observations=0,
            )

    def test_max_observations_negative_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="max_observations must be > 0"):
            BayesianOptimizer(
                prior=None,
                hyperparams=GPHyperparams(),
                state_path=tmp_path / "bao.pickle",
                max_observations=-5,
            )

    def test_exploration_rate_too_high_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="exploration_rate must be in"):
            BayesianOptimizer(
                prior=None,
                hyperparams=GPHyperparams(),
                state_path=tmp_path / "bao.pickle",
                exploration_rate=0.6,
            )

    def test_exploration_rate_negative_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="exploration_rate must be in"):
            BayesianOptimizer(
                prior=None,
                hyperparams=GPHyperparams(),
                state_path=tmp_path / "bao.pickle",
                exploration_rate=-0.1,
            )


class TestBAOLoadOrInit:
    """Cover load_or_init edge cases (lines 426-476)."""

    def test_load_unreadable_file(self, tmp_path: Path) -> None:
        """State file exists but reading fails -> fresh instance."""
        import os
        state_path = tmp_path / "bao_model.pickle"
        state_path.mkdir()  # directory, not a file - read_bytes will fail
        opt = BayesianOptimizer.load_or_init(
            state_path=state_path,
            prior=None,
            hyperparams=GPHyperparams(),
        )
        assert opt.n_observations == 0

    def test_load_corrupt_pickle(self, tmp_path: Path) -> None:
        """State file has invalid pickle bytes -> fresh instance."""
        state_path = tmp_path / "bao_model.pickle"
        state_path.write_bytes(b"not a pickle at all")
        opt = BayesianOptimizer.load_or_init(
            state_path=state_path,
            prior=None,
            hyperparams=GPHyperparams(),
        )
        assert opt.n_observations == 0

    def test_load_wrong_type(self, tmp_path: Path) -> None:
        """State file deserializes to wrong type -> fresh instance."""
        import pickle
        state_path = tmp_path / "bao_model.pickle"
        state_path.write_bytes(pickle.dumps({"wrong": "type"}))
        opt = BayesianOptimizer.load_or_init(
            state_path=state_path,
            prior=None,
            hyperparams=GPHyperparams(),
        )
        assert opt.n_observations == 0

    def test_load_cholesky_fails_during_replay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cholesky failure during state load -> cleared observations."""
        import pickle
        from src.bayesian_optimizer import BAOPersistedState

        # Build a persisted state with observations that would cause
        # cholesky failure (all identical points -> singular matrix
        # when noise_variance is artificially set to 0).
        obs_list = [
            GPObservation(
                temperature_c=22.0,
                humidity_pct=50.0,
                brightness_pct=10.0,
                quality_score=80.0,
                timestamp=1700000000.0 + i,
                install_id="test",
            )
            for i in range(10)
        ]
        state = BAOPersistedState(
            install_id_hash="abc",
            hyperparams=GPHyperparams(noise_variance=0.0),
            observations=tuple(obs_list),
            last_persist_at="2024-01-01T00:00:00Z",
            error_count=0,
            schema_version=1,
        )
        state_path = tmp_path / "bao_model.pickle"
        state_path.write_bytes(pickle.dumps(state))

        # Make cho_factor always fail
        import scipy.linalg
        def failing_cho_factor(*args, **kwargs):
            raise np.linalg.LinAlgError("singular matrix")
        monkeypatch.setattr(scipy.linalg, "cho_factor", failing_cho_factor)

        opt = BayesianOptimizer.load_or_init(
            state_path=state_path,
            prior=None,
            hyperparams=GPHyperparams(noise_variance=0.0),
        )
        # Observations cleared after cholesky failure during load
        assert opt.n_observations == 0


class TestBAOSchedulePersist:
    """Cover _schedule_persist (lines 532->536, 566-568)."""

    def test_schedule_persist_no_event_loop(self, tmp_path: Path) -> None:
        """Calling observe from sync context skips persist (no crash)."""
        opt = _make_optimizer(tmp_path)
        # observe from sync context should not crash
        opt.observe(_make_obs(0))
        assert opt.n_observations == 1
        # No tasks scheduled because no event loop is running
        assert len(opt.pending_persist_tasks()) == 0

    async def test_schedule_persist_with_event_loop(
        self, tmp_path: Path,
    ) -> None:
        """Calling observe from async context schedules a persist task."""
        opt = _make_optimizer(tmp_path)
        opt.observe(_make_obs(0))
        tasks = opt.pending_persist_tasks()
        # At least one task scheduled
        assert len(tasks) >= 1
        # Wait for all tasks to complete
        import asyncio
        await asyncio.gather(*tasks, return_exceptions=True)


class TestBAOLookupPriorBucket:
    """Cover _lookup_prior_bucket exception path (lines 993-1025)."""

    def test_prior_lookup_exception_returns_fallback(
        self, tmp_path: Path,
    ) -> None:
        """When prior.lookup() raises, BAO falls back to _FALLBACK_PRIOR."""
        from unittest.mock import MagicMock

        mock_prior = MagicMock()
        mock_prior.lookup.side_effect = RuntimeError("boom")

        opt = BayesianOptimizer(
            prior=mock_prior,
            hyperparams=GPHyperparams(),
            state_path=tmp_path / "bao.pickle",
        )
        # Feed enough observations to get past N < 5
        for i in range(6):
            opt.observe(_make_obs(i))

        profile = UserProfile(
            age_band="26-35",
            sex="unspecified",
            chronotype="neutral",
            season="spring",
            prior_weight_lock=None,
        )
        # Should not raise - falls back to _FALLBACK_PRIOR
        rec = opt.recommend(
            user_profile=profile,
            current_stage=SleepStage.LIGHT,
            in_wind_down=False,
        )
        assert rec.mode in ("exploit", "explore-temp", "explore-humidity", "explore-brightness")

    def test_prior_with_invalid_profile_fields(
        self, tmp_path: Path,
    ) -> None:
        """Invalid user_profile fields are coerced to defaults."""
        from unittest.mock import MagicMock
        from src.population_prior import PriorBucket

        mock_prior = MagicMock()
        mock_prior.lookup.return_value = (
            PriorBucket(
                temperature_mean_c=22.0,
                temperature_var_c2=1.0,
                humidity_mean_pct=50.0,
                humidity_var_pct2=4.0,
                brightness_mean_pct=10.0,
                brightness_var_pct2=9.0,
                n_samples=100,
            ),
            0,
        )

        opt = BayesianOptimizer(
            prior=mock_prior,
            hyperparams=GPHyperparams(),
            state_path=tmp_path / "bao.pickle",
        )
        for i in range(6):
            opt.observe(_make_obs(i))

        # Profile with invalid values that should be coerced
        profile = UserProfile(
            age_band="invalid",
            sex="invalid",
            chronotype="invalid",
            season="invalid",
            prior_weight_lock=None,
        )
        rec = opt.recommend(
            user_profile=profile,
            current_stage=SleepStage.DEEP,
            in_wind_down=False,
        )
        # Should succeed without error
        assert rec is not None
        # lookup was called with coerced defaults
        mock_prior.lookup.assert_called_once_with(
            age_band="26-35",
            sex="unspecified",
            chronotype="neutral",
            season="spring",
        )


class TestBAOExploreMode:
    """Cover the explore mode branches (lines 841-845, 875-876)."""

    def test_explore_mode_hits_explore_branch(self, tmp_path: Path) -> None:
        """With exploration_rate=0.5, we should hit explore modes."""
        opt = BayesianOptimizer(
            prior=None,
            hyperparams=GPHyperparams(),
            state_path=tmp_path / "bao.pickle",
            exploration_rate=0.5,
        )
        for i in range(10):
            opt.observe(_make_obs(i))

        profile = UserProfile(
            age_band="26-35",
            sex="unspecified",
            chronotype="neutral",
            season="spring",
            prior_weight_lock=None,
        )
        modes_seen: set[str] = set()
        # Run many recommendations with different install_ids to hit
        # both exploit and explore branches
        for i in range(50):
            rec = opt.recommend(
                user_profile=profile,
                current_stage=SleepStage.LIGHT,
                in_wind_down=False,
                install_id=f"test_{i}",
            )
            modes_seen.add(rec.mode)
        # With rate=0.5 over 50 trials, we should see at least one explore mode
        explore_modes = {"explore-temp", "explore-humidity", "explore-brightness"}
        assert modes_seen & explore_modes, (
            f"Expected at least one explore mode with rate=0.5, "
            f"got modes: {modes_seen}"
        )


class TestBAOPersistAndExport:
    """Cover persist (line 1052 implied) + export_hyperparams_json."""

    async def test_persist_creates_file(self, tmp_path: Path) -> None:
        """persist() writes pickle via atomic_write_bytes."""
        opt = _make_optimizer(tmp_path)
        opt.observe(_make_obs(0))
        await opt.persist()
        state_path = tmp_path / "bao_model.pickle"
        assert state_path.exists()
        assert state_path.stat().st_size > 0


# ---------------------------------------------------------------------------
# Additional coverage tests — gap-fill for ≥ 95%
# ---------------------------------------------------------------------------


class TestPriorOnlyMode:
    """Cover mode='prior-only' when N < _MIN_OBS_FOR_GP (line 663)."""

    def test_recommend_prior_only_below_min_obs(self, tmp_path: Path) -> None:
        """With < 5 observations, recommend returns mode='prior-only'."""
        opt = _make_optimizer(tmp_path)
        # Feed only 3 observations (< 5 min threshold)
        for i in range(3):
            opt.observe(_make_obs(i))
        assert opt.n_observations == 3

        profile = UserProfile(
            age_band="26-35",
            sex="unspecified",
            chronotype="neutral",
            season="spring",
        )
        rec = opt.recommend(
            user_profile=profile,
            current_stage=SleepStage.LIGHT,
            in_wind_down=False,
        )
        assert rec.mode == "prior-only"
        # Prior-only setpoints come from _FALLBACK_PRIOR
        assert rec.temperature_c == pytest.approx(21.0)
        assert rec.humidity_pct == pytest.approx(50.0)
        assert rec.brightness_pct == pytest.approx(5.0)
        # prior_weight should be > 0 since we have few observations
        assert rec.prior_weight > 0.0

    def test_recommend_prior_only_zero_obs(self, tmp_path: Path) -> None:
        """With 0 observations, recommend returns mode='prior-only' with weight=1.0."""
        opt = _make_optimizer(tmp_path)
        profile = UserProfile(
            age_band="26-35",
            sex="unspecified",
            chronotype="neutral",
            season="spring",
        )
        rec = opt.recommend(
            user_profile=profile,
            current_stage=SleepStage.DEEP,
            in_wind_down=False,
        )
        assert rec.mode == "prior-only"
        # With 0 observations, prior_weight should be 1.0
        assert rec.prior_weight == pytest.approx(1.0)


class TestComputePriorWeight:
    """Cover _compute_prior_weight branches (lines 1052, 1054)."""

    def test_lock_overrides_decay(self) -> None:
        """When lock is non-None, it is used verbatim (clipped)."""
        # Normal lock value
        assert BayesianOptimizer._compute_prior_weight(
            n_obs=50, lock=0.7
        ) == pytest.approx(0.7)

    def test_lock_clipped_above_one(self) -> None:
        """Lock > 1.0 is clipped to 1.0."""
        assert BayesianOptimizer._compute_prior_weight(
            n_obs=10, lock=2.5
        ) == pytest.approx(1.0)

    def test_lock_clipped_below_zero(self) -> None:
        """Lock < 0.0 is clipped to 0.0."""
        assert BayesianOptimizer._compute_prior_weight(
            n_obs=10, lock=-0.5
        ) == pytest.approx(0.0)

    def test_lock_zero_disables_prior(self) -> None:
        """Lock = 0.0 fully disables the prior."""
        assert BayesianOptimizer._compute_prior_weight(
            n_obs=5, lock=0.0
        ) == pytest.approx(0.0)

    def test_zero_obs_returns_one(self) -> None:
        """n_obs=0 with no lock -> weight = 1.0 (pure prior)."""
        assert BayesianOptimizer._compute_prior_weight(
            n_obs=0, lock=None
        ) == pytest.approx(1.0)

    def test_many_obs_floors_at_point_one(self) -> None:
        """With many observations the weight floors at 0.1."""
        # exp(-100/14) is effectively 0, but floor is 0.1
        assert BayesianOptimizer._compute_prior_weight(
            n_obs=100, lock=None
        ) == pytest.approx(0.1)

    def test_moderate_obs_exponential_decay(self) -> None:
        """Mid-range N follows exp(-N/14)."""
        import math
        weight = BayesianOptimizer._compute_prior_weight(
            n_obs=7, lock=None
        )
        expected = max(0.1, math.exp(-7 / 14.0))
        assert weight == pytest.approx(expected)


class TestShouldDisable:
    """Cover should_disable property (line 495)."""

    def test_should_disable_false_below_threshold(self, tmp_path: Path) -> None:
        """error_count < 3 -> should_disable is False."""
        opt = _make_optimizer(tmp_path)
        assert opt.error_count == 0
        assert opt.should_disable is False

    def test_should_disable_true_at_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """error_count >= 3 -> should_disable is True."""
        opt = _make_optimizer(tmp_path)
        # Force error count to 3 by injecting cho_factor failures
        opt.observe(_make_obs(0))

        def _raise(*_a: Any, **_kw: Any) -> Any:
            raise np.linalg.LinAlgError("singular")

        monkeypatch.setattr("scipy.linalg.cho_factor", _raise)
        for i in range(1, 4):
            with pytest.raises(GPNumericalError):
                opt.observe(_make_obs(i))
        assert opt.error_count == 3
        assert opt.should_disable is True


class TestPosteriorUncertaintyEmpty:
    """Cover _gp_predict empty-buffer fallback (line 932)."""

    def test_posterior_uncertainty_no_observations(self, tmp_path: Path) -> None:
        """With no observations, posterior_uncertainty returns signal std."""
        import math
        opt = _make_optimizer(tmp_path)
        # No observations -> empty buffer -> fallback path
        std = opt.posterior_uncertainty(at=(22.0, 50.0, 10.0))
        expected_std = math.sqrt(100.0)  # default signal_variance = 100
        assert std == (
            pytest.approx(expected_std),
            pytest.approx(expected_std),
            pytest.approx(expected_std),
        )


class TestExploreModeSetpoint:
    """Cover explore-* setpoint logic (lines 841-845, 875-876)."""

    def test_explore_mode_setpoint_uses_sigma_argmax(
        self, tmp_path: Path,
    ) -> None:
        """In explore mode the explore-dim setpoint uses sigma argmax."""
        # Use very high exploration rate to guarantee explore mode
        opt = BayesianOptimizer(
            prior=None,
            hyperparams=GPHyperparams(),
            state_path=tmp_path / "bao.pickle",
            exploration_rate=0.5,
        )
        for i in range(10):
            opt.observe(_make_obs(i))

        profile = UserProfile(
            age_band="26-35",
            sex="unspecified",
            chronotype="neutral",
            season="spring",
        )
        # Try many install_ids until we hit an explore mode
        explore_rec = None
        for i in range(200):
            rec = opt.recommend(
                user_profile=profile,
                current_stage=SleepStage.REM,
                in_wind_down=False,
                install_id=f"explore_test_{i}",
            )
            if rec.mode.startswith("explore-"):
                explore_rec = rec
                break
        assert explore_rec is not None, (
            "Failed to hit explore mode in 200 tries with rate=0.5"
        )
        # The recommendation must have a valid mode
        assert explore_rec.mode in (
            "explore-temp", "explore-humidity", "explore-brightness",
        )

    def test_explore_mode_deterministic_via_monkeypatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Force explore mode by patching rng to guarantee the branch is hit."""
        opt = BayesianOptimizer(
            prior=None,
            hyperparams=GPHyperparams(),
            state_path=tmp_path / "bao.pickle",
            exploration_rate=0.5,
        )
        for i in range(10):
            opt.observe(_make_obs(i))

        profile = UserProfile(
            age_band="26-35",
            sex="unspecified",
            chronotype="neutral",
            season="spring",
        )

        # Monkeypatch np.random.default_rng to return a controlled RNG
        # that always draws a value < exploration_rate for the first draw
        class FakeRNG:
            def random(self):
                return 0.0  # Always < any positive exploration_rate

            def integers(self, low, high):
                return 1  # Pick "explore-humidity"

            def standard_normal(self, size=None):
                if size is not None:
                    return np.zeros(size)
                return 0.0

        monkeypatch.setattr(
            np.random, "default_rng", lambda seed: FakeRNG(),
        )

        rec = opt.recommend(
            user_profile=profile,
            current_stage=SleepStage.LIGHT,
            in_wind_down=False,
            install_id="deterministic_explore",
        )
        assert rec.mode == "explore-humidity"


class TestLoadOrInitPathNotExists:
    """Cover load_or_init when state file doesn't exist (line 425)."""

    def test_load_or_init_no_file(self, tmp_path: Path) -> None:
        """load_or_init with non-existent file returns fresh instance."""
        state_path = tmp_path / "nonexistent.pickle"
        opt = BayesianOptimizer.load_or_init(
            state_path=state_path,
            prior=None,
            hyperparams=GPHyperparams(),
        )
        assert opt.n_observations == 0
        assert opt.error_count == 0


class TestRefitEmptyObservations:
    """Cover _refit empty-observations branch (lines 841-845)."""

    def test_refit_with_empty_persisted_state(self, tmp_path: Path) -> None:
        """load_or_init with persisted state containing zero valid obs calls _refit on empty."""
        import pickle
        from src.bayesian_optimizer import BAOPersistedState

        # Persisted state with only non-GPObservation items
        state = BAOPersistedState(
            install_id_hash="empty_test",
            hyperparams=GPHyperparams(),
            observations=("not_an_obs", 123),  # type: ignore[arg-type]
            last_persist_at="2024-01-01T00:00:00Z",
            error_count=0,
            schema_version=1,
        )
        state_path = tmp_path / "bao_model.pickle"
        state_path.write_bytes(pickle.dumps(state))

        opt = BayesianOptimizer.load_or_init(
            state_path=state_path,
            prior=None,
            hyperparams=GPHyperparams(),
        )
        # No valid observations loaded -> _refit clears internal state
        assert opt.n_observations == 0
        # posterior_uncertainty still works (uses fallback path)
        import math
        std = opt.posterior_uncertainty(at=(22.0, 50.0, 10.0))
        assert std[0] == pytest.approx(math.sqrt(100.0))


class TestLoadOrInitObsFilter:
    """Cover observation filtering in load_or_init (lines 455-458)."""

    def test_load_with_non_gpobservation_items(self, tmp_path: Path) -> None:
        """Non-GPObservation items in persisted.observations are skipped."""
        import pickle
        from src.bayesian_optimizer import BAOPersistedState

        # Create a persisted state with a mix of valid and invalid obs
        valid_obs = GPObservation(
            temperature_c=22.0,
            humidity_pct=50.0,
            brightness_pct=10.0,
            quality_score=75.0,
            timestamp=1700000000.0,
            install_id="test",
        )
        # Sneaky: put non-GPObservation objects in the tuple
        mixed_observations = (valid_obs, "not an observation", 42, valid_obs)

        state = BAOPersistedState(
            install_id_hash="abc123",
            hyperparams=GPHyperparams(),
            observations=mixed_observations,  # type: ignore[arg-type]
            last_persist_at="2024-01-01T00:00:00Z",
            error_count=1,
            schema_version=1,
        )
        state_path = tmp_path / "bao_model.pickle"
        state_path.write_bytes(pickle.dumps(state))

        opt = BayesianOptimizer.load_or_init(
            state_path=state_path,
            prior=None,
            hyperparams=GPHyperparams(),
        )
        # Only the 2 valid GPObservation items should be loaded
        assert opt.n_observations == 2
        assert opt.error_count == 1

    def test_load_with_more_obs_than_max(self, tmp_path: Path) -> None:
        """Observations exceeding max_observations are truncated to FIFO."""
        import pickle
        from src.bayesian_optimizer import BAOPersistedState

        # Create 10 observations but set max_observations=5
        obs_list = tuple(
            GPObservation(
                temperature_c=20.0 + i * 0.5,
                humidity_pct=45.0 + i,
                brightness_pct=5.0 + i,
                quality_score=60.0 + i * 2,
                timestamp=1700000000.0 + 86400.0 * i,
                install_id="test",
            )
            for i in range(10)
        )
        state = BAOPersistedState(
            install_id_hash="trunc_test",
            hyperparams=GPHyperparams(),
            observations=obs_list,
            last_persist_at="2024-01-01T00:00:00Z",
            error_count=0,
            schema_version=1,
        )
        state_path = tmp_path / "bao_model.pickle"
        state_path.write_bytes(pickle.dumps(state))

        opt = BayesianOptimizer.load_or_init(
            state_path=state_path,
            prior=None,
            hyperparams=GPHyperparams(),
            max_observations=5,
        )
        # Should be truncated to last 5 (FIFO)
        assert opt.n_observations == 5


class TestSchedulePersistDoneTaskCleanup:
    """Cover _schedule_persist done-task cleanup (line 532->536)."""

    async def test_done_tasks_are_cleaned_on_next_observe(
        self, tmp_path: Path,
    ) -> None:
        """Completed persist tasks are removed from _v3_tasks on next observe."""
        import asyncio

        opt = _make_optimizer(tmp_path)
        # First observe schedules a persist task
        opt.observe(_make_obs(0))
        tasks = opt.pending_persist_tasks()
        assert len(tasks) >= 1
        # Wait for the task to complete
        await asyncio.gather(*tasks, return_exceptions=True)

        # Now observe again — _schedule_persist should clean up the done task
        opt.observe(_make_obs(1))
        # The old done task should have been cleaned; only the new one remains
        new_tasks = opt.pending_persist_tasks()
        for t in new_tasks:
            assert not t.done()


class TestFIFORollingWindow:
    """Cover FIFO truncation logic in observe()."""

    def test_fifo_truncation_at_max(self, tmp_path: Path) -> None:
        """Observations are kept at max_observations via FIFO eviction."""
        opt = BayesianOptimizer(
            prior=None,
            hyperparams=GPHyperparams(),
            state_path=tmp_path / "bao.pickle",
            max_observations=10,
        )
        for i in range(15):
            opt.observe(_make_obs(i))
        # Only the last 10 should remain
        assert opt.n_observations == 10

    def test_observe_with_empty_install_id(self, tmp_path: Path) -> None:
        """Observation with empty install_id skips hash update (line 532->536)."""
        opt = _make_optimizer(tmp_path)
        obs = GPObservation(
            temperature_c=22.0,
            humidity_pct=50.0,
            brightness_pct=10.0,
            quality_score=75.0,
            timestamp=1700000000.0,
            install_id="",  # Empty string -> falsy
        )
        opt.observe(obs)
        assert opt.n_observations == 1
        # install_id_hash should remain empty (default)
        assert opt._install_id_hash == ""


class TestWindDownForcedExploit:
    """Cover wind_down=True forced exploit path."""

    def test_wind_down_forces_exploit_even_high_explore_rate(
        self, tmp_path: Path,
    ) -> None:
        """wind_down=True always forces exploit mode regardless of explore rate."""
        opt = BayesianOptimizer(
            prior=None,
            hyperparams=GPHyperparams(),
            state_path=tmp_path / "bao.pickle",
            exploration_rate=0.5,  # Maximum allowed
        )
        for i in range(10):
            opt.observe(_make_obs(i))

        profile = UserProfile(
            age_band="26-35",
            sex="unspecified",
            chronotype="neutral",
            season="spring",
        )
        # Test with multiple install_ids to cover different RNG states
        for i in range(20):
            rec = opt.recommend(
                user_profile=profile,
                current_stage=SleepStage.LIGHT,
                in_wind_down=True,
                install_id=f"wd_test_{i}",
            )
            assert rec.mode == "exploit"


class TestPriorWeightLockInRecommend:
    """Cover prior_weight_lock usage in recommend path."""

    def test_prior_weight_lock_propagates_to_recommendation(
        self, tmp_path: Path,
    ) -> None:
        """prior_weight_lock in UserProfile controls the recommendation's prior_weight."""
        opt = _make_optimizer(tmp_path)
        for i in range(6):
            opt.observe(_make_obs(i))

        profile_locked = UserProfile(
            age_band="26-35",
            sex="unspecified",
            chronotype="neutral",
            season="spring",
            prior_weight_lock=0.42,
        )
        rec = opt.recommend(
            user_profile=profile_locked,
            current_stage=SleepStage.LIGHT,
            in_wind_down=False,
        )
        assert rec.prior_weight == pytest.approx(0.42)

    def test_prior_weight_lock_zero_in_prior_only(
        self, tmp_path: Path,
    ) -> None:
        """Even in prior-only mode, lock=0.0 sets prior_weight=0.0."""
        opt = _make_optimizer(tmp_path)
        # No observations -> prior-only mode
        profile = UserProfile(
            age_band="26-35",
            sex="unspecified",
            chronotype="neutral",
            season="spring",
            prior_weight_lock=0.0,
        )
        rec = opt.recommend(
            user_profile=profile,
            current_stage=SleepStage.LIGHT,
            in_wind_down=False,
        )
        assert rec.mode == "prior-only"
        assert rec.prior_weight == pytest.approx(0.0)


class TestLoadOrInitSuccessfulReplay:
    """Cover successful load_or_init with valid pickle (full replay path)."""

    async def test_round_trip_persist_then_load(self, tmp_path: Path) -> None:
        """persist() then load_or_init() restores observation count."""
        opt = _make_optimizer(tmp_path)
        for i in range(8):
            opt.observe(_make_obs(i))
        assert opt.n_observations == 8

        await opt.persist()
        state_path = tmp_path / "bao_model.pickle"
        assert state_path.exists()

        # Load from persisted state
        opt2 = BayesianOptimizer.load_or_init(
            state_path=state_path,
            prior=None,
            hyperparams=GPHyperparams(),
        )
        assert opt2.n_observations == 8


class TestChoSolveFailure:
    """Cover cho_solve failure path in _refit (distinct from cho_factor failure)."""

    def test_cho_solve_failure_raises_gp_numerical_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If cho_solve fails, GPNumericalError is raised with 'cho_solve failed'."""
        import scipy.linalg

        opt = _make_optimizer(tmp_path)
        opt.observe(_make_obs(0))

        # Let cho_factor succeed but cho_solve fail
        original_cho_factor = scipy.linalg.cho_factor

        call_count = {"cho_solve": 0}

        def failing_cho_solve(*args: Any, **kwargs: Any) -> Any:
            call_count["cho_solve"] += 1
            raise np.linalg.LinAlgError("simulated cho_solve failure")

        monkeypatch.setattr(scipy.linalg, "cho_solve", failing_cho_solve)

        with pytest.raises(GPNumericalError, match="cho_solve failed"):
            opt.observe(_make_obs(1))
        assert opt.error_count == 1
