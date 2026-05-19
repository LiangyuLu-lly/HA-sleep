"""``eval_stage_predictor_hitrate.py`` —— EMST 60s 提前命中率离线评估（R15.1 方向 4）.

This script is the offline evaluator for `Requirement 15.1
<../.kiro/specs/algorithmic-moat-v3.0.0/requirements.md>`_ direction 4
and design §3.8.6: it measures how often the EMST stage predictor
(``src/stage_predictor.py``) correctly anticipates the next sleep
stage on a Sleep-EDF test split, broken down per source stage, plus
the per-call inference latency distribution (p50 / p95 / p99) needed
to validate the R9.4 50 ms budget.  The output is a markdown report
(with optional matplotlib bar chart) whose filename suffix carries
the 7-character git commit hash (R15.5)::

    <out-prefix>_predictor_hitrate_<sha7>.md
    <out-prefix>_predictor_hitrate_<sha7>.png    # only when matplotlib is present

Two operational modes
---------------------

* **Real test mode** (default).  ``--edf-test`` must point at a JSONL
  file produced by the EDF preprocessing pipeline (one record per
  prediction window, fields ``hrv_ms / motion_au /
  breathing_rate_bpm`` arrays + ``current_stage`` /
  ``actual_stage_60s_later``); ``--model`` must point at a
  ``stage_predictor.onnx`` artifact ≤ 80 KB (R9.2).  The evaluator
  loads the model via :class:`src.stage_predictor.StagePredictor`,
  drives :meth:`StagePredictor.predict` over the windows, and
  reports per-stage hit rate (``LIGHT → DEEP``, ``DEEP → REM``, etc.)
  alongside the latency distribution.
* **Synthetic mode** (``--synthetic``).  Skips real EDF + ONNX I/O
  entirely.  Generates a fake prediction sequence in-memory using a
  simple deterministic accuracy model (≈ 75% hit rate by default,
  configurable through ``--seed``) plus a Gaussian latency
  distribution (μ = 8 ms, σ = 4 ms, clipped at 0).  This mirrors the
  ``--synthetic`` pattern used by ``scripts/train_stage_predictor.py``
  and ``scripts/eval_population_prior_rmse.py`` so CI / local dev can
  smoke-test the report-generation path without shipping the (large)
  Sleep-EDF data or a trained ONNX artifact.

CLI contract (design §3.8.6)::

    --edf-test     <path>   Sleep-EDF test JSONL (required)
    --model        <path>   stage_predictor.onnx (required)
    --seed         <int>    RNG seed, default 20260518 (R15.5)
    --out-prefix   <str>    output filename prefix, default "predictor"
    --out-dir      <path>   output directory, default cwd
    --synthetic             skip --edf-test / --model I/O and use a
                            deterministic in-memory fake (CI smoke)

Exit codes
----------

* ``0`` — OK, report written.
* ``1`` — invalid arguments (missing inputs in non-synthetic mode,
  unparseable JSONL, model load failure, etc.).

Soft dependencies
-----------------

* ``matplotlib`` is imported lazily and skipped silently when missing
  (PR4 / R12.5).  When unavailable the bar-chart PNG is omitted; the
  markdown report is always produced.
* ``numpy`` is the only hard dep beyond stdlib (used for percentile
  arithmetic + RMSE-style aggregation).
* ``onnxruntime`` is **only** imported by
  :class:`src.stage_predictor.StagePredictor` and only when the real
  test mode is used.  ``--synthetic`` does **not** require
  ``onnxruntime`` to be installed — important for CI matrices that
  do not pin the runtime extras.

:Validates: Requirements 15.1, 15.2, 15.5
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

import numpy as np

# ---------------------------------------------------------------------------
# Make ``src`` importable when running from the repo root.  Mirrors the
# convention used by ``scripts/eval_bayesian_regret.py`` /
# ``scripts/eval_causal_synthetic.py``.
# ---------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
_REPO_ROOT_STR: str = str(REPO_ROOT)
if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)

logger = logging.getLogger("eval_stage_predictor_hitrate")


# ---------------------------------------------------------------------------
# Constants — keep aligned with src.stage_predictor + design §3.8.6.
# ---------------------------------------------------------------------------

#: Default RNG seed (R15.5).  Matches the project-wide convention.
DEFAULT_SEED: int = 20260518

#: Default output filename prefix.  Combined with the git-sha suffix
#: produces ``predictor_predictor_hitrate_<sha7>.md``.
DEFAULT_OUT_PREFIX: str = "predictor"

#: Canonical stage names — must match
#: :data:`src.stage_predictor._STAGE_NAMES`.  Re-declared locally so
#: the evaluator does not require ``src.stage_predictor`` to import in
#: ``--synthetic`` mode (which deliberately does not pull onnxruntime).
_STAGE_NAMES: tuple[str, ...] = ("AWAKE", "LIGHT", "DEEP", "REM")

#: R9.4 latency budget — surfaced verbatim in the report.
_LATENCY_BUDGET_MS: float = 50.0

#: R10.4 hit-rate floor — surfaced verbatim in the report.
_HIT_RATE_FLOOR: float = 0.70

#: Window length expected by the EMST input contract (R9.3): 5 minutes
#: at 1 Hz = 300 samples.  Used by the synthetic-mode generator.
_WINDOW_SAMPLES: int = 300

#: Highlighted transitions called out in the markdown summary.  These
#: are the two transitions explicitly mentioned in design §3.4.1
#: (``LIGHT → DEEP`` and ``DEEP → REM``); the report still lists every
#: observed transition, but these two get an emphasised callout row.
_HIGHLIGHTED_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("LIGHT", "DEEP"),
    ("DEEP", "REM"),
)


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

    Mirrors :func:`scripts.eval_population_prior_rmse._git_short_sha`.
    Best effort, never raises.
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
# Real-mode test-set parsing
# ---------------------------------------------------------------------------


class _TestWindow:
    """One record from the EDF test JSONL (or its synthetic equivalent)."""

    __slots__ = (
        "hrv_ms",
        "motion_au",
        "breathing_rate_bpm",
        "current_stage",
        "actual_stage_60s_later",
    )

    def __init__(
        self,
        *,
        hrv_ms: tuple[float | None, ...],
        motion_au: tuple[float | None, ...],
        breathing_rate_bpm: tuple[float | None, ...],
        current_stage: str,
        actual_stage_60s_later: str,
    ) -> None:
        self.hrv_ms = hrv_ms
        self.motion_au = motion_au
        self.breathing_rate_bpm = breathing_rate_bpm
        self.current_stage = current_stage
        self.actual_stage_60s_later = actual_stage_60s_later


def _parse_test_jsonl(path: Path) -> list[_TestWindow]:
    """Parse the Sleep-EDF test JSONL into a list of :class:`_TestWindow`.

    Records that lack required fields (or have wrong-length channel
    arrays) are logged + skipped.  Fatal errors (e.g. file missing,
    JSON parse error on the entire file) raise.
    """
    rows: list[_TestWindow] = []
    with path.open("r", encoding="utf-8-sig") as fh:
        for line_idx, raw_line in enumerate(fh, start=1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "Skipping line %d of %s (invalid JSON): %s",
                    line_idx, path, exc,
                )
                continue
            try:
                hrv = tuple(record["hrv_ms"])
                motion = tuple(record["motion_au"])
                breathing = tuple(record["breathing_rate_bpm"])
                cur = str(record["current_stage"])
                actual = str(record["actual_stage_60s_later"])
            except (KeyError, TypeError) as exc:
                logger.warning(
                    "Skipping line %d of %s (missing field): %s",
                    line_idx, path, exc,
                )
                continue
            if (
                len(hrv) != _WINDOW_SAMPLES
                or len(motion) != _WINDOW_SAMPLES
                or len(breathing) != _WINDOW_SAMPLES
            ):
                logger.warning(
                    "Skipping line %d of %s (channel length != %d)",
                    line_idx, path, _WINDOW_SAMPLES,
                )
                continue
            rows.append(
                _TestWindow(
                    hrv_ms=hrv,
                    motion_au=motion,
                    breathing_rate_bpm=breathing,
                    current_stage=cur,
                    actual_stage_60s_later=actual,
                )
            )
    return rows


# ---------------------------------------------------------------------------
# Real-mode evaluation: drive StagePredictor.predict
# ---------------------------------------------------------------------------


async def _evaluate_real(
    *,
    model_path: Path,
    audit_jsonl: Path,
    windows: list[_TestWindow],
) -> tuple[list[tuple[str, str, str, float]], list[float], int]:
    """Drive :class:`StagePredictor` over the test set.

    Returns a tuple ``(events, latencies_ms, n_skipped)`` where:

    * ``events``: list of ``(current_stage, predicted_stage,
      actual_stage_60s_later, confidence)`` tuples — one per
      successful prediction.
    * ``latencies_ms``: list of per-call ``inference_ms`` values for
      successful predictions.
    * ``n_skipped``: number of windows for which
      :meth:`StagePredictor.predict` returned ``None`` (insufficient
      window, predictor disabled, or load failure).
    """
    # Local import: keeps ``--help`` / synthetic mode runnable on
    # machines without onnxruntime installed.
    from src.stage_predictor import (  # noqa: WPS433 — lazy import
        PredictorInput,
        StagePredictor,
        _STAGE_NAMES as PREDICTOR_STAGE_NAMES,
    )

    # Sanity-check that our local stage-name tuple matches the one
    # baked into the predictor (defensive — drift would silently
    # corrupt the per-stage hit-rate table).
    assert PREDICTOR_STAGE_NAMES == _STAGE_NAMES, (
        f"stage-name drift: {PREDICTOR_STAGE_NAMES} vs {_STAGE_NAMES}"
    )

    predictor = StagePredictor.try_load(
        model_path=model_path,
        audit_jsonl=audit_jsonl,
    )
    if predictor is None:
        raise RuntimeError(
            f"StagePredictor.try_load returned None for model={model_path}; "
            "either onnxruntime is missing, the model file is absent, "
            "or it exceeds the 80 KB R9.2 cap."
        )

    events: list[tuple[str, str, str, float]] = []
    latencies: list[float] = []
    n_skipped = 0
    for window in windows:
        pi = PredictorInput(
            hrv_ms=window.hrv_ms,
            motion_au=window.motion_au,
            breathing_rate_bpm=window.breathing_rate_bpm,
        )
        out = await predictor.predict(pi)
        if out is None or not out.is_valid:
            n_skipped += 1
            continue
        # Argmax → predicted stage name.
        probs = (out.p_awake, out.p_light, out.p_deep, out.p_rem)
        pred_idx = max(range(4), key=lambda i: probs[i])
        predicted = _STAGE_NAMES[pred_idx]
        events.append(
            (
                window.current_stage,
                predicted,
                window.actual_stage_60s_later,
                float(out.confidence),
            )
        )
        latencies.append(float(out.inference_ms))
    return events, latencies, n_skipped


# ---------------------------------------------------------------------------
# Synthetic mode — fake prediction stream
# ---------------------------------------------------------------------------


def _synthesize_events(
    *,
    rng: random.Random,
    n_events: int = 240,
    base_accuracy: float = 0.78,
) -> tuple[list[tuple[str, str, str, float]], list[float]]:
    """Generate a deterministic fake prediction stream + latencies.

    The stream simulates ``n_events`` prediction events.  The
    ``current_stage`` is sampled with a realistic frequency (LIGHT
    dominates at ≈ 50%, DEEP and REM at ≈ 20% each, AWAKE at ≈ 10%).
    The ``actual_stage_60s_later`` is sampled from a transition matrix
    biased toward staying in the same stage.  The predictor is
    correct with probability ``base_accuracy`` per call; on a miss it
    picks one of the other 3 stages uniformly.

    Latencies are drawn from a clipped Gaussian centred at 8 ms with
    σ = 4 ms — well below the R9.4 50 ms budget — plus a 1% chance of
    a "slow" sample at 35–48 ms so the p99 row is non-trivial.

    The defaults produce a sample large enough that every highlighted
    transition has at least a few dozen events, yet small enough that
    the synthetic eval finishes in well under a second.
    """
    # Realistic stationary distribution for the current stage.
    stage_weights: tuple[tuple[str, float], ...] = (
        ("AWAKE", 0.10),
        ("LIGHT", 0.50),
        ("DEEP",  0.20),
        ("REM",   0.20),
    )
    stages = [name for name, _ in stage_weights]
    weights = [w for _, w in stage_weights]

    # Transition matrix — diagonal heavy, with the highlighted
    # forward transitions LIGHT→DEEP and DEEP→REM at non-trivial mass.
    transitions: dict[str, tuple[tuple[str, float], ...]] = {
        "AWAKE": (("AWAKE", 0.55), ("LIGHT", 0.40), ("DEEP", 0.03), ("REM", 0.02)),
        "LIGHT": (("AWAKE", 0.10), ("LIGHT", 0.55), ("DEEP", 0.25), ("REM", 0.10)),
        "DEEP":  (("AWAKE", 0.02), ("LIGHT", 0.20), ("DEEP", 0.55), ("REM", 0.23)),
        "REM":   (("AWAKE", 0.05), ("LIGHT", 0.25), ("DEEP", 0.10), ("REM", 0.60)),
    }

    events: list[tuple[str, str, str, float]] = []
    latencies: list[float] = []
    for _ in range(n_events):
        current = rng.choices(stages, weights=weights, k=1)[0]
        next_stages, next_weights = zip(*transitions[current], strict=True)
        actual = rng.choices(
            list(next_stages),
            weights=list(next_weights),
            k=1,
        )[0]
        # The synthetic predictor's "best guess" is the most likely
        # transition (matches ``actual`` in expectation).  On miss,
        # pick one of the other 3 uniformly.
        if rng.random() < base_accuracy:
            predicted = actual
            confidence = rng.uniform(0.60, 0.95)
        else:
            others = [s for s in stages if s != actual]
            predicted = rng.choice(others)
            confidence = rng.uniform(0.40, 0.65)
        events.append((current, predicted, actual, round(confidence, 3)))

        # Latency distribution: 99% fast Gaussian + 1% slow tail.
        if rng.random() < 0.01:
            latency = rng.uniform(35.0, 48.0)
        else:
            latency = max(0.5, rng.gauss(8.0, 4.0))
        latencies.append(round(latency, 3))
    return events, latencies


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _per_source_stage_hit_rate(
    events: Iterable[tuple[str, str, str, float]],
) -> dict[str, dict[str, float]]:
    """Compute hit rate stratified by source ``current_stage``.

    Returns a dict ``{stage: {hit_rate, n_events, n_correct}}``.
    """
    counts: dict[str, list[int]] = {s: [0, 0] for s in _STAGE_NAMES}
    for current, predicted, actual, _conf in events:
        if current not in counts:
            counts[current] = [0, 0]
        counts[current][0] += 1
        if predicted == actual:
            counts[current][1] += 1
    out: dict[str, dict[str, float]] = {}
    for stage, (n_events, n_correct) in counts.items():
        rate = (n_correct / n_events) if n_events > 0 else float("nan")
        out[stage] = {
            "n_events": float(n_events),
            "n_correct": float(n_correct),
            "hit_rate": rate,
        }
    return out


def _per_transition_hit_rate(
    events: Iterable[tuple[str, str, str, float]],
) -> dict[tuple[str, str], dict[str, float]]:
    """Compute hit rate stratified by ``(current → actual)`` pair.

    Useful for surfacing the highlighted ``LIGHT → DEEP`` and
    ``DEEP → REM`` transitions called out by design §3.4.1.  Bucket
    keys are the *true* transitions (``current_stage`` →
    ``actual_stage_60s_later``); the predictor's job is to put mass
    on ``actual`` rather than on a different stage.
    """
    counts: dict[tuple[str, str], list[int]] = {}
    for current, predicted, actual, _conf in events:
        key = (current, actual)
        bucket = counts.setdefault(key, [0, 0])
        bucket[0] += 1
        if predicted == actual:
            bucket[1] += 1
    out: dict[tuple[str, str], dict[str, float]] = {}
    for key, (n_events, n_correct) in counts.items():
        rate = (n_correct / n_events) if n_events > 0 else float("nan")
        out[key] = {
            "n_events": float(n_events),
            "n_correct": float(n_correct),
            "hit_rate": rate,
        }
    return out


def _latency_percentiles(latencies: list[float]) -> dict[str, float]:
    """Return p50 / p95 / p99 / max latencies in milliseconds.

    Returns NaN values when ``latencies`` is empty so the markdown
    table can still render the metadata row gracefully.
    """
    if not latencies:
        return {
            "n": 0.0,
            "p50": float("nan"),
            "p95": float("nan"),
            "p99": float("nan"),
            "max": float("nan"),
            "mean": float("nan"),
        }
    arr = np.asarray(latencies, dtype=float)
    return {
        "n": float(arr.size),
        "p50": float(np.percentile(arr, 50.0)),
        "p95": float(np.percentile(arr, 95.0)),
        "p99": float(np.percentile(arr, 99.0)),
        "max": float(arr.max()),
        "mean": float(arr.mean()),
    }


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def _format_pct(x: float) -> str:
    if not np.isfinite(x):
        return "n/a"
    return f"{x * 100.0:.1f}%"


def _format_ms(x: float) -> str:
    if not np.isfinite(x):
        return "n/a"
    return f"{x:.2f} ms"


def _write_png(
    out_path: Path,
    *,
    per_stage: dict[str, dict[str, float]],
    latencies: list[float],
) -> bool:
    """Emit a 2-panel chart: per-stage hit rate + latency histogram."""
    if not _HAS_MATPLOTLIB:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stages = list(_STAGE_NAMES)
    rates = [
        per_stage[s]["hit_rate"] if s in per_stage
        and np.isfinite(per_stage[s]["hit_rate"]) else 0.0
        for s in stages
    ]
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5), dpi=120)

    axes[0].bar(stages, rates, color="tab:blue")
    axes[0].axhline(
        _HIT_RATE_FLOOR, color="tab:red", linestyle="--",
        label=f"R10.4 floor = {_HIT_RATE_FLOOR:.0%}",
    )
    axes[0].set_ylim(0.0, 1.05)
    axes[0].set_ylabel("Hit rate (60s ahead)")
    axes[0].set_title("Per-source-stage 60s anticipation hit rate")
    axes[0].grid(True, axis="y", alpha=0.3)
    axes[0].legend(loc="lower right")

    if latencies:
        axes[1].hist(latencies, bins=30, color="tab:orange",
                     edgecolor="black", linewidth=0.4)
        axes[1].axvline(
            _LATENCY_BUDGET_MS, color="tab:red", linestyle="--",
            label=f"R9.4 budget = {_LATENCY_BUDGET_MS:.0f} ms",
        )
        axes[1].set_xlabel("Inference latency (ms)")
        axes[1].set_ylabel("# predictions")
        axes[1].set_title("Per-call inference latency distribution")
        axes[1].grid(True, axis="y", alpha=0.3)
        axes[1].legend(loc="upper right")
    else:
        axes[1].text(
            0.5, 0.5, "no latencies (predictor disabled)",
            ha="center", va="center", transform=axes[1].transAxes,
        )
        axes[1].set_axis_off()

    fig.suptitle(
        "Stage predictor anticipation hit rate + inference latency "
        "(eval-only artifact)"
    )
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)
    return True


def _write_md(
    out_path: Path,
    *,
    events: list[tuple[str, str, str, float]],
    latencies: list[float],
    per_stage: dict[str, dict[str, float]],
    per_transition: dict[tuple[str, str], dict[str, float]],
    latency_pct: dict[str, float],
    seed: int,
    edf_test: Path | None,
    model_path: Path | None,
    synthetic: bool,
    n_skipped: int,
    sha7: str,
    png_path: Path,
    png_written: bool,
) -> None:
    """Emit the markdown hit-rate report.

    The markdown is the **authoritative** output (matplotlib is a
    soft dep); it always includes:

    * the run metadata block (mode, seed, dataset paths, sha7);
    * the overall hit rate;
    * the per-source-stage hit rate table (with R10.4 floor callout);
    * the highlighted transition rows (``LIGHT → DEEP``, ``DEEP →
      REM``);
    * the latency percentile table (with R9.4 budget callout);
    * the standard "局限性" paragraph (R15.3).
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n_events = len(events)
    n_correct = sum(1 for _c, p, a, _ in events if p == a)
    overall = (n_correct / n_events) if n_events > 0 else float("nan")

    lines: list[str] = []
    lines.append(f"# Stage Predictor 60s 命中率评估报告（{sha7}）")
    lines.append("")
    lines.append(
        "> 由 `scripts/eval_stage_predictor_hitrate.py` 生成；该脚本"
        "在 Sleep-EDF 测试切分（或 `--synthetic` 合成数据）上度量 EMST"
        "60 秒提前命中率与单次推理延迟分布（R15.1 / R15.2 / R15.5）。"
    )
    lines.append("")
    lines.append("## 1. 运行元数据")
    lines.append("")
    lines.append(f"- 生成时间（UTC）：{now_utc}")
    lines.append(f"- 模式：`{'synthetic' if synthetic else 'real-test'}`")
    lines.append(
        f"- `--edf-test`：`{edf_test if edf_test else '（合成模式）'}`"
    )
    lines.append(
        f"- `--model`：`{model_path if model_path else '（合成模式）'}`"
    )
    lines.append(f"- `--seed`：{seed}")
    lines.append(f"- git commit：`{sha7}`")
    lines.append(f"- 成功预测数：**{n_events}**")
    lines.append(f"- 跳过窗口数（窗口不全 / 推理无效 / 推理 disabled）：{n_skipped}")
    lines.append(f"- 整体命中率：**{_format_pct(overall)}**")
    lines.append("")

    lines.append("## 2. 按源 stage 分类的 60s 提前命中率")
    lines.append("")
    lines.append(
        f"R10.4 命中率下限 **{_format_pct(_HIT_RATE_FLOOR)}**：连续 3 晚低于"
        "此阈值会触发 `predictor_status = auto_disabled`。下表中以 ❌ 标注"
        "低于阈值的源 stage。"
    )
    lines.append("")
    lines.append(
        "| 源 stage | n_events | n_correct | hit_rate | ≥ R10.4? |"
    )
    lines.append("|---|---|---|---|---|")
    for stage in _STAGE_NAMES:
        info = per_stage.get(stage, {})
        n_ev = int(info.get("n_events", 0.0))
        n_co = int(info.get("n_correct", 0.0))
        rate = float(info.get("hit_rate", float("nan")))
        if n_ev == 0 or not np.isfinite(rate):
            ok = "n/a"
        else:
            ok = "✅" if rate >= _HIT_RATE_FLOOR else "❌"
        lines.append(
            f"| `{stage}` | {n_ev} | {n_co} | {_format_pct(rate)} | {ok} |"
        )
    lines.append("")

    lines.append("## 3. 重点 stage 切换命中率（design §3.4.1）")
    lines.append("")
    lines.append(
        "EMST 提前控制路径仅对慢响应设备（climate / humidifier）生效；"
        "其中最具收益的两个切换是 `LIGHT → DEEP`（深睡前预冷）与 "
        "`DEEP → REM`（REM 前微抬亮度温度）。下表是这两个 *真实* 切换"
        "对应的命中率。"
    )
    lines.append("")
    lines.append(
        "| 真实切换 | n_events | n_correct | hit_rate |"
    )
    lines.append("|---|---|---|---|")
    for src, dst in _HIGHLIGHTED_TRANSITIONS:
        info = per_transition.get((src, dst), {})
        n_ev = int(info.get("n_events", 0.0))
        n_co = int(info.get("n_correct", 0.0))
        rate = float(info.get("hit_rate", float("nan")))
        lines.append(
            f"| `{src} → {dst}` | {n_ev} | {n_co} | {_format_pct(rate)} |"
        )
    lines.append("")

    lines.append("## 4. 推理延迟分布")
    lines.append("")
    lines.append(
        f"R9.4 单次推理预算 **{_LATENCY_BUDGET_MS:.0f} ms**：超过"
        "即记一次错误，连续 3 次后冷却 1 小时（`disabled_until`）。"
        "p95 与 p99 应该都明显低于此阈值；如果 p99 接近预算，需要"
        "复检 ONNX 量化与硬件占用。"
    )
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 样本数 n | {int(latency_pct['n'])} |")
    lines.append(f"| 平均 | {_format_ms(latency_pct['mean'])} |")
    lines.append(f"| p50 | {_format_ms(latency_pct['p50'])} |")
    lines.append(f"| p95 | {_format_ms(latency_pct['p95'])} |")
    lines.append(f"| p99 | {_format_ms(latency_pct['p99'])} |")
    lines.append(f"| 最大 | {_format_ms(latency_pct['max'])} |")
    lines.append(
        f"| R9.4 预算 | {_LATENCY_BUDGET_MS:.2f} ms "
        + (
            "（p99 通过）"
            if np.isfinite(latency_pct["p99"])
            and latency_pct["p99"] <= _LATENCY_BUDGET_MS
            else "（请复检）"
        )
        + " |"
    )
    lines.append("")

    lines.append("## 5. 输出文件")
    lines.append("")
    lines.append(f"- 摘要：`{out_path.name}`")
    if png_written:
        lines.append(f"- 图表：`{png_path.name}`")
    else:
        lines.append(
            "- 图表：未生成（`matplotlib` 缺失 —— 训练 / 评估"
            "环境请通过 `pip install -r requirements-train.txt` 安装；"
            "summary 仍然产出）。"
        )
    lines.append("")

    lines.append("## 6. 评估方法与局限")
    lines.append("")
    lines.append(
        "- 真实模式（`--edf-test` + `--model`）通过 "
        "`src.stage_predictor.StagePredictor.try_load` 加载 ONNX，"
        "对每条 5 分钟窗口调用 `predict()` 并比对 60 秒后的真实 stage。"
    )
    lines.append(
        "- 合成模式（`--synthetic`）跳过磁盘 I/O 与 onnxruntime，"
        "通过确定性伪随机生成 240 条预测事件 + 高斯延迟分布，主要"
        "目的是在 CI / 本地开发环境验证脚本管线，固定 `--seed` 即可"
        "复现（R15.5）。合成的命中率不代表真实性能。"
    )
    lines.append(
        "- 报告基于 IID 假设；真实部署中数据非 IID（季节切换、"
        "设备故障、生活变化），预测器在跨人群迁移时可能出现性能退化。"
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
        prog="eval_stage_predictor_hitrate",
        description=(
            "Evaluate the EMST stage predictor's 60-second-ahead hit "
            "rate on a Sleep-EDF test split (R15.1 direction 4 / "
            "design §3.8.6).  Produces a markdown summary with "
            "per-stage hit rate + latency p50/p95/p99 and an optional "
            "matplotlib bar chart; both filenames carry the git short "
            "SHA per Requirement 15.5."
        ),
    )
    parser.add_argument(
        "--edf-test",
        type=Path,
        required=False,
        help=(
            "Path to the Sleep-EDF test JSONL (required unless "
            "--synthetic is set).  See module docstring for the "
            "expected fields."
        ),
    )
    parser.add_argument(
        "--model",
        type=Path,
        required=False,
        help=(
            "Path to a stage_predictor.onnx (required unless "
            "--synthetic is set).  Must be ≤ 80 KB per R9.2."
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
            "The full filenames are "
            "<prefix>_predictor_hitrate_<sha7>.md and "
            "<prefix>_predictor_hitrate_<sha7>.png."
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
            "Skip the real Sleep-EDF test split and the ONNX runtime; "
            "use a deterministic in-memory fake prediction stream "
            "(CI smoke).  Useful when onnxruntime is not installed."
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

    if args.synthetic:
        logger.info(
            "Running in synthetic mode (seed=%d): skipping --edf-test / "
            "--model I/O.",
            seed,
        )
        rng = random.Random(seed)
        events, latencies = _synthesize_events(rng=rng)
        n_skipped = 0
        edf_test_path: Path | None = None
        model_path: Path | None = None
    else:
        if args.edf_test is None or args.model is None:
            print(
                "--edf-test and --model are required unless --synthetic "
                "is set.",
                file=sys.stderr,
            )
            return 1
        if not args.edf_test.exists():
            print(
                f"--edf-test path does not exist: {args.edf_test}",
                file=sys.stderr,
            )
            return 1
        if not args.model.exists():
            print(
                f"--model path does not exist: {args.model}",
                file=sys.stderr,
            )
            return 1
        try:
            windows = _parse_test_jsonl(args.edf_test)
        except (OSError, ValueError) as exc:
            print(
                f"Failed to parse --edf-test JSONL: {exc}",
                file=sys.stderr,
            )
            return 1
        if not windows:
            print(
                f"--edf-test JSONL {args.edf_test} produced 0 windows.",
                file=sys.stderr,
            )
            return 1
        # Use a per-run temp directory for the predictor's audit
        # JSONL — the eval is read-only with respect to /data.
        with tempfile.TemporaryDirectory(
            prefix="eval_stage_predictor_hitrate_",
        ) as td:
            audit_jsonl = Path(td) / "predictor_audit.jsonl"
            try:
                events, latencies, n_skipped = asyncio.run(
                    _evaluate_real(
                        model_path=args.model,
                        audit_jsonl=audit_jsonl,
                        windows=windows,
                    )
                )
            except RuntimeError as exc:
                print(
                    f"Predictor evaluation failed: {exc}",
                    file=sys.stderr,
                )
                return 1
        edf_test_path = args.edf_test
        model_path = args.model

    if not events:
        print(
            "No successful predictions produced — aborting "
            "(skipped=%d)." % n_skipped,
            file=sys.stderr,
        )
        return 1

    per_stage = _per_source_stage_hit_rate(events)
    per_transition = _per_transition_hit_rate(events)
    latency_pct = _latency_percentiles(latencies)

    sha7 = _git_short_sha()
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{args.out_prefix}_predictor_hitrate_{sha7}.md"
    png_path = out_dir / f"{args.out_prefix}_predictor_hitrate_{sha7}.png"

    if not _HAS_MATPLOTLIB:
        logger.info(
            "matplotlib unavailable; PNG will be skipped (markdown only).",
        )
    png_written = _write_png(
        png_path,
        per_stage=per_stage,
        latencies=latencies,
    )
    if png_written:
        logger.info("Wrote hit-rate chart: %s", png_path)

    _write_md(
        md_path,
        events=events,
        latencies=latencies,
        per_stage=per_stage,
        per_transition=per_transition,
        latency_pct=latency_pct,
        seed=seed,
        edf_test=edf_test_path,
        model_path=model_path,
        synthetic=args.synthetic,
        n_skipped=n_skipped,
        sha7=sha7,
        png_path=png_path,
        png_written=png_written,
    )
    logger.info("Wrote hit-rate summary: %s", md_path)

    n_correct = sum(1 for _c, p, a, _ in events if p == a)
    overall = (n_correct / len(events)) if events else 0.0
    print(
        f"\nStage predictor hit-rate evaluation complete:\n"
        f"  events:       {len(events)} (skipped {n_skipped})\n"
        f"  overall hit:  {overall * 100.0:.1f}%\n"
        f"  p95 latency:  {latency_pct['p95']:.2f} ms "
        f"(budget {_LATENCY_BUDGET_MS:.0f} ms)\n"
        f"  output:       {md_path}\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover — CLI passthrough
    raise SystemExit(main())
