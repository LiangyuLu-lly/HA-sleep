"""``eval_population_prior_rmse.py`` —— PP 桶预测 RMSE 离线评估（R15.1 方向 3）.

This script is the offline evaluator for `Requirement 15.1
<../.kiro/specs/algorithmic-moat-v3.0.0/requirements.md>`_ direction 3
and design §3.8.5: it measures how well the population prior bucket
mean predicts a held-out individual's bedroom environment as a
function of how many *individual* nights have already been observed
(``N``).  The output is a markdown report (with optional matplotlib
plot) whose filename suffix carries the 7-character git commit hash
(R15.5)::

    <out-prefix>_prior_rmse_<sha7>.md
    <out-prefix>_prior_rmse_<sha7>.png    # only when matplotlib is present

Two operational modes
---------------------

* **Real holdout mode** (default).  ``--mesa-holdout`` must point at a
  CSV exported from the MESA holdout split (one row per
  subject-night, columns ``age_band, sex, chronotype, season,
  temperature_c, humidity_pct, brightness_pct``).  ``--prior`` must
  point at a ``population_prior.pickle`` produced by
  ``scripts/train_population_prior.py``.  The evaluator looks up the
  prior bucket for each subject's ``(age_band, sex, chronotype,
  season)``, computes the RMSE of the bucket mean against the
  subject's own per-night individual baseline, and reports the
  result both globally and stratified by bucket.
* **Synthetic mode** (``--synthetic``).  Skips the real MESA holdout
  entirely and synthesises an in-memory prior + holdout sample so CI
  / local dev can smoke-test the report-generation path without
  shipping the (large) NSRR data.  This mirrors the pattern used by
  ``scripts/train_population_prior.py``.

The evaluator deliberately reports RMSE for **multiple** values of
``N ∈ {0, 1, 3, 7, 14}`` so the markdown table directly visualises the
"prior helpful when N is small, individual baseline takes over as N
grows" claim made in design §3.1 and §3.2.3.  At ``N = 0`` the
individual has no observations of their own and the prior is the only
estimate — this is the cold-start row.  At ``N = 14`` the individual
has accumulated two weeks of personal data and we expect the bucket
prior to be no better than (and often worse than) the individual's
running mean, illustrating the BAO ``prior_weight = exp(-N/14)``
schedule (P7).

CLI contract (design §3.8.5)::

    --mesa-holdout <path>     MESA holdout CSV  (required)
    --prior        <path>     population_prior.pickle (required)
    --seed         <int>      RNG seed, default 20260518 (R15.5)
    --out-prefix   <str>      output filename prefix, default "prior"
    --out-dir      <path>     output directory, default cwd
    --synthetic               skip --mesa-holdout / --prior IO and use
                              a tiny in-memory fixture (CI smoke)

Exit codes
----------

* ``0`` — OK, report written.
* ``1`` — invalid arguments (missing inputs in non-synthetic mode,
  unparseable CSV, prior pickle load failure, etc.).

Soft dependencies
-----------------

* ``matplotlib`` is imported lazily and skipped silently when missing
  (it lives in ``requirements-train.txt``, not
  ``requirements-runtime.txt`` — PR4 / R12.5).  When unavailable the
  PNG is omitted; the markdown report is always produced.
* ``numpy`` is the only hard dep beyond stdlib (used for the RMSE
  arithmetic and the coverage statistics).

:Validates: Requirements 15.1, 15.2, 15.5
"""
from __future__ import annotations

import argparse
import csv
import logging
import math
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np

# ---------------------------------------------------------------------------
# Make ``src.population_prior`` importable when running from the repo root.
# Mirrors the convention used by ``scripts/eval_bayesian_regret.py`` /
# ``scripts/train_population_prior.py``.
# ---------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
_REPO_ROOT_STR: str = str(REPO_ROOT)
if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)

from src.population_prior import (  # noqa: E402 — sys.path tweak above
    AgeBand,
    BucketKey,
    Chronotype,
    PopulationPrior,
    PopulationPriorRepository,
    PriorBucket,
    PriorMetadata,
    Season,
    Sex,
)

logger = logging.getLogger("eval_population_prior_rmse")


# ---------------------------------------------------------------------------
# Constants — keep aligned with src.population_prior + design §3.8.5.
# ---------------------------------------------------------------------------

