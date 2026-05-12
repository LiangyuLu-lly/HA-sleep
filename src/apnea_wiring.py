"""Runtime glue between the pure-function apnea detector and the orchestrator.

What this file is
=================

The v1.6.0 :mod:`src.apnea_detector` is intentionally pure-functional:
it takes a ``list[BreathingSample]``, a ``UserBaseline``, and returns
a :class:`ApneaTrend` bucket.  Easy to unit-test, easy to reason about,
but **not** wired up to any HA event source or persistence layer.

v1.7.0 plugs that PoC into the live data path.  The glue is deliberately
kept in a separate module from ``apnea_detector.py`` so:

* The algorithm stays pure-functional (anyone porting this to a custom
  component or a different home-automation platform doesn't inherit
  the HA-specific I/O assumptions).
* The consent gate, baseline persistence, and session lifecycle all
  land in one explicit place that's easy to audit for medical-safety
  properties.

Consent model
-------------

AHI is a clinical metric.  A naïve sensor that reports ``sensor.apnea
_index = 12`` without context is irresponsible:

* The user might read it as a diagnosis.
* Thresholds without polysomnography ground truth aren't reliable.
* The first ~7 nights of calibration produce meaningless numbers.

So the add-on REFUSES to publish anything beyond ``pending_consent``
until the user explicitly toggles ``input_boolean.
sleep_classifier_apnea_consent``.  Once toggled on, we publish the
coarse ``red/amber/green/calibrating`` trend — never a numeric AHI.

Every state transition is logged (so a clinician-partner can audit
after the fact).  Consent can be revoked at any time by toggling the
input_boolean off; on revocation we clear the per-user baseline and
drop the sensor back to ``pending_consent``.

Persistence
-----------

Baseline estimation needs ``calibration_nights`` (default 7) of data
before any non-``calibrating`` output.  Per-night samples are NOT
persisted — only the final aggregated :class:`UserBaseline` is
written to ``/data/apnea_baseline.json``.  Losing a night's samples
is acceptable (the next night will add to the baseline).  Losing
the baseline file means ~1 week of re-calibration, which the user
sees as ``calibrating`` on the sensor — transparent.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.apnea_detector import (
    ApneaDetectorConfig,
    ApneaTrend,
    BreathingSample,
    NightSummary,
    UserBaseline,
    compute_baseline,
    summarise_night,
    trend_for,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration + persisted state
# ---------------------------------------------------------------------------


@dataclass
class ApneaWiringConfig:
    """Orchestrator-facing knobs read from
    ``home_assistant.apnea.*`` in ``effective_config.json``.

    ``enabled=False`` short-circuits every method — the orchestrator
    can safely instantiate this object unconditionally and only the
    methods are no-ops when not configured.
    """

    enabled: bool = False
    breathing_rate_source: str = ""
    chest_amplitude_source: str = ""
    consent_entity: str = "input_boolean.sleep_classifier_apnea_consent"
    baseline_path: str = "/data/apnea_baseline.json"
    # Propagated to ApneaDetectorConfig — exposing a handful so power
    # users can tighten thresholds without code changes.
    calibration_nights: int = 7
    min_signal_coverage: float = 0.3

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ApneaWiringConfig":
        valid = {f for f in cls.__dataclass_fields__}    # type: ignore[attr-defined]
        cleaned: Dict[str, Any] = {}
        for k, v in raw.items():
            if k not in valid:
                continue
            # Treat the literal ``""`` that HA's Configuration form
            # sometimes keeps around as an empty string (same pattern
            # as the slot bindings in run.sh).
            if isinstance(v, str) and v.strip() in ('""', "''"):
                v = ""
            cleaned[k] = v
        return cls(**cleaned)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class _BaselineStore:
    """Atomic JSON persistence for :class:`UserBaseline`.

    Kept as a tiny internal class so the wiring doesn't depend on
    whatever future storage backend the add-on settles on (SQLite,
    HA state machine, whatever).
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> Optional[UserBaseline]:
        if not self._path.exists():
            return None
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Apnea baseline file %s unreadable (%s); resetting to "
                "calibrating state.", self._path, exc,
            )
            return None
        try:
            return UserBaseline(
                rate_bpm_median=float(raw["rate_bpm_median"]),
                amplitude_median=float(raw["amplitude_median"]),
                nights_observed=int(raw.get("nights_observed", 0)),
            )
        except (KeyError, ValueError, TypeError):
            logger.warning(
                "Apnea baseline file %s has wrong schema; resetting.",
                self._path,
            )
            return None

    def save(self, baseline: UserBaseline) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "rate_bpm_median": baseline.rate_bpm_median,
            "amplitude_median": baseline.amplitude_median,
            "nights_observed": baseline.nights_observed,
            "updated_at": time.time(),
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2), encoding="utf-8",
        )
        tmp.replace(self._path)

    def clear(self) -> None:
        """Called on consent revocation — delete the baseline on disk."""
        if self._path.exists():
            try:
                self._path.unlink()
                logger.info(
                    "Apnea baseline cleared (consent revoked).",
                )
            except OSError as exc:    # pragma: no cover
                logger.warning("Could not clear %s: %s", self._path, exc)


