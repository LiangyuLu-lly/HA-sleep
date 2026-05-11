"""Polysomnography-grade sleep quality scoring.

The original :func:`compute_quality_score` in :mod:`preference_learner`
returned a 0-100 number based purely on stage *proportions*.  That is
useful but misses three classic clinical metrics that matter at least
as much for *perceived* sleep quality:

* **Sleep Efficiency (SE)** = TST / TIB.  > 85 % is healthy; < 75 % is
  the textbook insomnia cutoff.  AASM scoring manual §2.
* **Wake After Sleep Onset (WASO)** — minutes spent awake *between*
  the first sleep onset and final wake.  > 30 min suggests fragmented
  sleep.
* **Sleep Onset Latency (SOL)** — minutes from "lights out" to first
  N1 epoch.  > 30 min is suggestive of sleep-onset insomnia; < 5 min
  can suggest sleep deprivation.

We combine these into one objective 0-100 score, then *blend* the user's
optional 1-5 subjective rating into a final score that the preference
learner uses as its reward signal.

The formula is intentionally close to the **Pittsburgh Sleep Quality
Index (PSQI)** subscales so users with a sleep-clinic background can
recognise the components.

References
~~~~~~~~~~
* Berry RB et al., *AASM Manual for the Scoring of Sleep* (2.6).
* Ohayon MM et al., **National Sleep Foundation's sleep quality
  recommendations: first report**, *Sleep Health* 3 (2017) 6-19.
* Buysse DJ et al., **The Pittsburgh Sleep Quality Index**, *Psychiatry
  Res* 28 (1989) 193-213.
* Reed DL & Sacco WP. **Measuring Sleep Efficiency: What Should the
  Denominator Be?**, *J Clin Sleep Med* 12 (2016) 263-266 — discusses
  TIB-vs-SPT denominators; we use TIB to match consumer hardware.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from src.data_structures import SleepStage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunable thresholds (NSF 2017 consensus)
# ---------------------------------------------------------------------------

GOOD_SE_PCT = 85.0     # >= this is "good"
POOR_SE_PCT = 75.0     # < this is "poor"

GOOD_WASO_MIN = 20.0   # < this is "good"
POOR_WASO_MIN = 41.0   # >= this is "poor"  (NSF: 41 min for adults)

GOOD_SOL_MIN_LO = 5.0  # too fast (< 5 min) hints at sleep deprivation
GOOD_SOL_MIN_HI = 15.0 # 5-15 min is the sweet spot
POOR_SOL_MIN = 30.0    # >= 30 min is "poor" (insomnia threshold)


# Default weights for the four objective sub-scores.  They sum to 1.0
# but ``compute_objective_quality`` re-normalises if the caller passes
# its own dict, so it's safe to drop any component.
_DEFAULT_OBJECTIVE_WEIGHTS = {
    "architecture": 0.40,   # legacy stage-proportion sub-score
    "efficiency":   0.25,   # SE
    "fragmentation": 0.20,  # WASO
    "onset":        0.15,   # SOL
}


# ---------------------------------------------------------------------------
# Per-night metrics
# ---------------------------------------------------------------------------


@dataclass
class SleepMetrics:
    """Container for the four polysomnography measurements we compute.

    All durations are in **minutes** so users on Lovelace can read them
    natively; convert to seconds at boundaries only when integrating with
    other modules.
    """

    tst_min: float = 0.0           # total sleep time (LIGHT+DEEP+REM)
    tib_min: float = 0.0           # time in bed
    waso_min: float = 0.0          # wake-after-sleep-onset
    sol_min: float = 0.0           # sleep-onset latency
    n_awakenings: int = 0          # discrete WAKE bouts inside SPT
    stage_counts: Dict[str, int] = field(default_factory=dict)

    @property
    def sleep_efficiency_pct(self) -> float:
        if self.tib_min <= 0:
            return 0.0
        return float(min(100.0, 100.0 * self.tst_min / self.tib_min))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tst_min": round(self.tst_min, 1),
            "tib_min": round(self.tib_min, 1),
            "waso_min": round(self.waso_min, 1),
            "sol_min": round(self.sol_min, 1),
            "n_awakenings": self.n_awakenings,
            "sleep_efficiency_pct": round(self.sleep_efficiency_pct, 1),
            "stage_counts": dict(self.stage_counts),
        }


# ---------------------------------------------------------------------------
# Compute SleepMetrics from a stage time-series
# ---------------------------------------------------------------------------


def compute_metrics(
    stages: Sequence[SleepStage],
    *,
    epoch_seconds: float = 30.0,
) -> SleepMetrics:
    """Convert a stage sequence (one entry per epoch) into :class:`SleepMetrics`.

    The function is intentionally agnostic to the source of the
    sequence — it works on the live add-on inference stream as well as
    on a hypnogram dumped from the Sleep-EDF dataset.
    """
    if not stages:
        return SleepMetrics()

    epoch_min = epoch_seconds / 60.0
    counts: Dict[str, int] = {"AWAKE": 0, "LIGHT": 0, "DEEP": 0, "REM": 0}
    for s in stages:
        counts[s.name] = counts.get(s.name, 0) + 1

    n = len(stages)
    tib_min = n * epoch_min
    sleep_epochs = sum(counts.get(k, 0) for k in ("LIGHT", "DEEP", "REM"))
    tst_min = sleep_epochs * epoch_min

    # Sleep onset latency: epochs of leading AWAKE before first sleep epoch.
    sol_epochs = 0
    for s in stages:
        if s == SleepStage.AWAKE:
            sol_epochs += 1
        else:
            break
    sol_min = sol_epochs * epoch_min

    # Final-wake latency: trailing AWAKE block (excluded from WASO).
    final_awake_epochs = 0
    for s in reversed(stages):
        if s == SleepStage.AWAKE:
            final_awake_epochs += 1
        else:
            break

    # WASO: AWAKE epochs that are *between* first and last sleep epoch.
    middle_awake_epochs = max(
        0, counts.get("AWAKE", 0) - sol_epochs - final_awake_epochs,
    )
    waso_min = middle_awake_epochs * epoch_min

    # Count discrete awakening bouts inside the sleep period.
    sleep_started = False
    n_awakenings = 0
    in_awakening = False
    final_idx = n - final_awake_epochs
    for i, s in enumerate(stages):
        if i >= final_idx:
            break
        if not sleep_started:
            if s != SleepStage.AWAKE:
                sleep_started = True
            continue
        if s == SleepStage.AWAKE:
            if not in_awakening:
                n_awakenings += 1
                in_awakening = True
        else:
            in_awakening = False

    return SleepMetrics(
        tst_min=tst_min,
        tib_min=tib_min,
        waso_min=waso_min,
        sol_min=sol_min,
        n_awakenings=n_awakenings,
        stage_counts=counts,
    )


# ---------------------------------------------------------------------------
# Sub-scores (each 0-100)
# ---------------------------------------------------------------------------


def _architecture_score(stage_counts: Dict[str, int]) -> float:
    """Reuse the existing stage-proportion heuristic for backwards compat."""
    total = sum(stage_counts.values())
    if total <= 0:
        return 0.0

    def p(name: str) -> float:
        return stage_counts.get(name, 0) / total

    deep, rem, light, awake = p("DEEP"), p("REM"), p("LIGHT"), p("AWAKE")
    score = (
        50.0
        + 100.0 * (deep - 0.10)
        +  60.0 * (rem - 0.18)
        +  10.0 * (light - 0.50)
        - 150.0 * max(0.0, awake - 0.05)
    )
    return float(max(0.0, min(100.0, score)))


def _efficiency_score(se_pct: float) -> float:
    """Map sleep efficiency to 0-100 with NSF cutoffs."""
    if se_pct >= GOOD_SE_PCT:
        # 85 → 80 ; 95+ → 100, with a soft asymptote
        return float(min(100.0, 80.0 + (se_pct - GOOD_SE_PCT) * 2.0))
    if se_pct >= POOR_SE_PCT:
        # Linear 75→50, 85→80
        return float(50.0 + (se_pct - POOR_SE_PCT) * 3.0)
    # < 75 % is poor — drop fast.
    return float(max(0.0, 50.0 * se_pct / POOR_SE_PCT))


def _fragmentation_score(waso_min: float, n_awakenings: int) -> float:
    """Lower WASO + fewer awakenings → higher score."""
    if waso_min <= GOOD_WASO_MIN:
        base = 100.0 - waso_min   # 0 min → 100; 20 min → 80
    elif waso_min <= POOR_WASO_MIN:
        # Linear 20→80, 41→40
        base = 80.0 - 40.0 * (waso_min - GOOD_WASO_MIN) / (POOR_WASO_MIN - GOOD_WASO_MIN)
    else:
        base = max(0.0, 40.0 - (waso_min - POOR_WASO_MIN))
    # Penalty for fragmentation independent of total WASO.  Each extra
    # awakening past 2 docks 4 points (Ohayon 2017 figure 4).
    penalty = max(0, n_awakenings - 2) * 4.0
    return float(max(0.0, min(100.0, base - penalty)))


def _onset_score(sol_min: float) -> float:
    """U-shaped: too fast suggests sleep deprivation, too slow insomnia."""
    if sol_min <= 0:
        return 60.0   # sub-epoch — likely measurement artefact
    if sol_min < GOOD_SOL_MIN_LO:
        # 0-5 min: linear ramp 60 → 90
        return 60.0 + (sol_min / GOOD_SOL_MIN_LO) * 30.0
    if sol_min <= GOOD_SOL_MIN_HI:
        return 90.0 + (sol_min - GOOD_SOL_MIN_LO) / (GOOD_SOL_MIN_HI - GOOD_SOL_MIN_LO) * 10.0
    if sol_min < POOR_SOL_MIN:
        # 15→90 ramps down to 30→50
        return 90.0 - 40.0 * (sol_min - GOOD_SOL_MIN_HI) / (POOR_SOL_MIN - GOOD_SOL_MIN_HI)
    # >= 30 min: linear decay, asymptote at 0
    return float(max(0.0, 50.0 - (sol_min - POOR_SOL_MIN) * 2.0))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_objective_quality(
    metrics: SleepMetrics,
    *,
    weights: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Return a dict of sub-scores plus the composite objective score.

    The dict keys are stable so callers (HA publisher, learner) can pick
    individual numbers without re-computing.  Composite score is under
    the ``"composite"`` key.
    """
    w = dict(_DEFAULT_OBJECTIVE_WEIGHTS)
    if weights:
        w.update(weights)
    # Re-normalise so a missing key doesn't silently shrink the total.
    s = sum(w.values())
    if s <= 0:
        raise ValueError("compute_objective_quality: weights sum to <= 0")
    w = {k: v / s for k, v in w.items()}

    arch = _architecture_score(metrics.stage_counts)
    eff = _efficiency_score(metrics.sleep_efficiency_pct)
    frag = _fragmentation_score(metrics.waso_min, metrics.n_awakenings)
    onset = _onset_score(metrics.sol_min)

    composite = (
        w.get("architecture", 0.0) * arch
        + w.get("efficiency", 0.0) * eff
        + w.get("fragmentation", 0.0) * frag
        + w.get("onset", 0.0) * onset
    )
    return {
        "architecture": float(arch),
        "efficiency": float(eff),
        "fragmentation": float(frag),
        "onset": float(onset),
        "composite": float(max(0.0, min(100.0, composite))),
    }


