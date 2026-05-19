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

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

from src.data_structures import SleepStage

logger = logging.getLogger(__name__)


# Stale-data guard: if the bound entity hasn't reported a new state for
# this long we surface the fact in ``status()`` so the user knows their
# tracker has dropped off (e.g. watch ran flat overnight).  We still keep
# returning the last-known stage because that's the closest thing to
# truth we have until the next sample arrives.
_DEFAULT_STALE_AFTER_SECONDS = 30 * 60     # 30 minutes


# Stage-debouncing default (v1.4.0).  A new stage must hold for this many
# seconds before we promote it to "stable" and let the controller act on
# it.  Rationale: wearables routinely report 30-second AWAKE blips during
# DEEP (a quick stir, not a real awakening); without a dwell guard the
# add-on would flap the lights on, then off again 90 s later.  60 s is
# short enough that a real transition (e.g. user actually wakes up) is
# reflected within one inference tick, but long enough to absorb the
# typical stir/cough false-positive.
_DEFAULT_MIN_STAGE_DWELL_SECONDS = 60.0


# v3.0.0 — pre-transition hook budget.  Hooks registered via
# :meth:`ExternalStageSubscriber.add_pre_transition_hook` are awaited
# synchronously in the same event-loop tick before the stable stage is
# promoted; if any single hook exceeds this budget we skip it (counted)
# and proceed with the transition.  100 ms is short enough that hook
# latency stays imperceptible to users while still leaving room for
# one ONNX inference (see ``StagePredictor.predict`` ≤ 50 ms in design).
_PRE_TRANSITION_HOOK_TIMEOUT_SECONDS = 0.1


