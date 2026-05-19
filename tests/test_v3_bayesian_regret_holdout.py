"""Property 3 — BAO 28 晚累积 regret holdout 评估（slow）。

**Validates: Requirements 3.4**

测试目标
--------

`requirements.md` R3.4 / `tasks.md` Task 10.8 / `design.md` §3.8.3 要求：
在合成已知最优场景下，v3.x BAO（GP + Thompson Sampling）的 28 晚累积
regret 至少比 v2.x ``PreferenceLearner.recommend`` 加权中位数路径低
30%。

合成环境（与 ``scripts/eval_bayesian_regret.py`` 保持完全一致）
---------------------------------------------------------------

* 真值函数 ``q(x) = 100 · exp(-0.5 · Σ_d ((x_d - x*_d) / l_d)²)``。
* 最优点 ``x* = (T=21°C, H=50%, L=10%)``，落在 P6 生理区间内。
* 长度尺度 ``(1.5, 8.0, 15.0)`` —— 与 BAO 默认 RBF kernel 对齐。
* 加性高斯噪声 ``ε ∼ N(0, 5²)``；同一晚的噪声样本对两个方法保持一致
  （paired 比较，最大化方差抵消）。
* v2.x baseline 默认 setpoint ``(22, 45, 5)``，故意偏离最优点，让加权
  中位数路径必须真正"学"才能赶上 BAO，避免比较退化为「v2.x 第 1 晚就
  锁在最优」。

实验设计
--------

* 100 个独立 RNG 种子（``20260518 .. 20260617``），每个种子独立跑一对
  v3.x + v2.x 模拟，把累积 regret 收集起来。
* 报告 ``mean(v3_regret)`` vs ``mean(v2_regret)``；100 次种子平均把 GP
  Thompson Sampling 的随机抖动压下去，确保结果是统计稳健的。
* 断言 ``mean(v3_regret) ≤ 0.7 · mean(v2_regret)``，即 v3.x 至少便宜
  30%。

为何不复用 ``scripts/eval_bayesian_regret.py``
-----------------------------------------------

该脚本顶层导入 matplotlib（虽然是软依赖），并把每次运行包装成可写
PNG / Markdown 的 CLI 命令；本测试只需要数值 regret 结果，因此把
``_run_v3`` / ``_run_baseline_v2x`` 的核心逻辑重新内联，避免被脚本里
为了产文档而引入的额外依赖污染（与 task 10.8 prompt 中的明确指引
一致）。

性能预算
--------

100 seeds × 28 nights × 2 methods = 5600 模拟夜，BAO ``observe`` 在
开发机上 ≤ 数毫秒（≤ 60 个观测的 cholesky 直接重算）；
:class:`PreferenceLearner` 每晚一次 ``atomic_write_json``。整体在
开发机上约 30–60 秒——明显超出 ``pytest --timeout=60`` 默认门限，故
该测试用 ``@pytest.mark.timeout(600)`` 把上限放宽到 10 分钟（与 CI
``slow`` 矩阵 ``--timeout=600`` 一致，task 11.3）。

:Validates: Requirements 3.4
"""
from __future__ import annotations

import math
import random
from pathlib import Path

import pytest

from src.bayesian_optimizer import (
    BayesianOptimizer,
    GPHyperparams,
    GPNumericalError,
    GPObservation,
    UserProfile,
)
from src.data_structures import SleepStage
from src.preference_learner import (
    EnvironmentParams,
    PreferenceConfig,
    PreferenceLearner,
    SleepSession,
)


# ---------------------------------------------------------------------------
# Synthetic RBF environment (verbatim from scripts/eval_bayesian_regret.py
# §"Synthetic environment").  Centralised here so the constants stay in
# sync with the design document and the offline evaluator.
# ---------------------------------------------------------------------------

#: True optimum point (T °C, H %, L %).
_X_OPT: tuple[float, float, float] = (21.0, 50.0, 10.0)

#: RBF length scales — match :data:`src.bayesian_optimizer._DEFAULT_LENGTH_*`.
_LENGTHS: tuple[float, float, float] = (1.5, 8.0, 15.0)

#: Quality at the true optimum (clean).  Cumulative regret therefore
#: reads in "% of optimum" units, with oracle path = 0.
_Q_MAX: float = 100.0

#: Additive observation noise σ (quality units).  Matches BAO's
#: ``_DEFAULT_NOISE_VARIANCE = 25`` (σ = √25 = 5) and the v2.x
#: weighted-median empirical jitter.
_NOISE_STD: float = 5.0