#: Default RNG seed (R15.5).  Matches the project-wide convention.
DEFAULT_SEED: int = 20260518

#: Default output filename prefix.  Combined with the git-sha suffix
#: produces ``prior_prior_rmse_<sha7>.md``.
DEFAULT_OUT_PREFIX: str = "prior"

#: ``N`` schedule reported in the RMSE table.  ``N = 0`` is the
#: cold-start row (no individual observations); the ``14`` upper bound
#: matches the BAO ``prior_weight = exp(-N/14)`` half-life (R8.4 / P7).
_N_SCHEDULE: tuple[int, ...] = (0, 1, 3, 7, 14)

#: CSV column names accepted by the real-holdout mode parser.
_REQUIRED_CSV_COLUMNS: tuple[str, ...] = (
    "subject_id",
    "age_band",
    "sex",
    "chronotype",
    "season",
    "temperature_c",
    "humidity_pct",
    "brightness_pct",
)

#: P6 physiological bounds — used to clip synthetic samples and to
#: spot egregious CSV parse errors.  Identical to the bounds asserted
#: by ``tests/test_population_prior.py::test_property_p6_*``.
_TEMP_BOUNDS: tuple[float, float] = (16.0, 28.0)
_HUM_BOUNDS: tuple[float, float] = (30.0, 70.0)
_BRI_BOUNDS: tuple[float, float] = (0.0, 50.0)


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
# Git SHA helper (R15.5)
# ---------------------------------------------------------------------------


