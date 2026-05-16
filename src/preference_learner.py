"""Learn the user's preferred sleep environment from history.

Every night, the smart service records a *sleep session* — a triple of

* environment parameters that were active during the session
  (temperature, humidity, brightness, fan speed),
* the sleep-stage time-series streamed from the user's HA stage
  entity (Mi Band / Apple Watch / sleep_as_android / mmWave radar /
  ...) via :class:`src.external_stage_subscriber.ExternalStageSubscriber`,
* a derived **sleep quality score**.

From this rolling history we then estimate, for each future stage, the
environment setpoints that historically correlated with the **best** sleep
quality.  We do so with a **non-parametric** estimator (median of the top
``quality_quantile`` quantile of sessions) which:

* needs no model training,
* is robust to outliers,
* is easy to explain to the user ("we picked 19 °C because on the 5 nights
  you slept best the bedroom averaged 19 °C").

The learner persists state to ``data/user_preferences.json`` after every
session so the model survives restarts.
"""
from __future__ import annotations

import json
import logging
import math
import os
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from src.data_structures import SleepStage
from src._time_utils import now_local
from src._io_utils import atomic_write_json

# ---------------------------------------------------------------------------
# Tunables that don't belong in the per-user config (engineering defaults)
# ---------------------------------------------------------------------------
#
# Half-life of the decay window, measured in days.  ``W(d) = 2^(-d/H)``
# means a session 14 days old still gets 50 % weight — enough that the
# learner adapts to seasonal changes within ~1 month without
# overreacting to a single bad night.
_DEFAULT_HALF_LIFE_DAYS: float = 14.0

# Hours after which an interval is no longer considered "the same
# bedtime hour" for k-NN matching.  A 1.5 h Gaussian σ gives a soft
# cut-off: 22:00 has 0.4 weight against 23:30, dropping to 0.05 against
# 02:00 — close enough that ad-hoc late nights still nudge the
# recommendation but can't override the user's normal schedule.
_DEFAULT_HOUR_SIGMA: float = 1.5

# Sigma for the ambient-temperature kernel (°C).  Within ±1 °C of
# tonight's reading we want past sessions to count fully; >3 °C apart
# they should barely contribute.
_DEFAULT_TEMP_SIGMA: float = 1.5

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------


def compute_quality_score(stage_counts: Dict[str, int]) -> float:
    """Translate a stage distribution into a single 0-100 quality score.

    Heuristic, but motivated by sleep medicine:

    * DEEP and REM are restorative — reward higher proportions.
    * AWAKE during sleep window is bad — punish.
    * LIGHT is neutral — most of a healthy night.

    The reference ratios (DEEP ~ 0.15, REM ~ 0.22, LIGHT ~ 0.55,
    AWAKE ~ 0.05) come from the AASM normal adult ranges.
    """
    total = sum(stage_counts.values())
    if total <= 0:
        return 0.0

    def p(name: str) -> float:
        return stage_counts.get(name, 0) / total

    deep = p("DEEP")
    rem = p("REM")
    light = p("LIGHT")
    awake = p("AWAKE")

    # Each term is centred so the score is ~50 for an "average" night and
    # approaches 100 for a near-ideal sleep architecture.
    score = (
        50.0
        + 100.0 * (deep - 0.10)       # DEEP boost: ≥10% = neutral
        +  60.0 * (rem - 0.18)        # REM boost: ≥18% = neutral
        +  10.0 * (light - 0.50)      # mild reward for plenty of LIGHT
        - 150.0 * max(0.0, awake - 0.05)  # heavy penalty for fragmentation
    )
    return float(max(0.0, min(100.0, score)))