#: v2.x baseline default setpoint — picked deliberately *off* the
#: optimum so the comparison does not degenerate to "v2.x always picks
#: the optimum from night 1".
_DEFAULT_BASELINE_ENV: tuple[float, float, float, float] = (22.0, 45.0, 5.0, 0.0)


def _true_quality(x: tuple[float, float, float]) -> float:
    """Return noise-free quality of setpoint ``x`` (0..100).

    :param x: 3-tuple ``(temperature_c, humidity_pct, brightness_pct)``.
    :returns: ``100 · exp(-0.5 · Σ_d ((x_d - x*_d) / l_d)²)``.
    """
    sq = sum(
        ((xv - opt) / l) ** 2
        for xv, opt, l in zip(x, _X_OPT, _LENGTHS, strict=True)
    )
    return _Q_MAX * math.exp(-0.5 * sq)


def _cumulative_regret(qualities: list[float]) -> float:
    """Return final cumulative regret ``Σ (Q_max - q_t)`` over the run.

    Cumulative regret uses the **noise-free** quality on purpose; the
    optimiser learns from noisy observations but the regret metric is
    the deterministic distance from the oracle.  Otherwise the metric
    would jitter with the noise stream and obscure the learning curve.
    """
    return sum((_Q_MAX - q) for q in qualities)


# ---------------------------------------------------------------------------
# v3.x — BayesianOptimizer (GP + Thompson Sampling)
# ---------------------------------------------------------------------------

def _run_v3(nights: int, noises: list[float], seed: int, state_path: Path
            ) -> list[float]:
    """Drive :class:`BayesianOptimizer` for ``nights`` nights.

    :param nights: simulation horizon.
    :param noises: pre-drawn noise samples shared with the v2.x
        baseline — ensures paired comparison.
    :param seed: forwarded into the per-night ``install_id`` so the
        BAO Thompson-Sampling RNG (R2.6, ``sha256(install_id +
        ISO-date)``) differs between nights.
    :param state_path: per-seed pickle path under the test's
        ``tmp_path`` so concurrent seeds do not race on the same file.
    :returns: per-night noise-free quality at the chosen point.
    """
    bao = BayesianOptimizer.load_or_init(
        state_path=state_path,
        prior=None,
        hyperparams=GPHyperparams(),
    )
    # Empty profile ⇒ BAO falls back to its inert neutral defaults
    # (``_FALLBACK_PRIOR`` in :mod:`src.bayesian_optimizer`).
    user_profile = UserProfile(
        age_band="", sex="", chronotype="", season="spring",
    )
    qualities: list[float] = []
    for t in range(nights):
        install_id = f"holdout-{seed}-{t}"
        rec = bao.recommend(
            user_profile=user_profile,
            current_stage=SleepStage.LIGHT,
            in_wind_down=False,
            install_id=install_id,
        )
        x = (rec.temperature_c, rec.humidity_pct, rec.brightness_pct)
        q_true = _true_quality(x)
        # Optimiser sees noisy quality clipped to the score range; the
        # regret metric uses the noise-free value.
        q_obs = max(0.0, min(100.0, q_true + noises[t]))
        qualities.append(q_true)
        try:
            bao.observe(GPObservation(
                temperature_c=x[0],
                humidity_pct=x[1],
                brightness_pct=x[2],
                quality_score=q_obs,
                timestamp=float(t),
                install_id=install_id,
            ))
        except GPNumericalError:
            # In production the orchestrator falls back to v2.x and
            # bumps ``error_count``; in this property test we simply
            # skip the observation so the next night still runs.
            # Cholesky failures on a 28-night RBF posterior are
            # vanishingly rare but the contract is part of R1.4.
            continue
    return qualities


# ---------------------------------------------------------------------------
# v2.x — PreferenceLearner weighted median
# ---------------------------------------------------------------------------