def _git_short_sha() -> str:
    """Return the 7-char git SHA of the repo, or ``"unknown"``.

    Mirrors :func:`scripts.eval_bayesian_regret._git_short_sha`.  Best
    effort, never raises, so the eval can still produce a report on
    machines where ``git`` is missing or the repo is detached.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=_REPO_ROOT_STR,
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


# ---------------------------------------------------------------------------
# Holdout sample structure
# ---------------------------------------------------------------------------


class _SubjectNight:
    """One row of the holdout CSV (or its synthetic equivalent).

    Plain ``__slots__`` class — frozen dataclass would also work but
    the eval is small enough that ``__slots__`` keeps memory tight
    when the CSV is in the hundreds of thousands of rows.
    """

    __slots__ = (
        "subject_id",
        "age_band",
        "sex",
        "chronotype",
        "season",
        "temperature_c",
        "humidity_pct",
        "brightness_pct",
    )

    def __init__(
        self,
        *,
        subject_id: str,
        age_band: AgeBand,
        sex: Sex,
        chronotype: Chronotype,
        season: Season,
        temperature_c: float,
        humidity_pct: float,
        brightness_pct: float,
    ) -> None:
        self.subject_id = subject_id
        self.age_band = age_band
        self.sex = sex
        self.chronotype = chronotype
        self.season = season
        self.temperature_c = temperature_c
        self.humidity_pct = humidity_pct
        self.brightness_pct = brightness_pct

    @property
    def bucket_key(self) -> BucketKey:
        return (self.age_band, self.sex, self.chronotype, self.season)


# ---------------------------------------------------------------------------
# Real-mode CSV parsing
# ---------------------------------------------------------------------------


def _parse_holdout_csv(path: Path) -> list[_SubjectNight]:
    """Parse the MESA holdout CSV into a list of :class:`_SubjectNight`.

    The CSV is expected to have a header row matching
    :data:`_REQUIRED_CSV_COLUMNS`; rows with malformed numbers are
    logged and skipped (not fatal — we want the report to still
    surface partial-data issues).
    """
    rows: list[_SubjectNight] = []
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"holdout CSV has no header: {path}")
        missing = [
            col for col in _REQUIRED_CSV_COLUMNS
            if col not in reader.fieldnames
        ]
        if missing:
            raise ValueError(
                f"holdout CSV {path} missing required columns: {missing}"
            )
        for row_idx, raw in enumerate(reader, start=2):  # 1 = header line
            try:
                rows.append(
                    _SubjectNight(
                        subject_id=str(raw["subject_id"]),
                        age_band=str(raw["age_band"]),  # type: ignore[arg-type]
                        sex=str(raw["sex"]),  # type: ignore[arg-type]
                        chronotype=str(raw["chronotype"]),  # type: ignore[arg-type]
                        season=str(raw["season"]),  # type: ignore[arg-type]
                        temperature_c=float(raw["temperature_c"]),
                        humidity_pct=float(raw["humidity_pct"]),
                        brightness_pct=float(raw["brightness_pct"]),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "Skipping holdout CSV row %d (%s): %s",
                    row_idx, path, exc,
                )
    return rows


# ---------------------------------------------------------------------------
# Synthetic mode — fixture generators
# ---------------------------------------------------------------------------


def _build_synthetic_prior(rng: random.Random) -> PopulationPriorRepository:
    """Generate a tiny in-memory prior covering 4 buckets.

    The buckets are intentionally placed at distinct corners of the
    P6 physiological band so the bucket-stratified RMSE table has
    measurable variation.  All buckets carry ``n_samples = 100`` so
    the runtime ``PopulationPriorRepository.lookup`` fallback ladder
    short-circuits at ``fallback_level = 0``.
    """
    leaf_keys: list[BucketKey] = [
        ("18-25", "M",            "morning", "summer"),
        ("36-50", "F",            "evening", "winter"),
        ("51-65", "unspecified",  "neutral", "spring"),
        ("65+",   "unspecified",  "neutral", "autumn"),
    ]
    buckets: dict[BucketKey, PriorBucket] = {}
    for key in leaf_keys:
        buckets[key] = PriorBucket(
            temperature_mean_c=round(rng.uniform(19.0, 23.0), 2),
            temperature_var_c2=round(rng.uniform(0.4, 1.4), 3),
            humidity_mean_pct=round(rng.uniform(40.0, 60.0), 2),
            humidity_var_pct2=round(rng.uniform(15.0, 35.0), 3),
            brightness_mean_pct=round(rng.uniform(2.0, 18.0), 2),
            brightness_var_pct2=round(rng.uniform(2.0, 9.0), 3),
            n_samples=100,
        )
    metadata = PriorMetadata(
        schema_version=1,
        sources=("synthetic-eval",),
        trained_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        git_commit=_git_short_sha(),
        n_subject_nights=len(buckets) * 100,
        sha256="0" * 64,  # not verified in synthetic mode
    )
    prior = PopulationPrior(buckets=buckets, metadata=metadata)
    # Construct directly through the public class since we are not
    # going through the on-disk verifier.
    return PopulationPriorRepository(prior, size_bytes=0)


def _build_synthetic_holdout(
    *,
    prior: PopulationPriorRepository,
    rng: random.Random,
    n_subjects: int = 40,
    nights_per_subject: int = 21,
) -> list[_SubjectNight]:
    """Synthesise a holdout sample drawn around each bucket's mean.

    Each synthetic subject is anchored to one of the prior's buckets;
    their per-night observations are drawn from a Gaussian centred on
    a *subject-specific* mean that itself is drawn from the bucket
    posterior.  This mirrors the hierarchical structure assumed by
    the prior trainer — the bucket mean is the population-level
    expectation, individual subjects are fixed-effect deviations.

    ``n_subjects × nights_per_subject`` rows are produced.  The
    default ``40 × 21 = 840`` keeps the synthetic eval fast (< 1 s)
    while still giving each ``N`` row in the RMSE table at least
    ``n_subjects`` observations to average over.
    """
    bucket_keys = list(prior.buckets.keys())
    rows: list[_SubjectNight] = []
    for sid_idx in range(n_subjects):
        key = bucket_keys[sid_idx % len(bucket_keys)]
        bucket = prior.buckets[key]
        # Subject-specific true mean drawn from the bucket posterior.
        subj_temp = rng.gauss(
            bucket.temperature_mean_c, math.sqrt(bucket.temperature_var_c2),
        )
        subj_hum = rng.gauss(
            bucket.humidity_mean_pct, math.sqrt(bucket.humidity_var_pct2),
        )
        subj_bri = rng.gauss(
            bucket.brightness_mean_pct, math.sqrt(bucket.brightness_var_pct2),
        )
        for night in range(nights_per_subject):
            # Per-night noise is small relative to the between-subject
            # variance so the prior is meaningful at low N.
            t = rng.gauss(subj_temp, 0.5)
            h = rng.gauss(subj_hum, 3.0)
            b = rng.gauss(subj_bri, 2.0)
            t = max(_TEMP_BOUNDS[0], min(_TEMP_BOUNDS[1], t))
            h = max(_HUM_BOUNDS[0], min(_HUM_BOUNDS[1], h))
            b = max(_BRI_BOUNDS[0], min(_BRI_BOUNDS[1], b))
            rows.append(
                _SubjectNight(
                    subject_id=f"synthetic-{sid_idx:03d}",
                    age_band=key[0],
                    sex=key[1],
                    chronotype=key[2],
                    season=key[3],
                    temperature_c=round(t, 3),
                    humidity_pct=round(h, 3),
                    brightness_pct=round(b, 3),
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Core RMSE computation
# ---------------------------------------------------------------------------


def _individual_running_mean(
    history: list[tuple[float, float, float]],
    n: int,
) -> tuple[float, float, float] | None:
    """Return the mean of the first ``n`` observations or ``None``.

    ``n == 0`` returns ``None`` — there is no individual baseline at
    cold start, only the prior is defined.
    """
    if n <= 0 or n > len(history):
        return None
    arr = np.asarray(history[:n], dtype=float)
    return (
        float(np.mean(arr[:, 0])),
        float(np.mean(arr[:, 1])),
        float(np.mean(arr[:, 2])),
    )


def _rmse_against_truth(
    estimates: Iterable[tuple[float, float, float]],
    truths: Iterable[tuple[float, float, float]],
) -> tuple[float, float, float] | None:
    """Per-dimension RMSE; returns ``None`` when no estimates are present."""
    est_arr = np.asarray(list(estimates), dtype=float)
    tru_arr = np.asarray(list(truths), dtype=float)
    if est_arr.size == 0:
        return None
    diffs = est_arr - tru_arr
    rmses = np.sqrt(np.mean(diffs ** 2, axis=0))
    return float(rmses[0]), float(rmses[1]), float(rmses[2])


def _evaluate(
    *,
    prior: PopulationPriorRepository,
    holdout: list[_SubjectNight],
) -> dict[str, object]:
    """Compute the global + per-bucket RMSE tables across the ``N`` schedule.

    For each subject we treat their first ``n`` observations as
    "history" and the remaining ``len(observations) - n`` as ground
    truth.  The prior estimate is constant across ``n`` (bucket mean
    only depends on the subject's bucket), so the prior RMSE is the
    same column-wise; what varies is the individual-baseline RMSE.

    Returns a dict with::

        {
            "n_subjects":     int,
            "n_total_nights": int,
            "global": {
                "n": [...],
                "prior_rmse":      [(t,h,b), ...],
                "individual_rmse": [(t,h,b) | None, ...],
                "n_truth_nights":  [int, ...],
            },
            "by_bucket": {
                bucket_key_str: {
                    "n_subjects": int,
                    "n_truth_nights": int,
                    "prior_rmse_n0": (t, h, b),
                    "individual_rmse_n7": (t, h, b) | None,
                    "prior_bucket_n_samples": int,
                    "fallback_level": int,
                },
                ...
            },
            "fallback_distribution": {0: int, 1: int, 2: int, 3: int},
        }
    """
    # 1. Group rows by subject_id, preserving order.
    by_subject: dict[str, list[_SubjectNight]] = {}
    for row in holdout:
        by_subject.setdefault(row.subject_id, []).append(row)

    # 2. Prior bucket lookup per subject (bucket is constant across nights
    #    for a given subject — we use the first row's metadata).
    prior_means: dict[str, tuple[float, float, float]] = {}
    fallback_levels: dict[str, int] = {}
    bucket_n_samples: dict[str, int] = {}
    for sid, rows in by_subject.items():
        first = rows[0]
        try:
            bucket, fallback = prior.lookup(
                age_band=first.age_band,
                sex=first.sex,
                chronotype=first.chronotype,
                season=first.season,
            )
        except ValueError:
            # Empty buckets dict — should never happen because the
            # constructor raises in that case, but be defensive.
            continue
        prior_means[sid] = (
            bucket.temperature_mean_c,
            bucket.humidity_mean_pct,
            bucket.brightness_mean_pct,
        )
        fallback_levels[sid] = fallback
        bucket_n_samples[sid] = bucket.n_samples

    # 3. Compute global RMSE per N.
    global_rows: list[dict[str, object]] = []
    for n in _N_SCHEDULE:
        prior_estimates: list[tuple[float, float, float]] = []
        indiv_estimates: list[tuple[float, float, float]] = []
        truths_for_prior: list[tuple[float, float, float]] = []
        truths_for_indiv: list[tuple[float, float, float]] = []
        for sid, rows in by_subject.items():
            if sid not in prior_means:
                continue
            history: list[tuple[float, float, float]] = [
                (r.temperature_c, r.humidity_pct, r.brightness_pct)
                for r in rows
            ]
            truth = history[n:]
            if not truth:
                continue
            prior_est = prior_means[sid]
            indiv_est = _individual_running_mean(history, n)
            for t in truth:
                prior_estimates.append(prior_est)
                truths_for_prior.append(t)
                if indiv_est is not None:
                    indiv_estimates.append(indiv_est)
                    truths_for_indiv.append(t)
        prior_rmse = _rmse_against_truth(prior_estimates, truths_for_prior)
        indiv_rmse = _rmse_against_truth(indiv_estimates, truths_for_indiv)
        global_rows.append(
            {
                "n": n,
                "prior_rmse": prior_rmse,
                "individual_rmse": indiv_rmse,
                "n_truth_nights": len(truths_for_prior),
            }
        )

    # 4. Per-bucket aggregation (we report the cold-start prior RMSE per
    #    bucket plus the N=7 individual RMSE — the two values that
    #    bracket the BAO prior_weight transition).
    by_bucket: dict[str, dict[str, object]] = {}
    bucket_groups: dict[BucketKey, list[str]] = {}
    for sid in prior_means:
        rows = by_subject[sid]
        first = rows[0]
        bucket_groups.setdefault(first.bucket_key, []).append(sid)

    for bkey, sids in bucket_groups.items():
        prior_pred: list[tuple[float, float, float]] = []
        prior_true: list[tuple[float, float, float]] = []
        indiv_pred: list[tuple[float, float, float]] = []
        indiv_true: list[tuple[float, float, float]] = []
        for sid in sids:
            rows = by_subject[sid]
            history: list[tuple[float, float, float]] = [
                (r.temperature_c, r.humidity_pct, r.brightness_pct)
                for r in rows
            ]
            # Prior cold-start: every night is truth.
            prior_est = prior_means[sid]
            for t in history:
                prior_pred.append(prior_est)
                prior_true.append(t)
            # N=7 individual baseline: first 7 nights are history.
            indiv_est = _individual_running_mean(history, 7)
            if indiv_est is not None:
                for t in history[7:]:
                    indiv_pred.append(indiv_est)
                    indiv_true.append(t)
        bucket_label = (
            f"{bkey[0]}|{bkey[1]}|{bkey[2]}|{bkey[3]}"
        )
        by_bucket[bucket_label] = {
            "n_subjects": len(sids),
            "n_truth_nights": len(prior_true),
            "prior_rmse_n0": _rmse_against_truth(prior_pred, prior_true),
            "individual_rmse_n7": _rmse_against_truth(indiv_pred, indiv_true),
            "prior_bucket_n_samples": bucket_n_samples[sids[0]],
            "fallback_level": fallback_levels[sids[0]],
        }

    # 5. Fallback ladder distribution (R8.6 visibility).
    fallback_dist = {0: 0, 1: 0, 2: 0, 3: 0}
    for level in fallback_levels.values():
        fallback_dist[int(level)] = fallback_dist.get(int(level), 0) + 1

    return {
        "n_subjects": len(by_subject),
        "n_total_nights": len(holdout),
        "global": {
            "n": [row["n"] for row in global_rows],
            "rows": global_rows,
        },
        "by_bucket": by_bucket,
        "fallback_distribution": fallback_dist,
    }


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def _format_rmse(rmse: tuple[float, float, float] | None) -> str:
    """Render a per-dimension RMSE triple as ``T / H / B`` or ``n/a``."""
    if rmse is None:
        return "n/a"
    return f"{rmse[0]:.3f} / {rmse[1]:.3f} / {rmse[2]:.3f}"


def _write_png(
    out_path: Path,
    *,
    global_rows: list[dict[str, object]],
) -> bool:
    """Emit a 3-panel RMSE-vs-N curve PNG; returns ``True`` on write."""
    if not _HAS_MATPLOTLIB:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ns = [int(row["n"]) for row in global_rows]
    prior_t = [row["prior_rmse"][0] if row["prior_rmse"] else float("nan")
               for row in global_rows]
    indiv_t = [
        row["individual_rmse"][0] if row["individual_rmse"] else float("nan")
        for row in global_rows
    ]
    prior_h = [row["prior_rmse"][1] if row["prior_rmse"] else float("nan")
               for row in global_rows]
    indiv_h = [
        row["individual_rmse"][1] if row["individual_rmse"] else float("nan")
        for row in global_rows
    ]
    prior_b = [row["prior_rmse"][2] if row["prior_rmse"] else float("nan")
               for row in global_rows]
    indiv_b = [
        row["individual_rmse"][2] if row["individual_rmse"] else float("nan")
        for row in global_rows
    ]
    fig, axes = plt.subplots(1, 3, figsize=(13, 4), dpi=120)
    for ax, prior_curve, indiv_curve, label in (
        (axes[0], prior_t, indiv_t, "Temperature (C)"),
        (axes[1], prior_h, indiv_h, "Humidity (%)"),
        (axes[2], prior_b, indiv_b, "Brightness (%)"),
    ):
        ax.plot(ns, prior_curve, "o-", label="Prior bucket mean",
                color="tab:blue", linewidth=2)
        ax.plot(ns, indiv_curve, "s--", label="Individual running mean",
                color="tab:orange", linewidth=2)
        ax.set_xlabel("N (individual nights observed)")
        ax.set_ylabel("RMSE")
        ax.set_title(label)
        ax.grid(True, alpha=0.3)
        ax.legend()
    fig.suptitle(
        "Population prior vs individual baseline RMSE (eval-only artifact)"
    )
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return True


def _write_md(
    out_path: Path,
    *,
    result: dict[str, object],
    seed: int,
    mesa_holdout: Path | None,
    prior_path: Path | None,
    synthetic: bool,
    sha7: str,
    png_path: Path,
    png_written: bool,
) -> None:
    """Emit the markdown RMSE report.

    The markdown is the **authoritative** output (matplotlib is a soft
    dep); it always includes:

    * the run metadata block (mode, seed, dataset paths, sha7);
    * the global RMSE table across :data:`_N_SCHEDULE`;
    * the per-bucket RMSE table with cold-start vs ``N = 7`` rows
      and the fallback level for each bucket;
    * the fallback-ladder distribution (R8.6 visibility);
    * the standard "局限性" paragraph (R15.3).
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    global_rows: list[dict[str, object]] = result["global"]["rows"]  # type: ignore[index]
    by_bucket: Mapping[str, Mapping[str, object]] = result["by_bucket"]  # type: ignore[assignment]
    fallback_dist: Mapping[int, int] = result["fallback_distribution"]  # type: ignore[assignment]

    lines: list[str] = []
    lines.append(f"# Population Prior RMSE 评估报告（{sha7}）")
    lines.append("")
    lines.append(
        "> 由 `scripts/eval_population_prior_rmse.py` 生成；该脚本对比 "
        "population prior 桶均值与个体 running-mean baseline 在 MESA "
        "holdout 集（或 `--synthetic` 合成数据）上的 RMSE（R15.1 / "
        "R15.2 / R15.5）。"
    )
    lines.append("")
    lines.append("## 1. 运行元数据")
    lines.append("")
    lines.append(f"- 生成时间（UTC）：{now_utc}")
    lines.append(f"- 模式：`{'synthetic' if synthetic else 'real-holdout'}`")
    lines.append(f"- `--mesa-holdout`：`{mesa_holdout if mesa_holdout else '（合成模式）'}`")
    lines.append(f"- `--prior`：`{prior_path if prior_path else '（合成模式）'}`")
    lines.append(f"- `--seed`：{seed}")
    lines.append(f"- git commit：`{sha7}`")
    lines.append(f"- 受试者数：**{result['n_subjects']}**")
    lines.append(f"- 总 holdout 晚数：**{result['n_total_nights']}**")
    lines.append("")

    lines.append("## 2. 全局 RMSE（按 N 个体观测数）")
    lines.append("")
    lines.append(
        "RMSE 列格式：`温度 / 湿度 / 亮度`（°C / % / %）。`N = 0` 是"
        "冷启动场景，individual baseline 不可用。"
    )
    lines.append("")
    lines.append("| N | Prior 桶均值 RMSE | Individual baseline RMSE | n_truth_nights |")
    lines.append("|---|---|---|---|")
    for row in global_rows:
        lines.append(
            f"| {row['n']} | {_format_rmse(row['prior_rmse'])} | "  # type: ignore[arg-type]
            f"{_format_rmse(row['individual_rmse'])} | "  # type: ignore[arg-type]
            f"{row['n_truth_nights']} |"
        )
    lines.append("")

    lines.append("## 3. 按桶分类（`age_band|sex|chronotype|season`）")
    lines.append("")
    lines.append(
        "| Bucket | n_subjects | Prior RMSE (N=0) | Individual RMSE (N=7) "
        "| 桶样本量 | fallback_level |"
    )
    lines.append("|---|---|---|---|---|---|")
    for label, info in sorted(by_bucket.items()):
        lines.append(
            f"| `{label}` | {info['n_subjects']} "
            f"| {_format_rmse(info['prior_rmse_n0'])} "  # type: ignore[arg-type]
            f"| {_format_rmse(info['individual_rmse_n7'])} "  # type: ignore[arg-type]
            f"| {info['prior_bucket_n_samples']} "
            f"| {info['fallback_level']} |"
        )
    lines.append("")

    lines.append("## 4. Fallback 阶梯分布（R8.6）")
    lines.append("")
    lines.append("| fallback_level | 含义 | 受试者数 |")
    lines.append("|---|---|---|")
    levels_meaning = {
        0: "精确匹配（exact bucket）",
        1: "sex 放宽到 `unspecified`",
        2: "chronotype 进一步放宽到 `neutral`",
        3: "age_band 进一步放宽（根桶）",
    }
    for lvl in (0, 1, 2, 3):
        lines.append(
            f"| {lvl} | {levels_meaning[lvl]} | "
            f"{int(fallback_dist.get(lvl, 0))} |"
        )
    lines.append("")

    lines.append("## 5. 输出文件")
    lines.append("")
    lines.append(f"- 摘要：`{out_path.name}`")
    if png_written:
        lines.append(f"- RMSE 曲线图：`{png_path.name}`")
    else:
        lines.append(
            "- RMSE 曲线图：未生成（`matplotlib` 缺失 —— 训练 / 评估"
            "环境请通过 `pip install -r requirements-train.txt` 安装；"
            "summary 仍然产出）。"
        )
    lines.append("")

    lines.append("## 6. 评估方法与局限")
    lines.append("")
    lines.append(
        "- 真实模式（`--mesa-holdout`）使用 MESA holdout CSV "
        "（每行一个 subject-night），按 `(age_band, sex, "
        "chronotype, season)` 4 维查表得到桶均值，作为 cold-start "
        "估计；individual baseline 取该受试者前 N 晚的算术平均。"
    )
    lines.append(
        "- 合成模式（`--synthetic`）跳过磁盘 I/O，构造一个 4 桶 "
        "in-memory prior + 受试者层级合成 holdout，用于 CI 烟测脚本"
        "管线（R15.5：固定种子可复现）。"
    )
    lines.append(
        "- 报告统计基于 IID 假设；真实部署中数据非 IID（季节切换、"
        "设备故障、生活变化），prior 在跨人群迁移时可能出现系统偏差。"
        "本节与 `docs/algorithm_evaluation.md` 顶部的 R15.3 局限性"
        "声明一致："
        "**v3.0.0 算法在 IID 假设下成立，季节切换 / 设备故障 / "
        "重大生活变化下可能性能退化。**"
    )
    lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval_population_prior_rmse",
        description=(
            "Evaluate the population prior bucket mean against the "
            "individual running-mean baseline on a MESA holdout split "
            "(R15.1 direction 3 / design §3.8.5).  Produces a markdown "
            "summary and an optional matplotlib PNG; both filenames "
            "carry the git short SHA per Requirement 15.5."
        ),
    )
    parser.add_argument(
        "--mesa-holdout",
        type=Path,
        required=False,
        help=(
            "Path to the MESA holdout CSV (required unless --synthetic "
            "is set).  See module docstring for the expected columns."
        ),
    )
    parser.add_argument(
        "--prior",
        type=Path,
        required=False,
        help=(
            "Path to a population_prior.pickle produced by "
            "scripts/train_population_prior.py (required unless "
            "--synthetic is set)."
        ),
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
            f'Output filename prefix (default: "{DEFAULT_OUT_PREFIX}"). '
            "The full filenames are <prefix>_prior_rmse_<sha7>.md and "
            "<prefix>_prior_rmse_<sha7>.png."
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path.cwd(),
        help="Destination directory for the output files (default: cwd).",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help=(
            "Skip the real MESA holdout and use an in-memory synthetic "
            "fixture; useful for CI smoke tests and local dev when "
            "NSRR data is unavailable."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _build_arg_parser().parse_args(argv)
    seed: int = args.seed
    rng = random.Random(seed)

    if args.synthetic:
        logger.info(
            "Running in synthetic mode (seed=%d): skipping --mesa-holdout / "
            "--prior I/O.",
            seed,
        )
        prior = _build_synthetic_prior(rng)
        holdout = _build_synthetic_holdout(prior=prior, rng=rng)
        mesa_holdout_path: Path | None = None
        prior_path: Path | None = None
    else:
        if args.mesa_holdout is None or args.prior is None:
            print(
                "--mesa-holdout and --prior are required unless --synthetic "
                "is set.",
                file=sys.stderr,
            )
            return 1
        if not args.mesa_holdout.exists():
            print(
                f"--mesa-holdout path does not exist: {args.mesa_holdout}",
                file=sys.stderr,
            )
            return 1
        if not args.prior.exists():
            print(
                f"--prior path does not exist: {args.prior}",
                file=sys.stderr,
            )
            return 1
        prior = PopulationPriorRepository.load(args.prior)
        if prior is None:
            print(
                f"Failed to load --prior pickle: {args.prior} "
                "(see WARN logs for details).",
                file=sys.stderr,
            )
            return 1
        try:
            holdout = _parse_holdout_csv(args.mesa_holdout)
        except (OSError, ValueError) as exc:
            print(
                f"Failed to parse --mesa-holdout CSV: {exc}",
                file=sys.stderr,
            )
            return 1
        if not holdout:
            print(
                f"--mesa-holdout CSV {args.mesa_holdout} produced 0 rows.",
                file=sys.stderr,
            )
            return 1
        mesa_holdout_path = args.mesa_holdout
        prior_path = args.prior

    logger.info(
        "Computing RMSE on %d subject-nights (synthetic=%s).",
        len(holdout), args.synthetic,
    )
    result = _evaluate(prior=prior, holdout=holdout)
    if result["n_subjects"] == 0:
        print(
            "No subject-nights matched any prior bucket — aborting.",
            file=sys.stderr,
        )
        return 1

    sha7 = _git_short_sha()
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{args.out_prefix}_prior_rmse_{sha7}.md"
    png_path = out_dir / f"{args.out_prefix}_prior_rmse_{sha7}.png"

    if not _HAS_MATPLOTLIB:
        logger.info(
            "matplotlib unavailable; PNG will be skipped (markdown only).",
        )
    png_written = _write_png(
        png_path,
        global_rows=result["global"]["rows"],  # type: ignore[index]
    )
    if png_written:
        logger.info("Wrote RMSE curves: %s", png_path)

    _write_md(
        md_path,
        result=result,
        seed=seed,
        mesa_holdout=mesa_holdout_path,
        prior_path=prior_path,
        synthetic=args.synthetic,
        sha7=sha7,
        png_path=png_path,
        png_written=png_written,
    )
    logger.info("Wrote RMSE summary: %s", md_path)

    print(
        f"\nRMSE evaluation complete:\n"
        f"  subjects:   {result['n_subjects']}\n"
        f"  nights:     {result['n_total_nights']}\n"
        f"  output:     {md_path}\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI passthrough
    raise SystemExit(main())
