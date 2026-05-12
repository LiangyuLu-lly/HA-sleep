"""Apnea / hypopnea trend detector — v1.6.0 PoC.

What this is
------------

A pure-function pipeline that turns a stream of breathing-rate +
chest-wall-amplitude samples into a coarse-grained nightly **trend
indicator** (red / amber / green / calibrating / pending_consent).

What this is **not**:

* It is **not** a clinical apnea-hypopnea index (AHI).  We never
  publish a numeric AHI to HA — only the trend bucket above — for
  three reasons documented in `docs/BACKLOG.md`:

  1. AHI is a clinical metric.  A user reading "AHI = 7" on a
     dashboard might mistake it for a diagnosis.
  2. Threshold calibration without polysomnography ground truth on
     real users is unreliable.  The trend bucket admits that
     uncertainty by being deliberately fuzzy.
  3. The first ~7 nights are baseline-collection — any number we
     could publish in that window is meaningless.

* It is **not** wired into the orchestrator yet.  Wiring requires
  new config slots (`apnea_breathing_source`, an `input_boolean` for
  consent, a sensor entity registration in the publisher), which
  belong in a separate v1.7 release that can ship the consent
  onboarding flow as one coherent story.

The PoC here is the *algorithm + tests*, ready for v1.7 to wire up.

Algorithm
---------

1. **Calibration.**  For the first ``calibration_nights`` recorded
   nights, accumulate per-night percentiles of breathing rate and
   amplitude.  Use the median as each user's baseline.  Until then
   the detector outputs ``calibrating``.

2. **Sliding window** of length ``window_seconds`` (default 60 s),
   hop ``hop_seconds`` (default 10 s).  For each window:

   * **Apneic event**: a contiguous interval of ≥
     ``min_event_seconds`` (default 10 s) where the breathing rate
     drops below ``apneic_bpm`` (default 4 bpm) **or** the chest-wall
     amplitude is below ``apneic_amp_floor`` (default 0.05) of the
     baseline.

   * **Hypopneic event**: a contiguous ≥ 10 s interval where
     breathing rate is below 50 % of the user's baseline **and**
     chest-wall amplitude is below 70 % of baseline.

3. **Per-night roll-up**: count events per hour of recorded sleep.
   The result is a *rate*, not an absolute count, so a 4-hour nap
   isn't compared apples-to-oranges with an 8-hour night.

4. **Trend bucket**: events/hour < 5 = green, 5-15 = amber,
   ≥ 15 = red.  These cut-offs are the AASM clinical buckets but we
   do **not** label them "AHI" anywhere user-facing.

The whole module is dependency-free (no scipy / numpy) so it works on
the Pi 4B add-on image without bloat.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class ApneaTrend(Enum):
    """Coarse-grained user-facing trend.

    The string values are the HA sensor states.  ``pending_consent``
    is the boot-time default until the user toggles the consent
    input_boolean — see the BACKLOG entry for the rationale.
    """
    PENDING_CONSENT = "pending_consent"
    CALIBRATING = "calibrating"
    GREEN = "green"
    AMBER = "amber"
    RED = "red"


@dataclass
class BreathingSample:
    """One reading from a breathing-capable sensor (e.g. R60ABD1).

    ``timestamp`` is unix seconds.  ``rate_bpm`` is breaths per minute.
    ``amplitude`` is a unitless 0..1 value normalised by the radar
    firmware (different vendors normalise differently — that's
    handled by the per-user baseline below).  Either field may be
    ``None`` if the sensor dropped that signal for this tick.
    """
    timestamp: float
    rate_bpm: Optional[float] = None
    amplitude: Optional[float] = None


@dataclass
class ApneaEvent:
    """A single apneic or hypopneic event detected within a night.

    Carries enough info to debug a borderline classification without
    surfacing it as a numeric AHI to the user.
    """
    kind: str                       # "apnea" | "hypopnea"
    start_ts: float
    duration_s: float
    confidence: float               # 0..1; how clean was the signal


@dataclass
class NightSummary:
    """Per-night roll-up, ready to translate to an :class:`ApneaTrend`.

    ``signal_coverage_fraction`` is the fraction of the recorded
    minutes that had a usable signal — sleeping on stomach often
    blocks the chest channel.  Used by the trend mapper to demote
    noisy nights to ``CALIBRATING`` instead of falsely going red.
    """
    events: List[ApneaEvent] = field(default_factory=list)
    recorded_seconds: float = 0.0
    signal_coverage_fraction: float = 0.0

    @property
    def events_per_hour(self) -> float:
        if self.recorded_seconds <= 0.0:
            return 0.0
        return len(self.events) / (self.recorded_seconds / 3600.0)


@dataclass
class UserBaseline:
    """Median resting breathing rate / amplitude for one user.

    Computed across the first ``calibration_nights`` worth of valid
    samples.  Persisted so we don't re-calibrate every restart.
    """
    rate_bpm_median: float
    amplitude_median: float
    nights_observed: int = 0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class ApneaDetectorConfig:
    """Tuning knobs.  Defaults are AASM-aligned but deliberately
    conservative — designed so a healthy sleeper sees almost-always
    GREEN and only a real respiratory issue trips AMBER.
    """
    # Minimum nights of baseline data before we leave CALIBRATING.
    calibration_nights: int = 7

    # Sliding-window dims.
    window_seconds: float = 60.0
    hop_seconds: float = 10.0
    min_event_seconds: float = 10.0

    # Apneic-event thresholds.
    apneic_bpm: float = 4.0           # breaths per minute below which
    apneic_amp_floor: float = 0.05    # OR amplitude / baseline below

    # Hypopneic-event thresholds (ratios vs user's baseline).
    hypopneic_rate_ratio: float = 0.50
    hypopneic_amp_ratio: float = 0.70

    # Trend cut-offs (events/hour).
    amber_threshold: float = 5.0      # AASM mild OSA = AHI 5-15
    red_threshold: float = 15.0       # AASM moderate OSA = AHI ≥ 15

    # Drop a night entirely if signal coverage is below this fraction.
    min_signal_coverage: float = 0.3


# ---------------------------------------------------------------------------
# Calibration helper
# ---------------------------------------------------------------------------


def _median(values: Sequence[float]) -> Optional[float]:
    """Numpy-free median; returns None for an empty input."""
    cleaned = sorted(v for v in values if v is not None)
    if not cleaned:
        return None
    n = len(cleaned)
    if n % 2 == 1:
        return cleaned[n // 2]
    return 0.5 * (cleaned[n // 2 - 1] + cleaned[n // 2])


def compute_baseline(samples: Sequence[BreathingSample]) -> Optional[UserBaseline]:
    """Median of valid rate / amplitude samples → user's resting state.

    Returns ``None`` when there's no usable data (every sample's
    rate or amplitude is None).  Callers stay in ``CALIBRATING``.
    """
    rates = [s.rate_bpm for s in samples if s.rate_bpm is not None]
    amps = [s.amplitude for s in samples if s.amplitude is not None]
    rate_med = _median(rates)
    amp_med = _median(amps)
    if rate_med is None or amp_med is None:
        return None
    return UserBaseline(
        rate_bpm_median=float(rate_med),
        amplitude_median=float(amp_med),
        nights_observed=1,
    )


# ---------------------------------------------------------------------------
# Event detection
# ---------------------------------------------------------------------------


def _contiguous_runs(
    bools: Sequence[bool],
    timestamps: Sequence[float],
    min_run_seconds: float,
) -> List[tuple[int, int, float]]:
    """Yield ``(start_idx, end_idx_exclusive, duration_s)`` for each run.

    A "run" is a contiguous stretch of ``True`` in ``bools`` that
    spans at least ``min_run_seconds`` in wall-clock time according
    to ``timestamps``.  Used for apnea event detection where we want
    sustained rather than instantaneous threshold crossings.
    """
    runs: List[tuple[int, int, float]] = []
    n = len(bools)
    i = 0
    while i < n:
        if not bools[i]:
            i += 1
            continue
        j = i
        while j < n and bools[j]:
            j += 1
        # j is now the first index that is False (or past the end).
        duration = float(timestamps[j - 1] - timestamps[i]) if j > i else 0.0
        if duration >= min_run_seconds:
            runs.append((i, j, duration))
        i = j
    return runs


def detect_events(
    samples: Sequence[BreathingSample],
    baseline: UserBaseline,
    config: Optional[ApneaDetectorConfig] = None,
) -> List[ApneaEvent]:
    """Return every apneic + hypopneic event found in ``samples``.

    Pure function — no side effects, no I/O, deterministic given a
    deterministic input.  Test-friendly.
    """
    cfg = config or ApneaDetectorConfig()
    if not samples:
        return []
    samples = sorted(samples, key=lambda s: s.timestamp)

    # Per-sample threshold flags.  A None reading is treated as
    # "below threshold = False" rather than "missing = unknown" so a
    # sensor dropout doesn't fabricate an event.
    n = len(samples)
    timestamps = [s.timestamp for s in samples]

    apneic = [False] * n
    hypopneic = [False] * n
    for k, s in enumerate(samples):
        rate_low = (
            s.rate_bpm is not None
            and s.rate_bpm < cfg.apneic_bpm
        )
        amp_floor = (
            s.amplitude is not None
            and baseline.amplitude_median > 0
            and (s.amplitude / baseline.amplitude_median) < cfg.apneic_amp_floor
        )
        if rate_low or amp_floor:
            apneic[k] = True
            continue   # apnea takes precedence over hypopnea

        # Hypopnea: BOTH rate AND amplitude reduced relative to baseline.
        rate_dip = (
            s.rate_bpm is not None
            and baseline.rate_bpm_median > 0
            and (s.rate_bpm / baseline.rate_bpm_median) < cfg.hypopneic_rate_ratio
        )
        amp_dip = (
            s.amplitude is not None
            and baseline.amplitude_median > 0
            and (s.amplitude / baseline.amplitude_median) < cfg.hypopneic_amp_ratio
        )
        if rate_dip and amp_dip:
            hypopneic[k] = True

    events: List[ApneaEvent] = []
    for start, end, dur in _contiguous_runs(
        apneic, timestamps, cfg.min_event_seconds,
    ):
        events.append(ApneaEvent(
            kind="apnea",
            start_ts=timestamps[start],
            duration_s=dur,
            confidence=_run_confidence(samples[start:end]),
        ))
    for start, end, dur in _contiguous_runs(
        hypopneic, timestamps, cfg.min_event_seconds,
    ):
        events.append(ApneaEvent(
            kind="hypopnea",
            start_ts=timestamps[start],
            duration_s=dur,
            confidence=_run_confidence(samples[start:end]),
        ))
    events.sort(key=lambda e: e.start_ts)
    return events


def _run_confidence(samples: Sequence[BreathingSample]) -> float:
    """Fraction of samples in a run that had *any* valid reading.

    A run sustained by a steady stream of None → low confidence.
    A run made of clean readings → high confidence.
    """
    if not samples:
        return 0.0
    valid = sum(
        1 for s in samples
        if s.rate_bpm is not None or s.amplitude is not None
    )
    return valid / len(samples)


# ---------------------------------------------------------------------------
# Per-night roll-up + trend mapping
# ---------------------------------------------------------------------------


def summarise_night(
    samples: Sequence[BreathingSample],
    baseline: UserBaseline,
    config: Optional[ApneaDetectorConfig] = None,
) -> NightSummary:
    """Detect events and compute the per-night summary.

    Glue function — pure composition of :func:`detect_events` and
    coverage accounting.
    """
    cfg = config or ApneaDetectorConfig()
    if not samples:
        return NightSummary()

    events = detect_events(samples, baseline, cfg)
    sorted_samples = sorted(samples, key=lambda s: s.timestamp)
    recorded = float(sorted_samples[-1].timestamp - sorted_samples[0].timestamp)
    valid_count = sum(
        1 for s in sorted_samples
        if s.rate_bpm is not None or s.amplitude is not None
    )
    coverage = (valid_count / len(sorted_samples)) if sorted_samples else 0.0
    return NightSummary(
        events=events,
        recorded_seconds=recorded,
        signal_coverage_fraction=coverage,
    )


def trend_for(
    summary: NightSummary,
    baseline: Optional[UserBaseline],
    consent_given: bool,
    config: Optional[ApneaDetectorConfig] = None,
) -> ApneaTrend:
    """Project a :class:`NightSummary` onto the user-facing enum.

    Order of priority (each gate dominates the ones below):

    1. Consent: no consent → :attr:`ApneaTrend.PENDING_CONSENT`.
    2. Calibration: no baseline yet → :attr:`ApneaTrend.CALIBRATING`.
    3. Coverage: too noisy → :attr:`ApneaTrend.CALIBRATING` (treat
       as "still gathering data" rather than fabricating a colour).
    4. Events / hour vs cut-offs.
    """
    cfg = config or ApneaDetectorConfig()
    if not consent_given:
        return ApneaTrend.PENDING_CONSENT
    if baseline is None or baseline.nights_observed < cfg.calibration_nights:
        return ApneaTrend.CALIBRATING
    if summary.signal_coverage_fraction < cfg.min_signal_coverage:
        return ApneaTrend.CALIBRATING
    rate = summary.events_per_hour
    if rate >= cfg.red_threshold:
        return ApneaTrend.RED
    if rate >= cfg.amber_threshold:
        return ApneaTrend.AMBER
    return ApneaTrend.GREEN


__all__ = [
    "ApneaTrend",
    "BreathingSample",
    "ApneaEvent",
    "NightSummary",
    "UserBaseline",
    "ApneaDetectorConfig",
    "compute_baseline",
    "detect_events",
    "summarise_night",
    "trend_for",
]
