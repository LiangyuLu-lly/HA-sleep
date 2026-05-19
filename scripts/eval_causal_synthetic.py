"""``eval_causal_synthetic.py`` —— CAE estimator 合成 ground-truth 评估脚本.

This script is the **off-line evaluation harness** for the v3.0.0
:class:`src.causal_attribution.CausalAttributionEngine` (CAE) estimator,
implementing task 10.4 of the ``algorithmic-moat-v3.0.0`` spec.

It drives the estimator with a synthetic ground-truth DAG whose causal
coefficients are known by construction, then aggregates per-factor
**bias / variance / 95% CI coverage** across many independent trials.
The result is a markdown report mirroring the design §3.8.4 contract,
with the report filename suffix forced to the 7-character git commit
hash (R15.5)::

    <out-prefix>_causal_summary_<sha7>.md

Why this matters
----------------

`Property 4 <../.kiro/specs/algorithmic-moat-v3.0.0/design.md>`_ requires
the bootstrap 95% confidence interval to cover the *true* effect with
probability ≥ 0.92 on factors whose ground-truth effect is exactly 0
(so-called *null* factors). This script is the operational tool that
empirically certifies the property on a fresh build:

* Run the estimator under the same code path as production
  (``CausalAttributionEngine._run_estimator``).
* Use a deterministic seed (R15.5 default ``20260518``) so a regression
  surfacing in CI can be replayed locally bit-for-bit.
* Exit with code ``1`` whenever **any** null factor's empirical CI
  coverage falls below 0.92, turning the markdown report into a CI
  guard rail.

CLI contract (R6.3 / design §3.8.4)::

    --n-nights   <int>   nights synthesised per trial, default 60
    --n-trials   <int>   independent trials, default 200
    --seed       <int>   master RNG seed, default 20260518 (R15.5)
    --out-prefix <str>   output filename prefix, default "causal"

Outputs
-------

``<out-prefix>_causal_summary_<sha7>.md`` —— a single markdown table
covering all 6 factors (`ALL_FACTORS`) with columns
``true_coef / is_null / bias / variance / 95% CI coverage /
n_estimates``, prefixed by a metadata block (timestamp, trial count,
seeds, ground-truth coefficients) and followed by a pass/fail summary
section that drives the process exit code.

Exit codes
----------

* ``0`` —— every null factor has empirical CI coverage ≥ 0.92.
* ``1`` —— at least one null factor breached the 0.92 floor (R6.3).
* ``2`` —— catastrophic failure (no estimates produced for *any* trial).

Design notes
------------

* We call :meth:`CausalAttributionEngine._run_estimator` directly
  rather than the public :meth:`attribute` coroutine so the harness
  can crank through 200 trials in a few seconds without any
  filesystem I/O.  ``_run_estimator`` is a pure function over a list
  of records and the trick is identical to what
  ``tests/test_causal_attribution.py`` does indirectly via the public
  path.  The estimator state is otherwise unchanged.
* Ground-truth coefficients (:data:`GROUND_TRUTH_COEFS`) include
  exactly **two** null factors (``light_leak`` and ``noise_level``) so
  the null-coverage statistic always has at least 1 entry per the
  task contract (≥ 1 null factor required) and the second null gives
  redundancy when interpreting borderline runs.
* The bootstrap RNG seed is derived per-trial from the master RNG so
  trials are independent yet reproducible from a single ``--seed``.
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Mapping

import numpy as np

# ---------------------------------------------------------------------------
# Make ``src.causal_attribution`` importable when running from the repo root.
# Mirrors the convention used by ``scripts/train_population_prior.py``.
# ---------------------------------------------------------------------------

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent
_REPO_ROOT_STR: Final[str] = str(REPO_ROOT)
if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)

from src.causal_attribution import (  # noqa: E402 — sys.path tweak above
    ALL_FACTORS,
    QUALITY_SUBSCORE_KEYS,
    CausalAttributionEngine,
    CausalEffect,
    CausalFactorRecord,
)

logger = logging.getLogger("eval_causal_synthetic")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default RNG seed (R15.5).  Matches the project-wide convention used
#: by ``scripts/train_population_prior.py`` so failed-build forensics
#: line up across the v3.0.0 algorithm-stack tooling.
DEFAULT_SEED: Final[int] = 20260518

#: Default number of synthesised nights per trial.
DEFAULT_N_NIGHTS: Final[int] = 60

#: Default number of independent trials.
DEFAULT_N_TRIALS: Final[int] = 200

#: Default output filename prefix.  Combined with the git-sha suffix to
#: produce ``<prefix>_causal_summary_<sha7>.md``.
DEFAULT_OUT_PREFIX: Final[str] = "causal"

#: Ground-truth linear-model coefficients applied to the 6 factors when
#: synthesising ``quality_total``.  Two of the six are exactly zero so
#: the null-coverage statistic (R6.3) always has ≥ 1 entry to report.
#: Magnitudes for the non-null factors are kept modest (``|c| ≤ 3``) so
#: the estimator's residual bootstrap stays well-behaved at the
#: smallest reasonable sample size (``n_nights = 30``).
GROUND_TRUTH_COEFS: Final[Mapping[str, float]] = {
    "temperature_drift": -3.0,
    "noise_level":         0.0,   # null factor
    "light_leak":          0.0,   # null factor
    "hrv_anomaly":        -0.8,
    "bedtime_offset":     -1.2,
    "prior_night_debt":   -2.5,
}

#: Quality-total intercept used in the synthesis equation; chosen so the
#: total stays within a reasonable [50, 95] band for the default
#: coefficients above.
_BASE_QUALITY: Final[float] = 75.0

#: Standard deviation of the additive Gaussian noise on
#: ``quality_total``.  Small enough that the linear signal dominates,
#: large enough that residuals span both signs (so the residual
#: bootstrap is non-degenerate).
_NOISE_STD: Final[float] = 1.5

#: The R6.3 floor for null-factor empirical CI coverage.  Any null
#: factor that lands below this triggers a non-zero exit code.
NULL_COVERAGE_FLOOR: Final[float] = 0.92


# ---------------------------------------------------------------------------
# Git-sha helper (R15.5)
# ---------------------------------------------------------------------------


def _git_short_sha() -> str:
    """Return the 7-char git SHA of the repo, or ``"unknown"``.

    Mirrors the helper in ``scripts/train_population_prior.py``: best
    effort, never raises, so the eval can still produce a report on
    machines where ``git`` is missing or the repo is detached.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=_REPO_ROOT_STR,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    sha = result.stdout.strip()
    return sha if sha else "unknown"


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------