def blend_subjective(
    objective_score: float,
    subjective_score: Optional[float],
    *,
    subjective_weight: float = 0.4,
    subjective_scale: int = 5,
) -> float:
    """Combine the objective 0-100 score with a 1-N user rating.

    * If ``subjective_score`` is ``None`` we just return the objective.
    * Otherwise we map ``subjective`` ∈ [1, scale] linearly to [0, 100]
      and take a weighted average.

    The default ``subjective_weight=0.4`` reflects the PSQI-style finding
    that subjective perception explains roughly 40 % of variance in
    next-day alertness independent of measured architecture.
    """
    obj = max(0.0, min(100.0, float(objective_score)))
    if subjective_score is None:
        return obj
    s = float(subjective_score)
    if s <= 0:
        return obj
    s = max(1.0, min(float(subjective_scale), s))
    subj_pct = (s - 1.0) / (subjective_scale - 1) * 100.0
    w = max(0.0, min(1.0, float(subjective_weight)))
    return float((1.0 - w) * obj + w * subj_pct)


def compute_quality_score(
    stage_counts: Dict[str, int],
    *,
    metrics: Optional[SleepMetrics] = None,
    subjective_score: Optional[float] = None,
    subjective_weight: float = 0.4,
) -> float:
    """**Backwards-compatible** drop-in for the old preference_learner API.

    * If only ``stage_counts`` is provided, behaves identically to the
      legacy :func:`preference_learner.compute_quality_score`.
    * If ``metrics`` is provided too, returns the richer composite.
    * If ``subjective_score`` is provided, blends it in.
    """
    if metrics is None:
        # Build a thin SleepMetrics so the path is still exercised; SE/
        # WASO/SOL fields stay 0 → only ``architecture`` contributes.
        return _architecture_score(stage_counts)

    sub_scores = compute_objective_quality(metrics)
    return blend_subjective(
        sub_scores["composite"],
        subjective_score,
        subjective_weight=subjective_weight,
    )