def _run_v2(nights: int, noises: list[float], seed: int, history_path: Path
            ) -> list[float]:
    """Drive :class:`PreferenceLearner` weighted-median path for ``nights``.

    :param nights: simulation horizon.
    :param noises: paired noise stream shared with :func:`_run_v3`.
    :param seed: feeds an isolated :class:`random.Random` so the (here
        unused) ``explore=False`` branch stays deterministic.
    :param history_path: per-seed JSON path under ``tmp_path``; the
        learner persists each session via ``atomic_write_json`` so a
        shared path would race across seeds.
    :returns: per-night noise-free quality at the chosen point.
    """
    cfg = PreferenceConfig(
        history_path=str(history_path),
        # Bump the FIFO cap so the 28-night window fits without
        # truncation — keeps the comparison apples-to-apples with BAO
        # (which has its own 60-night FIFO).
        max_sessions_kept=max(200, nights * 2),
    )
    pl = PreferenceLearner(cfg, rng=random.Random(seed))
    defaults = EnvironmentParams(
        temperature_c=_DEFAULT_BASELINE_ENV[0],
        humidity_pct=_DEFAULT_BASELINE_ENV[1],
        brightness_pct=_DEFAULT_BASELINE_ENV[2],
        fan_speed_pct=_DEFAULT_BASELINE_ENV[3],
    )
    qualities: list[float] = []
    for t in range(nights):
        # ``now_ts`` advances by 1 day per simulated night so the v2.x
        # exponential decay weight (default 14-day half-life) actually
        # decays across the simulation.
        rec = pl.recommend(defaults, explore=False, now_ts=float(t * 86400))
        x = (
            rec.temperature_c if rec.temperature_c is not None
            else defaults.temperature_c,
            rec.humidity_pct if rec.humidity_pct is not None
            else defaults.humidity_pct,
            rec.brightness_pct if rec.brightness_pct is not None
            else defaults.brightness_pct,
        )
        q_true = _true_quality(x)
        q_obs = max(0.0, min(100.0, q_true + noises[t]))
        qualities.append(q_true)

        sess = SleepSession(
            session_id=f"holdout-{seed}-{t:03d}",
            started_at=float(t * 86400),
            ended_at=float(t * 86400 + 8 * 3600),
            env_params=EnvironmentParams(
                temperature_c=x[0],
                humidity_pct=x[1],
                brightness_pct=x[2],
                fan_speed_pct=0.0,
            ),
            stage_counts={"AWAKE": 30, "LIGHT": 240, "DEEP": 90, "REM": 60},
            quality_score=q_obs,
            n_samples=420,
            recorded_at=float(t * 86400 + 8 * 3600),
        )
        pl.record_session(sess)
    return qualities


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------

@pytest.mark.slow
@pytest.mark.timeout(600)
def test_property_p3_regret_at_least_30pct_lower_than_v2(
    tmp_path: Path,
) -> None:
    """Property 3 — v3.x BAO 28 晚累积 regret 比 v2.x 中位数低 ≥ 30%。

    **Validates: Requirements 3.4**

    跑 100 个独立种子 × 28 晚 × {v3.x, v2.x} 的 paired 模拟，平均累积
    regret 后断言 ``v3_mean ≤ 0.7 × v2_mean``。100 次种子平均把 BAO
    的 Thompson Sampling 随机抖动压到统计稳健水平；任意单种子的曲线
    可能因为「头几晚撞到坏 sample」而把 v3 推高，但平均下来 GP 后验
    必然主导。

    断言失败时打印 mean / median / per-seed 序列，便于调试是 GP 模型
    退化、合成环境意外飘移、还是 PreferenceLearner 的 weighted median
    路径意外击穿了 BAO。
    """
    n_seeds = 100
    n_nights = 28
    base_seed = 20260518  # 与项目其它 R15.5 训练 / 评估脚本默认种子一致

    v3_regrets: list[float] = []
    v2_regrets: list[float] = []

    for i in range(n_seeds):
        seed = base_seed + i
        # Single noise stream shared between the two methods so the
        # comparison is paired (variance cancellation).
        noise_rng = random.Random(seed)
        noises = [noise_rng.gauss(0.0, _NOISE_STD) for _ in range(n_nights)]

        v3_state_path = tmp_path / f"bao_state_{seed}.pickle"
        v2_history_path = tmp_path / f"prefs_{seed}.json"

        v3_q = _run_v3(n_nights, noises, seed, v3_state_path)
        v2_q = _run_v2(n_nights, noises, seed, v2_history_path)

        v3_regrets.append(_cumulative_regret(v3_q))
        v2_regrets.append(_cumulative_regret(v2_q))

    assert len(v3_regrets) == n_seeds
    assert len(v2_regrets) == n_seeds

    v3_mean = sum(v3_regrets) / n_seeds
    v2_mean = sum(v2_regrets) / n_seeds

    # The threshold "30% lower" is exactly the R3.4 / Property 3 wording.
    threshold = 0.7 * v2_mean
    assert v3_mean <= threshold, (
        f"Property 3 violated: v3.x mean cumulative regret over "
        f"{n_seeds} seeds × {n_nights} nights = {v3_mean:.2f}, "
        f"but v2.x mean = {v2_mean:.2f} (0.7 × v2 = {threshold:.2f}). "
        f"Expected at least 30% reduction."
    )
