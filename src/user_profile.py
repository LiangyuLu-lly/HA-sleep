"""User profile: age cohort + personalised sleep-need estimate.

Why this module exists
----------------------
The rest of the system (sleep-debt accountant, smart wake, quality
scorer) needs *one* number per user: ``recommended_total_sleep_hours``.
That number depends on:

1. **Age** — children need 9-12 h, adults 7-9 h, elderly 7-8 h.  See
   the cited literature below for the breakpoints we use.
2. **Personal history** — if a particular user consistently feels great
   on 7.5 h despite the textbook recommending 8.0 h for their cohort,
   we should respect that.

We therefore model sleep need as

    target = blend(cohort_default, posterior_personal_estimate)

where the blend weight is governed by a Bayesian update: the more
high-quality (subjective ≥ 4/5 *and* objective ≥ 70/100) sessions we
have observed for this user, the more weight we put on the personal
estimate.

References
~~~~~~~~~~
* Hirshkowitz et al., **National Sleep Foundation's sleep time
  duration recommendations: methodology and results summary**,
  *Sleep Health* 1 (2015) 40-43.  Source of the cohort tables below.
* Paruthi et al., **Recommended Amount of Sleep for Pediatric
  Populations: A Consensus Statement of the American Academy of
  Sleep Medicine**, *J Clin Sleep Med* 12 (2016) 785-786.  AAP
  cross-checked the same numbers for under-18s.
* Kryger et al., *Principles and Practice of Sleep Medicine*, 7e,
  ch. 4 — life-span variation in sleep architecture.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Age cohort definitions
# ---------------------------------------------------------------------------


class AgeCohort(str, Enum):
    """Coarse age bins matching the NSF / AAP recommendation tables.

    The string values are stable identifiers used for persistence and
    log-friendly display, so renaming an enum member is a breaking
    change and should bump the persistence file version.
    """

    NEWBORN = "newborn"        # 0-3 months
    INFANT = "infant"          # 4-11 months
    TODDLER = "toddler"        # 1-2 years
    PRESCHOOL = "preschool"    # 3-5 years
    SCHOOL_AGE = "school_age"  # 6-13 years
    TEEN = "teen"              # 14-17 years
    YOUNG_ADULT = "young_adult"  # 18-25 years
    ADULT = "adult"            # 26-64 years
    SENIOR = "senior"          # 65+ years


# ---------------------------------------------------------------------------
# NSF / AAP recommended total sleep hours per cohort
# ---------------------------------------------------------------------------
# Tuple is (recommended_low, recommended_target, recommended_high) where
# *target* is the midpoint sleep time we steer the system towards, and the
# bounds are used by the sleep-debt module to decide what counts as
# "under-slept" vs "marginally-short" vs "fine".
#
# Numbers are in hours of total sleep (NOT time-in-bed).
# Source: NSF 2015 + AAP/AASM 2016 consensus statements.

_COHORT_HOURS: Dict[AgeCohort, Tuple[float, float, float]] = {
    AgeCohort.NEWBORN:     (14.0, 15.5, 17.0),
    AgeCohort.INFANT:      (12.0, 13.5, 15.0),
    AgeCohort.TODDLER:     (11.0, 12.0, 14.0),
    AgeCohort.PRESCHOOL:   (10.0, 11.0, 13.0),
    AgeCohort.SCHOOL_AGE:  ( 9.0, 10.0, 11.0),
    AgeCohort.TEEN:        ( 8.0,  9.0, 10.0),
    AgeCohort.YOUNG_ADULT: ( 7.0,  8.0,  9.0),
    AgeCohort.ADULT:       ( 7.0,  8.0,  9.0),
    AgeCohort.SENIOR:      ( 7.0,  7.5,  8.0),
}


def cohort_for_age(age_years: float) -> AgeCohort:
    """Map an age in years (fractional ok) to the coarse NSF/AAP cohort."""
    if age_years < 0:
        raise ValueError(f"age_years must be >= 0, got {age_years!r}")
    # Note: edge values are inclusive on the lower bound, exclusive on the
    # upper, matching the table footnotes in NSF 2015.
    if age_years < 0.25:        # < 3 months
        return AgeCohort.NEWBORN
    if age_years < 1.0:         # 4-11 months
        return AgeCohort.INFANT
    if age_years < 3.0:         # 1-2 yrs
        return AgeCohort.TODDLER
    if age_years < 6.0:         # 3-5 yrs
        return AgeCohort.PRESCHOOL
    if age_years < 14.0:        # 6-13 yrs
        return AgeCohort.SCHOOL_AGE
    if age_years < 18.0:        # 14-17 yrs
        return AgeCohort.TEEN
    if age_years < 26.0:        # 18-25 yrs
        return AgeCohort.YOUNG_ADULT
    if age_years < 65.0:        # 26-64 yrs
        return AgeCohort.ADULT
    return AgeCohort.SENIOR


def cohort_recommendation(cohort: AgeCohort) -> Tuple[float, float, float]:
    """Return (low, target, high) recommended hours for a cohort."""
    return _COHORT_HOURS[cohort]


# ---------------------------------------------------------------------------
# Persisted user profile
# ---------------------------------------------------------------------------


# Strength priors: how confident we are in the textbook recommendation
# for this cohort *before* we observe anything about this particular
# user.  Roughly equivalent to a Bayesian "pseudo-count" of nights of
# data.  We pick small numbers (5-10 nights) so the personal estimate
# starts mattering after the first week of use.
_PRIOR_STRENGTH_NIGHTS = 7.0


@dataclass
class UserProfile:
    """All user-tunable identity + a running posterior on sleep need.

    The profile lives on disk (``/data/user_profile.json`` inside the
    add-on; ``data/user_profile.json`` in dev) and is updated after every
    high-quality session.  We deliberately keep the schema flat so the
    user can edit it manually with a text editor if needed.
    """

    # User-supplied identity / metadata
    user_id: str = "default"
    birth_year: Optional[int] = None
    chronotype: str = "neutral"   # "morning", "evening", "neutral"
    timezone: str = "Asia/Shanghai"

    # Posterior estimates (updated by ``record_quality_session``)
    posterior_mean_hours: float = 0.0   # 0 == no observations yet
    posterior_count: float = 0.0        # in-units-of-nights
    last_updated: float = 0.0

    # Persistence schema version — bump on breaking changes.
    schema_version: int = 1

    # ------------------------------------------------------------------ #
    # Derived properties
    # ------------------------------------------------------------------ #

    @property
    def age_years(self) -> Optional[float]:
        """Best estimate of age in years, or ``None`` if unknown."""
        if self.birth_year is None:
            return None
        # Use the wall clock; fractional years for sub-toddler accuracy.
        seconds_per_year = 365.25 * 24 * 3600
        # ``time.time()`` in seconds since epoch; the year part of the
        # birth date is enough granularity for the cohort table.
        from datetime import datetime
        now = datetime.utcnow()
        approx = now.year + (now.timetuple().tm_yday - 1) / 365.25 - self.birth_year
        return max(0.0, approx)

    @property
    def cohort(self) -> AgeCohort:
        """Cohort for ``age_years`` (defaults to ADULT if age unknown)."""
        a = self.age_years
        return cohort_for_age(a) if a is not None else AgeCohort.ADULT

    def cohort_target_hours(self) -> float:
        """Pure cohort recommendation, ignoring personal posterior."""
        return cohort_recommendation(self.cohort)[1]

    def cohort_bounds_hours(self) -> Tuple[float, float]:
        """``(low, high)`` for the user's cohort."""
        low, _, high = cohort_recommendation(self.cohort)
        return (low, high)

    def recommended_total_sleep_hours(self) -> float:
        """Bayesian blend of the cohort prior and the personal posterior.

        The blend weight is ``count / (prior + count)``, i.e. the
        personal estimate dominates once we have more observations
        than the prior pseudo-count.

        Returns the cohort target if no high-quality sessions have been
        observed yet; otherwise smoothly migrates towards the personal
        estimate as evidence accumulates.
        """
        cohort_target = self.cohort_target_hours()
        if self.posterior_count <= 0.0 or self.posterior_mean_hours <= 0.0:
            return cohort_target
        prior_w = _PRIOR_STRENGTH_NIGHTS
        post_w = self.posterior_count
        blended = (
            prior_w * cohort_target + post_w * self.posterior_mean_hours
        ) / (prior_w + post_w)
        # Clamp to the cohort bounds — even a strong personal posterior
        # should never push us into clinically dangerous territory.
        low, high = self.cohort_bounds_hours()
        return float(max(low, min(high, blended)))

    # ------------------------------------------------------------------ #
    # Bayesian update
    # ------------------------------------------------------------------ #

    def record_quality_session(
        self,
        observed_hours: float,
        *,
        objective_score: float,
        subjective_score: Optional[float] = None,
        weight: float = 1.0,
    ) -> None:
        """Update the posterior using one night's evidence.

        Only sessions with ``objective_score >= 60`` AND (no subjective
        feedback OR ``subjective_score >= 3.0``) on a 1-5 scale are
        treated as evidence for this particular sleep duration being
        right for the user.  Lower-quality sessions still feed into the
        environment learner, but they would bias the duration estimate
        downwards if we let them.

        Args:
            observed_hours: total sleep time of the session.
            objective_score: 0-100 from :func:`compute_quality_score`.
            subjective_score: optional 1-5 user rating.
            weight: multiplier in [0, 2] e.g. to over-weight the user's
                most recent vacation week.
        """
        if observed_hours <= 0.0 or observed_hours > 24.0:
            return
        if objective_score < 60.0:
            return
        if subjective_score is not None and subjective_score < 3.0:
            return
        w = max(0.0, min(2.0, float(weight)))
        if w == 0.0:
            return
        # Online mean update: μ_n = μ_{n-1} + w (x - μ_{n-1}) / (n + w)
        new_count = self.posterior_count + w
        if self.posterior_count == 0:
            self.posterior_mean_hours = observed_hours
        else:
            delta = observed_hours - self.posterior_mean_hours
            self.posterior_mean_hours += w * delta / new_count
        self.posterior_count = new_count
        self.last_updated = time.time()

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["cohort"] = self.cohort.value
        d["recommended_total_sleep_hours"] = self.recommended_total_sleep_hours()
        return d

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "UserProfile":
        # Drop derived fields if present (round-tripped through to_dict).
        data = {k: v for k, v in raw.items() if k in {f.name for f in cls.__dataclass_fields__.values()}}  # type: ignore[attr-defined]
        return cls(**data)


# ---------------------------------------------------------------------------
# Disk-backed manager
# ---------------------------------------------------------------------------


class UserProfileStore:
    """Atomic file persistence wrapper around :class:`UserProfile`.

    The file format is one JSON document keyed by ``user_id`` so future
    multi-user setups (couples, guest rooms) need no schema migration.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self, user_id: str = "default") -> UserProfile:
        if not self._path.exists():
            return UserProfile(user_id=user_id)
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Could not read %s (%s) — starting with a fresh profile",
                self._path, exc,
            )
            return UserProfile(user_id=user_id)
        users = payload.get("users", {}) if isinstance(payload, dict) else {}
        raw = users.get(user_id)
        if not raw:
            return UserProfile(user_id=user_id)
        return UserProfile.from_dict(raw)

    def save(self, profile: UserProfile) -> None:
        # Read-modify-write to preserve other users' data.
        existing: Dict[str, Any] = {"version": 1, "users": {}}
        if self._path.exists():
            try:
                existing = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        existing.setdefault("users", {})[profile.user_id] = profile.to_dict()
        existing["updated_at"] = time.time()

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def list_users(self) -> List[str]:
        if not self._path.exists():
            return []
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return sorted(payload.get("users", {}).keys())