def stage_counts_from_sequence(stages: Sequence[SleepStage]) -> Dict[str, int]:
    """Return ``{"AWAKE": n, "LIGHT": n, "DEEP": n, "REM": n}`` from a list."""
    counts = {"AWAKE": 0, "LIGHT": 0, "DEEP": 0, "REM": 0}
    for s in stages:
        counts[s.name] = counts.get(s.name, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class EnvironmentParams:
    """A snapshot of the bedroom environment during a session."""

    temperature_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    brightness_pct: Optional[float] = None
    fan_speed_pct: Optional[float] = None

    def to_dict(self) -> Dict[str, Optional[float]]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "EnvironmentParams":
        return cls(
            temperature_c=raw.get("temperature_c"),
            humidity_pct=raw.get("humidity_pct"),
            brightness_pct=raw.get("brightness_pct"),
            fan_speed_pct=raw.get("fan_speed_pct"),
        )


@dataclass
class SleepSession:
    """One night (or one nap) of accumulated data.

    ``recorded_at`` is the wall-clock instant the session was committed
    to disk — we keep it separate from ``ended_at`` because the latter
    can be backfilled (e.g. a user importing historical data) and we
    want the decay weight to reflect "how long ago did we *learn* this?".
    Default = ``ended_at`` so old session files without the field still
    decay correctly.
    """

    session_id: str
    started_at: float                  # unix timestamp
    ended_at: float
    env_params: EnvironmentParams
    stage_counts: Dict[str, int]
    quality_score: float
    n_samples: int = 0
    notes: Optional[str] = None
    recorded_at: float = 0.0
    # v1.5.0 — env snapshot taken when the user *entered* each stage,
    # used by :meth:`PreferenceLearner.recommend_per_stage_deltas` to
    # learn each user's idiosyncratic AWAKE/LIGHT/DEEP/REM offsets
    # instead of using the hard-coded clinical defaults.  Keys are
    # SleepStage.name strings ("AWAKE", "LIGHT", "DEEP", "REM") to
    # keep the JSON round-trip cheap; empty dict means the session
    # was recorded pre-v1.5 and has no per-stage info.
    env_by_stage: Dict[str, EnvironmentParams] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Backfill ``recorded_at`` for sessions loaded from a pre-v1.3
        # JSON file (where the field didn't exist).
        if not self.recorded_at:
            self.recorded_at = self.ended_at or self.started_at or time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "env_params": self.env_params.to_dict(),
            "stage_counts": self.stage_counts,
            "quality_score": self.quality_score,
            "n_samples": self.n_samples,
            "notes": self.notes,
            "recorded_at": self.recorded_at,
            # v1.5.0 — serialise each per-stage snapshot.  We tolerate
            # an empty dict so the file shape stays valid for pre-v1.5
            # sessions reloaded from disk.
            "env_by_stage": {
                k: v.to_dict() for k, v in (self.env_by_stage or {}).items()
            },
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SleepSession":
        raw_eb = raw.get("env_by_stage") or {}
        env_by_stage: Dict[str, EnvironmentParams] = {}
        # Tolerate older sessions where env_by_stage is missing or
        # malformed — those sessions just don't contribute to the
        # per-stage learner.
        if isinstance(raw_eb, dict):
            for stage_name, env in raw_eb.items():
                if isinstance(env, dict):
                    env_by_stage[str(stage_name)] = EnvironmentParams.from_dict(env)
        return cls(
            session_id=str(raw["session_id"]),
            started_at=float(raw.get("started_at", 0)),
            ended_at=float(raw.get("ended_at", 0)),
            env_params=EnvironmentParams.from_dict(raw.get("env_params", {})),
            stage_counts=dict(raw.get("stage_counts", {})),
            quality_score=float(raw.get("quality_score", 0)),
            n_samples=int(raw.get("n_samples", 0)),
            notes=raw.get("notes"),
            recorded_at=float(raw.get("recorded_at", 0)),
            env_by_stage=env_by_stage,
        )


# ---------------------------------------------------------------------------
# Learner
# ---------------------------------------------------------------------------


@dataclass
class PreferenceConfig:
    enabled: bool = True
    history_path: str = "data/user_preferences.json"
    min_sessions_for_personalisation: int = 3
    quality_quantile: float = 0.7
    max_sessions_kept: int = 60
    exploration_rate: float = 0.1
    # v1.3.0 — decay + k-NN tunables.  Defaults match the engineering
    # constants at module top but stay overridable from add-on options.
    decay_half_life_days: float = _DEFAULT_HALF_LIFE_DAYS
    knn_k: int = 5
    knn_hour_sigma: float = _DEFAULT_HOUR_SIGMA
    knn_temp_sigma: float = _DEFAULT_TEMP_SIGMA

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "PreferenceConfig":
        valid = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in valid})


