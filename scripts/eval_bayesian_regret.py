"""离线评估 v3.0.0 BAO（GP+TS）vs v2.x 加权中位数的累积 regret —— R3.3 / R15.

This script is the offline evaluator for Requirement 3.3 / Requirement 15 in
``.kiro/specs/algorithmic-moat-v3.0.0/requirements.md``: it runs a paired
N-night simulation on a synthetic RBF environment with a known optimum,
records both the v3.x ``BayesianOptimizer.recommend`` decision path and a
chosen baseline (v2.x ``PreferenceLearner.recommend`` weighted median /
uniform random / optimal oracle), and emits two artifacts whose filenames
carry the repo's git short SHA (R15.5)::

    <out-prefix>_regret_curve_<sha7>.png    # cumulative regret plot
    <out-prefix>_regret_summary_<sha7>.md   # markdown summary table

CLI contract (R3.3 / design.md §3.8.3)::

    --user-prefs   <path>     /data/user_preferences.json (sanitised OK)
    --baseline     {v2.x,random,optimal_oracle}  default: v2.x
    --nights       <int>      default: 28
    --seed         <int>      default: 20260518
    --out-prefix   <str>      default: regret
    --out-dir      <path>     default: current working directory

Exit codes
----------

* ``0`` — OK.
* ``1`` — invalid arguments (missing user-prefs, non-positive nights).

matplotlib is intentionally a soft dependency (it ships in
``requirements-train.txt`` but **not** in ``requirements-runtime.txt`` —
PR4 / R12.5 isolation contract).  When the import fails the evaluator
prints a warning and skips the PNG; the markdown summary is always
emitted so the script remains usable in a stripped-down CI matrix.

Theoretical regret bound
------------------------

The summary always includes the paragraph required by R3.5 / design.md
§3.2.4::

    在 RBF kernel + 加性高斯噪声假设下，GP-UCB regret bound 为
    O(sqrt(T log T))；本评估的实测 v3 regret = X，v2.x baseline regret = Y。

The bound itself is *illustrative*: Srinivas et al. (2010) prove
``R_T ≤ O(sqrt(T · β_T · γ_T))`` with ``γ_T = O((log T)^{d+1})`` for an RBF
kernel in ``d`` dimensions.  We deliberately do not paint a numerical
bound on the plot to avoid implying tighter constants than the IID
assumption supports; the markdown text is the authoritative statement.

Synthetic environment
---------------------

* True optimum at ``(T=21 °C, H=50 %, L=10 %)`` — chosen to land inside
  the P6 physiological range and the BAO default candidate grid.
* Length scales ``(1.5, 8.0, 15.0)`` — same as
  :data:`src.bayesian_optimizer._DEFAULT_LENGTH_*`.
* Quality function ``q(x) = 100 · exp(-0.5 · Σ_d ((x_d - x*_d) / l_d)²)``.
* Additive noise ``ε ∼ N(0, 5²)``; the same noise sample is reused for
  both methods at the same night index (paired comparison) so that
  variance cancellation is maximised.

Repeatability
-------------

* The synthetic noise stream is seeded with ``--seed`` (default
  ``20260518``, R15.5).
* The BAO per-decision RNG is seeded with
  ``sha256(install_id + ISO-date)`` per its R2.6 contract.  We vary
  ``install_id`` per simulated night (``f"eval-{seed}-{t}"``) so that
  the 28 decisions do not share a single Bernoulli draw.
* The baseline ``PreferenceLearner`` is given a dedicated
  :class:`random.Random(seed)` so its ``explore=False`` branch is fully
  deterministic (it does not actually use the RNG when ``explore=False``,
  but the wiring is there for future variants).

:Validates: Requirements 3.3, 3.5, 15.2, 15.5
"""
from __future__ import annotations

import argparse
import logging
import math
import random
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Make ``src`` importable when running from the repo root.  Mirrors the
# convention used by ``scripts/train_population_prior.py`` /
# ``scripts/check_artifacts.py``.
# ---------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
_REPO_ROOT_STR = str(REPO_ROOT)
if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)

logger = logging.getLogger("eval_bayesian_regret")


# ---------------------------------------------------------------------------
# Constants — keep aligned with src.bayesian_optimizer + design.md §3.2 /
# §3.8.3 + requirements.md R3 / R15.
# ---------------------------------------------------------------------------

