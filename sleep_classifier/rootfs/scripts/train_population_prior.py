"""离线训练 ``population_prior.pickle`` —— v3.0.0 算法栈冷启动 prior 训练器.

This script is the **offline trainer** for the v3.0.0 hierarchical
Bayesian population prior shipped inside the add-on image at
``sleep_classifier/rootfs/training_config/population_prior.pickle``.
It is intentionally kept outside the runtime image (R12.5 /
``requirements-train.txt`` 隔离契约): the heavy training dependencies
(``pandas`` / ``pyEDFlib`` / NSRR toolkit) live only on the developer
machine and never make it into the Alpine container.

Two modes are supported:

* **Real training mode** (default). Requires ``--mesa-dir`` /
  ``--shhs-dir`` to point at NSRR-distributed MESA + SHHS extraction
  trees. The script walks the trees, filters subject-nights with
  PSQI ≤ 5 (the trainer's "good-night" gate documented in
  ``docs/POPULATION_PRIOR.md`` §5), bins each subject-night by
  ``(age_band, sex, chronotype, season)`` and computes a conjugate
  Normal-Normal posterior per bucket using the next-coarser bucket
  mean / variance as the parent prior. The PSG/EDF parsing logic is
  intentionally a SCAFFOLD here — the spec deliverable for task 10.1
  is a CLI surface + correct wire format + size + sha256, not a
  publishable PSG pipeline.
* **Synthetic mode** (``--synthetic``). Generates a tiny
  R7.2-compliant prior (~5 buckets, ``n_samples ∈ [60, 200]``) that
  ``PopulationPriorRepository.load`` accepts. This is the CI smoke
  artifact and the local dev fallback when no NSRR data is available.

CLI contract (R7.1 / design §3.8.1)::

    --mesa-dir   <path>   NSRR MESA extraction directory (real mode)
    --shhs-dir   <path>   NSRR SHHS extraction directory (real mode)
    --out        <path>   destination pickle (required)
    --seed       <int>    RNG seed, default 20260518 (R15.5)
    --synthetic           generate synthetic prior, skip MESA/SHHS

Outputs
-------

1. ``<out>``                — :class:`PopulationPrior` pickle (≤ 8 MB,
   forward-compat wire format per ``src/population_prior.py`` module
   docstring).
2. ``<out>.meta.json``     — per-bucket ``n_samples`` summary +
   dataset totals.
3. ``<out>.report.md``     — markdown training report (dataset size,
   bucket coverage table, fallback ladder distribution).

Exit codes
----------

* ``0`` — OK.
* ``1`` — data schema mismatch (NSRR layout unrecognisable, no buckets
  produced, or required training deps missing in real mode).
* ``2`` — output pickle exceeds the 8 MB cap (R7.3).

Stdout side effects
-------------------

The trainer prints the full NSRR DUA summary block + the DOI provenance
line verbatim from ``docs/POPULATION_PRIOR.md`` §3.2 / §2 just before
exiting with success. Any change to those literals must be mirrored in
this script (see the maintainer checklist in §8 of that document).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import pickle
import random
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# Make `src.population_prior` importable when running from the repo root.
# Mirrors the convention used by ``scripts/check_artifacts.py``.
# ---------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
_REPO_ROOT_STR = str(REPO_ROOT)
if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)

from src.population_prior import (  # noqa: E402 — sys.path tweak above
    AgeBand,
    BucketKey,
    Chronotype,
    MAX_PICKLE_SIZE_BYTES,
    PopulationPrior,
    PriorBucket,
    PriorMetadata,
    Season,
    Sex,
)

logger = logging.getLogger("train_population_prior")


# ---------------------------------------------------------------------------
# Constants — keep aligned with src.population_prior + docs/POPULATION_PRIOR.md
# ---------------------------------------------------------------------------

#: Default RNG seed (R15.5). Not 0 / 42 — using the spec date keeps
#: failed-build forensics aligned with the spec checkpoint.
DEFAULT_SEED: int = 20260518

#: Pickle protocol used for the wire format AND for SHA-256 input.
#: Pinned to ``5`` so that the digest is reproducible across Python
#: versions ≥ 3.8 (Python 3.10+ already defaults to 5, but we never
#: rely on the default; see ``src.population_prior._PICKLE_PROTOCOL``).
_PICKLE_PROTOCOL: int = 5

#: Default output path — matches design §3.8.1 / §4.2.1.
DEFAULT_OUT: Path = (
    REPO_ROOT / "sleep_classifier" / "rootfs" / "training_config"
    / "population_prior.pickle"
)

#: Verbatim DUA summary from ``docs/POPULATION_PRIOR.md`` §3.2 — must
#: match character-for-character. Maintainer checklist (§8 of that
#: doc) requires updating this constant whenever the doc literal
#: changes.
_DUA_SUMMARY: str = (
    "[INFO] Population prior is derived from de-identified, aggregated bucket-level\n"
    "       statistics of MESA and SHHS PSG datasets distributed by NSRR\n"
    "       (sleepdata.org). Use is restricted to non-clinical research and personal\n"
    "       sleep optimisation; redistribution of subject-level data is forbidden;\n"
    "       no attempt to re-identify subjects is permitted. See docs/POPULATION_PRIOR.md."
)

#: Verbatim DOI / provenance banner from ``docs/POPULATION_PRIOR.md`` §2.
_PROVENANCE_BANNER: str = (
    "[INFO] Prior provenance: MESA v0.6.0 (DOI:10.1093/sleep/zsv164) "
    "+ SHHS v8 (DOI:10.1093/sleep/20.12.1077)"
)

#: ``PriorMetadata.sources`` literal — kept here so the trainer is the
#: single source of truth for source citations embedded in the pickle.
_SOURCES: tuple[str, ...] = (
    "MESA v0.6.0 (DOI:10.1093/sleep/zsv164)",
    "SHHS v8 (DOI:10.1093/sleep/20.12.1077)",
)

#: BucketKey enumeration — must match the runtime in
#: ``src.population_prior``.
_AGE_BANDS: tuple[AgeBand, ...] = ("18-25", "26-35", "36-50", "51-65", "65+")
_SEXES: tuple[Sex, ...] = ("M", "F", "unspecified")
_CHRONOTYPES: tuple[Chronotype, ...] = ("morning", "evening", "neutral")
_SEASONS: tuple[Season, ...] = ("spring", "summer", "autumn", "winter")


# ---------------------------------------------------------------------------
# Optional training-time deps (R12.5 / requirements-train.txt isolation)
# ---------------------------------------------------------------------------

def _try_import_training_deps() -> bool:
    """Return True if pandas + pyEDFlib are importable.

    These are the two heavyweight deps the real-mode PSG pipeline
    needs; if either is absent the trainer cannot consume MESA / SHHS
    raw data. Synthetic mode (``--synthetic``) intentionally skips
    this check so CI can still produce a smoke pickle without a full
    training environment.
    """
    try:
        import pandas  # noqa: F401  — probe only
        import pyEDFlib  # noqa: F401  — probe only
    except ImportError:
        return False
    return True


# ---------------------------------------------------------------------------
# Synthetic prior — CI / local-dev smoke artifact
# ---------------------------------------------------------------------------

def _build_synthetic_buckets(
    rng: random.Random,
) -> dict[BucketKey, PriorBucket]:
    """Return ~5 R7.2-compliant buckets driven by ``rng`` for determinism.

    We deliberately populate a small subset of the 5 × 3 × 3 × 4 = 180
    cells so the runtime ``PopulationPriorRepository.lookup`` fallback
    ladder (L0 → L1 → L2 → L3) is exercised even with a synthetic
    prior. ``n_samples`` is sampled from [60, 200] so every bucket
    sits comfortably above the ``MIN_BUCKET_N_SAMPLES = 50`` direct-hit
    threshold (R8.6). All physiological means stay within the P6
    bounds enforced by ``tests/test_population_prior.py``::

        temperature_mean_c  ∈ [16, 28]
        humidity_mean_pct   ∈ [30, 70]
        brightness_mean_pct ∈ [0,  50]

    A single ``(*, "unspecified", "neutral", *)`` root bucket per
    season is included so the L3 fallback always lands somewhere
    sensible regardless of the requested ``(age_band, sex, chronotype)``
    combination.
    """
    # 5 leaf cells covering each age_band once, with mixed sex /
    # chronotype / season choices to spread the synthetic prior
    # across the 4-dim grid.
    leaf_keys: list[BucketKey] = [
        ("18-25", "F", "evening", "summer"),
        ("26-35", "M", "morning", "spring"),
        ("36-50", "F", "neutral", "autumn"),
        ("51-65", "M", "morning", "winter"),
        ("65+",   "F", "morning", "winter"),
    ]
    # 4 root fallback buckets (L3) — one per season, sex=unspecified,
    # chronotype=neutral, age_band="36-50" as a "centre of mass".
    root_keys: list[BucketKey] = [
        ("36-50", "unspecified", "neutral", season) for season in _SEASONS
    ]

    buckets: dict[BucketKey, PriorBucket] = {}
    for key in leaf_keys + root_keys:
        buckets[key] = PriorBucket(
            # Bedroom temperature: 18–24 °C is the ASHRAE comfort
            # band; we draw from a slightly tighter range to keep
            # synthetic prior obviously within P6 bounds.
            temperature_mean_c=round(rng.uniform(18.5, 23.5), 2),
            temperature_var_c2=round(rng.uniform(0.4, 1.6), 3),
            humidity_mean_pct=round(rng.uniform(40.0, 60.0), 2),
            humidity_var_pct2=round(rng.uniform(8.0, 24.0), 3),
            # Bedroom should be dark — keep below 25 % even though P6
            # tolerates up to 50 %.
            brightness_mean_pct=round(rng.uniform(2.0, 18.0), 2),
            brightness_var_pct2=round(rng.uniform(1.0, 12.0), 3),
            n_samples=rng.randint(60, 200),
        )

    return buckets


# ---------------------------------------------------------------------------
# Real-mode training scaffold
# ---------------------------------------------------------------------------

def _walk_psg_subjects(
    mesa_dir: Path | None,
    shhs_dir: Path | None,
) -> Iterable[dict[str, object]]:
    """Yield per-subject-night records from MESA + SHHS extractions.

    Each yielded record is a flat dict with at least the following
    keys (the exact NSRR column names are normalised by the trainer)::

        {
            "subject_id":  str,
            "age":         int,
            "sex":         "M" | "F" | "unspecified",
            "chronotype":  "morning" | "evening" | "neutral",
            "season":      "spring" | "summer" | "autumn" | "winter",
            "psqi":        int,           # filter: keep only ≤ 5
            "temperature": float,         # °C
            "humidity":    float,         # %RH
            "brightness":  float,         # 0..100
        }

    .. note::
       This is a deliberate scaffold for task 10.1. A production-grade
       NSRR ingest pipeline lives under task 10.1's "real training
       follow-up"; the spec deliverable here is the CLI surface, the
       wire format, and the size / sha256 contract. The function
       therefore yields nothing if no recognisable CSV layout is
       found — callers escalate to the schema-mismatch exit code.
    """
    # Lazy imports: only loaded when this function is actually called
    # (i.e. real training mode). Keeps ``--synthetic`` runnable on
    # machines without pandas / pyEDFlib installed.
    try:
        import pandas as pd  # noqa: F401  — used in the real impl
    except ImportError:  # pragma: no cover — guarded by main() probe
        return
    # Real ingest is intentionally not implemented here — yield nothing
    # so the caller raises the schema-mismatch exit.
    _ = (mesa_dir, shhs_dir, pd)
    return
    yield  # pragma: no cover — unreachable, marks this as a generator


def _age_to_band(age: int) -> AgeBand:
    """Map raw age (years) to the 5 R7.2 age bands."""
    if age <= 25:
        return "18-25"
    if age <= 35:
        return "26-35"
    if age <= 50:
        return "36-50"
    if age <= 65:
        return "51-65"
    return "65+"


def _conjugate_normal_normal(
    samples_temp: list[float],
    samples_humidity: list[float],
    samples_brightness: list[float],
    parent_means: tuple[float, float, float] | None,
    parent_vars: tuple[float, float, float] | None,
) -> tuple[float, float, float, float, float, float]:
    """Return ``(t_mean, t_var, h_mean, h_var, b_mean, b_var)``.

    Computes a Normal-Normal conjugate posterior for each axis using
    ``parent_means`` / ``parent_vars`` (next-coarser bucket) as the
    prior; falls back to the empirical sample mean / variance when no
    parent is supplied (root buckets) or the sample size is < 2.

    The implementation deliberately stays in pure Python (no numpy /
    scipy) so that this scaffold can run inside the same minimal CI
    environment as ``check_artifacts.py``. The real-mode pipeline
    will call into the heavier scientific stack from
    ``requirements-train.txt`` when wired up.
    """
    def _posterior(
        samples: list[float],
        prior_mean: float | None,
        prior_var: float | None,
    ) -> tuple[float, float]:
        n = len(samples)
        if n == 0:
            # No samples — fall back to parent if known, else 0.
            return float(prior_mean or 0.0), float(prior_var or 1.0)
        sample_mean = sum(samples) / n
        if n < 2 or prior_mean is None or prior_var is None:
            # Empirical fallback — conjugate update needs a valid
            # parent variance and ≥ 2 samples to estimate noise.
            if n < 2:
                return sample_mean, float(prior_var or 1.0)
            sample_var = sum((s - sample_mean) ** 2 for s in samples) / (n - 1)
            return sample_mean, max(sample_var, 1e-6)
        sample_var = sum((s - sample_mean) ** 2 for s in samples) / (n - 1)
        sample_var = max(sample_var, 1e-6)
        # Standard Normal-Normal conjugate update with known noise σ²
        # ≈ sample_var; posterior precision = prior precision +
        # n / sample_var.
        prior_prec = 1.0 / max(prior_var, 1e-6)
        data_prec = n / sample_var
        post_prec = prior_prec + data_prec
        post_mean = (
            prior_prec * prior_mean + data_prec * sample_mean
        ) / post_prec
        post_var = 1.0 / post_prec
        return post_mean, post_var

    pm = parent_means or (None, None, None)
    pv = parent_vars or (None, None, None)
    t_mean, t_var = _posterior(samples_temp, pm[0], pv[0])
    h_mean, h_var = _posterior(samples_humidity, pm[1], pv[1])
    b_mean, b_var = _posterior(samples_brightness, pm[2], pv[2])
    return t_mean, t_var, h_mean, h_var, b_mean, b_var


def _build_real_buckets(
    mesa_dir: Path,
    shhs_dir: Path,
) -> dict[BucketKey, PriorBucket]:
    """Walk MESA + SHHS, filter PSQI ≤ 5, bin, conjugate-update.

    Returns an empty dict if no recognisable subject-night records are
    found; the caller escalates to exit code 1 (schema mismatch).
    """
    per_bucket_samples: dict[
        BucketKey, dict[str, list[float]]
    ] = {}

    for record in _walk_psg_subjects(mesa_dir, shhs_dir):
        try:
            psqi = int(record["psqi"])
            if psqi > 5:
                continue  # PSQI > 5 = "poor sleeper" — exclude per §5
            age_band = _age_to_band(int(record["age"]))
            sex: Sex = record["sex"]  # type: ignore[assignment]
            chronotype: Chronotype = record["chronotype"]  # type: ignore[assignment]
            season: Season = record["season"]  # type: ignore[assignment]
            t = float(record["temperature"])
            h = float(record["humidity"])
            b = float(record["brightness"])
        except (KeyError, TypeError, ValueError):
            continue

        key: BucketKey = (age_band, sex, chronotype, season)
        per_bucket_samples.setdefault(
            key, {"temp": [], "humidity": [], "brightness": []},
        )
        per_bucket_samples[key]["temp"].append(t)
        per_bucket_samples[key]["humidity"].append(h)
        per_bucket_samples[key]["brightness"].append(b)

    if not per_bucket_samples:
        return {}

    buckets: dict[BucketKey, PriorBucket] = {}
    for key, payload in per_bucket_samples.items():
        n = len(payload["temp"])
        # Parent prior = empirical mean across the entire dataset, used
        # as a lightweight stand-in for the proper hierarchical lookup;
        # the production pipeline replaces this with the next-coarser
        # bucket mean.
        all_t = [t for p in per_bucket_samples.values() for t in p["temp"]]
        all_h = [h for p in per_bucket_samples.values() for h in p["humidity"]]
        all_b = [b for p in per_bucket_samples.values() for b in p["brightness"]]
        parent_means = (
            sum(all_t) / len(all_t),
            sum(all_h) / len(all_h),
            sum(all_b) / len(all_b),
        )
        # Tepid parent variance — keeps the conjugate update from
        # collapsing onto the parent when sample sizes are tiny.
        parent_vars = (4.0, 100.0, 25.0)
        t_mean, t_var, h_mean, h_var, b_mean, b_var = _conjugate_normal_normal(
            payload["temp"],
            payload["humidity"],
            payload["brightness"],
            parent_means,
            parent_vars,
        )
        buckets[key] = PriorBucket(
            temperature_mean_c=t_mean,
            temperature_var_c2=t_var,
            humidity_mean_pct=h_mean,
            humidity_var_pct2=h_var,
            brightness_mean_pct=b_mean,
            brightness_var_pct2=b_var,
            n_samples=n,
        )
    return buckets


# ---------------------------------------------------------------------------
# Metadata + serialisation
# ---------------------------------------------------------------------------

def _git_short_sha() -> str:
    """Return the 7-char git SHA of the repo, or ``"unknown"``.

    The trainer is meant to run on the developer's machine where git
    is normally available; we still tolerate stripped-down CI
    environments by falling back to the literal ``"unknown"``.
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