class PreferenceLearner:
    """Track sleep sessions and recommend personalised setpoints.

    Public surface:

    * :meth:`record_session` — append a new session and persist to disk.
    * :meth:`recommend` — return the best historical env setpoints, optionally
      perturbed by ``exploration_rate`` for active exploration.
    * :meth:`status` — quick summary string for logs / debugging.

    The class is intentionally stateless across the wire: every read reloads
    JSON, every write rewrites it.  That keeps it correct under concurrent
    access from multiple processes (e.g. the service + an ad-hoc CLI).
    """

    def __init__(
        self,
        config: PreferenceConfig,
        rng: Optional[random.Random] = None,
    ) -> None:
        self.config = config
        self._history_path = Path(config.history_path)
        self._rng = rng or random.Random()
        # In-memory cache; reloaded lazily.
        self._sessions: Optional[List[SleepSession]] = None

    # ------------------------------------------------------------------ #
    # Persistence                                                        #
    # ------------------------------------------------------------------ #

    def _load(self) -> List[SleepSession]:
        if self._sessions is not None:
            return self._sessions
        if not self._history_path.exists():
            # v1.8.0 — try the .bak file if the main file is missing.
            bak = self._history_path.with_suffix(
                self._history_path.suffix + ".bak",
            )
            if bak.exists():
                logger.warning(
                    "Main file %s missing; falling back to %s",
                    self._history_path, bak,
                )
                try:
                    with open(bak, "r", encoding="utf-8") as fh:
                        raw = json.load(fh)
                    items = raw.get("sessions", []) if isinstance(raw, dict) else raw
                    self._sessions = [SleepSession.from_dict(item) for item in items]
                    return self._sessions
                except (OSError, json.JSONDecodeError) as exc:
                    logger.warning("Backup also unreadable: %s — starting fresh", exc)
            self._sessions = []
            return self._sessions
        try:
            with open(self._history_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read %s: %s — trying backup",
                           self._history_path, exc)
            # v1.8.0 — fall back to .bak on read failure.
            bak = self._history_path.with_suffix(
                self._history_path.suffix + ".bak",
            )
            if bak.exists():
                try:
                    with open(bak, "r", encoding="utf-8") as fh:
                        raw = json.load(fh)
                    items = raw.get("sessions", []) if isinstance(raw, dict) else raw
                    self._sessions = [SleepSession.from_dict(item) for item in items]
                    logger.info("Recovered %d sessions from backup %s",
                                len(self._sessions), bak)
                    return self._sessions
                except (OSError, json.JSONDecodeError) as exc2:
                    logger.warning("Backup also unreadable: %s — starting fresh", exc2)
            self._sessions = []
            return self._sessions
        items = raw.get("sessions", []) if isinstance(raw, dict) else raw
        self._sessions = [SleepSession.from_dict(item) for item in items]
        return self._sessions

    def _save(self) -> None:
        assert self._sessions is not None
        # Cap history length (keep the most recent)
        kept = self._sessions[-self.config.max_sessions_kept :]
        self._sessions = kept

        self._history_path.parent.mkdir(parents=True, exist_ok=True)

        # v1.8.0 — rolling backup: copy current file to .bak before
        # overwriting.  Only one backup is kept.
        if self._history_path.exists():
            import shutil
            bak = self._history_path.with_suffix(
                self._history_path.suffix + ".bak",
            )
            try:
                shutil.copy2(self._history_path, bak)
            except OSError as exc:
                logger.warning("Failed to create backup %s: %s", bak, exc)

        payload = {
            "version": 1,
            "updated_at": time.time(),
            "sessions": [s.to_dict() for s in kept],
        }
        atomic_write_json(self._history_path, payload)

    # ------------------------------------------------------------------ #
    # Recording                                                          #
    # ------------------------------------------------------------------ #

    def record_session(self, session: SleepSession) -> None:
        sessions = self._load()
        sessions.append(session)
        self._save()
        logger.info(
            "Recorded session %s (quality=%.1f, samples=%d)",
            session.session_id, session.quality_score, session.n_samples,
        )

    # ------------------------------------------------------------------ #
    # Recommendation                                                     #
    # ------------------------------------------------------------------ #

    def sessions(self) -> List[SleepSession]:
        """Return a *defensive copy* of the recorded session history.

        Public alternative to the underscore-prefixed ``_load`` so other
        modules (sleep-debt accountant, scripts/notebooks) can read the
        ledger without depending on private internals.
        """
        return list(self._load())

    def n_sessions(self) -> int:
        return len(self._load())

    def status(self) -> str:
        n = self.n_sessions()
        if n == 0:
            return "no history yet — using defaults"
        scores = [s.quality_score for s in self._load()]
        return (
            f"{n} session(s); quality min={min(scores):.1f} "
            f"avg={sum(scores) / n:.1f} max={max(scores):.1f}"
        )

    # ------------------------------------------------------------------ #
    # Weighting helpers (v1.3.0)                                          #
    # ------------------------------------------------------------------ #

    def _decay_weight(self, session: SleepSession, now_ts: float) -> float:
        """Exponential-decay weight for one session.

        ``w(d) = 2 ** (-d / H)`` where ``d`` is age in days and ``H`` is
        the configured half-life.  We clamp negative ages (a session
        timestamped in the future after a clock jump) to zero so they
        still count as "today" instead of getting an unbounded boost.
        """
        h = max(self.config.decay_half_life_days, 0.5)
        age_days = max(0.0, (now_ts - session.recorded_at) / 86400.0)
        return 2.0 ** (-age_days / h)

    def _session_weights(
        self,
        sessions: Sequence[SleepSession],
        now_ts: Optional[float] = None,
    ) -> List[float]:
        """Combine decay × quality into a non-negative weight per session.

        Quality is rescaled from the 0-100 range into 0.1..1.1 so the
        worst night still contributes a little (otherwise a freak bad
        score would zero its decay weight and the learner would lose a
        whole day of data).
        """
        if now_ts is None:
            now_ts = time.time()
        out: List[float] = []
        for s in sessions:
            q = 0.1 + max(0.0, min(100.0, s.quality_score)) / 100.0
            out.append(self._decay_weight(s, now_ts) * q)
        return out

    def _top_sessions(
        self,
        now_ts: Optional[float] = None,
    ) -> List[SleepSession]:
        """Return sessions whose *decayed* quality lands in the top quantile.

        v1.3.0 change: thresholding now uses ``quality × decay`` rather
        than raw quality, so a great night from 3 months ago no longer
        outranks a merely-good night from yesterday.
        """
        sessions = self._load()
        if len(sessions) < self.config.min_sessions_for_personalisation:
            return []
        weights = self._session_weights(sessions, now_ts)
        ranked = sorted(zip(sessions, weights), key=lambda p: p[1])
        idx = int(self.config.quality_quantile * (len(ranked) - 1))
        threshold = ranked[idx][1]
        return [s for s, w in zip(sessions, weights) if w >= threshold]

    @staticmethod
    def _median(values: List[float]) -> Optional[float]:
        cleaned = [v for v in values if v is not None and not math.isnan(v)]
        if not cleaned:
            return None
        cleaned.sort()
        mid = len(cleaned) // 2
        if len(cleaned) % 2:
            return cleaned[mid]
        return 0.5 * (cleaned[mid - 1] + cleaned[mid])

    @staticmethod
    def _weighted_median(
        values: Sequence[Optional[float]],
        weights: Sequence[float],
    ) -> Optional[float]:
        """Return the weighted median of ``values`` (None entries dropped).

        Implementation: sort (value, weight) pairs, walk the cumulative
        weight until it crosses half of the total.  Falls back to the
        plain median when all weights collapse to zero.
        """
        # Keep every numeric value even if its weight is zero — we still
        # want to return *something* when all decay weights underflow.
        pairs = [
            (float(v), max(0.0, float(w))) for v, w in zip(values, weights)
            if v is not None and not math.isnan(float(v))
        ]
        if not pairs:
            return None
        pairs.sort(key=lambda p: p[0])
        total = sum(w for _, w in pairs)
        if total <= 0:
            return PreferenceLearner._median([v for v, _ in pairs])
        cum = 0.0
        for v, w in pairs:
            cum += w
            if cum >= total / 2.0:
                return v
        return pairs[-1][0]

    def recommend(
        self,
        defaults: EnvironmentParams,
        *,
        explore: bool = False,
        now_ts: Optional[float] = None,
    ) -> EnvironmentParams:
        """Return personalised setpoints, falling back to ``defaults``.

        Args:
            defaults: setpoints the caller wants used when there is not enough
                history (or when a particular field has no data).
            explore: if True, add small Gaussian noise (controlled by
                ``exploration_rate``) so the learner occasionally tries values
                outside the historical best window.
            now_ts: override "now" for the decay weight (used by tests).
        """
        top = self._top_sessions(now_ts=now_ts)
        if not top:
            logger.debug("PreferenceLearner: not enough history; using defaults")
            return defaults

        # Each surviving top-session still gets a per-field weighted vote
        # so recent good nights count more than old good nights within
        # the same top quantile.
        weights = self._session_weights(top, now_ts)
        rec = EnvironmentParams(
            temperature_c=self._weighted_median(
                [s.env_params.temperature_c for s in top], weights,
            ),
            humidity_pct=self._weighted_median(
                [s.env_params.humidity_pct for s in top], weights,
            ),
            brightness_pct=self._weighted_median(
                [s.env_params.brightness_pct for s in top], weights,
            ),
            fan_speed_pct=self._weighted_median(
                [s.env_params.fan_speed_pct for s in top], weights,
            ),
        )
        # Replace any unknown field with the caller's default.
        if rec.temperature_c is None:
            rec.temperature_c = defaults.temperature_c
        if rec.humidity_pct is None:
            rec.humidity_pct = defaults.humidity_pct
        if rec.brightness_pct is None:
            rec.brightness_pct = defaults.brightness_pct
        if rec.fan_speed_pct is None:
            rec.fan_speed_pct = defaults.fan_speed_pct

        if explore and self.config.exploration_rate > 0:
            scale = self.config.exploration_rate
            if rec.temperature_c is not None:
                rec.temperature_c += self._rng.gauss(0, 1.0 * scale)
            if rec.humidity_pct is not None:
                rec.humidity_pct += self._rng.gauss(0, 5.0 * scale)
            if rec.brightness_pct is not None:
                rec.brightness_pct = max(
                    0.0,
                    min(100.0, rec.brightness_pct + self._rng.gauss(0, 10.0 * scale)),
                )
            if rec.fan_speed_pct is not None:
                rec.fan_speed_pct = max(
                    0.0,
                    min(100.0, rec.fan_speed_pct + self._rng.gauss(0, 10.0 * scale)),
                )

        logger.debug(
            "PreferenceLearner: from %d top sessions → temp=%.1f hum=%.0f "
            "bright=%.0f fan=%s",
            len(top),
            rec.temperature_c or float("nan"),
            rec.humidity_pct or float("nan"),
            rec.brightness_pct or float("nan"),
            f"{rec.fan_speed_pct:.0f}" if rec.fan_speed_pct is not None else "—",
        )
        return rec

    # ------------------------------------------------------------------ #
    # Bedtime recommendation (v1.3.0)                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _bucket_of(ts: float) -> str:
        """Return ``"weekend"`` or ``"workday"`` for a bedtime timestamp.

        A bedtime occurring on Friday evening leads into Saturday's
        sleep, which the user mentally files under "weekend".  We
        therefore look at the *wake* day, approximated as ``ts + 6h``.
        """
        wake = datetime.fromtimestamp(ts + 6 * 3600)
        return "weekend" if wake.weekday() >= 5 else "workday"

    @staticmethod
    def _seconds_to_hhmm(seconds: float) -> str:
        """Format ``seconds since midnight`` as a zero-padded ``HH:MM``."""
        # Wrap around 24 h so e.g. -30 min → 23:30 (yesterday-late = today-early).
        s = int(seconds) % 86400
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}"

    def recommend_bedtime(
        self,
        now: Optional[datetime] = None,
        *,
        min_per_bucket: int = 3,
    ) -> Dict[str, Any]:
        """Suggest tonight's bedtime, separately for workday vs weekend.

        Methodology:

        1. Bucket every session into ``weekend`` or ``workday`` based on
           the day the user *woke up* (see :meth:`_bucket_of`).
        2. Within each bucket, compute the decay-weighted median of
           ``started_at`` translated to seconds-since-midnight.
        3. Return both buckets plus a ``next_bedtime`` aimed at the
           bucket *tonight* lands in (i.e. if today is Friday → weekend
           bedtime).

        Empty buckets return ``None`` rather than guessing — the
        caller (a Lovelace card) should hide that field instead of
        showing a misleading "00:00".
        """
        if now is None:
            now = now_local()

        sessions = self._load()
        now_ts = now.timestamp()
        per_bucket: Dict[str, List[Tuple[float, float]]] = {
            "weekend": [], "workday": [],
        }
        for s in sessions:
            if not s.started_at:
                continue
            local_started = datetime.fromtimestamp(s.started_at)
            # Seconds since midnight; if bedtime is past 18:00 we shift by
            # -86400 so a 23:30 night clusters next to a 00:30 one.
            sec = (
                local_started.hour * 3600
                + local_started.minute * 60
                + local_started.second
            )
            if sec >= 18 * 3600:
                sec -= 86400
            bucket = self._bucket_of(s.started_at)
            w = self._decay_weight(s, now_ts) * (
                0.1 + max(0.0, min(100.0, s.quality_score)) / 100.0
            )
            per_bucket[bucket].append((float(sec), float(w)))

        def _bucket_median(samples: List[Tuple[float, float]]) -> Optional[str]:
            if len(samples) < min_per_bucket:
                return None
            secs = [v for v, _ in samples]
            wts = [w for _, w in samples]
            m = self._weighted_median(secs, wts)
            return None if m is None else self._seconds_to_hhmm(m)

        weekday_bedtime = _bucket_median(per_bucket["workday"])
        weekend_bedtime = _bucket_median(per_bucket["weekend"])
        tonight_bucket = self._bucket_of(now_ts)
        next_bedtime = (
            weekend_bedtime if tonight_bucket == "weekend" else weekday_bedtime
        )

        return {
            "weekday_bedtime": weekday_bedtime,
            "weekend_bedtime": weekend_bedtime,
            "next_bedtime": next_bedtime,
            "tonight_bucket": tonight_bucket,
            "n_workday": len(per_bucket["workday"]),
            "n_weekend": len(per_bucket["weekend"]),
            "confidence": min(
                1.0, (len(per_bucket["workday"]) + len(per_bucket["weekend"])) / 14.0,
            ),
        }

    # ------------------------------------------------------------------ #
    # k-NN environment recommendation (v1.3.0)                            #
    # ------------------------------------------------------------------ #

    def _knn_weights(
        self,
        sessions: Sequence[SleepSession],
        *,
        now_ts: float,
        target_hour: Optional[float],
        target_temp_c: Optional[float],
    ) -> List[float]:
        """Per-session kernel weight for k-NN.

        Combines the standard decay × quality weight with a Gaussian
        kernel on bedtime-hour and current ambient temperature.  Any
        field whose target is ``None`` is silently skipped (so a
        cold-start with no ambient reading degrades to plain decay).
        """
        hour_sigma = max(self.config.knn_hour_sigma, 0.1)
        temp_sigma = max(self.config.knn_temp_sigma, 0.1)
        weights: List[float] = []
        for s in sessions:
            base = self._decay_weight(s, now_ts) * (
                0.1 + max(0.0, min(100.0, s.quality_score)) / 100.0
            )
            kernel = 1.0
            if target_hour is not None and s.started_at:
                d = datetime.fromtimestamp(s.started_at)
                h = d.hour + d.minute / 60.0
                # Wrap so 23:30 vs 00:30 = 1 h apart, not 23 h.
                diff = (h - target_hour) % 24
                diff = min(diff, 24 - diff)
                kernel *= math.exp(-(diff * diff) / (2 * hour_sigma * hour_sigma))
            if target_temp_c is not None and s.env_params.temperature_c is not None:
                d = s.env_params.temperature_c - target_temp_c
                kernel *= math.exp(-(d * d) / (2 * temp_sigma * temp_sigma))
            weights.append(base * kernel)
        return weights

    def recommend_knn(
        self,
        defaults: EnvironmentParams,
        *,
        now: Optional[datetime] = None,
        current_temp_c: Optional[float] = None,
        k: Optional[int] = None,
    ) -> Dict[str, Any]:
        """k-NN-flavoured recommendation conditioned on tonight's context.

        Picks the ``k`` past sessions most similar to *tonight* — same
        bedtime hour, same ambient temperature — and returns the
        weighted-median of their env params.  The neighbour list is
        echoed back so a Lovelace explainability card can show *which*
        nights the recommendation is based on.

        Returns a dict so the publisher can write the entire payload
        verbatim into the attribute panel without a second pass.
        """
        if now is None:
            now = now_local()
        if k is None:
            k = self.config.knn_k

        sessions = self._load()
        if len(sessions) < self.config.min_sessions_for_personalisation:
            return {
                "env": defaults,
                "neighbors": [],
                "n_used": 0,
                "confidence": 0.0,
            }

        target_hour: Optional[float] = now.hour + now.minute / 60.0
        weights = self._knn_weights(
            sessions,
            now_ts=now.timestamp(),
            target_hour=target_hour,
            target_temp_c=current_temp_c,
        )
        # Pick the top-k by weight.
        ranked = sorted(
            zip(sessions, weights), key=lambda p: p[1], reverse=True,
        )[: max(1, k)]
        top_sessions = [s for s, _ in ranked]
        top_weights = [w for _, w in ranked]

        env = EnvironmentParams(
            temperature_c=self._weighted_median(
                [s.env_params.temperature_c for s in top_sessions], top_weights,
            ),
            humidity_pct=self._weighted_median(
                [s.env_params.humidity_pct for s in top_sessions], top_weights,
            ),
            brightness_pct=self._weighted_median(
                [s.env_params.brightness_pct for s in top_sessions], top_weights,
            ),
            fan_speed_pct=self._weighted_median(
                [s.env_params.fan_speed_pct for s in top_sessions], top_weights,
            ),
        )
        # Fallback to defaults per field.
        if env.temperature_c is None:
            env.temperature_c = defaults.temperature_c
        if env.humidity_pct is None:
            env.humidity_pct = defaults.humidity_pct
        if env.brightness_pct is None:
            env.brightness_pct = defaults.brightness_pct
        if env.fan_speed_pct is None:
            env.fan_speed_pct = defaults.fan_speed_pct

        # Confidence = 0 when no neighbour has any signal, 1 when all
        # k neighbours contribute close to their full base weight.
        total_w = sum(top_weights)
        confidence = min(1.0, total_w / max(1, len(top_sessions)))

        return {
            "env": env,
            "neighbors": [
                {
                    "session_id": s.session_id,
                    "weight": round(w, 4),
                    "quality": round(s.quality_score, 1),
                    "started_at": s.started_at,
                }
                for s, w in zip(top_sessions, top_weights)
            ],
            "n_used": len(top_sessions),
            "confidence": confidence,
        }

    # ------------------------------------------------------------------ #
    # Per-stage deltas (v1.5.0)                                           #
    # ------------------------------------------------------------------ #

    # Minimum effective sample size before we trust a learned delta.
    # ESS = (Σw)² / Σ(w²); with all weights = 1 this collapses to n.
    # Below this we fall back to the clinical default so a noisy
    # learner doesn't keep the bedroom too dim/cold during DEEP.
    _MIN_ESS_FOR_DELTA: float = 4.0

    # The reference stage that all deltas are computed *relative to*.
    # LIGHT is the natural baseline because it's the most populated
    # stage in any session (most users spend ~50 % of the night there).
    _DELTA_BASELINE: str = "LIGHT"

    def recommend_per_stage_deltas(
        self,
        now: Optional[float] = None,
    ) -> Dict[str, Dict[str, Optional[float]]]:
        """Return learned env *deltas* per stage relative to LIGHT.

        Output shape::

            {
              "AWAKE": {"temperature_c": +2.1, "humidity_pct": None,
                        "brightness_pct": +18.0, "fan_speed_pct": None,
                        "ess": 7.3, "n_sessions": 9},
              "LIGHT": {... all zeros, baseline ...},
              "DEEP":  {"temperature_c": -1.8, ...},
              "REM":   {"temperature_c": -1.5, ...},
            }

        A field is ``None`` when the effective sample size (ESS) is
        below ``_MIN_ESS_FOR_DELTA`` for that stage — the caller
        should fall back to the clinical default in
        :mod:`smart_environment_controller`.

        Algorithm:

        1. Take every session with a non-empty ``env_by_stage``.
        2. For each session that has both the stage *and* the LIGHT
           baseline recorded, compute the per-field delta
           ``env[stage] - env[LIGHT]``.
        3. Weight each session by the existing exponential time decay
           (``_session_weights``) so old / stale preferences fade.
        4. The reported delta is the **weighted median** of those
           per-session deltas — same robust estimator as
           ``recommend_knn`` so a single anomalous night can't move
           the learned delta.
        5. Report the effective sample size alongside each stage so
           the controller and the explainability sensor can decide
           whether to trust it.

        Why deltas rather than absolute targets:
            Anchoring on LIGHT means the user's *baseline* (a hot
            sleeper at 24 °C vs a cold one at 18 °C) is captured by
            the LIGHT k-NN already, and the per-stage method only has
            to learn the *shape* of the night (how much cooler DEEP
            wants to be).  This is a much lower-dimensional learning
            problem that hits ESS ≥ 4 in ~1-2 weeks of nightly use
            instead of months.
        """
        sessions = [
            s for s in self._load() if s.env_by_stage and self._DELTA_BASELINE in s.env_by_stage
        ]
        now_ts = float(now) if now is not None else time.time()
        weights = self._session_weights(sessions, now_ts)

        result: Dict[str, Dict[str, Optional[float]]] = {}
        for stage_name in ("AWAKE", "LIGHT", "DEEP", "REM"):
            # The baseline stage's delta is, by definition, zero.
            if stage_name == self._DELTA_BASELINE:
                result[stage_name] = {
                    "temperature_c": 0.0,
                    "humidity_pct": 0.0,
                    "brightness_pct": 0.0,
                    "fan_speed_pct": 0.0,
                    "ess": float(len(sessions)),
                    "n_sessions": len(sessions),
                }
                continue

            # Filter to sessions that have BOTH this stage and LIGHT.
            stage_deltas: Dict[str, List[float]] = {
                "temperature_c": [],
                "humidity_pct": [],
                "brightness_pct": [],
                "fan_speed_pct": [],
            }
            stage_weights: List[float] = []
            for sess, w in zip(sessions, weights):
                if stage_name not in sess.env_by_stage:
                    continue
                base_env = sess.env_by_stage[self._DELTA_BASELINE]
                this_env = sess.env_by_stage[stage_name]
                # Compute delta per field; only contribute fields
                # where *both* the baseline and this stage have a
                # numeric reading.
                appended = False
                for field_name in stage_deltas:
                    bv = getattr(base_env, field_name)
                    tv = getattr(this_env, field_name)
                    if bv is None or tv is None:
                        continue
                    stage_deltas[field_name].append(tv - bv)
                    appended = True
                if appended:
                    stage_weights.append(w)

            ess = self._effective_sample_size(stage_weights)
            entry: Dict[str, Optional[float]] = {
                "ess": ess,
                "n_sessions": len(stage_weights),
            }
            for field_name, deltas in stage_deltas.items():
                if ess < self._MIN_ESS_FOR_DELTA or not deltas:
                    entry[field_name] = None
                    continue
                # Pad weights to match the deltas-per-field cardinality.
                # In practice every session with both stages snapshotted
                # contributes to every field with valid bv/tv, so
                # ``len(deltas) == len(stage_weights)`` holds.  Defensive
                # truncation if a future schema change breaks the
                # invariant.
                wts = stage_weights[: len(deltas)]
                entry[field_name] = self._weighted_median(deltas, wts)
            result[stage_name] = entry

        return result

    @staticmethod
    def _effective_sample_size(weights: Sequence[float]) -> float:
        """Kish's effective sample size: (Σw)² / Σ(w²).

        ESS = n when all weights are equal; ESS = 1 when a single
        weight dominates.  We use it as the trust threshold instead of
        the raw session count so a learner with 30 sessions but
        crushed by exponential decay still reports a realistic count.
        """
        sw = sum(weights)
        sw2 = sum(w * w for w in weights)
        if sw2 <= 0.0:
            return 0.0
        return (sw * sw) / sw2

    # ------------------------------------------------------------------ #
    # Explainability panel (v1.3.0)                                       #
    # ------------------------------------------------------------------ #

    def explain(
        self,
        defaults: EnvironmentParams,
        *,
        now: Optional[datetime] = None,
        current_temp_c: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Return a JSON-serialisable explanation of the current rec.

        Designed to be set as the ``attributes`` of an HA sensor entity
        (``sensor.sleep_recommendation_explanation``) so the user can
        open the More-Info card and see exactly why we picked tonight's
        setpoints.  The payload is intentionally small — Lovelace
        truncates attribute lists past ~16 KB.
        """
        if now is None:
            now = now_local()
        now_ts = now.timestamp()
        sessions = self._load()
        n_total = len(sessions)

        if n_total < self.config.min_sessions_for_personalisation:
            return {
                "ready": False,
                "reason": (
                    f"need {self.config.min_sessions_for_personalisation} "
                    f"sessions, have {n_total}"
                ),
                "n_total": n_total,
                "recommendation": defaults.to_dict(),
            }

        knn = self.recommend_knn(
            defaults, now=now, current_temp_c=current_temp_c,
        )
        bedtime = self.recommend_bedtime(now=now)
        weights = self._session_weights(sessions, now_ts)
        avg_age_d = (
            sum(now_ts - s.recorded_at for s in sessions) / n_total / 86400.0
        )

        return {
            "ready": True,
            "method": "knn+decay",
            "n_total": n_total,
            "avg_age_days": round(avg_age_d, 1),
            "decay_half_life_days": self.config.decay_half_life_days,
            "effective_sample_size": round(sum(weights), 2),
            "recommendation": knn["env"].to_dict(),
            "neighbors": knn["neighbors"],
            "bedtime": bedtime,
            "confidence": knn["confidence"],
        }


__all__ = [
    "EnvironmentParams",
    "SleepSession",
    "PreferenceConfig",
    "PreferenceLearner",
    "compute_quality_score",
    "stage_counts_from_sequence",
]
