"""Edge Micro-Stage Transformer (EMST) — ONNX runtime stage predictor.

This module loads an INT8-quantized transformer ONNX model from
``training_config/stage_predictor.onnx`` and uses it to predict the
sleep stage 60 seconds ahead. The output drives pre-emptive setpoint
dispatch for slow-response devices (climate / humidifier), giving heat
blankets and air conditioners a head start before the user actually
transitions into a deeper stage.

:Design reference: design.md §3.4
:Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 10.1, 10.2, 10.3, 10.4

Operational contracts
---------------------

* ``onnxruntime`` is imported lazily inside :meth:`try_load` and
  :meth:`_load_session`. The ``import src.stage_predictor`` chain
  must remain importable on machines that did not install the
  optional runtime — graceful R11.3 degradation.
* The :class:`onnxruntime.InferenceSession` is *not* materialized in
  :meth:`__init__`; it is built on first :meth:`predict` call so
  unit tests can construct the predictor without onnxruntime.
* :meth:`StagePredictor.maybe_anticipate` never calls the HA service
  client directly. It only forwards intent to
  :meth:`SmartEnvironmentController.dispatch_with_lookahead` (task
  5.3), which holds the dry-run / device-class guards in one place
  (PR1).
* All persistence (predictor audit JSONL) flows through
  :func:`src._io_utils.atomic_append_jsonl` (PR3).
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from src._io_utils import atomic_append_jsonl, atomic_write_text
from src.data_structures import SleepStage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.smart_environment_controller import SmartEnvironmentController


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Maximum on-disk size of the ONNX artifact (R9.2). The INT8 quantized
# transformer typically lands around 50 KB; we refuse to load anything
# larger than 80 KB so a corrupt or unquantized artifact cannot
# silently inflate the add-on image (PR4) or stall inference.
_MAX_MODEL_BYTES: int = 80 * 1024

# Window length of the predictor input (R9.3): 5 minutes at 1 Hz.
_WINDOW_SAMPLES: int = 300

# How many consecutive failures (timeout or exception) trigger the
# 1-hour cool-down (R9.4 + design §3.4.2 contract).
_DISABLE_AFTER_CONSECUTIVE_ERRORS: int = 3

# Cool-down duration after the predictor trips its error budget.
_DISABLED_DURATION_SECONDS: float = 3600.0

# Hit-rate cache TTL (R10.3): refresh the rolling 7-night percentage
# at most once per hour to avoid re-scanning the JSONL on every poll.
_HIT_RATE_CACHE_SECONDS: float = 3600.0

# Audit retention window (R10.2): drop hit records older than 7 days.
_AUDIT_RETENTION_SECONDS: float = 7 * 24 * 3600.0

# Auto-disable threshold (R10.4): three consecutive nights with
# ``hit_rate < 0.70`` trips ``predictor_status = auto_disabled``.
_HIT_RATE_FLOOR: float = 0.70
_BAD_NIGHTS_BEFORE_AUTO_DISABLE: int = 3

# Names of the four sleep stages in the canonical ONNX output order.
# Aligned with :class:`SleepStage` so ``argmax`` ↔ ``SleepStage(idx)``.
_STAGE_NAMES: tuple[str, ...] = ("AWAKE", "LIGHT", "DEEP", "REM")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PredictorInput:
    """Five-minute sliding window of the three predictor input channels.

    All three channels are sampled at 1 Hz, so each tuple is exactly
    ``300`` long. ``None`` values represent dropped sensor samples —
    the inference path uses :attr:`is_complete_enough` to skip windows
    where any single channel lost more than half of its samples
    (R9.6).

    :ivar hrv_ms: Heart-rate variability in milliseconds.
    :ivar motion_au: Body motion in arbitrary normalized units.
    :ivar breathing_rate_bpm: Breathing rate in breaths per minute.
    """

    hrv_ms: tuple[float | None, ...]
    motion_au: tuple[float | None, ...]
    breathing_rate_bpm: tuple[float | None, ...]

    @property
    def is_complete_enough(self) -> bool:
        """Return ``True`` when every channel has ≥ 50 % non-``None`` samples.

        Implements R9.6: if any of the three channels has more than
        half of its 5-minute window dropped, we abort the prediction
        rather than zero-fill — zero-filling would bias the model
        toward AWAKE.
        """
        threshold = _WINDOW_SAMPLES * 0.5
        for channel in (
            self.hrv_ms, self.motion_au, self.breathing_rate_bpm,
        ):
            non_none = sum(1 for v in channel if v is not None)
            if non_none < threshold:
                return False
        return True


@dataclass(frozen=True, slots=True)
class PredictorOutput:
    """One prediction over the next-stage probability simplex.

    ``confidence`` is the maximum of ``p_awake / p_light / p_deep /
    p_rem`` and is what :meth:`StagePredictor.maybe_anticipate`
    checks against ``min_confidence``. ``is_valid`` summarizes the
    R9.5 sanity contract (probabilities in ``[0, 1]``, sum within
    ``1 ± 0.01``, no NaN); a malformed output flows through but
    :meth:`maybe_anticipate` will refuse to dispatch.

    :ivar inference_ms: Wall-clock duration of the inference call.
    """

    p_awake: float
    p_light: float
    p_deep: float
    p_rem: float
    confidence: float
    inference_ms: float
    is_valid: bool


@dataclass(frozen=True, slots=True)
class HitRecord:
    """One row of ``/data/predictor_audit.jsonl``.

    :ivar timestamp: ISO-8601 UTC timestamp of the prediction.
    :ivar predicted_stage: Stage name (``"AWAKE" | "LIGHT" | "DEEP" |
        "REM"``) corresponding to ``argmax(p_*)`` at prediction time.
    :ivar actual_stage_60s_later: Stage name 60 seconds later or
        ``None`` if the orchestrator could not observe the ground
        truth (e.g. user woke up before the look-ahead window
        closed).
    :ivar confidence: Maximum probability at prediction time, useful
        for slicing per-confidence bucket statistics in evaluations.
    """

    timestamp: str
    predicted_stage: str
    actual_stage_60s_later: str | None
    confidence: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_probabilities(
    p_awake: float, p_light: float, p_deep: float, p_rem: float,
) -> bool:
    """Return ``True`` iff the four probabilities satisfy R9.5.

    Contract:

    * No ``NaN`` (``math.isnan`` short-circuits before any other check).
    * All four values within ``[0.0, 1.0]``.
    * ``|sum - 1.0| <= 0.01``.
    """
    probs = (p_awake, p_light, p_deep, p_rem)
    for p in probs:
        if math.isnan(p):
            return False
        if not (0.0 <= p <= 1.0):
            return False
    return abs(sum(probs) - 1.0) <= 0.01


def _argmax_stage_name(out: PredictorOutput) -> str:
    """Return the stage name corresponding to ``argmax`` of the four probs."""
    probs = (out.p_awake, out.p_light, out.p_deep, out.p_rem)
    idx = max(range(4), key=lambda i: probs[i])
    return _STAGE_NAMES[idx]


def _parse_iso_timestamp(ts: str) -> float | None:
    """Parse an ISO-8601 timestamp into Unix seconds.

    Returns ``None`` on malformed input rather than raising — audit
    pruning must be resilient to partially-corrupt history files.
    """
    try:
        # ``fromisoformat`` accepts ``+00:00`` but not the trailing
        # ``Z`` shorthand on Python 3.10. Normalize first.
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).timestamp()
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# StagePredictor
# ---------------------------------------------------------------------------


class StagePredictor:
    """ONNX-runtime backed next-stage predictor with hit-rate audit.

    Construction is lazy: :meth:`try_load` validates the ONNX artifact
    on disk and probes ``onnxruntime`` importability but does *not*
    materialize the :class:`onnxruntime.InferenceSession`. The session
    is built on the first :meth:`predict` call so unit tests, which
    cannot depend on ``onnxruntime`` being installed, can still
    exercise the surrounding bookkeeping.

    :param model_path: Path to ``stage_predictor.onnx``.
    :param audit_jsonl: Path to ``/data/predictor_audit.jsonl``.
    :param max_inference_ms: Per-call wall-clock budget (R9.4).
    :param min_confidence: ``maybe_anticipate`` floor (R9.5 / R10.1).
    :param slow_devices_only: HA ``device_class`` whitelist for
        pre-emptive dispatch — kept here for documentation; the
        actual filter lives in
        :meth:`SmartEnvironmentController.dispatch_with_lookahead`
        (task 5.3) so dry-run / device-class guards live in one
        place (PR1).
    """

    def __init__(
        self,
        *,
        model_path: Path,
        audit_jsonl: Path,
        max_inference_ms: float = 50.0,
        min_confidence: float = 0.6,
        slow_devices_only: frozenset[str] = frozenset(
            {"climate", "humidifier"}
        ),
    ) -> None:
        self._model_path = Path(model_path)
        self._audit_jsonl = Path(audit_jsonl)
        self._max_inference_ms = float(max_inference_ms)
        self._min_confidence = float(min_confidence)
        self._slow_devices_only = frozenset(slow_devices_only)

        # Lazily initialized on first predict() call (R11.3 graceful):
        # the test harness can construct StagePredictor even when
        # onnxruntime is not installed.
        self._session: Any = None
        self._session_load_failed: bool = False

        # Error budget tracking (R9.4).
        self._error_count: int = 0
        self._disabled_until: float = 0.0

        # Hit-rate cache (R10.3).
        self._hit_rate_cache: float | None = None
        self._hit_rate_cache_ts: float = 0.0

        # Auto-disable latch (R10.4): once flipped, stays True until a
        # human restart / retrain.
        self._auto_disabled: bool = False

    # ------------------------------------------------------------------ #
    # Construction helpers
    # ------------------------------------------------------------------ #

    @classmethod
    def try_load(
        cls,
        *,
        model_path: Path,
        audit_jsonl: Path,
        **kwargs: Any,
    ) -> "StagePredictor | None":
        """Validate the ONNX artifact and return a predictor or ``None``.

        Returns ``None`` (and logs an INFO message) when:

        * ``onnxruntime`` is not installed (graceful R11.3).
        * ``model_path`` does not exist.
        * ``model_path`` is larger than 80 KB (R9.2 — refuses to load
          an unquantized or corrupt artifact).

        The probe imports ``onnxruntime`` so callers can short-circuit
        the wiring before any inference attempt. The actual
        :class:`InferenceSession` is still deferred until the first
        :meth:`predict` call so a transient runtime issue does not
        crash startup.
        """
        try:
            import onnxruntime  # noqa: F401 - probe only
        except ImportError:
            logger.info(
                "stage_predictor: onnxruntime not installed; "
                "predictor disabled (graceful R11.3 degradation)."
            )
            return None

        path = Path(model_path)
        if not path.exists():
            logger.info(
                "stage_predictor: model file %s missing; "
                "predictor disabled.",
                path,
            )
            return None

        size = path.stat().st_size
        if size > _MAX_MODEL_BYTES:
            logger.info(
                "stage_predictor: model %s is %d bytes (> %d limit); "
                "predictor disabled (R9.2).",
                path, size, _MAX_MODEL_BYTES,
            )
            return None

        return cls(
            model_path=path,
            audit_jsonl=Path(audit_jsonl),
            **kwargs,
        )

    def _load_session(self) -> Any:
        """Build the ONNX :class:`InferenceSession` on first use.

        Returns ``None`` and remembers the failure on import / load
        failure so subsequent calls short-circuit. The deferred load
        is what lets unit tests construct :class:`StagePredictor`
        without ``onnxruntime`` installed.
        """
        if self._session is not None:
            return self._session
        if self._session_load_failed:
            return None
        try:
            import onnxruntime  # local import: graceful R11.3
            self._session = onnxruntime.InferenceSession(
                str(self._model_path),
                providers=["CPUExecutionProvider"],
            )
        except Exception as exc:  # broad: any runtime failure disables
            logger.warning(
                "stage_predictor: failed to materialize "
                "InferenceSession (%s); predictor disabled until restart.",
                exc,
            )
            self._session_load_failed = True
            self._session = None
        return self._session

    # ------------------------------------------------------------------ #
    # Inference
    # ------------------------------------------------------------------ #

    async def predict(
        self, window: PredictorInput,
    ) -> PredictorOutput | None:
        """Run the next-stage prediction for ``window``.

        Returns ``None`` when:

        * The window does not have ≥ 50 % non-``None`` samples on every
          channel (R9.6) — silent skip, no error count.
        * The predictor is in cool-down (3+ consecutive errors, R9.4)
          and the window has not yet expired.
        * The :class:`InferenceSession` could not be materialized.
        * ``onnxruntime`` raised an exception (counts toward error
          budget).
        * The inference exceeded ``max_inference_ms`` (counts toward
          error budget).

        On a fast successful inference the consecutive-error counter
        resets to zero (graceful recovery). Output validity (R9.5) is
        carried in :attr:`PredictorOutput.is_valid`; the call still
        returns the (potentially invalid) output so the caller can
        observe and audit, but :meth:`maybe_anticipate` will refuse
        to dispatch.
        """
        if not window.is_complete_enough:
            return None

        now = time.time()
        # If a previous cool-down expired, give the model three fresh
        # attempts before another disable. The contract says "3
        # consecutive errors" — start the count from zero post-recovery.
        if self._disabled_until > 0 and now >= self._disabled_until:
            self._disabled_until = 0.0
            self._error_count = 0
        if now < self._disabled_until:
            return None

        session = self._load_session()
        if session is None:
            return None

        arr = np.asarray(
            [
                [
                    self._zero_fill(window.hrv_ms),
                    self._zero_fill(window.motion_au),
                    self._zero_fill(window.breathing_rate_bpm),
                ],
            ],
            dtype=np.float32,
        )

        started = time.perf_counter()
        try:
            input_name = session.get_inputs()[0].name
            raw = session.run(None, {input_name: arr})
        except Exception as exc:
            self._record_error(f"inference exception: {exc}")
            return None

        inference_ms = (time.perf_counter() - started) * 1000.0
        if inference_ms > self._max_inference_ms:
            self._record_error(
                f"inference {inference_ms:.1f} ms exceeded budget "
                f"{self._max_inference_ms:.1f} ms"
            )
            return None

        # Successful fast inference — reset the error budget.
        self._error_count = 0

        flat = np.asarray(raw[0], dtype=np.float64).reshape(-1)
        # Pad short outputs with NaN so is_valid trips deterministically
        # rather than raising IndexError on truncated artifacts.
        probs = [float(flat[i]) if i < flat.size else float("nan")
                 for i in range(4)]
        p_awake, p_light, p_deep, p_rem = probs
        is_valid = _validate_probabilities(
            p_awake, p_light, p_deep, p_rem,
        )
        confidence = (
            max(p_awake, p_light, p_deep, p_rem) if is_valid else 0.0
        )
        return PredictorOutput(
            p_awake=p_awake,
            p_light=p_light,
            p_deep=p_deep,
            p_rem=p_rem,
            confidence=confidence,
            inference_ms=inference_ms,
            is_valid=is_valid,
        )

    @staticmethod
    def _zero_fill(channel: tuple[float | None, ...]) -> list[float]:
        """Convert one input channel to a fixed-length float list.

        ``None`` samples (dropped by the sensor) are zero-filled — but
        only after :attr:`PredictorInput.is_complete_enough` already
        gated the call, so the model never sees a window that is more
        than half synthetic (R9.6).
        """
        return [0.0 if v is None else float(v) for v in channel]

    def _record_error(self, reason: str) -> None:
        """Increment the consecutive-error counter and trip cool-down."""
        self._error_count += 1
        logger.warning(
            "stage_predictor: %s (errors=%d)",
            reason, self._error_count,
        )
        if self._error_count >= _DISABLE_AFTER_CONSECUTIVE_ERRORS:
            self._disabled_until = time.time() + _DISABLED_DURATION_SECONDS
            logger.warning(
                "stage_predictor: %d consecutive errors; cooling down "
                "for %.0f s.",
                self._error_count, _DISABLED_DURATION_SECONDS,
            )

    # ------------------------------------------------------------------ #
    # Pre-emptive dispatch
    # ------------------------------------------------------------------ #

    async def maybe_anticipate(
        self,
        *,
        current_stage: SleepStage,
        predicted: PredictorOutput,
        controller: "SmartEnvironmentController",
    ) -> None:
        """Forward an early-action intent on a likely LIGHT → DEEP transition.

        Triggers iff **all** of the following hold:

        * ``current_stage == SleepStage.LIGHT``
        * ``argmax(predicted) == "DEEP"``
        * ``predicted.confidence >= self._min_confidence``
        * ``predicted.is_valid``

        The HA ``device_class`` filter (R10.1: only ``climate`` and
        ``humidifier``) is intentionally enforced inside
        :meth:`SmartEnvironmentController.dispatch_with_lookahead`
        rather than here, so the dry-run / device-class guard lives
        in exactly one place (PR1). This module never calls
        ``ha_client.call_service`` directly.
        """
        if current_stage != SleepStage.LIGHT:
            return
        if not predicted.is_valid:
            return
        if predicted.confidence < self._min_confidence:
            return
        if _argmax_stage_name(predicted) != "DEEP":
            return

        # ``dispatch_with_lookahead`` is added by task 5.3; we call it
        # dynamically so this module stays import-clean before that
        # landing. Static type checkers see the controller via
        # ``TYPE_CHECKING``.
        try:
            await controller.dispatch_with_lookahead(
                stage=SleepStage.DEEP, lead_seconds=60,
            )
        except AttributeError:
            logger.debug(
                "stage_predictor: controller has no "
                "dispatch_with_lookahead yet (task 5.3 pending); "
                "skipping anticipation."
            )
        except Exception as exc:
            logger.warning(
                "stage_predictor: dispatch_with_lookahead failed: %s",
                exc,
            )

    # ------------------------------------------------------------------ #
    # Hit audit
    # ------------------------------------------------------------------ #

    async def record_hit(
        self,
        *,
        predicted_stage: str,
        confidence: float,
        actual_stage_after_60s: str,
    ) -> None:
        """Append one :class:`HitRecord` and prune entries older than 7 days.

        Persistence flows through
        :func:`src._io_utils.atomic_append_jsonl` (PR3). After the
        append we re-read the file, drop rows whose timestamp parses
        to a time more than 7 days in the past, and rewrite atomically
        when at least one row was dropped (R10.2).
        """
        now = time.time()
        record = {
            "timestamp": datetime.fromtimestamp(
                now, timezone.utc,
            ).isoformat(),
            "predicted_stage": predicted_stage,
            "actual_stage_60s_later": actual_stage_after_60s,
            "confidence": float(confidence),
        }
        atomic_append_jsonl(self._audit_jsonl, record, max_lines=None)
        self._prune_audit(now)

        # Force the next ``hit_rate_7d`` call to recompute.
        self._hit_rate_cache = None
        self._hit_rate_cache_ts = 0.0

    def _prune_audit(self, now: float) -> None:
        """Drop hit records older than 7 days. No-op if file missing."""
        cutoff = now - _AUDIT_RETENTION_SECONDS
        try:
            raw = self._audit_jsonl.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        if not raw:
            return
        kept: list[str] = []
        any_dropped = False
        for line in raw.splitlines():
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                # Keep malformed lines so debugging isn't lost.
                kept.append(line)
                continue
            ts = _parse_iso_timestamp(str(parsed.get("timestamp", "")))
            if ts is not None and ts < cutoff:
                any_dropped = True
                continue
            kept.append(line)
        if any_dropped:
            atomic_write_text(
                self._audit_jsonl,
                "\n".join(kept) + ("\n" if kept else ""),
            )

    def hit_rate_7d(self) -> float | None:
        """Return the rolling-7-night hit rate as a percentage in ``[0, 100]``.

        Cached for one hour (R10.3): the publisher polls this on a
        sensor refresh cadence which can be tighter than the audit
        write cadence, so we avoid re-scanning the JSONL on every
        call. Returns ``None`` when fewer than 7 distinct night-days
        of records exist — the metric is undefined for partial weeks.

        Side-effect (R10.4): when the most recent three distinct
        nights all sit below 70 %, latch :attr:`_auto_disabled` to
        ``True`` so :attr:`predictor_status` flips to
        ``"auto_disabled"``. The latch only releases on a process
        restart or manual retrain (per R10.4).
        """
        now = time.time()
        if (
            self._hit_rate_cache is not None
            and (now - self._hit_rate_cache_ts) < _HIT_RATE_CACHE_SECONDS
        ):
            return self._hit_rate_cache

        try:
            raw = self._audit_jsonl.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._hit_rate_cache = None
            self._hit_rate_cache_ts = now
            return None
        if not raw:
            self._hit_rate_cache = None
            self._hit_rate_cache_ts = now
            return None

        cutoff = now - _AUDIT_RETENTION_SECONDS
        per_night_hits: dict[str, list[bool]] = {}
        rolling_total = 0
        rolling_hits = 0
        for line in raw.splitlines():
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_iso_timestamp(str(parsed.get("timestamp", "")))
            if ts is None or ts < cutoff:
                continue
            actual = parsed.get("actual_stage_60s_later")
            if actual is None:
                # Cannot evaluate hit when ground truth unobserved.
                continue
            predicted = parsed.get("predicted_stage")
            hit = bool(predicted == actual)
            night_key = (
                datetime.fromtimestamp(ts, timezone.utc)
                .date()
                .isoformat()
            )
            per_night_hits.setdefault(night_key, []).append(hit)
            rolling_total += 1
            if hit:
                rolling_hits += 1

        if len(per_night_hits) < 7 or rolling_total == 0:
            self._hit_rate_cache = None
            self._hit_rate_cache_ts = now
            self._update_auto_disable(per_night_hits)
            return None

        rate_pct = (rolling_hits / rolling_total) * 100.0
        self._hit_rate_cache = rate_pct
        self._hit_rate_cache_ts = now
        self._update_auto_disable(per_night_hits)
        return rate_pct

    def _update_auto_disable(
        self, per_night_hits: dict[str, list[bool]],
    ) -> None:
        """Latch ``_auto_disabled`` when the last three nights are all bad.

        Stateless re-evaluation (R10.4): we look at the most recent
        :data:`_BAD_NIGHTS_BEFORE_AUTO_DISABLE` distinct nights with at
        least one hit record and check that every one of them has a
        per-night hit rate below ``_HIT_RATE_FLOOR``. The latch is
        sticky — once flipped, only a restart or manual retrain
        clears it (per R10.4 user-facing contract).
        """
        if self._auto_disabled:
            return
        if len(per_night_hits) < _BAD_NIGHTS_BEFORE_AUTO_DISABLE:
            return
        recent_nights = sorted(per_night_hits.keys())[
            -_BAD_NIGHTS_BEFORE_AUTO_DISABLE:
        ]
        all_bad = True
        for night_key in recent_nights:
            bucket = per_night_hits[night_key]
            if not bucket:
                all_bad = False
                break
            night_rate = sum(bucket) / len(bucket)
            if night_rate >= _HIT_RATE_FLOOR:
                all_bad = False
                break
        if all_bad:
            self._auto_disabled = True
            logger.warning(
                "stage_predictor: 3 consecutive nights below %.0f%% "
                "hit rate; predictor_status -> auto_disabled (R10.4).",
                _HIT_RATE_FLOOR * 100,
            )

    # ------------------------------------------------------------------ #
    # Status surface
    # ------------------------------------------------------------------ #

    @property
    def predictor_status(self) -> str:
        """Return the user-facing status label.

        ``"auto_disabled"`` once three consecutive nights tripped the
        70 % floor (R10.4); ``"degraded"`` while the cool-down window
        is active (R9.4); otherwise ``"healthy"``.
        """
        if self._auto_disabled:
            return "auto_disabled"
        if time.time() < self._disabled_until:
            return "degraded"
        return "healthy"

    @property
    def error_count(self) -> int:
        """Consecutive-error counter (resets on a fast successful inference)."""
        return self._error_count

    @property
    def should_disable(self) -> bool:
        """Return ``True`` when ``error_count >= 3`` (R11.3 threshold)."""
        return self._error_count >= 3

    @property
    def disabled_until(self) -> float:
        """Unix timestamp at which the cool-down window expires (0 if healthy)."""
        return self._disabled_until


__all__ = [
    "HitRecord",
    "PredictorInput",
    "PredictorOutput",
    "StagePredictor",
]
