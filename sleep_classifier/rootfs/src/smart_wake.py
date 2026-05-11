"""Smart wake-up planner.

Goal
----
Wake the user inside a *user-supplied window* — e.g. ``07:00 – 07:30`` —
at the moment that minimises **sleep inertia** (the groggy "where am I"
period that follows abrupt awakening from deep sleep).

The literature is consistent on three findings:

1.  **Avoid waking from N3 / SWS (DEEP).**  Hilditch & McHill (2019)
    review concludes inertia is 5-10× longer when woken from N3 vs N1
    or REM.  We therefore *strongly* prefer to wake during LIGHT or
    just-after-REM transitions.
2.  **Aim slightly before the natural REM-out edge.**  Trotter et al.
    (2018) showed alarm-induced wake is most graceful when it happens
    in the late portion of a REM cycle, just as the brain is climbing
    back to N1/awake.
3.  **Gradual light + sound > sudden alarm.**  Phipps-Nelson et al.
    (2003) and Gabel et al. (2013) show 30-min ramped blue-enriched
    light reduces inertia by ~40 % even when the actual wake instant
    is held constant.  Our planner therefore returns *both* a
    "light-ramp start" and a "alarm fire" timestamp.

Constraints
-----------
* Only the **window end** is hard: we never wake the user outside the
  user-supplied bounds.  The window start is the *earliest acceptable*
  wake time.
* If we get to the late edge and the user is still in DEEP, we wake
  anyway — being late > being woken from N3.

This module has zero asyncio, zero HA dependencies, and no global
state, so it's straightforward to unit-test against synthetic stage
sequences.

References
~~~~~~~~~~
* Hilditch CJ, McHill AW. Sleep inertia: current insights. *Nat Sci
  Sleep* 11 (2019) 155-165.
* Trotter MI et al. The effect of alarm clock waveform on subjective
  alertness. *Sleep* 41 (2018) abstract 0203.
* Phipps-Nelson J et al. Daytime exposure to bright light, as
  compared to dim light, decreases sleepiness and improves
  psychomotor vigilance performance. *Sleep* 26 (2003) 695-700.
* Gabel V et al. Effects of artificial dawn and morning blue light
  on daytime cognitive performance. *Chronobiol Int* 30 (2013).
* Geerdink M et al. Short blue-enriched light pulses promote alertness
  and morning behaviour. *J Biol Rhythms* 31 (2016) 483-497.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

from src._time_utils import now_local
from src.data_structures import SleepStage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# How far before the alarm fire to start ramping the bedroom light.  30 min
# is the value Phipps-Nelson 2003 used and is now the de-facto standard.
LIGHT_RAMP_DURATION_MIN = 30

# Minimum confidence in the current stage prediction below which we won't
# act on it.  Otherwise a flap from LIGHT→DEEP→LIGHT could trigger an
# unwanted wake-up at the start of the window.
MIN_STAGE_CONFIDENCE = 0.55

# How many of the last stages must agree we're "out of DEEP" before we
# call a wake-friendly moment.  Smooths over single-frame mis-classifications.
# At infer_interval=30s, 3 means ~90s of consistent non-DEEP.
NON_DEEP_DEBOUNCE = 3

# Prefer to wake at a LIGHT-following-REM boundary (post-REM), but if we
# don't see a REM exit within the window, plain LIGHT is acceptable.
PREFER_POST_REM = True

# Hard guarantee: we always wake by ``window_end - SAFETY_MARGIN_MIN`` if
# nothing better has been found, so the user never overshoots.
SAFETY_MARGIN_SEC = 60


class WakeDecision(str, Enum):
    """What the planner is currently telling the caller to do."""

    HOLD = "hold"        # too early to wake, keep sleeping
    PRE_RAMP = "pre_ramp"   # start light-ramp now, alarm later
    OPEN_WINDOW = "open_window"  # in window, wait for friendly stage
    FIRE_NOW = "fire_now"    # wake now: friendly stage OR safety margin
    POST_WAKE = "post_wake"  # already woken; nothing to do


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class WakeWindow:
    """User-supplied range during which a wake is permitted.

    Times are stored as :class:`datetime` so callers don't have to
    re-parse strings on every tick.  Use :meth:`from_strings` for the
    common ``"HH:MM"`` case.
    """

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(
                f"WakeWindow.end ({self.end}) must be after start ({self.start})"
            )

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def contains(self, t: datetime) -> bool:
        return self.start <= t <= self.end

    @classmethod
    def from_strings(
        cls,
        start_hhmm: str,
        end_hhmm: str,
        *,
        ref: Optional[datetime] = None,
    ) -> "WakeWindow":
        """Build a window from ``"07:00"``/``"07:30"`` strings.

        The window is anchored on the first day from ``ref`` whose
        ``end`` is in the future.  ``ref`` defaults to "now".
        """
        ref = ref or now_local()
        sh, sm = (int(x) for x in start_hhmm.split(":"))
        eh, em = (int(x) for x in end_hhmm.split(":"))
        s = ref.replace(hour=sh, minute=sm, second=0, microsecond=0)
        e = ref.replace(hour=eh, minute=em, second=0, microsecond=0)
        if e <= ref:
            s += timedelta(days=1)
            e += timedelta(days=1)
        return cls(start=s, end=e)


@dataclass
class WakePlan:
    """A snapshot of what the planner would do *right now*.

    Consumers should re-call :meth:`SmartWakePlanner.tick` every few
    seconds (the inference loop already runs at 30 s) and act on the
    latest plan.
    """

    decision: WakeDecision
    light_ramp_start: Optional[datetime]
    alarm_time: Optional[datetime]
    reason: str
    matched_stage: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision": self.decision.value,
            "light_ramp_start": self.light_ramp_start.isoformat()
            if self.light_ramp_start else None,
            "alarm_time": self.alarm_time.isoformat()
            if self.alarm_time else None,
            "reason": self.reason,
            "matched_stage": self.matched_stage,
        }


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class SmartWakePlanner:
    """Stateful planner; maintain ONE per active wake window.

    Lifecycle::

        # When user sets a window:
        planner = SmartWakePlanner(window)

        # Every inference tick (~30s):
        planner.observe_stage(SleepStage.LIGHT, confidence=0.91)
        plan = planner.tick(now=now_local())
        if plan.decision == WakeDecision.PRE_RAMP:
            await ha.call_service("light", "turn_on", brightness_pct=0)
            ...
        elif plan.decision == WakeDecision.FIRE_NOW:
            await ha.call_service("media_player", "play_media", ...)
            planner.mark_woken()
    """

    def __init__(
        self,
        window: WakeWindow,
        *,
        light_ramp_min: int = LIGHT_RAMP_DURATION_MIN,
        min_confidence: float = MIN_STAGE_CONFIDENCE,
        debounce: int = NON_DEEP_DEBOUNCE,
        prefer_post_rem: bool = PREFER_POST_REM,
        safety_margin_sec: int = SAFETY_MARGIN_SEC,
    ) -> None:
        self.window = window
        self.light_ramp_min = int(light_ramp_min)
        self.min_confidence = float(min_confidence)
        self.debounce = int(debounce)
        self.prefer_post_rem = bool(prefer_post_rem)
        self.safety_margin_sec = int(safety_margin_sec)

        # Recent stage history for boundary detection.
        self._stage_history: Deque[Tuple[SleepStage, float]] = deque(
            maxlen=max(self.debounce, 8),
        )
        self._woken = False

    # ------------------------------------------------------------------ #
    # Inputs
    # ------------------------------------------------------------------ #

    def observe_stage(self, stage: SleepStage, confidence: float) -> None:
        """Feed one inference result into the planner."""
        self._stage_history.append((stage, float(confidence)))

    def mark_woken(self) -> None:
        """Caller signals that the user is now awake; planner goes dormant."""
        self._woken = True

    # ------------------------------------------------------------------ #
    # Decision
    # ------------------------------------------------------------------ #

    def tick(self, *, now: Optional[datetime] = None) -> WakePlan:
        """Return the current :class:`WakePlan` given internal state.

        This method is **pure** w.r.t. wall-clock time — pass ``now`` in
        unit tests to test boundary cases.  In production, leave it
        ``None`` and we'll use :func:`src._time_utils.now_local` so that
        a wake window typed as ``"07:00"`` matches the user's local clock.
        """
        ref = now or now_local()
        if self._woken:
            return WakePlan(
                decision=WakeDecision.POST_WAKE,
                light_ramp_start=None, alarm_time=None,
                reason="user already woken",
            )

        ramp_start = self.window.end - timedelta(minutes=self.light_ramp_min)

        # Phase 1: too early — keep sleeping.
        if ref < ramp_start:
            return WakePlan(
                decision=WakeDecision.HOLD,
                light_ramp_start=ramp_start,
                alarm_time=self.window.end,
                reason=(
                    f"sleeping; light ramp begins at {ramp_start.time()}"
                ),
            )

        # Phase 2: between ramp_start and window.start — ramp lights but
        # don't fire alarm yet (window hasn't opened).
        if ref < self.window.start:
            return WakePlan(
                decision=WakeDecision.PRE_RAMP,
                light_ramp_start=ramp_start,
                alarm_time=self._best_friendly_time(ref) or self.window.end,
                reason="pre-window light ramp underway",
            )

        # Phase 3: inside the window — wake on a friendly boundary OR
        # at the safety margin, whichever comes first.
        safety_deadline = self.window.end - timedelta(seconds=self.safety_margin_sec)
        if ref >= safety_deadline:
            return WakePlan(
                decision=WakeDecision.FIRE_NOW,
                light_ramp_start=ramp_start,
                alarm_time=ref,
                reason="reached safety margin — wake regardless of stage",
                matched_stage=self._latest_stage_name(),
            )

        if self._is_friendly_now():
            return WakePlan(
                decision=WakeDecision.FIRE_NOW,
                light_ramp_start=ramp_start,
                alarm_time=ref,
                reason=self._friendly_reason(),
                matched_stage=self._latest_stage_name(),
            )

        return WakePlan(
            decision=WakeDecision.OPEN_WINDOW,
            light_ramp_start=ramp_start,
            alarm_time=safety_deadline,
            reason=(
                "in window; waiting for LIGHT/REM-exit "
                f"(current={self._latest_stage_name() or 'unknown'})"
            ),
            matched_stage=self._latest_stage_name(),
        )

    # ------------------------------------------------------------------ #
    # Internal heuristics
    # ------------------------------------------------------------------ #

    def _latest_stage_name(self) -> Optional[str]:
        if not self._stage_history:
            return None
        return self._stage_history[-1][0].name

    def _is_friendly_now(self) -> bool:
        """True iff the current stage is a low-inertia exit point.

        The rules:
        * Last stage must be LIGHT or AWAKE with confidence > min_confidence.
        * The previous ``debounce-1`` stages must all be NOT DEEP.
        * If ``prefer_post_rem`` is set, REM-then-LIGHT in the trailing
          window is the ideal fire-now signal.
        """
        if len(self._stage_history) < self.debounce:
            return False
        last_stage, last_conf = self._stage_history[-1]
        if last_conf < self.min_confidence:
            return False
        if last_stage not in (SleepStage.LIGHT, SleepStage.AWAKE):
            return False
        # Debounce: none of the last ``debounce`` frames may be DEEP.
        recent = list(self._stage_history)[-self.debounce :]
        if any(s == SleepStage.DEEP for s, _ in recent):
            return False
        if self.prefer_post_rem:
            # Look for a REM in the slightly broader history that's
            # been followed by LIGHT — the ideal post-REM moment.
            stages = [s for s, _ in self._stage_history]
            if SleepStage.REM in stages[:-1] and last_stage == SleepStage.LIGHT:
                return True
            # Pure LIGHT also acceptable but flagged in reason text.
            return last_stage in (SleepStage.LIGHT, SleepStage.AWAKE)
        return True

    def _friendly_reason(self) -> str:
        if not self._stage_history:
            return "friendly stage"
        stages = [s for s, _ in self._stage_history]
        last = stages[-1]
        if SleepStage.REM in stages[:-1] and last == SleepStage.LIGHT:
            return "post-REM transition into LIGHT — ideal wake point"
        if last == SleepStage.LIGHT:
            return "stable LIGHT sleep — low-inertia wake point"
        return f"acceptable wake stage: {last.name}"

    def _best_friendly_time(
        self, ref: datetime,
    ) -> Optional[datetime]:
        """Predict the next likely friendly wake-up time within the window.

        Currently a stub: we simply return the window start, since
        accurate REM-cycle prediction requires the full hypnogram.  The
        full implementation would forecast the next LIGHT exit using
        the typical 90-min REM cycle clock.  Left as a future hook so
        the public API stays stable.
        """
        if ref >= self.window.start:
            return ref
        return self.window.start

    # ------------------------------------------------------------------ #
    # Convenience
    # ------------------------------------------------------------------ #

    def state_summary(self) -> Dict[str, Any]:
        """Diagnostic dict used by the publisher to populate HA attrs."""
        return {
            "window_start": self.window.start.isoformat(),
            "window_end": self.window.end.isoformat(),
            "stage_history": [
                (s.name, round(c, 2)) for s, c in self._stage_history
            ],
            "woken": self._woken,
        }


# ---------------------------------------------------------------------------
# Light-ramp helper
# ---------------------------------------------------------------------------


def light_ramp_brightness(
    *,
    now: datetime,
    ramp_start: datetime,
    ramp_end: datetime,
    target_pct: float = 100.0,
    min_pct: float = 0.0,
    curve: str = "exp",
) -> float:
    """Return the bedroom light brightness % at instant ``now``.

    The curve options match what the literature compares:

    * ``"linear"`` — simple lerp from ``min_pct`` to ``target_pct``.
    * ``"exp"`` — fast at the end, mimicking a natural sunrise (the
      Phipps-Nelson 2003 protocol).  This is the default because most
      consumer dawn-simulators use it and users perceive it as gentler.

    Outside the ramp window we clamp to the appropriate bound so callers
    can call this every tick without their own time-checks.
    """
    if now <= ramp_start:
        return float(min_pct)
    if now >= ramp_end:
        return float(target_pct)
    elapsed = (now - ramp_start).total_seconds()
    duration = max(1.0, (ramp_end - ramp_start).total_seconds())
    frac = elapsed / duration
    if curve == "linear":
        out = min_pct + (target_pct - min_pct) * frac
    else:  # exp
        # `(exp(k * f) - 1) / (exp(k) - 1)` shape, k=3 gives a nice toe.
        import math
        k = 3.0
        shape = (math.exp(k * frac) - 1.0) / (math.exp(k) - 1.0)
        out = min_pct + (target_pct - min_pct) * shape
    return float(max(min_pct, min(target_pct, out)))
