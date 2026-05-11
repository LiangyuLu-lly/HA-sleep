"""Sleep-debt accountant + recovery planner.

What is sleep debt?
-------------------
The cumulative shortfall between **how much sleep you needed** (driven by
your :class:`UserProfile`) and **how much you actually got** over a
rolling window.  E.g. a 30-year-old whose target is 8 h/night but who
only slept 6 h on Mon and 7 h on Tue carries a debt of (8-6) + (8-7) =
3 h going into Wednesday.

Why a separate module
---------------------
Sleep debt is a **physiological** quantity with peer-reviewed dose-
response curves; it doesn't belong inside the environment-preference
learner.  Keeping it standalone means we can:

* expose ``sensor.sleep_debt_hours`` independently to HA dashboards;
* run unit tests against published reference numbers without booting the
  full asyncio service;
* let the user query "how should I recover?" via a one-shot CLI call.

How recovery works (this is the patentable bit)
-----------------------------------------------
We blend three insights from the literature:

1.  **One-shot recovery is impossible past ~2 h of debt.**  Van Dongen
    et al. (2003) show that a single 10-h "recovery night" only
    restores baseline performance up to ≈ 2 h of accumulated debt; past
    that, more nights are required.  We therefore cap the same-night
    recovery contribution at ``MAX_SAME_NIGHT_RECOVERY_H`` (2.0 h).

2.  **Diminishing returns above ~10 h of TIB.**  Sleeping 12 h does
    *not* dump 4 h of debt — extra REM cycles past hour 10 add little.
    We model recovery efficiency as a saturating exponential
    ``η(extra_h) = 1 - exp(-extra_h / τ)`` with τ ≈ 1.5 h, calibrated
    to Belenky et al. (2003) dose-response data.

3.  **Multi-night smoothing wins.**  Recovering 5 h of debt by adding
    +1 h/night for 5 nights produces measurably better daytime alertness
    than one +5 h marathon (Banks et al. 2010).  When debt exceeds the
    same-night cap we therefore propose a multi-night plan that pays
    down a fixed *fraction* per night (default 50 %) until the debt is
    below the cap, then a single recovery night closes the rest.

References
~~~~~~~~~~
* Van Dongen et al., *Sleep* 26(2) 2003, 117-126 — "The cumulative cost
  of additional wakefulness".
* Belenky et al., *J Sleep Res* 12(1) 2003, 1-12 — dose-response with
  3 / 5 / 7 / 9 h TIB.
* Banks et al., *Sleep* 33(8) 2010, 1013-1026 — recovery from chronic
  restriction.
* Walker, *Why We Sleep*, 2017 — popular synthesis covering both above.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from src._time_utils import date_from_timestamp_local, now_local

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables, exposed as module constants so callers can monkey-patch them
# in unit tests or expose them in the add-on Configuration tab later.
# ---------------------------------------------------------------------------

# Sliding-window length used to compute *acute* debt — anything older is
# considered "chronic" and discounted geometrically (see ``_DECAY``).
ACUTE_WINDOW_DAYS = 7

# Geometric decay applied to nights older than ACUTE_WINDOW_DAYS.  0.85
# means the 8-th-most-recent night counts 85 % of the 7-th, and so on.
# Picked to match the half-life of perceived fatigue in Belenky 2003.
_DECAY_PER_DAY = 0.85

# A single recovery night cannot make up for more than this many hours of
# debt.  Beyond it, daytime cognitive deficits persist (Van Dongen 2003).
MAX_SAME_NIGHT_RECOVERY_H = 2.0

# Saturation time-constant of the recovery-efficiency curve, in hours.
# Calibrated so ``η(2 h) ≈ 0.74`` and ``η(4 h) ≈ 0.93``.
_RECOVERY_TAU_H = 1.5

# Hard physiological ceiling on how much extra sleep we'll prescribe in
# one night, regardless of debt.  Sleeping > 11-12 h typically reflects a
# pathology, not normal recovery.
MAX_NIGHT_TOTAL_HOURS = 11.0

# What fraction of remaining debt to attempt to repay each night when
# the debt exceeds ``MAX_SAME_NIGHT_RECOVERY_H``.  0.5 = pay half, sleep
# on it, re-evaluate tomorrow.  Keeps each night's bedtime nudge small
# enough to be socially feasible.
DEBT_PAYDOWN_FRACTION = 0.5

# Per-chronotype shift (in hours) applied to the recommended bedtime
# *and* the implied wake target.  Numbers come from Roenneberg et al.
# 2007 "Epidemiology of the human circadian clock" — morning types
# ("larks") prefer to sleep ~1 hour earlier than the population mean,
# evening types ("owls") ~1 hour later.  Conservative ±45 min keeps
# the nudge perceptible without dragging late types into a 3-AM
# bedtime that breaks family schedules.
_CHRONOTYPE_BEDTIME_SHIFT_H = {
    "morning": -0.75,   # earlier than the wake_window-anchored default
    "neutral":  0.0,
    "evening": +0.75,   # later
}


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


class DebtSeverity(str, Enum):
    """Coarse banding used for UI badging and automation triggers."""

    NONE = "none"          # |debt| < 0.5 h — within noise
    MILD = "mild"          # 0.5 - 2 h
    MODERATE = "moderate"  # 2 - 4 h
    SEVERE = "severe"      # 4 - 8 h
    CHRONIC = "chronic"    # > 8 h sustained


def _severity(debt_hours: float) -> DebtSeverity:
    """Map a continuous debt to a discrete severity bucket."""
    a = abs(debt_hours)
    if a < 0.5:
        return DebtSeverity.NONE
    if a < 2.0:
        return DebtSeverity.MILD
    if a < 4.0:
        return DebtSeverity.MODERATE
    if a < 8.0:
        return DebtSeverity.SEVERE
    return DebtSeverity.CHRONIC


@dataclass
class NightRecord:
    """One night's sleep in the debt ledger."""

    date: str                # ISO ``YYYY-MM-DD`` of the *wake* day
    target_hours: float      # what the profile says they should have got
    actual_hours: float      # measured TST (total sleep time)
    quality_score: float = 0.0   # 0-100 from quality scorer

    @property
    def shortfall_hours(self) -> float:
        """Positive = under-slept; negative = over-slept."""
        return self.target_hours - self.actual_hours