# ---------------------------------------------------------------------------
# Runtime session
# ---------------------------------------------------------------------------


@dataclass
class _LiveSession:
    """Rolling buffer of BreathingSamples for the current night.

    Intentionally not persisted — losing it on a restart means the
    detector starts the next session fresh, which is strictly safer
    than persisting incomplete samples across potentially-significant
    environment changes (e.g. user moved rooms, swapped sensors).
    """

    samples: List[BreathingSample] = field(default_factory=list)
    last_rate: Optional[float] = None
    last_amplitude: Optional[float] = None

    def tick(self, ts: float) -> None:
        """Snapshot the most recently observed (rate, amplitude) pair.

        Called from the inference loop at its normal cadence (~30 s).
        If neither field has been observed since the last tick we
        still emit a sample with both set to None — the detector
        uses coverage fraction to downgrade noisy nights to
        ``calibrating`` rather than fabricating an event out of
        missing data.
        """
        self.samples.append(BreathingSample(
            timestamp=ts,
            rate_bpm=self.last_rate,
            amplitude=self.last_amplitude,
        ))


# ---------------------------------------------------------------------------
# Main wiring
# ---------------------------------------------------------------------------


class ApneaWiring:
    """Orchestrator-facing façade.

    Lifetime is one add-on lifecycle; ``begin_session()`` / ``end_session()``
    bracket one sleep session.  Every inference tick should call
    :meth:`tick` so the running sample buffer stays populated.
    """

    def __init__(
        self,
        cfg: ApneaWiringConfig,
        detector_cfg: Optional[ApneaDetectorConfig] = None,
    ) -> None:
        self.cfg = cfg
        self.detector_cfg = detector_cfg or ApneaDetectorConfig(
            calibration_nights=cfg.calibration_nights,
            min_signal_coverage=cfg.min_signal_coverage,
        )
        self._store = _BaselineStore(cfg.baseline_path)
        self._baseline: Optional[UserBaseline] = (
            self._store.load() if cfg.enabled else None
        )
        self._consent: bool = False
        self._session: Optional[_LiveSession] = None
        # Most recent published trend — the publisher uses it to
        # detect state transitions (log once per change).
        self._last_trend: Optional[ApneaTrend] = None

    # ---- HA event routing ------------------------------------------------

    def on_state_change(
        self,
        entity_id: str,
        new_state_string: str,
        *,
        numeric_value: Optional[float] = None,
    ) -> bool:
        """Route one HA state_changed event.

        Returns True iff this event was claimed by the apnea module
        (so the orchestrator doesn't double-route to other handlers).
        """
        if not self.cfg.enabled:
            return False

        # Consent input_boolean — claim and track.
        if entity_id == self.cfg.consent_entity:
            was_consented = self._consent
            self._consent = str(new_state_string).lower() in ("on", "true", "1")
            if self._consent != was_consented:
                if self._consent:
                    logger.info(
                        "Apnea monitoring consent granted by user; "
                        "detector will publish trend once calibration completes.",
                    )
                else:
                    logger.info(
                        "Apnea monitoring consent revoked; clearing "
                        "baseline and reverting sensor to pending_consent.",
                    )
                    self._store.clear()
                    self._baseline = None
            return True

        # Live session required for rate/amplitude routing.
        if self._session is None:
            return False

        if entity_id == self.cfg.breathing_rate_source:
            self._session.last_rate = numeric_value
            return True
        if entity_id == self.cfg.chest_amplitude_source:
            self._session.last_amplitude = numeric_value
            return True
        return False

    # ---- Session lifecycle ----------------------------------------------

    def begin_session(self) -> None:
        """Called once by the orchestrator when a sleep session opens.

        Idempotent: re-calling without an intervening :meth:`end_session`
        just resets the live buffer.
        """
        if not self.cfg.enabled:
            return
        self._session = _LiveSession()

    def tick(self, now: Optional[float] = None) -> None:
        """Append the current (rate, amplitude) snapshot to the session."""
        if not self.cfg.enabled or self._session is None:
            return
        self._session.tick(time.time() if now is None else now)

    def end_session(self) -> Optional[ApneaTrend]:
        """Finalise the session; returns the trend to publish.

        Persists the baseline if this night's data was sufficient to
        update it.  Returns None if apnea monitoring is disabled so the
        publisher can skip the write.
        """
        if not self.cfg.enabled:
            return None
        session = self._session
        self._session = None
        if session is None or not session.samples:
            # No samples — nothing to summarise.  Still publish the
            # current trend so the sensor reflects state.
            return self.current_trend_now(summary=None)

        # If we're still calibrating, accumulate into the baseline.
        if (
            self._baseline is None
            or self._baseline.nights_observed < self.detector_cfg.calibration_nights
        ):
            fresh = compute_baseline(session.samples)
            if fresh is not None:
                if self._baseline is None:
                    self._baseline = fresh
                else:
                    # Running average toward the new median — smooths
                    # night-to-night variation in resting breath
                    # patterns.  Weight equally because this whole
                    # path only runs during calibration (≤ 7 nights).
                    n_new = self._baseline.nights_observed + 1
                    new_rate = (
                        (self._baseline.rate_bpm_median *
                         self._baseline.nights_observed)
                        + fresh.rate_bpm_median
                    ) / n_new
                    new_amp = (
                        (self._baseline.amplitude_median *
                         self._baseline.nights_observed)
                        + fresh.amplitude_median
                    ) / n_new
                    self._baseline = UserBaseline(
                        rate_bpm_median=new_rate,
                        amplitude_median=new_amp,
                        nights_observed=n_new,
                    )
                self._store.save(self._baseline)

        summary = summarise_night(
            session.samples, self._baseline
            or UserBaseline(rate_bpm_median=15.0, amplitude_median=1.0),
            self.detector_cfg,
        )
        return self.current_trend_now(summary=summary)

    # ---- Trend projection + state ---------------------------------------

    def current_trend_now(
        self, summary: Optional[NightSummary] = None,
    ) -> ApneaTrend:
        """Return the trend that should be on the sensor right now.

        Safe to call at any time; uses a zero-events NightSummary
        when called mid-session without a finalised summary.
        """
        if not self.cfg.enabled:
            return ApneaTrend.PENDING_CONSENT
        trend = trend_for(
            summary or NightSummary(),
            self._baseline,
            self._consent,
            self.detector_cfg,
        )
        if trend != self._last_trend:
            logger.info("Apnea trend: %s → %s",
                        self._last_trend.value if self._last_trend else "—",
                        trend.value)
            self._last_trend = trend
        return trend

    # ---- Diagnostics ----------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Summary for the sensor's attributes panel.

        Does NOT include AHI / events/hour — the whole design is to
        keep numeric clinical values off the user-facing entity.  What
        we DO expose: calibration progress, consent flag, baseline
        freshness.
        """
        return {
            "enabled": self.cfg.enabled,
            "consent": self._consent,
            "calibration_nights_required": self.detector_cfg.calibration_nights,
            "calibration_nights_completed": (
                self._baseline.nights_observed if self._baseline else 0
            ),
            "last_trend": (
                self._last_trend.value if self._last_trend else None
            ),
        }


__all__ = [
    "ApneaWiring",
    "ApneaWiringConfig",
]