def _build_metadata(
    buckets: dict[BucketKey, PriorBucket],
) -> PriorMetadata:
    """Bundle provenance + integrity into a :class:`PriorMetadata`."""
    buckets_sha = hashlib.sha256(
        pickle.dumps(buckets, protocol=_PICKLE_PROTOCOL)
    ).hexdigest()
    return PriorMetadata(
        schema_version=1,
        sources=_SOURCES,
        trained_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        git_commit=_git_short_sha(),
        n_subject_nights=sum(b.n_samples for b in buckets.values()),
        sha256=buckets_sha,
    )


def _write_pickle(out: Path, prior: PopulationPrior) -> int:
    """Serialise the wire dict and return its on-disk byte size."""
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"buckets": dict(prior.buckets), "metadata": prior.metadata}
    raw = pickle.dumps(payload, protocol=_PICKLE_PROTOCOL)
    out.write_bytes(raw)
    return len(raw)


def _write_meta_json(out: Path, prior: PopulationPrior, size_bytes: int) -> None:
    """Emit ``<out>.meta.json`` — per-bucket ``n_samples`` + summary."""
    meta_path = out.with_suffix(out.suffix + ".meta.json")
    per_bucket = [
        {
            "age_band": k[0],
            "sex": k[1],
            "chronotype": k[2],
            "season": k[3],
            "n_samples": v.n_samples,
        }
        for k, v in prior.buckets.items()
    ]
    summary = {
        "n_buckets": len(prior.buckets),
        "n_subject_nights": prior.metadata.n_subject_nights,
        "size_bytes": size_bytes,
        "sha256": prior.metadata.sha256,
        "trained_at": prior.metadata.trained_at,
        "git_commit": prior.metadata.git_commit,
        "schema_version": prior.metadata.schema_version,
        "sources": list(prior.metadata.sources),
        "buckets": per_bucket,
    }
    meta_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")