def _synthesise_records(
    *, n_nights: int, rng: np.random.Generator
) -> list[CausalFactorRecord]:
    """Generate *n_nights* :class:`CausalFactorRecord` rows.

    Each record's ``quality_total`` follows::

        quality_total = _BASE_QUALITY
                      + Σ_f GROUND_TRUTH_COEFS[f] * factor_value[f]
                      + Normal(0, _NOISE_STD)

    Factor values are drawn from ``Normal(0.5, 0.3)``, matching the
    distribution used by the property test in
    ``tests/test_causal_attribution.py`` so the eval harness exercises
    the same statistical regime the unit tests cover.

    All factors are observed (no missingness) — the eval focuses on
    the estimator's bias/variance/coverage, not the missing-value
    pathway, which has its own dedicated tests.
    """
    install_hash = "0" * 64  # constant — engine never inspects it here
    records: list[CausalFactorRecord] = []
    for i in range(n_nights):
        true_values: dict[str, float] = {
            f: float(rng.normal(loc=0.5, scale=0.3)) for f in ALL_FACTORS
        }
        signal = sum(
            GROUND_TRUTH_COEFS[f] * true_values[f] for f in ALL_FACTORS
        )
        noise = float(rng.normal(loc=0.0, scale=_NOISE_STD))
        quality_total = float(_BASE_QUALITY + signal + noise)
        records.append(
            CausalFactorRecord(
                # Synthetic timestamp — stable across runs given the seed.
                timestamp=(
                    f"2026-{((i // 28) % 12) + 1:02d}"
                    f"-{(i % 28) + 1:02d}T03:00:00Z"
                ),
                install_id_hash=install_hash,
                factors=true_values,
                quality_subscores={k: 75.0 for k in QUALITY_SUBSCORE_KEYS},
                quality_total=quality_total,
            )
        )
    return records