# Type alias for the hook callback.  Receives ``(new_stage, last_stage)``
# in that order — same convention as ``_emit_transition`` so callers can
# log "from → to" symmetrically.  The hook MUST be awaitable; sync
# callbacks should wrap themselves in ``async def``.
PreTransitionHook = Callable[[SleepStage, SleepStage], Awaitable[None]]


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
        min_stage_dwell_seconds: float = _DEFAULT_MIN_STAGE_DWELL_SECONDS,
    ) -> None:
        if not stage_entity_id or not isinstance(stage_entity_id, str):
            raise ValueError(
                "stage_entity_id is required (e.g. sensor.bedroom_sleep_stage)"
            )
        self.stage_entity_id = stage_entity_id
        self._stale_after = float(stale_after_seconds)
        self._min_dwell = max(0.0, float(min_stage_dwell_seconds))

        # Bootstrap state: until the first real update arrives we report
        # LIGHT @ low confidence so the controller stays conservative
        # (it won't aggressively crank the AC based on a default).
        #
        # Two stage values are tracked since v1.4.0:
        #   * ``_raw_stage``      — the most recent observation, no
        #                           filtering.  Surfaces in :meth:`status`
        #                           for debugging "why isn't the entity
        #                           promoting?".
        #   * ``_stable_stage``   — what :meth:`current` returns.  Only
        #                           updates after a new candidate stage
        #                           has held for ``min_stage_dwell``
        #                           seconds, suppressing 30-second
        #                           wearable blips.
        self._raw_stage: SleepStage = initial_stage
        self._stable_stage: SleepStage = initial_stage
        # Candidate = the most recent *different* stage we've observed
        # but haven't promoted yet.  None means "no pending candidate".
        self._candidate_stage: Optional[SleepStage] = None
        self._candidate_since_ts: float = 0.0

        self._confidence: float = 0.25
        self._last_update_ts: float = 0.0     # 0 means never updated
        self._update_count: int = 0
        self._numeric_convention_logged: bool = False

        # ── v3.0.0 pre-transition hooks (R9.1 / R10.1 / R11.3) ───────
        # Hooks are awaited *just before* the stable stage flips, so
        # consumers (e.g. ``StagePredictor.maybe_anticipate``) get a
        # chance to dispatch pre-emptive setpoints.  We hold a list
        # rather than a single callback so multiple v3 modules can
        # observe the same edge without a central dispatcher.
        #
        # Counters are exposed as read-only properties for the v3
        # health-summary sensor (see ``hook_error_count``); they are
        # never reset at runtime — a non-zero value is a useful signal
        # that some downstream consumer is misbehaving.
        self._pre_transition_hooks: List[PreTransitionHook] = []
        self._hook_error_count: int = 0
        self._hook_timeout_count: int = 0

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
                "from %s (held %s)",
                new_state, entity_id, self._stable_stage.name,
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

        now = time.time()
        self._raw_stage = parsed
        self._confidence = _parse_confidence(attributes)
        self._last_update_ts = now
        self._update_count += 1

        # ── Stage debouncing (v1.4.0) ────────────────────────────────
        # If the raw observation matches what we're already publishing,
        # cancel any pending candidate — the blip was transient.
        if parsed == self._stable_stage:
            if self._candidate_stage is not None:
                logger.debug(
                    "ExternalStageSubscriber: candidate %s cancelled "
                    "(reverted to stable %s within dwell window)",
                    self._candidate_stage.name, self._stable_stage.name,
                )
                self._candidate_stage = None
        elif parsed != self._candidate_stage:
            # New candidate — start the dwell timer.
            self._candidate_stage = parsed
            self._candidate_since_ts = now
            logger.debug(
                "ExternalStageSubscriber: %s -> candidate %s "
                "(dwell %.0fs needed before promotion)",
                entity_id, parsed.name, self._min_dwell,
            )
        # else: same candidate as before, just keep accumulating dwell time.

        # Eager-promote on every observation to keep ``current()``
        # cheap and stateless from the caller's perspective.
        self._maybe_promote_candidate(now)

        logger.debug(
            "ExternalStageSubscriber: %s raw=%s stable=%s (conf=%.2f)",
            entity_id, parsed.name, self._stable_stage.name, self._confidence,
        )
        return True

    def _maybe_promote_candidate(self, now: float) -> None:
        """Promote ``_candidate_stage`` to ``_stable_stage`` if it has held.

        Called from both :meth:`observe` (so periodic emitters promote
        on the next sample) and :meth:`current` (so event-only emitters
        promote when the inference loop polls — no extra observation
        needed once the dwell timer has expired).
        """
        if self._candidate_stage is None:
            return
        if self._candidate_stage == self._stable_stage:
            self._candidate_stage = None
            return
        if (now - self._candidate_since_ts) >= self._min_dwell:
            logger.info(
                "ExternalStageSubscriber: stable transition %s -> %s "
                "(held %.0fs, threshold %.0fs)",
                self._stable_stage.name, self._candidate_stage.name,
                now - self._candidate_since_ts, self._min_dwell,
            )
            new_stage = self._candidate_stage
            last_stage = self._stable_stage
            # Clear the candidate BEFORE invoking the hook so a hook
            # that calls back into ``current()`` / ``observe()`` does
            # not re-enter this promotion path (would cause duplicate
            # hook invocations for the same transition).  We're
            # committed to the flip at this point regardless.
            self._candidate_stage = None
            # v3.0.0 — fan out to pre-transition hooks BEFORE applying
            # the flip, so consumers that need to know "the controller
            # is about to react to a new stage" can dispatch pre-emptive
            # setpoints (e.g. start cooling for impending DEEP).  At
            # this point ``current()`` still returns ``last_stage``,
            # giving the hook a stable view of "old → new".
            self._run_pre_transition_hooks(new_stage, last_stage)
            self._emit_transition(new_stage, last_stage)

    # ------------------------------------------------------------------ #
    # v3.0.0 pre-transition hook plumbing                                #
    # ------------------------------------------------------------------ #

    def add_pre_transition_hook(self, hook: PreTransitionHook) -> None:
        """Register a callback fired just before each stable transition.

        :param hook: Awaitable receiving ``(new_stage, last_stage)``.  The
            argument order matches :meth:`_emit_transition` so callers can
            log "from → to" symmetrically.

        The hook is awaited with a 100 ms budget per invocation
        (``_PRE_TRANSITION_HOOK_TIMEOUT_SECONDS``).  Behaviour on misuse:

        * **Timeout** → ``_hook_timeout_count`` is incremented, a warning
          is logged, and the transition still fires.  The pre-emptive
          dispatch is opportunistic, not load-bearing.
        * **Exception** → ``_hook_error_count`` is incremented, the
          exception is logged at WARN, and the transition still fires.
          We never propagate hook errors back into the WS event loop —
          a buggy v3 module must not break stage forwarding (R11.3).

        Multiple hooks can be registered; they fire in registration order.
        Hooks are dispatched on the running event loop when one is
        present (production path); in synchronous test contexts the hook
        is driven via a one-shot ``asyncio.run`` so unit tests can
        assert on side-effects without an explicit event loop.
        """
        if hook is None or not callable(hook):
            raise TypeError(
                "add_pre_transition_hook requires an awaitable callable"
            )
        self._pre_transition_hooks.append(hook)

    @property
    def hook_error_count(self) -> int:
        """Pre-transition hook invocations that raised an exception.

        Exposed for the v3 health-summary sensor and the auto-degrade
        state machine in :mod:`scripts.run_ha_smart_service` (≥ 3
        errors → mark the StagePredictor module as ``degraded``).
        """
        return self._hook_error_count

    @property
    def hook_timeout_count(self) -> int:
        """Pre-transition hook invocations that exceeded the 100 ms budget."""
        return self._hook_timeout_count

    def _emit_transition(
        self, new_stage: SleepStage, last_stage: SleepStage
    ) -> None:
        """Apply the stable stage flip.

        Extracted from :meth:`_maybe_promote_candidate` in v3.0.0 so the
        pre-transition hook fan-out has a single, well-named call site
        to wrap.  Keep this small: any new "on transition" logic should
        live in a hook (registered via :meth:`add_pre_transition_hook`),
        not here, so v2.x tests that mutate ``_stable_stage`` directly
        keep working.

        ``last_stage`` is unused inside the body but kept in the
        signature so the symmetry with :meth:`_run_pre_transition_hooks`
        is obvious to readers.
        """
        del last_stage     # documented above; kept for signature parity
        self._stable_stage = new_stage

    def _run_pre_transition_hooks(
        self, new_stage: SleepStage, last_stage: SleepStage
    ) -> None:
        """Fan out registered hooks with a 100 ms per-hook budget.

        Each hook gets its own 100 ms slice rather than a shared budget
        across the list — fairness across v3 modules outweighs the
        tail-latency concern, since the hook list will in practice be
        ≤ 2 entries (StagePredictor + a possible diagnostics hook).

        This method NEVER raises and NEVER propagates hook errors; the
        contract is best-effort delivery so a misbehaving downstream
        module cannot break stage forwarding (R11.3).
        """
        if not self._pre_transition_hooks:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        for hook in list(self._pre_transition_hooks):
            try:
                coro = hook(new_stage, last_stage)
            except Exception as exc:    # noqa: BLE001 - hook is user code
                self._hook_error_count += 1
                logger.warning(
                    "pre_transition_hook raised before await: %r "
                    "(error_count=%d)",
                    exc, self._hook_error_count,
                )
                continue

            if loop is not None:
                self._dispatch_hook_on_loop(loop, coro, new_stage, last_stage)
            else:
                self._dispatch_hook_synchronously(coro, new_stage, last_stage)

    def _dispatch_hook_on_loop(
        self,
        loop: "asyncio.AbstractEventLoop",
        coro: Awaitable[None],
        new_stage: SleepStage,
        last_stage: SleepStage,
    ) -> None:
        """Drive a hook coroutine on an already-running event loop.

        ``observe()`` / ``current()`` already run on the loop thread, so
        we cannot ``run_until_complete``.  Instead we wrap the coroutine
        in :func:`asyncio.wait_for` with the 100 ms budget and schedule
        it via ``loop.create_task`` — fire-and-forget with bounded
        latency.  Errors / timeouts bump the counters even though the
        transition has already fired by then; this matches the design
        intent that pre-emptive dispatch is *advisory*, not blocking.
        """
        async def _runner() -> None:
            try:
                await asyncio.wait_for(
                    coro, timeout=_PRE_TRANSITION_HOOK_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                self._hook_timeout_count += 1
                logger.warning(
                    "pre_transition_hook timed out (>%dms) on %s -> %s "
                    "(timeout_count=%d)",
                    int(_PRE_TRANSITION_HOOK_TIMEOUT_SECONDS * 1000),
                    last_stage.name, new_stage.name,
                    self._hook_timeout_count,
                )
            except Exception as exc:    # noqa: BLE001 - hook is user code
                self._hook_error_count += 1
                logger.warning(
                    "pre_transition_hook raised %r on %s -> %s "
                    "(error_count=%d)",
                    exc, last_stage.name, new_stage.name,
                    self._hook_error_count,
                )

        loop.create_task(_runner())

    def _dispatch_hook_synchronously(
        self,
        coro: Awaitable[None],
        new_stage: SleepStage,
        last_stage: SleepStage,
    ) -> None:
        """Drive a hook coroutine when no loop is running.

        Used in synchronous test contexts where ``observe()`` is called
        outside any event loop.  We spin up a one-shot :func:`asyncio.run`
        with the 100 ms budget; any error path still bumps the counters
        but never re-raises into the caller.
        """
        async def _runner() -> None:
            await asyncio.wait_for(
                coro, timeout=_PRE_TRANSITION_HOOK_TIMEOUT_SECONDS
            )

        try:
            asyncio.run(_runner())
        except asyncio.TimeoutError:
            self._hook_timeout_count += 1
            logger.warning(
                "pre_transition_hook timed out (>%dms) on %s -> %s "
                "(timeout_count=%d)",
                int(_PRE_TRANSITION_HOOK_TIMEOUT_SECONDS * 1000),
                last_stage.name, new_stage.name,
                self._hook_timeout_count,
            )
        except RuntimeError:
            # ``asyncio.run`` rejects re-entry if a loop is already
            # bound to the current thread (rare but possible in
            # certain test fixtures).  Treat as "no usable loop" and
            # skip — counted as an error so the misuse stays visible.
            self._hook_error_count += 1
            logger.debug(
                "pre_transition_hook skipped: no usable event loop "
                "(error_count=%d)",
                self._hook_error_count,
            )
            try:
                coro.close()    # type: ignore[union-attr]
            except Exception:    # noqa: BLE001
                pass
        except Exception as exc:    # noqa: BLE001 - hook is user code
            self._hook_error_count += 1
            logger.warning(
                "pre_transition_hook raised %r on %s -> %s "
                "(error_count=%d)",
                exc, last_stage.name, new_stage.name,
                self._hook_error_count,
            )

    def current(self) -> Tuple[SleepStage, float]:
        """Return ``(stable_stage, confidence)`` for the inference loop.

        Always returns a usable pair so the controller never has to
        worry about ``None``.  Confidence stays low until a real update
        arrives, which keeps the controller in a conservative regime
        during the cold-start window after add-on restart.

        The returned stage is the *debounced* value (see :data:`_DEFAULT_MIN_STAGE_DWELL_SECONDS`).
        Use :attr:`raw_stage` if you need the un-filtered most-recent
        observation (e.g. for a diagnostic Lovelace sensor).
        """
        # Lazy-promote in case enough time has elapsed since the last
        # observation but no new event has arrived (event-only sources).
        self._maybe_promote_candidate(time.time())
        return (self._stable_stage, self._confidence)

    @property
    def raw_stage(self) -> SleepStage:
        """The most recent un-filtered observation.  Useful for diagnostics."""
        return self._raw_stage

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
            "current_stage": self._stable_stage.name,
            "raw_stage": self._raw_stage.name,
            "candidate_stage": (
                self._candidate_stage.name if self._candidate_stage else None
            ),
            "min_dwell_seconds": self._min_dwell,
            "confidence": round(self._confidence, 3),
            "updates_received": self._update_count,
            "last_update_ts": self._last_update_ts,
            "stale": self.is_stale(),
            # v3.0.0 — pre-transition hook diagnostics (R11.6)
            "pre_transition_hooks_registered": len(self._pre_transition_hooks),
            "hook_error_count": self._hook_error_count,
            "hook_timeout_count": self._hook_timeout_count,
        }


__all__ = ["ExternalStageSubscriber", "PreTransitionHook"]