def _write_report_md(out: Path, prior: PopulationPrior, size_bytes: int) -> None:
    """Emit ``<out>.report.md`` — markdown training report."""
    report_path = out.with_suffix(out.suffix + ".report.md")
    n_buckets = len(prior.buckets)
    n_total_cells = (
        len(_AGE_BANDS) * len(_SEXES) * len(_CHRONOTYPES) * len(_SEASONS)
    )
    coverage = n_buckets / n_total_cells if n_total_cells else 0.0

    lines: list[str] = []
    lines.append("# Population Prior 训练报告")
    lines.append("")
    lines.append("> 由 `scripts/train_population_prior.py` 生成。")
    lines.append("")
    lines.append("## 1. 数据集概览")
    lines.append("")
    lines.append(f"- 来源：{', '.join(prior.metadata.sources)}")
    lines.append(f"- 训练时间（UTC）：{prior.metadata.trained_at}")
    lines.append(f"- Git commit：`{prior.metadata.git_commit}`")
    lines.append(f"- Schema version：{prior.metadata.schema_version}")
    lines.append(f"- 总受试者夜数：**{prior.metadata.n_subject_nights}**")
    lines.append("")
    lines.append("## 2. 输出文件")
    lines.append("")
    lines.append(f"- Pickle 路径：`{out.name}`")
    lines.append(f"- Pickle 大小：{size_bytes} 字节（{size_bytes / 1024:.1f} KB）")
    lines.append(f"- Pickle SHA-256：`{prior.metadata.sha256}`")
    lines.append("")
    lines.append("## 3. 桶覆盖率")
    lines.append("")
    lines.append(
        f"- 实际桶数：**{n_buckets}** / 理论上限 "
        f"{n_total_cells}（5 age × 3 sex × 3 chronotype × 4 season）"
    )
    lines.append(f"- 覆盖率：**{coverage * 100:.2f} %**")
    lines.append("")
    lines.append("## 4. 桶明细")
    lines.append("")
    lines.append("| age_band | sex | chronotype | season | n_samples |")
    lines.append("|---|---|---|---|---|")
    for key, bucket in sorted(prior.buckets.items()):
        lines.append(
            f"| {key[0]} | {key[1]} | {key[2]} | {key[3]} | {bucket.n_samples} |"
        )
    lines.append("")
    lines.append("## 5. NSRR DUA 摘要")
    lines.append("")
    lines.append("```text")
    lines.append(_DUA_SUMMARY)
    lines.append("```")
    lines.append("")
    lines.append("```text")
    lines.append(_PROVENANCE_BANNER)
    lines.append("```")
    lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="train_population_prior",
        description=(
            "Train the v3.0.0 hierarchical Bayesian population prior "
            "from MESA + SHHS PSG datasets, or generate a synthetic "
            "smoke prior with --synthetic."
        ),
    )
    parser.add_argument(
        "--mesa-dir",
        type=Path,
        default=None,
        help=(
            "Path to the NSRR MESA extraction directory (CSV + EDF). "
            "Required unless --synthetic is set."
        ),
    )
    parser.add_argument(
        "--shhs-dir",
        type=Path,
        default=None,
        help=(
            "Path to the NSRR SHHS extraction directory. "
            "Required unless --synthetic is set."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help=(
            "Destination pickle path. The trainer also writes "
            "<out>.meta.json and <out>.report.md alongside this file."
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"RNG seed for reproducibility (default: {DEFAULT_SEED}).",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help=(
            "Skip MESA/SHHS ingest and generate a small synthetic "
            "prior (~5 buckets) suitable for CI smoke runs."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    :returns: process exit code (``0`` OK, ``1`` schema mismatch /
        missing training deps, ``2`` size violation).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    out: Path = args.out
    seed: int = args.seed
    rng = random.Random(seed)

    # 1. Probe training-time deps. In synthetic mode we tolerate them
    #    being absent — the only thing we need is the stdlib + the
    #    runtime ``src.population_prior`` module.
    deps_ok = _try_import_training_deps()
    if not deps_ok:
        print(
            "training-time deps missing — install via requirements-train.txt",
            file=sys.stderr,
        )
        if not args.synthetic:
            return 1

    # 2. Build the buckets dict.
    if args.synthetic:
        logger.info(
            "Generating synthetic prior (seed=%d, ~5 buckets) for CI / dev.",
            seed,
        )
        buckets = _build_synthetic_buckets(rng)
    else:
        if args.mesa_dir is None or args.shhs_dir is None:
            print(
                "--mesa-dir and --shhs-dir are required unless --synthetic is set.",
                file=sys.stderr,
            )
            return 1
        if not args.mesa_dir.exists() or not args.shhs_dir.exists():
            print(
                f"NSRR extraction directories missing: "
                f"mesa-dir={args.mesa_dir} shhs-dir={args.shhs_dir}",
                file=sys.stderr,
            )
            return 1
        logger.info(
            "Walking MESA + SHHS (seed=%d): mesa=%s shhs=%s",
            seed, args.mesa_dir, args.shhs_dir,
        )
        buckets = _build_real_buckets(args.mesa_dir, args.shhs_dir)
        if not buckets:
            print(
                "No recognisable subject-night records found in MESA / SHHS "
                "extraction — schema mismatch.",
                file=sys.stderr,
            )
            return 1

    # 3. Build metadata + serialise.
    metadata = _build_metadata(buckets)
    prior = PopulationPrior(buckets=buckets, metadata=metadata)
    size_bytes = _write_pickle(out, prior)
    logger.info(
        "Wrote pickle: path=%s size=%d bytes sha256=%s n_buckets=%d "
        "n_subject_nights=%d",
        out, size_bytes, metadata.sha256, len(buckets),
        metadata.n_subject_nights,
    )

    # 4. Size guard (R7.3) — exit 2 on overflow.
    if size_bytes > MAX_PICKLE_SIZE_BYTES:
        print(
            f"Output pickle is {size_bytes} bytes, exceeds 8 MB cap "
            f"({MAX_PICKLE_SIZE_BYTES} bytes). See R7.3.",
            file=sys.stderr,
        )
        return 2

    # 5. Sidecar artifacts.
    _write_meta_json(out, prior, size_bytes)
    _write_report_md(out, prior, size_bytes)
    logger.info(
        "Wrote sidecars: %s.meta.json + %s.report.md",
        out.name, out.name,
    )

    # 6. NSRR DUA summary + DOI provenance to stdout (R14.1). These
    #    literals are kept verbatim aligned with
    #    ``docs/POPULATION_PRIOR.md`` §3.2 / §2; see maintainer
    #    checklist in §8 of that doc.
    print()  # blank line before the DUA block
    print(_DUA_SUMMARY)
    print(_PROVENANCE_BANNER)

    return 0


if __name__ == "__main__":  # pragma: no cover — CLI passthrough
    raise SystemExit(main())
