"""Learn the user's preferred sleep environment from history.

Every night, the smart service records a *sleep session* — a triple of

* environment parameters that were active during the session
  (temperature, humidity, brightness, fan speed),
* the sleep-stage time-series produced by the CNN-BiLSTM model,
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
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from src.data_structures import SleepStage

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
    """One night (or one nap) of accumulated data."""

    session_id: str
    started_at: float                  # unix timestamp
    ended_at: float
    env_params: EnvironmentParams
    stage_counts: Dict[str, int]
    quality_score: float
    n_samples: int = 0
    notes: Optional[str] = None

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
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SleepSession":
        return cls(
            session_id=str(raw["session_id"]),
            started_at=float(raw.get("started_at", 0)),
            ended_at=float(raw.get("ended_at", 0)),
            env_params=EnvironmentParams.from_dict(raw.get("env_params", {})),
            stage_counts=dict(raw.get("stage_counts", {})),
            quality_score=float(raw.get("quality_score", 0)),
            n_samples=int(raw.get("n_samples", 0)),
            notes=raw.get("notes"),
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
            self._sessions = []
            return self._sessions
        try:
            with open(self._history_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read %s: %s — starting fresh",
                           self._history_path, exc)
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
        payload = {
            "version": 1,
            "updated_at": time.time(),
            "sessions": [s.to_dict() for s in kept],
        }
        # Atomic write: dump to a temp file then rename.
        tmp = self._history_path.with_suffix(self._history_path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self._history_path)

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

    def _top_sessions(self) -> List[SleepSession]:
        sessions = self._load()
        if len(sessions) < self.config.min_sessions_for_personalisation:
            return []
        scores = sorted(s.quality_score for s in sessions)
        idx = int(self.config.quality_quantile * (len(scores) - 1))
        threshold = scores[idx]
        return [s for s in sessions if s.quality_score >= threshold]

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

    def recommend(
        self,
        defaults: EnvironmentParams,
        *,
        explore: bool = False,
    ) -> EnvironmentParams:
        """Return personalised setpoints, falling back to ``defaults``.

        Args:
            defaults: setpoints the caller wants used when there is not enough
                history (or when a particular field has no data).
            explore: if True, add small Gaussian noise (controlled by
                ``exploration_rate``) so the learner occasionally tries values
                outside the historical best window.
        """
        top = self._top_sessions()
        if not top:
            logger.debug("PreferenceLearner: not enough history; using defaults")
            return defaults

        rec = EnvironmentParams(
            temperature_c=self._median([s.env_params.temperature_c for s in top]),
            humidity_pct=self._median([s.env_params.humidity_pct for s in top]),
            brightness_pct=self._median([s.env_params.brightness_pct for s in top]),
            fan_speed_pct=self._median([s.env_params.fan_speed_pct for s in top]),
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


__all__ = [
    "EnvironmentParams",
    "SleepSession",
    "PreferenceConfig",
    "PreferenceLearner",
    "compute_quality_score",
    "stage_counts_from_sequence",
]