#: Default RNG seed (R15.5). Same value used by every other R15 / training
#: artifact in the repo so that failed-build forensics can correlate runs.
DEFAULT_SEED: int = 20260518

#: Default simulation horizon (R3.4 / R3.3).  28 nights = 4 weeks, the
#: convergence claim made by README / docs/algorithm_evaluation.md.
DEFAULT_NIGHTS: int = 28

#: Default ``--out-prefix`` value; produces ``regret_regret_curve_<sha7>.png``
#: + ``regret_regret_summary_<sha7>.md``.
DEFAULT_OUT_PREFIX: str = "regret"

#: ``--baseline`` choices.  ``v2.x`` is the production-comparable path
#: (weighted-median ``PreferenceLearner.recommend``); ``random`` is a
#: sanity-check upper bound (a non-learning strategy should be much
#: worse); ``optimal_oracle`` is a sanity-check lower bound (zero
#: regret by definition).
BASELINE_CHOICES: tuple[str, ...] = ("v2.x", "random", "optimal_oracle")

#: True optimum point on the synthetic environment (T °C, H %, L %).
_X_OPT: tuple[float, float, float] = (21.0, 50.0, 10.0)

#: Length scales of the synthetic RBF quality function.  Match the BAO
#: defaults so the optimiser's candidate grid covers ±1 length scale.
_LENGTHS: tuple[float, float, float] = (1.5, 8.0, 15.0)

#: Decision bounds (T °C, H %, L %).  Used by the random baseline + the
#: P6 physiological gate.  Brightness 50 % is the upper P6 bound.
_BOUNDS: tuple[tuple[float, float], ...] = (
    (16.0, 28.0), (30.0, 70.0), (0.0, 50.0),
)

#: Quality at the true optimum (clean).  100 ⇒ regret is reported in
#: "% of optimum" units so ``cumulative regret == 0`` ⇔ oracle path.
_Q_MAX: float = 100.0

#: Additive observation noise standard deviation (in quality units).
#: 5 quality points / night matches the v2.x weighted-median empirical
#: jitter and the ``_DEFAULT_NOISE_VARIANCE = 25`` BAO hyperparameter
#: (σ = sqrt(25) = 5).
_NOISE_STD: float = 5.0

#: Defaults handed to ``PreferenceLearner.recommend`` for the v2.x
#: baseline.  Picked deliberately *off* the true optimum so the v2.x
#: path has to actually learn from the recorded sessions to catch up;
#: otherwise the comparison would degenerate to "v2.x always picks
#: the optimum from night 1".
_DEFAULT_BASELINE_ENV: tuple[float, float, float, float] = (22.0, 45.0, 5.0, 0.0)


# ---------------------------------------------------------------------------
# matplotlib soft dependency (PR4 / R12.5)
# ---------------------------------------------------------------------------

try:
    import matplotlib  # type: ignore[import-not-found]
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # type: ignore[import-not-found]
    _HAS_MATPLOTLIB: bool = True
except ImportError:
    matplotlib = None  # type: ignore[assignment]
    plt = None  # type: ignore[assignment]
    _HAS_MATPLOTLIB = False


# ---------------------------------------------------------------------------
# Synthetic environment + git provenance helpers
# ---------------------------------------------------------------------------

def _true_quality(x: tuple[float, float, float]) -> float:
    """Return the noise-free quality of setpoint ``x``.

    :param x: 3-tuple ``(temperature_c, humidity_pct, brightness_pct)``.
    :returns: ``100 · exp(-0.5 · Σ_d ((x_d - x*_d) / l_d)²)`` ∈ [0, 100].

    The function peaks at :data:`_X_OPT` with value :data:`_Q_MAX` and
    decays as a Gaussian with anisotropic length scales :data:`_LENGTHS`.
    Matches the kernel used by :class:`src.bayesian_optimizer.BayesianOptimizer`
    so the GP has a well-specified target — the regret comparison is
    therefore a "best case" for v3.x and any v2.x deficit is purely a
    consequence of weighted-median lacking a posterior.
    """
    sq = sum(
        ((xv - opt) / l) ** 2
        for xv, opt, l in zip(x, _X_OPT, _LENGTHS, strict=True)
    )
    return _Q_MAX * math.exp(-0.5 * sq)