def _run_one_trial(
    *,
    n_nights: int,
    data_seed: int,
    bootstrap_seed: int,
    jsonl_path: Path,
) -> tuple[CausalEffect, ...]:
    """Synthesise data + run the CAE estimator once.

    Uses :meth:`CausalAttributionEngine._run_estimator` directly so we
    skip the "30-record gate" + ``personal_30d_mean`` short-circuit
    that gate the public :meth:`attribute` coroutine.  Synthetic data
    doesn't have a meaningful personal mean, and bypassing the
    coroutine means we don't pay for ``asyncio.to_thread`` or
    ``atomic_append_jsonl`` overhead 200 times.  The estimator code
    path itself is identical (no monkey-patching).
    """
    rng = np.random.default_rng(data_seed)
    records = _synthesise_records(n_nights=n_nights, rng=rng)
    engine = CausalAttributionEngine(
        jsonl_path=jsonl_path,
        rng_seed=bootstrap_seed,
    )
    return engine._run_estimator(records)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _aggregate_trials(
    *,
    n_nights: int,
    n_trials: int,
    seed: int,
    jsonl_path: Path,
) -> tuple[dict[str, list[float]], dict[str, list[bool]], int]:
    """Run *n_trials* trials and aggregate per-factor diagnostics.

    :returns: A tuple ``(estimates, ci_covers, n_failed_trials)`` where:

        * ``estimates[f]``: list of ``effect_pp`` estimates from
          successful trials (NaN estimates filtered out).
        * ``ci_covers[f]``: list of booleans, one per successful
          trial, recording whether the bootstrap 95% CI covered the
          ground-truth ``GROUND_TRUTH_COEFS[f]``.
        * ``n_failed_trials``: count of trials in which the estimator
          raised — included in the report for transparency but not
          counted toward coverage.
    """
    master_rng = np.random.default_rng(seed)
    estimates: dict[str, list[float]] = {f: [] for f in ALL_FACTORS}
    ci_covers: dict[str, list[bool]] = {f: [] for f in ALL_FACTORS}
    n_failed = 0
    for trial_idx in range(n_trials):
        # Derive two independent seeds per trial: one for synthesising
        # the data, one for the bootstrap RNG inside the estimator.
        # Both come from the master RNG so the whole run is
        # reproducible from ``--seed`` alone.
        data_seed = int(master_rng.integers(0, 2**31 - 1))
        bootstrap_seed = int(master_rng.integers(0, 2**31 - 1))
        try:
            effects = _run_one_trial(
                n_nights=n_nights,
                data_seed=data_seed,
                bootstrap_seed=bootstrap_seed,
                jsonl_path=jsonl_path,
            )
        except Exception:  # noqa: BLE001 — defensive boundary
            logger.exception("trial %d crashed", trial_idx)
            n_failed += 1
            continue
        for effect in effects:
            true_coef = GROUND_TRUTH_COEFS[effect.factor]
            if not np.isfinite(effect.effect_pp):
                # NaN estimate — record nothing; this is not a "failed
                # trial" but rather a degenerate-design outcome which
                # shouldn't happen for the eval's fully-observed data.
                continue
            estimates[effect.factor].append(float(effect.effect_pp))
            covered = bool(
                np.isfinite(effect.ci_low)
                and np.isfinite(effect.ci_high)
                and effect.ci_low <= true_coef <= effect.ci_high
            )
            ci_covers[effect.factor].append(covered)
    return estimates, ci_covers, n_failed


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def _format_pct(x: float) -> str:
    """Render a coverage / probability as ``XX.X%`` or ``n/a``."""
    if not np.isfinite(x):
        return "n/a"
    return f"{x * 100.0:.1f}%"


