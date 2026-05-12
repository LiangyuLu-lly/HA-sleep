"""External sleep-stage subscriber — drop-in replacement for the in-house
CNN-BiLSTM inference engine.

Rationale (v1.3.0 product pivot)
================================
Mass-market wearables (Mi Band, Apple Watch, Garmin) and bedside radars
(R60ABD1, Withings, Eight Sleep) already publish a sleep-stage signal
into Home Assistant.  Running another deep model on top of those derived
labels is pure overhead — we lose nothing by reading the device's own
stage entity and forwarding it to the rest of the pipeline (preference
learner, smart-wake, soundscape, quality scoring).

This module exposes the *exact same surface* the legacy
``_InferenceEngine`` had inside ``scripts/run_ha_smart_service.py`` so the
inference loop, controller, publisher and learner code paths can stay
untouched:

    engine = ExternalStageSubscriber(stage_entity_id="sensor.bedroom_sleep_stage")
    engine.observe(entity_id, new_state, attributes=...)   # from HA WS
    stage, confidence = engine.current()                   # called every infer_interval

Stage value parsing
-------------------
Different devices use wildly different encodings.  We accept any of:

* English strings:    ``"AWAKE" | "LIGHT" | "DEEP" | "REM"`` (any case)
* Compact aliases:    ``"wake" / "light_sleep" / "deep_sleep" / "rem"``
* Chinese labels:     ``"清醒" / "浅睡" / "深睡" / "REM"`` / ``"快速眼动"``
* Numeric codes:      ``0..3`` (our convention) or ``1..4`` (Mi Band /
                      Withings convention — auto-shifted)
* HA ``unknown`` /    ``unavailable`` / empty → treated as no-update (we
                      hold the previous stage).

Confidence is read from a sibling attribute (``confidence`` /
``probability`` / ``score``) when present, else defaults to ``1.0``
because the device is presumed authoritative.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Mapping, Optional, Tuple

from src.data_structures import SleepStage

logger = logging.getLogger(__name__)


# Stale-data guard: if the bound entity hasn't reported a new state for
# this long we surface the fact in ``status()`` so the user knows their
# tracker has dropped off (e.g. watch ran flat overnight).  We still keep
# returning the last-known stage because that's the closest thing to
# truth we have until the next sample arrives.
_DEFAULT_STALE_AFTER_SECONDS = 30 * 60     # 30 minutes


# ---------------------------------------------------------------------------
# Vocabulary maps                                                             #
# ---------------------------------------------------------------------------
#
# Keep the dictionaries flat & explicit — easier to audit than a regex.
# Match is case-insensitive (we ``.casefold()`` before lookup) and
# whitespace-tolerant (callers can pre-strip but we belt-and-brace it).

_STRING_TO_STAGE: Dict[str, SleepStage] = {
    # ---- English (canonical) ----
    "awake": SleepStage.AWAKE,
    "wake": SleepStage.AWAKE,
    "wakeful": SleepStage.AWAKE,
    "wakefulness": SleepStage.AWAKE,
    "light": SleepStage.LIGHT,
    "light_sleep": SleepStage.LIGHT,
    "lightsleep": SleepStage.LIGHT,
    "n1": SleepStage.LIGHT,
    "n2": SleepStage.LIGHT,
    "deep": SleepStage.DEEP,
    "deep_sleep": SleepStage.DEEP,
    "deepsleep": SleepStage.DEEP,
    "n3": SleepStage.DEEP,
    "sws": SleepStage.DEEP,            # slow-wave sleep
    "rem": SleepStage.REM,
    "r": SleepStage.REM,
    # ---- Chinese (Mi Home, native ESPHome firmwares) ----
    "清醒": SleepStage.AWAKE,
    "醒着": SleepStage.AWAKE,
    "浅睡": SleepStage.LIGHT,
    "浅度睡眠": SleepStage.LIGHT,
    "深睡": SleepStage.DEEP,
    "深度睡眠": SleepStage.DEEP,
    "快速眼动": SleepStage.REM,
    "快速眼动睡眠": SleepStage.REM,
    "眼动期": SleepStage.REM,
}


# Numeric vocabularies.  Two industry conventions exist; we accept both
# but log on first encounter so the user knows which interpretation
# kicked in.  ``0..3`` matches our own :class:`SleepStage` enum; ``1..4``
# matches Mi Band / Withings exports.
_NUMERIC_0_BASED: Dict[int, SleepStage] = {
    0: SleepStage.AWAKE,
    1: SleepStage.LIGHT,
    2: SleepStage.DEEP,
    3: SleepStage.REM,
}
_NUMERIC_1_BASED: Dict[int, SleepStage] = {
    1: SleepStage.AWAKE,
    2: SleepStage.LIGHT,
    3: SleepStage.DEEP,
    4: SleepStage.REM,
}


def _parse_stage(raw: Any) -> Optional[SleepStage]:
    """Best-effort conversion from a HA state value to :class:`SleepStage`.

    Returns ``None`` when the value carries no information (HA's
    ``"unknown"`` / ``"unavailable"`` placeholders, empty string, or a
    type we don't know how to map).  The caller treats ``None`` as
    "hold the previous reading" — strictly better than guessing.
    """
    if raw is None:
        return None
    # Numeric paths first because ``bool`` is a subclass of ``int`` and
    # would otherwise match the string branch as ``"True"`` / ``"False"``.
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        n = int(raw)
        if n in _NUMERIC_0_BASED:
            return _NUMERIC_0_BASED[n]
        if n in _NUMERIC_1_BASED:
            return _NUMERIC_1_BASED[n]
        return None
    if isinstance(raw, str):
        key = raw.strip().casefold()
        if key in ("", "unknown", "unavailable", "none", "null"):
            return None
        if key in _STRING_TO_STAGE:
            return _STRING_TO_STAGE[key]
        # Fall through: try as a numeric string (e.g. "2").
        try:
            n = int(key)
        except ValueError:
            return None
        if n in _NUMERIC_0_BASED:
            return _NUMERIC_0_BASED[n]
        if n in _NUMERIC_1_BASED:
            return _NUMERIC_1_BASED[n]
    return None


def _parse_confidence(attributes: Optional[Mapping[str, Any]]) -> float:
    """Extract a 0..1 confidence from the entity's attribute payload.

    HA conventions vary across integrations:
      * ``confidence``        → 0..1 (what we use)
      * ``probability``       → 0..1 (Withings) — same scale
      * ``score`` / ``conf``  → 0..100 (some firmware) — auto-rescaled

    Missing attributes default to ``1.0`` because the source device is
    presumed authoritative; downstream code already uses confidence
    only as a soft tiebreaker (smart-wake threshold, quality scoring).
    """
    if not attributes:
        return 1.0
    for key in ("confidence", "probability", "conf", "score"):
        if key in attributes:
            try:
                val = float(attributes[key])
            except (TypeError, ValueError):
                continue
            # Auto-rescale: a value > 1 is almost certainly a percentage.
            if val > 1.0:
                val = val / 100.0
            return max(0.0, min(1.0, val))
    return 1.0


# ---------------------------------------------------------------------------
# ExternalStageSubscriber                                                    #
# ---------------------------------------------------------------------------


class ExternalStageSubscriber:
    """Drop-in stage source backed by an external HA sensor.

    Thread-safety note: HA WebSocket events are dispatched on the asyncio
    event loop, and the inference loop reads ``current()`` on the same
    loop.  We therefore don't need locks — single-writer / single-reader
    by construction.
    """

    def __init__(
        self,
        stage_entity_id: str,
        *,
        stale_after_seconds: float = _DEFAULT_STALE_AFTER_SECONDS,
        initial_stage: SleepStage = SleepStage.LIGHT,
    ) -> None:
        if not stage_entity_id or not isinstance(stage_entity_id, str):
            raise ValueError(
                "stage_entity_id is required (e.g. sensor.bedroom_sleep_stage)"
            )
        self.stage_entity_id = stage_entity_id
        self._stale_after = float(stale_after_seconds)

        # Bootstrap state: until the first real update arrives we report
        # LIGHT @ low confidence so the controller stays conservative
        # (it won't aggressively crank the AC based on a default).
        self._stage: SleepStage = initial_stage
        self._confidence: float = 0.25
        self._last_update_ts: float = 0.0     # 0 means never updated
        self._update_count: int = 0
        self._numeric_convention_logged: bool = False

    # ------------------------------------------------------------------ #
    # Compatibility shims with the legacy _InferenceEngine               #
    # ------------------------------------------------------------------ #
    #
    # The old engine exposed ``buffer_ready`` / ``infer`` / ``push_*``
    # methods.  Keeping the same names avoids touching the inference
    # loop in run_ha_smart_service.py.

    def buffer_ready(self) -> bool:
        """True once we have received at least one real stage update.

        Mirrors the legacy semantic that a CNN inference needed enough
        samples to fill the 1024-sample window.  Here "ready" means
        "the user's tracker has reported in".
        """
        return self._update_count > 0

    def infer(self) -> Tuple[SleepStage, float]:
        """Return the most recently observed stage.

        Named ``infer`` to be a literal drop-in for the old engine.  The
        new :meth:`current` is the preferred name for new call sites.
        """
        return self.current()

    # No-op pushes: the legacy engine accepted raw HR / movement samples.
    # We accept (and silently ignore) those so a caller that still pushes
    # them won't crash during the v1.2 → v1.3 transition.

    def push_hr(self, value: float) -> None:
        return None

    def push_movement(self, value: float) -> None:
        return None

    # ------------------------------------------------------------------ #
    # Primary API                                                        #
    # ------------------------------------------------------------------ #

    def observe(
        self,
        entity_id: str,
        new_state: Any,
        attributes: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        """Feed one HA ``state_changed`` event into the subscriber.

        Returns ``True`` iff this event updated the cached stage.  Events
        for unrelated entities, ``unknown`` placeholders, or unparseable
        values are silently dropped so the caller can route every event
        through here without an upfront filter.
        """
        if entity_id != self.stage_entity_id:
            return False

        parsed = _parse_stage(new_state)
        if parsed is None:
            logger.debug(
                "ExternalStageSubscriber: ignoring unparseable state %r "
                "from %s (held %s)", new_state, entity_id, self._stage.name,
            )
            return False

        # One-time log of which numeric convention we picked, to help
        # users debug "my watch sends 2 but the add-on thinks it's DEEP".
        if (
            not self._numeric_convention_logged
            and isinstance(new_state, (int, float))
        ):
            n = int(new_state)
            if n in _NUMERIC_1_BASED and n not in _NUMERIC_0_BASED:
                logger.info(
                    "ExternalStageSubscriber: %s emits 1-based stage codes "
                    "(1=AWAKE..4=REM); mapped %d → %s.",
                    entity_id, n, parsed.name,
                )
            else:
                logger.info(
                    "ExternalStageSubscriber: %s emits 0-based stage codes "
                    "(0=AWAKE..3=REM); mapped %d → %s.",
                    entity_id, n, parsed.name,
                )
            self._numeric_convention_logged = True

        self._stage = parsed
        self._confidence = _parse_confidence(attributes)
        self._last_update_ts = time.time()
        self._update_count += 1
        logger.debug(
            "ExternalStageSubscriber: %s -> %s (conf=%.2f)",
            entity_id, parsed.name, self._confidence,
        )
        return True

    def current(self) -> Tuple[SleepStage, float]:
        """Return ``(stage, confidence)`` for the inference loop.

        Always returns a usable pair so the controller never has to
        worry about ``None``.  Confidence stays low until a real update
        arrives, which keeps the controller in a conservative regime
        during the cold-start window after add-on restart.
        """
        return (self._stage, self._confidence)

    def is_stale(self, now: Optional[float] = None) -> bool:
        """True if no update has arrived within ``stale_after_seconds``.

        Useful for surfacing a "tracker not reporting" status sensor in
        the user's Lovelace dashboard.  Does NOT change what
        :meth:`current` returns — we still hand back the last known
        value because that's strictly better than guessing.
        """
        if self._update_count == 0:
            # Bootstrap: not "stale", just "uninitialised".
            return False
        now = time.time() if now is None else now
        return (now - self._last_update_ts) > self._stale_after

    def status(self) -> Dict[str, Any]:
        """Human-readable snapshot for logs / diagnostic publishing."""
        return {
            "stage_entity_id": self.stage_entity_id,
            "current_stage": self._stage.name,
            "confidence": round(self._confidence, 3),
            "updates_received": self._update_count,
            "last_update_ts": self._last_update_ts,
            "stale": self.is_stale(),
        }


__all__ = ["ExternalStageSubscriber"]