def _git_short_sha() -> str:
    """Return the 7-char git SHA of the repo, or ``"unknown"``.

    Mirrors :func:`scripts.train_population_prior._git_short_sha` so the
    R15.5 filename suffix is consistent across the v3.0.0 evaluator
    suite.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if out.returncode == 0:
            sha = out.stdout.strip()
            if sha:
                return sha
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _cumulative_regret(qualities: list[float]) -> list[float]:
    """Return the per-night cumulative regret series.

    :param qualities: Per-night noise-free quality at the chosen point.
    :returns: ``[Σ_{i ≤ t} (Q_max - q_i)]`` for ``t = 1..len(qualities)``.

    Cumulative regret uses the **noise-free** quality on purpose; the
    optimiser learns from noisy observations but the regret metric we
    report is the deterministic distance from the oracle (otherwise
    the comparison would jitter with the noise stream and obscure the
    learning curve).
    """
    cum: list[float] = []
    total = 0.0
    for q in qualities:
        total += (_Q_MAX - q)
        cum.append(total)
    return cum


# ---------------------------------------------------------------------------
# v3.x — BAO (GP + Thompson Sampling)
# ---------------------------------------------------------------------------

def _run_v3(nights: int, noises: list[float], seed: int) -> list[float]:
    """Run the v3.x BAO path for ``nights`` nights.

    :param nights: Simulation horizon.
    :param noises: Pre-drawn noise samples, one per night.
    :param seed: Forwarded into the per-night ``install_id`` so the
        BAO per-decision RNG (R2.6, ``sha256(install_id + ISO-date)``)
        differs between nights even when the script runs in < 1 s of
        wall clock.
    :returns: List of per-night noise-free quality at the chosen point.
    """
    # Lazy imports so ``--help`` does not trigger numpy / scipy load.
    from src.bayesian_optimizer import (
        BayesianOptimizer,
        GPHyperparams,
        GPNumericalError,
        GPObservation,
        UserProfile,
    )
    from src.data_structures import SleepStage

    tmp = Path(tempfile.mkdtemp(prefix="eval_bayesian_regret_v3_"))
    state_path = tmp / "bao_model.pickle"
    bao = BayesianOptimizer.load_or_init(
        state_path=state_path,
        prior=None,
        hyperparams=GPHyperparams(),
    )

    # Empty profile → BAO falls back to neutral defaults internally
    # (see :meth:`BayesianOptimizer._lookup_prior_bucket`).
    user_profile = UserProfile(
        age_band="", sex="", chronotype="", season="spring",
    )

    qualities: list[float] = []
    for t in range(nights):
        install_id = f"eval-{seed}-{t}"
        rec = bao.recommend(
            user_profile=user_profile,
            current_stage=SleepStage.LIGHT,
            in_wind_down=False,
            install_id=install_id,
        )
        x = (rec.temperature_c, rec.humidity_pct, rec.brightness_pct)
        q_true = _true_quality(x)
        # Optimiser sees noisy quality; regret is computed on the
        # noise-free value (see :func:`_cumulative_regret`).
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
        except GPNumericalError as exc:
            # In a real add-on the orchestrator falls back to v2.x and
            # bumps ``error_count``; in the evaluator we just log and
            # skip the observation so the next night still runs.
            logger.warning(
                "BAO numerical error at night %d: %s — skipped observation.",
                t, exc,
            )
    return qualities


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------

def _run_baseline_v2x(
    nights: int,
    noises: list[float],
    user_prefs_src: Path,
    seed: int,
) -> list[float]:
    """Run the v2.x ``PreferenceLearner.recommend`` path for ``nights`` nights.

    The input ``user_prefs_src`` is copied into a private temp dir so
    the simulation's :meth:`PreferenceLearner.record_session` calls
    never mutate the user's real history file.  An empty-dict input
    file is tolerated — :meth:`PreferenceLearner._load` treats
    ``raw.get("sessions", [])`` on an empty mapping as an empty
    history, returning the ``defaults`` argument from
    :meth:`PreferenceLearner.recommend` until enough sessions have
    been recorded.
    """
    from src.preference_learner import (
        EnvironmentParams,
        PreferenceConfig,
        PreferenceLearner,
        SleepSession,
    )

    tmp = Path(tempfile.mkdtemp(prefix="eval_bayesian_regret_v2_"))
    history_path = tmp / "user_preferences.json"
    if user_prefs_src.exists():
        # PreferenceLearner._load uses ``open(..., encoding="utf-8")``
        # which raises on a UTF-8 BOM (Windows ``Out-File -Encoding
        # utf8`` writes a BOM by default).  Re-encode the file as
        # plain UTF-8 in the temp dir so the simulation does not
        # depend on the .bak fallback path.
        try:
            raw_text = user_prefs_src.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "Could not read --user-prefs (%s); v2.x baseline will "
                "start from defaults.", exc,
            )
            raw_text = "{}"
        history_path.write_text(raw_text, encoding="utf-8")

    cfg = PreferenceConfig(
        history_path=str(history_path),
        # Bump cap so the entire 28-night simulation fits without FIFO
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
            rec.temperature_c
            if rec.temperature_c is not None else defaults.temperature_c,
            rec.humidity_pct
            if rec.humidity_pct is not None else defaults.humidity_pct,
            rec.brightness_pct
            if rec.brightness_pct is not None else defaults.brightness_pct,
        )
        q_true = _true_quality(x)
        q_obs = max(0.0, min(100.0, q_true + noises[t]))
        qualities.append(q_true)

        sess = SleepSession(
            session_id=f"eval-{t:03d}",
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


def _run_baseline_random(
    nights: int, noises: list[float], seed: int,
) -> list[float]:
    """Uniform-random baseline within :data:`_BOUNDS`.

    This is a sanity-check upper bound on regret: a non-learning
    strategy should never beat either v3.x or v2.x.  ``noises`` is
    accepted (and unused) so the function shares the same signature as
    :func:`_run_baseline_v2x`, simplifying the dispatcher in :func:`main`.
    """
    rng = random.Random(seed + 1)
    _ = noises  # paired noise stream not needed for the regret metric
    qualities: list[float] = []
    for _t in range(nights):
        x = (
            rng.uniform(*_BOUNDS[0]),
            rng.uniform(*_BOUNDS[1]),
            rng.uniform(*_BOUNDS[2]),
        )
        qualities.append(_true_quality(x))
    return qualities


def _run_baseline_oracle(nights: int) -> list[float]:
    """Optimal-oracle baseline: always picks :data:`_X_OPT`.

    Cumulative regret of this baseline is therefore identically zero;
    it is exposed for sanity-checking the regret integrator.
    """
    return [_Q_MAX] * nights


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------

def _write_png(
    out_path: Path,
    nights: int,
    v3_cum: list[float],
    base_cum: list[float],
    baseline_label: str,
) -> bool:
    """Emit the cumulative-regret PNG.  Returns ``True`` if written.

    Matplotlib is a soft dep — when unavailable the function returns
    ``False`` and the caller falls back to a markdown-only summary.
    """
    if not _HAS_MATPLOTLIB:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8, 5), dpi=120)
    xs = list(range(1, nights + 1))
    ax.plot(xs, v3_cum, label="v3.x (BAO GP+TS)",
            color="tab:blue", linewidth=2)
    ax.plot(xs, base_cum, label=f"{baseline_label} baseline",
            color="tab:orange", linewidth=2, linestyle="--")
    ax.set_xlabel("Night #")
    ax.set_ylabel("Cumulative regret (quality units, lower is better)")
    # Plot title is intentionally ASCII-only to avoid matplotlib's
    # "Glyph missing from font(s) DejaVu Sans" warnings on default
    # CI installations (Chinese fonts are not bundled with the
    # matplotlib wheel). The R3.5 mandated Chinese statement still
    # lives verbatim in the markdown summary.
    ax.set_title(
        "Cumulative regret: v3.x BAO vs "
        f"{baseline_label} baseline\n"
        "(GP-UCB bound holds under RBF kernel + additive Gaussian noise)"
    )
    ax.grid(True, alpha=0.3)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return True


def _write_md(
    out_path: Path,
    *,
    nights: int,
    v3_cum: list[float],
    base_cum: list[float],
    baseline_label: str,
    seed: int,
    user_prefs: Path,
    sha7: str,
    png_path: Path,
    png_written: bool,
) -> None:
    """Emit the markdown regret summary.

    The summary always includes the R3.5 / design.md §3.2.4 paragraph::

        在 RBF kernel + 加性高斯噪声假设下，GP-UCB regret bound 为
        O(sqrt(T log T))；本评估的实测 v3 regret = X，v2.x baseline
        regret = Y。

    The numerical bound is intentionally not painted on the PNG to
    avoid implying tighter constants than the IID assumption supports;
    Srinivas et al. (2010) prove ``R_T ≤ O(sqrt(T · β_T · γ_T))`` with
    ``γ_T = O((log T)^{d+1})`` for an RBF kernel in ``d`` dimensions.
    """
    v3_final = v3_cum[-1] if v3_cum else 0.0
    base_final = base_cum[-1] if base_cum else 0.0
    if base_final > 1e-9:
        relative = (1.0 - v3_final / base_final) * 100.0
    else:
        relative = float("nan")

    lines: list[str] = []
    lines.append(f"# Bayesian Optimizer Regret 评估报告（{sha7}）")
    lines.append("")
    lines.append(
        "> 由 `scripts/eval_bayesian_regret.py` 生成；该脚本对比 v2.x "
        "加权中位数与 v3.x GP+TS 的累积 regret（R3.3 / R15.2 / R15.5）。"
    )
    lines.append("")
    lines.append("## 1. 输入参数")
    lines.append("")
    lines.append(f"- `--user-prefs`：`{user_prefs}`")
    lines.append(f"- `--baseline`：`{baseline_label}`")
    lines.append(f"- `--nights`：{nights}")
    lines.append(f"- `--seed`：{seed}")
    lines.append(f"- git commit：`{sha7}`")
    lines.append("")
    lines.append("## 2. 累积 regret 总结")
    lines.append("")
    lines.append("| 方法 | 最终累积 regret（{n} 晚） |".format(n=nights))
    lines.append("|---|---|")
    lines.append(f"| v3.x（BAO GP+TS） | {v3_final:.2f} |")
    lines.append(f"| {baseline_label}（baseline） | {base_final:.2f} |")
    if math.isfinite(relative):
        lines.append(
            f"| **相对改善（lower is better）** | **{relative:.1f} %** |"
        )
    lines.append("")
    lines.append("## 3. GP-UCB 理论上界")
    lines.append("")
    lines.append(
        f"在 RBF kernel + 加性高斯噪声假设下，GP-UCB regret bound 为 "
        f"O(sqrt(T log T))；本评估的实测 v3 regret = {v3_final:.2f}，"
        f"{baseline_label} baseline regret = {base_final:.2f}。"
    )
    lines.append("")
    lines.append(
        "> 引用：Srinivas et al., 2010, *Gaussian Process Optimization "
        "in the Bandit Setting: No Regret and Experimental Design*，ICML。"
        "原始结果为 ``R_T ≤ O(sqrt(T · β_T · γ_T))``，其中 RBF kernel "
        "在 ``d`` 维下 ``γ_T = O((log T)^(d+1))``；本报告不在曲线图上画"
        "数值上界，避免在非 IID 部署条件下夸大常数。"
    )
    lines.append("")
    lines.append("## 4. 输出文件")
    lines.append("")
    if png_written:
        lines.append(f"- 曲线图：`{png_path.name}`")
    else:
        lines.append(
            "- 曲线图：未生成（`matplotlib` 缺失 —— 请通过 "
            "`pip install -r requirements-train.txt` 安装；"
            "summary 仍然产出）"
        )
    lines.append(f"- 摘要：`{out_path.name}`")
    lines.append("")
    lines.append("## 5. 评估方法与局限")
    lines.append("")
    lines.append(
        "- 合成 RBF 真值函数：峰值在 (T=21°C, H=50%, L=10%)，长度尺度 "
        "(1.5, 8.0, 15.0)（与 BAO 默认 RBF 长度尺度对齐）。"
    )
    lines.append(
        "- 加性高斯噪声 σ=5 quality 分；同一晚的噪声样本对两个方法保持"
        "一致（paired 比较，最大化方差抵消）。"
    )
    lines.append(
        "- regret 度量基于 noise-free 真值差 `Q_max - f(x_t)`；"
        "优化器从 noisy quality 学习，但 regret 自身不含噪声项。"
    )
    lines.append(
        "- 真实部署中数据非 IID（季节切换、设备故障、生活变化）；"
        "本理论上界在 IID 假设下成立，季节切换 / 设备故障 / 重大生活"
        "变化下可能性能退化（与 R15.3 一致）。"
    )
    lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval_bayesian_regret",
        description=(
            "Compare v2.x weighted-median vs v3.x BAO (GP+TS) cumulative "
            "regret on a synthetic RBF environment with a known optimum. "
            "Outputs a regret-curve PNG (if matplotlib is available) and a "
            "markdown summary; both filenames carry the git short SHA "
            "suffix per Requirement 15.5."
        ),
    )
    parser.add_argument(
        "--user-prefs",
        type=Path,
        required=True,
        help=(
            "Path to a user_preferences.json file (sanitised copies are "
            "recommended). The file is copied into a private temp "
            "directory before any simulation writes occur, so the "
            "original is never mutated. An empty-dict file is "
            "tolerated; the v2.x baseline simply starts from defaults."
        ),
    )
    parser.add_argument(
        "--baseline",
        choices=BASELINE_CHOICES,
        default="v2.x",
        help=(
            "Comparison baseline: 'v2.x' (PreferenceLearner weighted "
            "median), 'random' (uniform within bounds), 'optimal_oracle' "
            "(always picks the true optimum). Default: 'v2.x'."
        ),
    )
    parser.add_argument(
        "--nights",
        type=int,
        default=DEFAULT_NIGHTS,
        help=f"Simulation horizon in nights (default: {DEFAULT_NIGHTS}).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"RNG seed for reproducibility (default: {DEFAULT_SEED}).",
    )
    parser.add_argument(
        "--out-prefix",
        type=str,
        default=DEFAULT_OUT_PREFIX,
        help=(
            "Base prefix for output filenames; the full filenames are "
            "<prefix>_regret_curve_<sha7>.png and "
            "<prefix>_regret_summary_<sha7>.md. "
            f"Default: '{DEFAULT_OUT_PREFIX}'."
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path.cwd(),
        help=(
            "Destination directory for the two output files. "
            "Default: current working directory."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    :returns: process exit code (``0`` OK, ``1`` invalid arguments).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if not args.user_prefs.exists():
        print(
            f"--user-prefs path does not exist: {args.user_prefs}",
            file=sys.stderr,
        )
        return 1
    if args.nights <= 0:
        print(
            f"--nights must be > 0, got {args.nights}",
            file=sys.stderr,
        )
        return 1

    if not _HAS_MATPLOTLIB:
        print(
            "[WARN] matplotlib is not installed (it ships in "
            "requirements-train.txt, not requirements-runtime.txt); "
            "regret curve PNG will be skipped, only the markdown "
            "summary will be emitted.",
            file=sys.stderr,
        )

    nights: int = args.nights
    seed: int = args.seed

    # Single noise stream shared between v3.x and the baseline so the
    # comparison is a paired t-style match (variance cancellation).
    noise_rng = random.Random(seed)
    noises = [noise_rng.gauss(0.0, _NOISE_STD) for _ in range(nights)]

    logger.info(
        "Running v3.x BAO (GP+TS) for %d nights (seed=%d)…",
        nights, seed,
    )
    v3_qualities = _run_v3(nights, noises, seed)

    logger.info(
        "Running baseline=%s for %d nights…", args.baseline, nights,
    )
    if args.baseline == "v2.x":
        base_qualities = _run_baseline_v2x(
            nights, noises, args.user_prefs, seed,
        )
    elif args.baseline == "random":
        base_qualities = _run_baseline_random(nights, noises, seed)
    elif args.baseline == "optimal_oracle":
        base_qualities = _run_baseline_oracle(nights)
    else:  # pragma: no cover — argparse choices exhaust this branch
        raise AssertionError(f"unreachable baseline: {args.baseline!r}")

    v3_cum = _cumulative_regret(v3_qualities)
    base_cum = _cumulative_regret(base_qualities)

    sha7 = _git_short_sha()
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    png_path = out_dir / f"{args.out_prefix}_regret_curve_{sha7}.png"
    md_path = out_dir / f"{args.out_prefix}_regret_summary_{sha7}.md"

    png_written = _write_png(
        png_path, nights, v3_cum, base_cum, args.baseline,
    )
    if png_written:
        logger.info("Wrote regret curve: %s", png_path)
    else:
        logger.info("Skipped regret curve PNG (no matplotlib).")

    _write_md(
        md_path,
        nights=nights,
        v3_cum=v3_cum,
        base_cum=base_cum,
        baseline_label=args.baseline,
        seed=seed,
        user_prefs=args.user_prefs,
        sha7=sha7,
        png_path=png_path,
        png_written=png_written,
    )
    logger.info("Wrote regret summary: %s", md_path)

    print(
        f"\nFinal cumulative regret over {nights} nights:\n"
        f"  v3.x (BAO GP+TS):       {v3_cum[-1]:.2f}\n"
        f"  {args.baseline:<22s}: {base_cum[-1]:.2f}\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI passthrough
    raise SystemExit(main())