def _render_report(
    *,
    n_nights: int,
    n_trials: int,
    seed: int,
    n_failed: int,
    estimates: dict[str, list[float]],
    ci_covers: dict[str, list[bool]],
    sha7: str,
) -> tuple[str, list[tuple[str, float]]]:
    """Render the markdown report and collect null-coverage violations.

    :returns: ``(markdown_text, violations)`` where *violations* is the
        list of ``(factor, coverage)`` tuples for null factors whose
        empirical coverage is below :data:`NULL_COVERAGE_FLOOR`.
        The caller maps a non-empty list to exit code 1.
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines: list[str] = []
    lines.append(f"# Causal Attribution Synthetic Eval — `{sha7}`")
    lines.append("")
    lines.append("> Generated by `scripts/eval_causal_synthetic.py` "
                 "(spec task 10.4, R6.3 / R15.2 / R15.5).")
    lines.append("")
    lines.append("## Run metadata")
    lines.append("")
    lines.append(f"- 生成时间（UTC）：{now_utc}")
    lines.append(f"- 试验数 `--n-trials`：**{n_trials}**")
    lines.append(f"- 每次合成晚数 `--n-nights`：**{n_nights}**")
    lines.append(f"- 主随机种子 `--seed`：**{seed}**")
    lines.append(f"- 失败试验（estimator 抛异常）：{n_failed}")
    lines.append(f"- Null factor 覆盖率下限：**{_format_pct(NULL_COVERAGE_FLOOR)}**"
                 " （R6.3，违反则退出码 = 1）")
    lines.append("")
    lines.append("## Ground-truth coefficients")
    lines.append("")
    lines.append("| Factor | true_coef | is_null |")
    lines.append("|---|---|---|")
    for factor in ALL_FACTORS:
        coef = GROUND_TRUTH_COEFS[factor]
        is_null = abs(coef) < 1e-9
        lines.append(
            f"| `{factor}` | {coef:+.3f} | {'✅' if is_null else '—'} |"
        )
    lines.append("")
    lines.append("## Per-factor diagnostics")
    lines.append("")
    lines.append(
        "| Factor | true_coef | is_null | bias | variance | "
        "95% CI coverage | n_estimates |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    violations: list[tuple[str, float]] = []
    for factor in ALL_FACTORS:
        true_coef = GROUND_TRUTH_COEFS[factor]
        is_null = abs(true_coef) < 1e-9
        ests_list = estimates[factor]
        covers_list = ci_covers[factor]
        if ests_list:
            ests_arr = np.asarray(ests_list, dtype=float)
            bias = float(np.mean(ests_arr - true_coef))
            variance = float(np.var(ests_arr, ddof=0))
        else:
            bias = float("nan")
            variance = float("nan")
        if covers_list:
            coverage = float(np.mean(covers_list))
        else:
            coverage = float("nan")
        bias_str = (
            f"{bias:+.3f}" if np.isfinite(bias) else "n/a"
        )
        variance_str = (
            f"{variance:.3f}" if np.isfinite(variance) else "n/a"
        )
        lines.append(
            f"| `{factor}` | {true_coef:+.3f} | "
            f"{'✅' if is_null else '—'} | "
            f"{bias_str} | {variance_str} | "
            f"{_format_pct(coverage)} | {len(ests_list)} |"
        )
        if is_null and np.isfinite(coverage) and coverage < NULL_COVERAGE_FLOOR:
            violations.append((factor, coverage))
    lines.append("")
    lines.append("## Null-factor coverage check (R6.3)")
    lines.append("")
    if violations:
        lines.append(
            f"❌ {len(violations)} null factor(s) below the "
            f"{_format_pct(NULL_COVERAGE_FLOOR)} floor:"
        )
        lines.append("")
        for factor, coverage in violations:
            lines.append(
                f"- `{factor}`: coverage = {_format_pct(coverage)} "
                f"< {_format_pct(NULL_COVERAGE_FLOOR)}"
            )
        lines.append("")
        lines.append("Process exit code: **1**")
    else:
        lines.append(
            f"✅ All null factors meet the "
            f"{_format_pct(NULL_COVERAGE_FLOOR)} floor."
        )
        lines.append("")
        lines.append("Process exit code: **0**")
    lines.append("")
    return "\n".join(lines) + "\n", violations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval_causal_synthetic",
        description=(
            "评估 src.causal_attribution.CausalAttributionEngine 在合成 "
            "ground-truth DAG 上的 bias / variance / 95% CI 覆盖率（R6.3）。"
        ),
    )
    parser.add_argument(
        "--n-nights",
        type=int,
        default=DEFAULT_N_NIGHTS,
        help=(
            "Number of synthesised nights per trial "
            f"(default: {DEFAULT_N_NIGHTS}). Must be ≥ 5 to clear the "
            "estimator's per-factor minimum-observation gate."
        ),
    )
    parser.add_argument(
        "--n-trials",
        type=int,
        default=DEFAULT_N_TRIALS,
        help=(
            "Number of independent trials "
            f"(default: {DEFAULT_N_TRIALS}). 200 is the smallest count "
            "that gives ±2 percentage-point precision on coverage."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=(
            f"Master RNG seed (default: {DEFAULT_SEED}). "
            "Per-trial data + bootstrap seeds are derived deterministically."
        ),
    )
    parser.add_argument(
        "--out-prefix",
        type=str,
        default=DEFAULT_OUT_PREFIX,
        help=(
            f'Output filename prefix (default: "{DEFAULT_OUT_PREFIX}"). '
            "The final filename is "
            "<prefix>_causal_summary_<sha7>.md (R15.5)."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Python logging level (default: INFO).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    args = _build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.n_nights < 5:
        logger.error(
            "--n-nights must be ≥ 5 (estimator min_per_factor_observations); "
            "got %d",
            args.n_nights,
        )
        return 2
    if args.n_trials < 1:
        logger.error("--n-trials must be ≥ 1; got %d", args.n_trials)
        return 2

    sha7 = _git_short_sha()
    out_path = Path(f"{args.out_prefix}_causal_summary_{sha7}.md").resolve()

    logger.info(
        "starting eval: n_nights=%d n_trials=%d seed=%d out=%s",
        args.n_nights,
        args.n_trials,
        args.seed,
        out_path,
    )

    # ``jsonl_path`` is only used inside the engine constructor for
    # type-checking; ``_run_estimator`` never touches the disk.  We
    # still pass a real path under a per-run temp dir so an accidental
    # IO call would land in a sandboxed location.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="eval_causal_synthetic_") as td:
        jsonl_path = Path(td) / "causal_factors.jsonl"
        estimates, ci_covers, n_failed = _aggregate_trials(
            n_nights=args.n_nights,
            n_trials=args.n_trials,
            seed=args.seed,
            jsonl_path=jsonl_path,
        )

    total_estimates = sum(len(v) for v in estimates.values())
    if total_estimates == 0:
        logger.error(
            "no estimates produced across %d trials; aborting",
            args.n_trials,
        )
        return 2

    markdown, violations = _render_report(
        n_nights=args.n_nights,
        n_trials=args.n_trials,
        seed=args.seed,
        n_failed=n_failed,
        estimates=estimates,
        ci_covers=ci_covers,
        sha7=sha7,
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")
    logger.info("wrote report: %s", out_path)

    if violations:
        for factor, coverage in violations:
            logger.error(
                "null factor %r CI coverage %.3f < %.3f (R6.3 violation)",
                factor,
                coverage,
                NULL_COVERAGE_FLOOR,
            )
        return 1
    logger.info(
        "all null factors meet ≥ %.0f%% CI coverage",
        NULL_COVERAGE_FLOOR * 100.0,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI shim
    raise SystemExit(main())