@dataclass
class RecoveryPlan:
    """Concrete bedtime recommendation produced by :class:`SleepDebtTracker`.

    Consumers (HA add-on, CLI) only need to look at:

    * ``tonight_target_hours`` — sleep this long tonight,
    * ``tonight_bedtime`` — go to bed at this wall-clock time,
    * ``nights_to_full_recovery`` — projected nights until debt < 0.5 h,
    * ``message`` — human-readable English/Chinese explanation.
    """

    current_debt_hours: float
    severity: DebtSeverity
    tonight_target_hours: float
    tonight_bedtime: Optional[datetime]
    wake_target: Optional[datetime]
    nights_to_full_recovery: int
    paydown_fraction_used: float
    message: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "current_debt_hours": round(self.current_debt_hours, 2),
            "severity": self.severity.value,
            "tonight_target_hours": round(self.tonight_target_hours, 2),
            "tonight_bedtime": self.tonight_bedtime.isoformat()
            if self.tonight_bedtime else None,
            "wake_target": self.wake_target.isoformat()
            if self.wake_target else None,
            "nights_to_full_recovery": self.nights_to_full_recovery,
            "paydown_fraction_used": round(self.paydown_fraction_used, 2),
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# The accountant
# ---------------------------------------------------------------------------


class SleepDebtTracker:
    """Compute current debt and prescribe a recovery plan.

    The tracker is a thin pure-Python wrapper around a list of
    :class:`NightRecord` objects, so it is easy to hydrate from
    :class:`PreferenceLearner` history or from a unit-test fixture.

    Use as::

        tracker = SleepDebtTracker(profile)
        for night in last_n_nights:
            tracker.add_night(night)
        plan = tracker.plan_recovery(wake_window=("07:00", "07:30"))
    """

    def __init__(
        self,
        profile: "UserProfile",     # type: ignore[name-defined]  # circular
        *,
        decay_per_day: float = _DECAY_PER_DAY,
        max_same_night_recovery_h: float = MAX_SAME_NIGHT_RECOVERY_H,
        recovery_tau_h: float = _RECOVERY_TAU_H,
        max_night_total_h: float = MAX_NIGHT_TOTAL_HOURS,
        paydown_fraction: float = DEBT_PAYDOWN_FRACTION,
    ) -> None:
        self.profile = profile
        self._nights: List[NightRecord] = []
        self.decay_per_day = float(decay_per_day)
        self.max_same_night_recovery_h = float(max_same_night_recovery_h)
        self.recovery_tau_h = float(recovery_tau_h)
        self.max_night_total_h = float(max_night_total_h)
        self.paydown_fraction = float(paydown_fraction)

    # ------------------------------------------------------------------ #
    # Ingestion
    # ------------------------------------------------------------------ #

    def add_night(self, night: NightRecord) -> None:
        """Append a night to the ledger.  Order of insertion doesn't matter."""
        self._nights.append(night)

    def add_nights(self, nights: List[NightRecord]) -> None:
        for n in nights:
            self.add_night(n)

    @property
    def n_nights(self) -> int:
        return len(self._nights)

    # ------------------------------------------------------------------ #
    # Debt computation
    # ------------------------------------------------------------------ #

    def current_debt_hours(self, *, now: Optional[datetime] = None) -> float:
        """Return the (positive = under-slept) debt in hours.

        We take the **age-weighted sum of shortfalls** over the past
        ``ACUTE_WINDOW_DAYS`` nights at full weight, then geometrically
        decay older nights.  Negative shortfalls (over-sleep) shrink the
        debt at the same weight, so a sustained early bedtime *can*
        actually drive the debt below zero ("sleep credit").
        """
        if not self._nights:
            return 0.0
        ref = (now or now_local()).date()
        debt = 0.0
        for night in self._nights:
            try:
                d = datetime.fromisoformat(night.date).date()
            except ValueError:
                logger.warning("Bad date in NightRecord: %r", night.date)
                continue
            age_days = (ref - d).days
            if age_days < 0:
                # Future-dated record (e.g. clock skew) — ignore.
                continue
            # Inclusive bound: a record dated *exactly* ``ACUTE_WINDOW_DAYS``
            # ago is still part of the acute window.  This matches the
            # plain-English reading "the past 7 days carry full weight".
            if age_days <= ACUTE_WINDOW_DAYS:
                weight = 1.0
            else:
                weight = self.decay_per_day ** (age_days - ACUTE_WINDOW_DAYS)
            debt += weight * night.shortfall_hours
        return float(debt)

    def severity(self, *, now: Optional[datetime] = None) -> DebtSeverity:
        return _severity(self.current_debt_hours(now=now))

    # ------------------------------------------------------------------ #
    # Recovery efficiency (Belenky 2003 saturating curve)
    # ------------------------------------------------------------------ #

    def recovery_efficiency(self, extra_hours: float) -> float:
        """How much of *extra_hours* slept past target actually pays debt.

        ``η(0)=0``, ``η(τ)≈0.63``, ``η(∞)=1``.  Multiplying by
        ``extra_hours`` therefore gives the *effective* debt reduction.
        """
        if extra_hours <= 0:
            return 0.0
        return 1.0 - math.exp(-extra_hours / self.recovery_tau_h)

    def effective_debt_reduction(self, extra_hours: float) -> float:
        """Hours of debt actually dumped by sleeping ``extra_hours`` extra."""
        return self.recovery_efficiency(extra_hours) * extra_hours

    # ------------------------------------------------------------------ #
    # Recovery planning
    # ------------------------------------------------------------------ #

    def plan_recovery(
        self,
        *,
        wake_window: Optional[Tuple[str, str]] = None,
        now: Optional[datetime] = None,
    ) -> RecoveryPlan:
        """Return a concrete RecoveryPlan for tonight.

        Args:
            wake_window: ``("HH:MM", "HH:MM")`` user-acceptable wake
                interval.  We aim the recovery at the *late* boundary
                so the user gets the maximum tolerable lie-in, then
                Smart-Wake will pick the actual moment within the
                window from sleep stages.
            now: override of "right now" for deterministic testing.
        """
        target = float(self.profile.recommended_total_sleep_hours())
        debt = self.current_debt_hours(now=now)
        sev = _severity(debt)
        ref = now or now_local()

        # ---- Decide tonight's sleep duration --------------------------
        if debt <= 0.5:
            # No meaningful debt — sleep your normal target.
            extra = max(0.0, -debt) * 0.0   # don't double-count credit
            tonight_target = target
            paydown_used = 0.0
            msg = self._compose_msg("none", debt, target, tonight_target)
        elif debt <= self.max_same_night_recovery_h:
            # Small debt — we can pay it off in one go.  Need to sleep
            # ``extra`` such that ``η(extra) * extra >= debt``.
            extra = self._invert_recovery(debt)
            tonight_target = min(self.max_night_total_h, target + extra)
            paydown_used = 1.0
            msg = self._compose_msg("single", debt, target, tonight_target)
        else:
            # Large debt — pay down a fraction tonight, plan the rest.
            target_payment = self.paydown_fraction * debt
            target_payment = min(
                target_payment,
                self.effective_debt_reduction(self.max_same_night_recovery_h),
            )
            extra = self._invert_recovery(target_payment)
            tonight_target = min(self.max_night_total_h, target + extra)
            paydown_used = self.paydown_fraction
            msg = self._compose_msg(
                "multi", debt, target, tonight_target,
            )

        # ---- Project nights to full recovery --------------------------
        n_more = self._project_recovery_nights(debt, tonight_target - target)

        # ---- Compute concrete bedtime if a wake_window is provided ----
        tonight_bedtime: Optional[datetime] = None
        wake_target: Optional[datetime] = None
        if wake_window is not None:
            wake_target = self._end_of_window(wake_window, ref=ref)
            # Apply chronotype-based bedtime shift.  Morning types go
            # to bed earlier (negative shift), evening types later.
            chronotype = getattr(self.profile, "chronotype", "neutral")
            shift_h = _CHRONOTYPE_BEDTIME_SHIFT_H.get(chronotype, 0.0)
            tonight_bedtime = (
                wake_target
                - timedelta(hours=tonight_target)
                + timedelta(hours=shift_h)
            )
            # Clamp: bedtime cannot be after wake_target (sanity).
            if tonight_bedtime >= wake_target:
                tonight_bedtime = wake_target - timedelta(hours=tonight_target)

        # Keep the *unrounded* target on the dataclass so callers can do
        # arithmetic against ``wake_target - tonight_bedtime`` without
        # losing seconds to display rounding.  ``to_dict()`` is what
        # renders the rounded number for HA / Lovelace.
        return RecoveryPlan(
            current_debt_hours=debt,
            severity=sev,
            tonight_target_hours=float(tonight_target),
            tonight_bedtime=tonight_bedtime,
            wake_target=wake_target,
            nights_to_full_recovery=n_more,
            paydown_fraction_used=paydown_used,
            message=msg,
        )

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _invert_recovery(self, target_debt_payment_h: float) -> float:
        """Solve for ``extra`` such that ``η(extra)*extra ≈ target_payment``.

        Closed form is messy; bisection over [0, MAX_NIGHT_TOTAL_HOURS-target]
        is cheap (≤ 30 iters) and avoids a numpy dependency in the
        runtime image.
        """
        if target_debt_payment_h <= 0.0:
            return 0.0
        lo, hi = 0.0, float(self.max_night_total_h)
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            if self.effective_debt_reduction(mid) < target_debt_payment_h:
                lo = mid
            else:
                hi = mid
            if hi - lo < 1e-3:
                break
        return 0.5 * (lo + hi)

    def _project_recovery_nights(
        self, debt_hours: float, tonight_extra: float,
    ) -> int:
        """Simulate nights until projected debt < 0.5 h, capped at 14."""
        if debt_hours <= 0.5:
            return 0
        sim_debt = debt_hours - self.effective_debt_reduction(tonight_extra)
        nights = 1
        # On subsequent nights we apply the same paydown_fraction rule,
        # which converges geometrically.  The recurrence has a closed
        # form but explicit simulation is clearer + easier to test.
        while sim_debt > 0.5 and nights < 14:
            payment = min(
                self.paydown_fraction * sim_debt
                if sim_debt > self.max_same_night_recovery_h else sim_debt,
                self.effective_debt_reduction(self.max_same_night_recovery_h),
            )
            extra = self._invert_recovery(payment)
            sim_debt -= self.effective_debt_reduction(extra)
            nights += 1
        return nights

    def _end_of_window(
        self, wake_window: Tuple[str, str], *, ref: datetime,
    ) -> datetime:
        """Return the *late* end of the wake window as a datetime.

        Convention: the window is in the user's local time and refers to
        tomorrow morning if "now" is already past midday.  Otherwise it
        refers to today.  Time zones are intentionally not handled here —
        the caller passes datetimes that already reflect the right TZ.
        """
        end_str = wake_window[1]
        h, m = (int(x) for x in end_str.split(":"))
        candidate = ref.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= ref:
            candidate += timedelta(days=1)
        return candidate

    def _compose_msg(
        self,
        kind: str,
        debt: float,
        target: float,
        tonight: float,
    ) -> str:
        if kind == "none":
            return (
                f"You're on track ({debt:+.1f} h debt). "
                f"Sleep your usual {target:.1f} h."
            )
        if kind == "single":
            return (
                f"You're {debt:.1f} h in debt. "
                f"Sleep {tonight:.1f} h tonight (+{tonight-target:.1f} h) "
                "to wipe it out."
            )
        # multi
        return (
            f"You're {debt:.1f} h in debt — too large for one night. "
            f"Sleep {tonight:.1f} h tonight (paying down "
            f"{self.paydown_fraction*100:.0f}% of debt); we'll re-plan tomorrow."
        )

    # ------------------------------------------------------------------ #
    # Convenience constructors
    # ------------------------------------------------------------------ #

    @classmethod
    def from_sessions(
        cls,
        profile: "UserProfile",     # type: ignore[name-defined]
        sessions: List[Any],
    ) -> "SleepDebtTracker":
        """Hydrate from a list of :class:`SleepSession` objects.

        The tracker only needs ``started_at``, ``ended_at`` and
        ``quality_score`` from each session; we duck-type the access so
        a circular import on PreferenceLearner is avoided.
        """
        tracker = cls(profile)
        target = float(profile.recommended_total_sleep_hours())
        for s in sessions:
            try:
                started = float(s.started_at)
                ended = float(s.ended_at)
                quality = float(s.quality_score)
            except AttributeError:
                continue
            actual_hours = max(0.0, (ended - started) / 3600.0)
            # Bucket sessions by *local* wake date so a 23:30 → 06:00
            # sleep is filed under the morning the user thinks of as today.
            wake_day = date_from_timestamp_local(ended).isoformat()
            tracker.add_night(NightRecord(
                date=wake_day,
                target_hours=target,
                actual_hours=actual_hours,
                quality_score=quality,
            ))
        return tracker
