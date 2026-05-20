"""Publish sleep diagnostics back to Home Assistant as virtual entities.

Why this module exists
----------------------
The add-on used to print ``infer stage=DEEP conf=0.91`` only to its own log.
Users on Lovelace dashboards therefore had no way to *see* what the model
thought at any given moment, much less plot it over time or trigger
automations on a stage transition.

This publisher solves that by writing a handful of entities directly to
HA's state machine via ``POST /api/states/<entity_id>``.  After the first
publish, HA treats them like any other sensor — they appear under
**Developer Tools → States**, can be put on Lovelace cards, and can be
referenced from automations:

    sensor.sleep_classifier_stage              # AWAKE / LIGHT / DEEP / REM
    sensor.sleep_classifier_confidence         # 0.00 .. 1.00
    sensor.sleep_classifier_quality_score      # last preference-learner score
    sensor.sleep_classifier_session_duration   # seconds since current session start

The state name format ``sensor.sleep_classifier_*`` is chosen so all four
sort together and clearly attribute themselves to this add-on.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.data_structures import SleepStage
from src.ha_api_client import HomeAssistantClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Friendly-name constants — keep in sync with DOCS.md / Lovelace examples.
# ---------------------------------------------------------------------------
ENTITY_STAGE = "sensor.sleep_classifier_stage"
ENTITY_CONFIDENCE = "sensor.sleep_classifier_confidence"
ENTITY_QUALITY = "sensor.sleep_classifier_quality_score"
ENTITY_DURATION = "sensor.sleep_classifier_session_duration"
ENTITY_LAST_ACTION = "sensor.sleep_classifier_last_action"

# v1.2.0 — natural-sleep entities
ENTITY_DEBT = "sensor.sleep_classifier_debt_hours"
ENTITY_RECOMMENDED_BEDTIME = "sensor.sleep_classifier_recommended_bedtime"
ENTITY_WAKE_DECISION = "sensor.sleep_classifier_wake_decision"
ENTITY_SOUNDSCAPE = "sensor.sleep_classifier_soundscape"

# v1.3.0 — preference-learning entities.  Four sensors that mirror the
# PreferenceLearner's public API so Lovelace can render the panel without
# any template helpers on the user's side.
ENTITY_LEARNED_BEDTIME_WORKDAY = "sensor.sleep_classifier_learned_bedtime_workday"
ENTITY_LEARNED_BEDTIME_WEEKEND = "sensor.sleep_classifier_learned_bedtime_weekend"
ENTITY_LEARNED_ENVIRONMENT = "sensor.sleep_classifier_learned_environment"
ENTITY_RECOMMENDATION_EXPLAIN = "sensor.sleep_classifier_recommendation_explain"

# v1.5.0 — surfaces the learned vs clinical per-stage deltas so the
# user can see *why* the controller is now targeting e.g. 19.5 °C
# during their DEEP stage instead of 19.0 °C.
ENTITY_PER_STAGE_DELTAS = "sensor.sleep_classifier_per_stage_deltas"

# v1.7.0 — apnea/hypopnea trend sensor.
#
# IMPORTANT — this sensor INTENTIONALLY does not publish a numeric AHI.
# AHI is a clinical metric; surfacing "AHI = 12" would be read as a
# diagnosis.  Instead the state is one of:
#   pending_consent   - user has not toggled input_boolean consent
#   calibrating       - still collecting the first 7 nights of baseline
#   green             - events/hour below AASM mild threshold
#   amber             - AASM mild-to-moderate OSA bucket
#   red               - AASM moderate-or-worse bucket
# See docs/BACKLOG.md "Sleep apnea detector" section for the
# medical-disclaimer rationale.
ENTITY_APNEA_INDEX = "sensor.sleep_classifier_apnea_index"

# v1.8.0 — aggregated health status sensor.
ENTITY_HEALTH = "sensor.sleep_classifier_health"

# v1.8.0 — quality sub-score sensors (architecture / efficiency /
# fragmentation / onset).  Each is 0-100, state_class=measurement.
ENTITY_QUALITY_ARCHITECTURE = "sensor.sleep_classifier_quality_architecture"
ENTITY_QUALITY_EFFICIENCY = "sensor.sleep_classifier_quality_efficiency"
ENTITY_QUALITY_FRAGMENTATION = "sensor.sleep_classifier_quality_fragmentation"
ENTITY_QUALITY_ONSET = "sensor.sleep_classifier_quality_onset"

# ``icon: mdi:...`` strings render in Lovelace.  ``state_class`` and
# ``device_class`` enable HA's long-term statistics (so the user gets a
# free trend graph without writing a SQL query).
_STATIC_ATTRS_STAGE = {
    "friendly_name": "Sleep stage",
    "icon": "mdi:sleep",
    # Enum-typed sensors get nice colour-coded chips on Lovelace.
    "device_class": "enum",
    "options": [s.name for s in SleepStage],
}
_STATIC_ATTRS_CONF = {
    "friendly_name": "Sleep classifier confidence",
    "icon": "mdi:gauge",
    "unit_of_measurement": "%",
    "state_class": "measurement",
}
_STATIC_ATTRS_QUALITY = {
    "friendly_name": "Last sleep quality score",
    "icon": "mdi:star-circle",
    "unit_of_measurement": "score",
    "state_class": "measurement",
}
_STATIC_ATTRS_DURATION = {
    "friendly_name": "Sleep session duration",
    "icon": "mdi:timer-sand",
    "unit_of_measurement": "s",
    "device_class": "duration",
    "state_class": "measurement",
}
_STATIC_ATTRS_LAST_ACTION = {
    "friendly_name": "Last sleep automation action",
    "icon": "mdi:robot",
}
_STATIC_ATTRS_DEBT = {
    "friendly_name": "Sleep debt",
    "icon": "mdi:bank-minus",
    "unit_of_measurement": "h",
    "state_class": "measurement",
}
_STATIC_ATTRS_RECOMMENDED_BEDTIME = {
    "friendly_name": "Recommended bedtime tonight",
    "icon": "mdi:bed-clock",
    "device_class": "timestamp",
}
_STATIC_ATTRS_WAKE_DECISION = {
    "friendly_name": "Smart wake decision",
    "icon": "mdi:alarm",
    "device_class": "enum",
    "options": ["hold", "pre_ramp", "open_window", "fire_now", "post_wake"],
}
_STATIC_ATTRS_SOUNDSCAPE = {
    "friendly_name": "Current soundscape",
    "icon": "mdi:weather-rainy",
    "device_class": "enum",
    # 7 non-off soundscapes + "off".  Keep list in sync with
    # :class:`src.whitenoise_matcher.Soundscape`.
    "options": [
        "off", "pink_noise", "brown_noise", "white_noise",
        "rain", "wind", "ocean", "dawn_chorus",
    ],
}

# v1.3.0 — learning-related entities.  ``state`` carries the headline
# value (a "HH:MM" string or "ready" / "not_ready"); the rich data lives
# in attributes so Lovelace's More-Info dialog can render it.
_STATIC_ATTRS_LEARNED_BEDTIME_WORKDAY = {
    "friendly_name": "Learned workday bedtime",
    "icon": "mdi:briefcase-clock",
}
_STATIC_ATTRS_LEARNED_BEDTIME_WEEKEND = {
    "friendly_name": "Learned weekend bedtime",
    "icon": "mdi:weekend",
}
_STATIC_ATTRS_LEARNED_ENVIRONMENT = {
    "friendly_name": "Learned best sleep environment",
    "icon": "mdi:home-thermometer",
}
_STATIC_ATTRS_RECOMMENDATION_EXPLAIN = {
    "friendly_name": "Recommendation explanation",
    "icon": "mdi:lightbulb-on",
    # device_class=enum gives Lovelace a chip-style rendering for
    # ``ready`` / ``not_ready`` without needing a custom card.
    "device_class": "enum",
    "options": ["ready", "not_ready"],
}
_STATIC_ATTRS_PER_STAGE_DELTAS = {
    "friendly_name": "Learned per-stage env deltas",
    "icon": "mdi:chart-bell-curve",
    # ``learning`` while the learner is still collecting evidence,
    # ``personalised`` once at least one stage has crossed the ESS
    # threshold, ``clinical`` when no learned override is active.
    "device_class": "enum",
    "options": ["clinical", "learning", "personalised"],
}

_STATIC_ATTRS_APNEA_INDEX = {
    "friendly_name": "Apnea / hypopnea trend",
    "icon": "mdi:lungs",
    "device_class": "enum",
    "options": [
        "pending_consent", "calibrating", "green", "amber", "red",
    ],
    # Mirrored into the attributes panel so every Lovelace view of
    # this entity carries the disclaimer.  Deliberately terse so
    # it fits HA's 255-char attribute-value limit on older builds.
    "disclaimer": (
        "This is a TREND indicator, not a medical diagnosis. "
        "Consult a sleep clinician for a real AHI."
    ),
}

# v1.8.0 — health status sensor attributes.
_STATIC_ATTRS_HEALTH = {
    "friendly_name": "Sleep classifier health",
    "icon": "mdi:heart-pulse",
    "device_class": "enum",
    "options": ["healthy", "degraded", "error"],
}

# v1.8.0 — quality sub-score sensor attributes.
_STATIC_ATTRS_QUALITY_ARCHITECTURE = {
    "friendly_name": "Quality: architecture",
    "icon": "mdi:chart-bar",
    "unit_of_measurement": "score",
    "state_class": "measurement",
}
_STATIC_ATTRS_QUALITY_EFFICIENCY = {
    "friendly_name": "Quality: efficiency",
    "icon": "mdi:speedometer",
    "unit_of_measurement": "score",
    "state_class": "measurement",
}
_STATIC_ATTRS_QUALITY_FRAGMENTATION = {
    "friendly_name": "Quality: fragmentation",
    "icon": "mdi:chart-scatter-plot",
    "unit_of_measurement": "score",
    "state_class": "measurement",
}
_STATIC_ATTRS_QUALITY_ONSET = {
    "friendly_name": "Quality: onset",
    "icon": "mdi:clock-start",
    "unit_of_measurement": "score",
    "state_class": "measurement",
}


# ---------------------------------------------------------------------------
# v3.0.0 — algorithmic moat sensors（PR2 兼容契约：仅追加，绝不修改既有
# entity_id）。表与 ``.kiro/specs/algorithmic-moat-v3.0.0/design.md`` §3.5
# 逐字对齐；任一新模块停用时仍然发布对应 sensor 但 state = ``"disabled"``，
# 保证 Lovelace 一致渲染（design §3.5 / §6.3）。
# ---------------------------------------------------------------------------
ENTITY_OPTIMIZER_HEALTH = "sensor.sleep_classifier_optimizer_health"
ENTITY_OPTIMIZER_STATUS = "sensor.sleep_classifier_optimizer_status"
ENTITY_OPTIMIZER_UNCERTAINTY = "sensor.sleep_classifier_optimizer_uncertainty"
ENTITY_DECISION_MODE = "sensor.sleep_classifier_decision_mode"
ENTITY_LOCKED_DIMENSIONS = "sensor.sleep_classifier_locked_dimensions"
ENTITY_QUALITY_TREND_14D = "sensor.sleep_classifier_quality_trend_14d"
ENTITY_ATTRIBUTION = "sensor.sleep_classifier_attribution"
ENTITY_ATTRIBUTION_FULL = "sensor.sleep_classifier_attribution_full"
ENTITY_PRIOR_STATUS = "sensor.sleep_classifier_prior_status"
ENTITY_PRIOR_WEIGHT = "sensor.sleep_classifier_prior_weight"
ENTITY_PREDICTOR_HEALTH = "sensor.sleep_classifier_predictor_health"
ENTITY_PREDICTOR_STATUS = "sensor.sleep_classifier_predictor_status"
ENTITY_PREDICTOR_HIT_RATE_7D = "sensor.sleep_classifier_predictor_hit_rate_7d"
ENTITY_V3_HEALTH_SUMMARY = "sensor.sleep_classifier_v3_health_summary"

# HA Core ``state`` 字段长度上限（design §3.5 契约）。超长内容仅放 attribute。
_HA_STATE_MAX_LEN = 255

_STATIC_ATTRS_OPTIMIZER_HEALTH = {
    "friendly_name": "Optimizer health (BAO)",
    "icon": "mdi:heart-pulse",
    "device_class": "enum",
    "options": ["healthy", "degraded", "disabled"],
}
_STATIC_ATTRS_OPTIMIZER_STATUS = {
    "friendly_name": "Optimizer status (BAO)",
    "icon": "mdi:trending-up",
    "device_class": "enum",
    # ``disabled`` 加进 options 是为了保持模块停用时也能在 Lovelace 上以
    # enum chip 形式渲染，与 design §3.5 「state = disabled」契约一致。
    "options": ["learning", "converging", "converged", "disabled"],
}
_STATIC_ATTRS_OPTIMIZER_UNCERTAINTY = {
    "friendly_name": "Optimizer posterior σ_T (°C)",
    "icon": "mdi:gauge-low",
    "unit_of_measurement": "°C",
    "state_class": "measurement",
}
_STATIC_ATTRS_DECISION_MODE = {
    "friendly_name": "BAO decision mode",
    "icon": "mdi:robot-confused",
    "device_class": "enum",
    "options": [
        "exploit", "explore-temp", "explore-humidity",
        "explore-brightness", "prior-only", "disabled",
    ],
}
_STATIC_ATTRS_LOCKED_DIMENSIONS = {
    "friendly_name": "BAO locked dimensions",
    "icon": "mdi:lock",
}
_STATIC_ATTRS_QUALITY_TREND_14D = {
    "friendly_name": "Quality trend (14-night slope)",
    "icon": "mdi:chart-line",
    "unit_of_measurement": "score/d",
    "state_class": "measurement",
}
_STATIC_ATTRS_ATTRIBUTION = {
    "friendly_name": "Causal attribution",
    "icon": "mdi:lightbulb-on-outline",
    # ``disabled`` 加进 options 是为了 design §3.5 「state = disabled」契约。
    "device_class": "enum",
    "options": [
        "ok", "nominal", "insufficient_data", "timeout", "disabled",
    ],
}
_STATIC_ATTRS_ATTRIBUTION_FULL = {
    "friendly_name": "Causal attribution (full effects)",
    "icon": "mdi:graph-outline",
    "device_class": "enum",
    "options": ["ok", "disabled"],
}
_STATIC_ATTRS_PRIOR_STATUS = {
    "friendly_name": "Population prior status",
    "icon": "mdi:database-check",
    "device_class": "enum",
    "options": ["loaded", "fallback", "unavailable", "disabled"],
}
_STATIC_ATTRS_PRIOR_WEIGHT = {
    "friendly_name": "Prior weight α",
    "icon": "mdi:weight",
    "state_class": "measurement",
}
_STATIC_ATTRS_PREDICTOR_HEALTH = {
    "friendly_name": "Predictor health (EMST)",
    "icon": "mdi:heart-pulse",
    "device_class": "enum",
    "options": ["healthy", "degraded", "disabled"],
}
_STATIC_ATTRS_PREDICTOR_STATUS = {
    "friendly_name": "Predictor status (EMST)",
    "icon": "mdi:radar",
    "device_class": "enum",
    "options": ["active", "auto_disabled", "disabled"],
}
_STATIC_ATTRS_PREDICTOR_HIT_RATE_7D = {
    "friendly_name": "Predictor 7-day hit rate",
    "icon": "mdi:target",
    "unit_of_measurement": "%",
    "state_class": "measurement",
}
_STATIC_ATTRS_V3_HEALTH_SUMMARY = {
    "friendly_name": "v3 algorithmic moat health",
    "icon": "mdi:shield-check",
    "device_class": "enum",
    # ``disabled`` 表示 4 个 flag 全部关闭（v2.1.0 等价模式，design §6.3）。
    "options": ["green", "amber", "red", "disabled"],
}


@dataclass
class PublisherStats:
    """Bookkeeping so we don't spam HA with redundant POSTs."""
    last_stage: Optional[str] = None
    last_conf: Optional[float] = None
    publishes: int = 0
    failures: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)


class SleepStatePublisher:
    """Best-effort writer for diagnostic state entities.

    Failures are logged but never raised — losing one HA write must not
    take down the inference loop.  HA gracefully de-dupes identical
    consecutive states on its side, but we also short-circuit obvious
    no-ops here to save HTTP round-trips.
    """

    def __init__(
        self,
        ha_client: HomeAssistantClient,
        *,
        confidence_deadband: float = 0.05,
    ) -> None:
        self._ha = ha_client
        self._deadband = float(confidence_deadband)
        self.stats = PublisherStats()
        # v3.0.0 — 4 个算法护城河模块的引用；编排层（Task 8.1）通过
        # :meth:`set_v3_modules` 注入。停用 / 未注入时 ``_publish_v3_sensors``
        # 仍会发布对应 sensor，但 state = ``"disabled"``，保证 Lovelace 渲染
        # 一致（design §3.5 / §6.3）。
        self._v3_modules_loaded: bool = False
        self._v3_bao: Any = None
        self._v3_cae_engine: Any = None
        self._v3_prior_repo: Any = None
        self._v3_predictor: Any = None

    async def publish_stage(
        self,
        stage: SleepStage,
        confidence: float,
        *,
        env_temperature_c: Optional[float] = None,
        env_humidity_pct: Optional[float] = None,
        env_brightness_pct: Optional[float] = None,
    ) -> None:
        """Push the latest stage + confidence to HA.

        Skips the round-trip when neither the stage label nor the
        confidence (within ``confidence_deadband``) has changed.
        """
        stage_name = stage.name
        conf_pct = round(float(confidence) * 100.0, 1)
        # Stage is a discrete label — always publish on change.
        # Confidence is continuous — only publish on meaningful change.
        prev_stage = self.stats.last_stage
        prev_conf = self.stats.last_conf
        stage_changed = stage_name != prev_stage
        conf_changed = (
            prev_conf is None
            or abs(prev_conf - conf_pct) >= self._deadband * 100.0
        )
        if not stage_changed and not conf_changed:
            return

        # Bundle the latest environment readings into the stage entity's
        # attributes so a single Lovelace card can show "DEEP @ 22 °C".
        attrs: Dict[str, Any] = dict(_STATIC_ATTRS_STAGE)
        if env_temperature_c is not None:
            attrs["temperature_c"] = round(float(env_temperature_c), 1)
        if env_humidity_pct is not None:
            attrs["humidity_pct"] = round(float(env_humidity_pct), 1)
        if env_brightness_pct is not None:
            attrs["brightness_pct"] = round(float(env_brightness_pct), 1)
        attrs["confidence_pct"] = conf_pct

        await self._safe_update(ENTITY_STAGE, stage_name, attrs)
        await self._safe_update(
            ENTITY_CONFIDENCE, conf_pct, _STATIC_ATTRS_CONF,
        )

        self.stats.last_stage = stage_name
        self.stats.last_conf = conf_pct

    async def publish_quality(self, score: float) -> None:
        """Reflect the latest preference-learner quality score."""
        await self._safe_update(
            ENTITY_QUALITY, round(float(score), 2), _STATIC_ATTRS_QUALITY,
        )

    async def publish_duration(self, seconds: float) -> None:
        """Reflect the running session length (one POST per inference tick)."""
        await self._safe_update(
            ENTITY_DURATION, int(seconds), _STATIC_ATTRS_DURATION,
        )

    async def publish_debt(
        self,
        debt_hours: float,
        *,
        severity: str,
        target_hours: Optional[float] = None,
        nights_to_full_recovery: Optional[int] = None,
    ) -> None:
        """Reflect the sleep-debt accountant's latest read-out.

        Extras (severity / target / recovery nights) are attached as
        entity attributes so Lovelace can show them on a badge card
        without a second entity per metric.
        """
        attrs = dict(_STATIC_ATTRS_DEBT)
        attrs["severity"] = severity
        if target_hours is not None:
            attrs["nightly_target_hours"] = round(float(target_hours), 2)
        if nights_to_full_recovery is not None:
            attrs["nights_to_full_recovery"] = int(nights_to_full_recovery)
        await self._safe_update(
            ENTITY_DEBT, round(float(debt_hours), 2), attrs,
        )

    async def publish_recommended_bedtime(
        self,
        bedtime: Optional[Any],
        *,
        tonight_target_hours: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Publish the bedtime suggested by :class:`SleepDebtTracker`.

        ``bedtime`` is expected to be a :class:`datetime.datetime`.  If
        ``None`` (no wake window set yet), we write the ``"unknown"``
        sentinel so HA keeps the entity in its state machine.
        """
        attrs = dict(_STATIC_ATTRS_RECOMMENDED_BEDTIME)
        if tonight_target_hours is not None:
            attrs["tonight_target_hours"] = round(float(tonight_target_hours), 2)
        if reason:
            attrs["reason"] = reason[:255]
        if bedtime is None:
            await self._safe_update(ENTITY_RECOMMENDED_BEDTIME, "unknown", attrs)
            return
        # HA's ``timestamp`` device_class requires an ISO 8601 string.
        iso = bedtime.isoformat() if hasattr(bedtime, "isoformat") else str(bedtime)
        await self._safe_update(ENTITY_RECOMMENDED_BEDTIME, iso, attrs)

    async def publish_wake_decision(
        self,
        decision: str,
        *,
        reason: Optional[str] = None,
        alarm_time: Optional[Any] = None,
        light_ramp_start: Optional[Any] = None,
        matched_stage: Optional[str] = None,
    ) -> None:
        """Reflect the :class:`SmartWakePlanner` current decision."""
        attrs = dict(_STATIC_ATTRS_WAKE_DECISION)
        if reason:
            attrs["reason"] = reason[:255]
        if alarm_time is not None:
            attrs["alarm_time"] = (
                alarm_time.isoformat()
                if hasattr(alarm_time, "isoformat") else str(alarm_time)
            )
        if light_ramp_start is not None:
            attrs["light_ramp_start"] = (
                light_ramp_start.isoformat()
                if hasattr(light_ramp_start, "isoformat") else str(light_ramp_start)
            )
        if matched_stage:
            attrs["matched_stage"] = matched_stage
        await self._safe_update(ENTITY_WAKE_DECISION, str(decision), attrs)

    async def publish_soundscape(
        self,
        soundscape: str,
        *,
        volume_pct: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Reflect the current :class:`WhiteNoiseMatcher` policy."""
        attrs = dict(_STATIC_ATTRS_SOUNDSCAPE)
        if volume_pct is not None:
            attrs["volume_pct"] = round(float(volume_pct), 1)
        if reason:
            attrs["reason"] = reason[:255]
        await self._safe_update(ENTITY_SOUNDSCAPE, str(soundscape), attrs)

    # ------------------------------------------------------------------ #
    # v1.3.0 — preference-learning entities                                #
    # ------------------------------------------------------------------ #

    async def publish_learned_bedtime(
        self,
        bedtime: Dict[str, Any],
    ) -> None:
        """Mirror the ``PreferenceLearner.recommend_bedtime`` payload.

        Two entities are written:

        * ``sensor.sleep_classifier_learned_bedtime_workday``
        * ``sensor.sleep_classifier_learned_bedtime_weekend``

        Each carries a ``"HH:MM"`` state (or ``"unknown"`` until the
        bucket has enough samples) and exposes the full bedtime dict
        as attributes so Lovelace can render confidence + sample count.
        """
        workday = bedtime.get("weekday_bedtime") or "unknown"
        weekend = bedtime.get("weekend_bedtime") or "unknown"

        wd_attrs = dict(_STATIC_ATTRS_LEARNED_BEDTIME_WORKDAY)
        wd_attrs["n_samples"] = int(bedtime.get("n_workday", 0))
        wd_attrs["confidence"] = round(float(bedtime.get("confidence", 0.0)), 2)
        wd_attrs["tonight_bucket"] = bedtime.get("tonight_bucket", "")
        await self._safe_update(
            ENTITY_LEARNED_BEDTIME_WORKDAY, str(workday), wd_attrs,
        )

        we_attrs = dict(_STATIC_ATTRS_LEARNED_BEDTIME_WEEKEND)
        we_attrs["n_samples"] = int(bedtime.get("n_weekend", 0))
        we_attrs["confidence"] = round(float(bedtime.get("confidence", 0.0)), 2)
        we_attrs["tonight_bucket"] = bedtime.get("tonight_bucket", "")
        await self._safe_update(
            ENTITY_LEARNED_BEDTIME_WEEKEND, str(weekend), we_attrs,
        )

    async def publish_learned_environment(
        self,
        env: Dict[str, Any],
        *,
        confidence: float = 0.0,
        n_used: int = 0,
    ) -> None:
        """Headline string for the recommended bedroom environment.

        State format: ``"19.5 °C / 50 % / 5 %"`` — readable at a glance
        on a chip-style Lovelace card.  Each numeric value lives in
        attributes too so users can wire them into custom automations.
        """
        def _fmt(v: Optional[float], suffix: str, dp: int = 1) -> str:
            return f"{round(float(v), dp)} {suffix}" if v is not None else "—"

        temp = env.get("temperature_c")
        hum = env.get("humidity_pct")
        bright = env.get("brightness_pct")
        state = " / ".join([
            _fmt(temp, "°C", 1),
            _fmt(hum, "%", 0),
            _fmt(bright, "%", 0),
        ])
        attrs = dict(_STATIC_ATTRS_LEARNED_ENVIRONMENT)
        if temp is not None:
            attrs["temperature_c"] = round(float(temp), 2)
        if hum is not None:
            attrs["humidity_pct"] = round(float(hum), 1)
        if bright is not None:
            attrs["brightness_pct"] = round(float(bright), 1)
        if env.get("fan_speed_pct") is not None:
            attrs["fan_speed_pct"] = round(float(env["fan_speed_pct"]), 1)
        attrs["confidence"] = round(float(confidence), 2)
        attrs["n_used"] = int(n_used)
        await self._safe_update(ENTITY_LEARNED_ENVIRONMENT, state, attrs)

    async def publish_recommendation_explain(
        self,
        explanation: Dict[str, Any],
    ) -> None:
        """Surface the ``PreferenceLearner.explain()`` payload to HA.

        State is the literal string ``"ready"`` or ``"not_ready"`` so
        Lovelace can colour-code it; the actual reasoning lives in
        attributes (capped at the 15 KB HA limit by truncating the
        ``neighbors`` list to the 5 highest-weight rows).
        """
        ready = bool(explanation.get("ready"))
        state = "ready" if ready else "not_ready"
        attrs = dict(_STATIC_ATTRS_RECOMMENDATION_EXPLAIN)
        for k in (
            "method", "n_total", "avg_age_days", "decay_half_life_days",
            "effective_sample_size", "recommendation", "bedtime",
            "confidence", "reason",
        ):
            if k in explanation:
                attrs[k] = explanation[k]
        # Cap neighbour list so attributes never bust HA's 16 KB limit.
        neighbors = list(explanation.get("neighbors") or [])[:5]
        if neighbors:
            attrs["neighbors"] = neighbors
        await self._safe_update(ENTITY_RECOMMENDATION_EXPLAIN, state, attrs)

    async def publish_per_stage_deltas(
        self,
        deltas: Dict[str, Dict[str, Any]],
        *,
        ess_threshold: float = 4.0,
    ) -> None:
        """Publish the learner's per-stage env deltas.

        ``deltas`` is the dict returned by
        :meth:`PreferenceLearner.recommend_per_stage_deltas`.  The
        state is a coarse-grained enum so HA UIs can colour-code the
        sensor at a glance:

        * ``clinical`` — no stage has a learned override active.
        * ``learning`` — at least one stage has *any* samples but none
          have crossed ``ess_threshold`` yet.
        * ``personalised`` — at least one non-baseline stage has a
          learned delta in use.

        Attributes carry the full per-stage breakdown (deltas + ESS +
        n_sessions per stage) so a Lovelace card can render the table.
        """
        if not deltas:
            await self._safe_update(
                ENTITY_PER_STAGE_DELTAS, "clinical",
                _STATIC_ATTRS_PER_STAGE_DELTAS,
            )
            return

        # Decide overall state.
        personalised = False
        learning = False
        for stage_name, entry in deltas.items():
            if stage_name == "LIGHT":
                continue
            ess = float(entry.get("ess", 0.0) or 0.0)
            # Did any field actually get a learned value?
            any_field_learned = any(
                entry.get(f) is not None
                for f in ("temperature_c", "humidity_pct",
                          "brightness_pct", "fan_speed_pct")
            )
            if any_field_learned and ess >= ess_threshold:
                personalised = True
            elif ess > 0:
                learning = True

        if personalised:
            state = "personalised"
        elif learning:
            state = "learning"
        else:
            state = "clinical"

        attrs = dict(_STATIC_ATTRS_PER_STAGE_DELTAS)
        # Flatten into HA-friendly keys: e.g.
        #   awake_temperature_c_delta = +2.1
        #   awake_ess = 7.3
        # This avoids nested objects in the attributes table which
        # some HA frontends can't render.
        for stage_name, entry in deltas.items():
            sn = stage_name.lower()
            for field in ("temperature_c", "humidity_pct",
                          "brightness_pct", "fan_speed_pct"):
                val = entry.get(field)
                if val is None:
                    continue
                attrs[f"{sn}_{field}_delta"] = round(float(val), 2)
            ess = entry.get("ess")
            if ess is not None:
                attrs[f"{sn}_ess"] = round(float(ess), 2)
            n_sess = entry.get("n_sessions")
            if n_sess is not None:
                attrs[f"{sn}_n_sessions"] = int(n_sess)
        attrs["ess_threshold"] = float(ess_threshold)

        await self._safe_update(ENTITY_PER_STAGE_DELTAS, state, attrs)

    async def publish_apnea_index(
        self,
        trend: str,
        *,
        status: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Publish the v1.7.0 apnea trend sensor.

        ``trend`` is one of the enum option strings declared in
        :data:`_STATIC_ATTRS_APNEA_INDEX` (``pending_consent`` /
        ``calibrating`` / ``green`` / ``amber`` / ``red``).
        ``status`` optionally carries calibration-progress /
        consent-flag / last-trend diagnostics pulled from
        :meth:`src.apnea_wiring.ApneaWiring.status`.

        Intentionally never writes a numeric AHI or events/hour value
        anywhere on this entity — the whole design is to keep clinical
        numbers off the user-facing dashboard.  If a future release
        decides to (e.g. under a second opt-in gate) it should use a
        separate entity with an even more prominent disclaimer.
        """
        attrs = dict(_STATIC_ATTRS_APNEA_INDEX)
        if status:
            # Only forward diagnostic flags — not events/hour or
            # baseline values that could be misread as AHI.
            for key in (
                "enabled",
                "consent",
                "calibration_nights_required",
                "calibration_nights_completed",
            ):
                if key in status:
                    attrs[key] = status[key]
        await self._safe_update(ENTITY_APNEA_INDEX, str(trend), attrs)

    async def publish_last_action(
        self,
        summary: str,
        *,
        executed: bool,
        skipped_by_capability: Optional[Dict[str, int]] = None,
    ) -> None:
        """Surface the most recent device action (or 'planned only' in dry-run).

        ``summary`` is something short like ``"light.bedroom_main → off"`` so
        Lovelace can show it on a chip.  The full payload is stored as an
        attribute for users who want to dig in.

        ``skipped_by_capability`` (v1.6.2) is a mapping from
        :class:`src.device_capabilities.Capability` value strings to the
        count of actions the controller declined to issue because the
        bound entity didn't advertise that feature.  Exposing it on
        the diagnostics sensor lets the user see, on their Lovelace
        dashboard, that e.g. their AC was skipped 12 times today for
        ``set_temperature`` support — a strong hint that the
        entity_id in Configuration is pointing at a preset-only
        device and should be rebound.
        """
        attrs = dict(_STATIC_ATTRS_LAST_ACTION)
        attrs["executed"] = bool(executed)
        if skipped_by_capability:
            # Cast to plain dict + sort by count desc so the Lovelace
            # attributes panel shows the most-skipped capability first.
            ordered = dict(
                sorted(
                    skipped_by_capability.items(),
                    key=lambda kv: kv[1], reverse=True,
                )
            )
            attrs["skipped_by_capability"] = ordered
        # 使用 ASCII 占位符避免 HA 前端在某些编码路径下把
        # U+2014 显示成乱码 ``â`` (v3.0.2 修复)。
        truncated = (summary or "-")[:255]
        await self._safe_update(ENTITY_LAST_ACTION, truncated, attrs)

    # ------------------------------------------------------------------ #
    # v1.8.0 — health status sensor                                       #
    # ------------------------------------------------------------------ #

    async def publish_health(
        self,
        *,
        stage_source_stale: bool = False,
        env_stale_fields: Optional[List[str]] = None,
        publisher_failures: int = 0,
        learner_sessions: int = 0,
        capability_skipped: int = 0,
    ) -> None:
        """Publish the aggregated health status sensor.

        Logic:
        - ``error``: stage source stale OR publisher consecutive failures > 5
        - ``degraded``: any env sensor stale OR capability skipped > 0
          OR learner history < 3 sessions
        - ``healthy``: none of the above
        """
        if stage_source_stale or publisher_failures > 5:
            state = "error"
        elif (env_stale_fields and len(env_stale_fields) > 0) or \
                capability_skipped > 0 or learner_sessions < 3:
            state = "degraded"
        else:
            state = "healthy"

        attrs = dict(_STATIC_ATTRS_HEALTH)
        attrs["stage_source_stale"] = stage_source_stale
        attrs["env_stale_fields"] = list(env_stale_fields or [])
        attrs["publisher_failures"] = publisher_failures
        attrs["learner_sessions"] = learner_sessions
        attrs["capability_skipped"] = capability_skipped
        await self._safe_update(ENTITY_HEALTH, state, attrs)

    # ------------------------------------------------------------------ #
    # v1.8.0 — quality sub-score sensors                                  #
    # ------------------------------------------------------------------ #

    async def publish_quality_sub_scores(
        self,
        sub_scores: Dict[str, float],
    ) -> None:
        """Publish the 4 quality sub-score sensors.

        ``sub_scores`` is the dict returned by
        :func:`src.sleep_quality_score.compute_objective_quality` with
        keys ``architecture``, ``efficiency``, ``fragmentation``,
        ``onset``.
        """
        mapping = {
            "architecture": (ENTITY_QUALITY_ARCHITECTURE, _STATIC_ATTRS_QUALITY_ARCHITECTURE),
            "efficiency": (ENTITY_QUALITY_EFFICIENCY, _STATIC_ATTRS_QUALITY_EFFICIENCY),
            "fragmentation": (ENTITY_QUALITY_FRAGMENTATION, _STATIC_ATTRS_QUALITY_FRAGMENTATION),
            "onset": (ENTITY_QUALITY_ONSET, _STATIC_ATTRS_QUALITY_ONSET),
        }
        for key, (entity_id, static_attrs) in mapping.items():
            value = sub_scores.get(key)
            if value is not None:
                await self._safe_update(
                    entity_id, round(float(value), 1), static_attrs,
                )

    async def publish_initial_placeholders(self) -> None:
        """Write a sentinel value for every owned entity at boot time.

        Without this, freshly-installed Lovelace cards show a frustrating
        "Entity not available" until the first inference tick (potentially
        ~10 minutes after a cold start).  We seed each entity with an
        ``"unknown"``-equivalent state on connect so the cards render
        immediately and update naturally as data arrives.
        """
        await self._safe_update(ENTITY_STAGE, "AWAKE", _STATIC_ATTRS_STAGE)
        await self._safe_update(ENTITY_CONFIDENCE, 0.0, _STATIC_ATTRS_CONF)
        await self._safe_update(ENTITY_QUALITY, 0.0, _STATIC_ATTRS_QUALITY)
        await self._safe_update(ENTITY_DURATION, 0, _STATIC_ATTRS_DURATION)
        await self._safe_update(
            ENTITY_LAST_ACTION, "-", _STATIC_ATTRS_LAST_ACTION,
        )
        # Natural-sleep entities — these are still safe to publish even
        # if the user didn't enable the corresponding module: the values
        # are placeholder neutrals.
        await self._safe_update(ENTITY_DEBT, 0.0, _STATIC_ATTRS_DEBT)
        await self._safe_update(
            ENTITY_RECOMMENDED_BEDTIME, "unknown",
            _STATIC_ATTRS_RECOMMENDED_BEDTIME,
        )
        await self._safe_update(
            ENTITY_WAKE_DECISION, "hold", _STATIC_ATTRS_WAKE_DECISION,
        )
        await self._safe_update(
            ENTITY_SOUNDSCAPE, "off", _STATIC_ATTRS_SOUNDSCAPE,
        )
        # v1.3.0 learning entities — placeholder until enough sessions
        # accumulate.  ``unknown`` is HA's standard not-yet-populated
        # sentinel; the explain entity uses ``not_ready`` to match its
        # enum options list.
        await self._safe_update(
            ENTITY_LEARNED_BEDTIME_WORKDAY, "unknown",
            _STATIC_ATTRS_LEARNED_BEDTIME_WORKDAY,
        )
        await self._safe_update(
            ENTITY_LEARNED_BEDTIME_WEEKEND, "unknown",
            _STATIC_ATTRS_LEARNED_BEDTIME_WEEKEND,
        )
        await self._safe_update(
            ENTITY_LEARNED_ENVIRONMENT, "—",
            _STATIC_ATTRS_LEARNED_ENVIRONMENT,
        )
        await self._safe_update(
            ENTITY_RECOMMENDATION_EXPLAIN, "not_ready",
            _STATIC_ATTRS_RECOMMENDATION_EXPLAIN,
        )
        await self._safe_update(
            ENTITY_PER_STAGE_DELTAS, "clinical",
            _STATIC_ATTRS_PER_STAGE_DELTAS,
        )
        # v1.7.0 — apnea trend defaults to pending_consent even if the
        # feature is enabled, because the user must still toggle the
        # consent input_boolean before anything meaningful is
        # published.  On add-ons where the feature is not configured
        # at all, this is also the harmless default shown on Lovelace.
        await self._safe_update(
            ENTITY_APNEA_INDEX, "pending_consent",
            _STATIC_ATTRS_APNEA_INDEX,
        )
        # v1.8.0 — health status + quality sub-scores.
        await self._safe_update(
            ENTITY_HEALTH, "healthy", _STATIC_ATTRS_HEALTH,
        )
        await self._safe_update(
            ENTITY_QUALITY_ARCHITECTURE, 0, _STATIC_ATTRS_QUALITY_ARCHITECTURE,
        )
        await self._safe_update(
            ENTITY_QUALITY_EFFICIENCY, 0, _STATIC_ATTRS_QUALITY_EFFICIENCY,
        )
        await self._safe_update(
            ENTITY_QUALITY_FRAGMENTATION, 0, _STATIC_ATTRS_QUALITY_FRAGMENTATION,
        )
        await self._safe_update(
            ENTITY_QUALITY_ONSET, 0, _STATIC_ATTRS_QUALITY_ONSET,
        )

        # v3.0.0 — boot-time seeding of the 14 algorithmic-moat sensors.
        # 仅当编排层（Task 8.1）已经调用 ``set_v3_modules`` 注入引用之后
        # 才发布 v3 sensor。4 个 flag 全关时主入口**不**调用
        # ``set_v3_modules``，``_v3_modules_loaded`` 保持 ``False``，本方法
        # 退回到 v2.1.0 行为（仅写 20 个旧 sensor），保证 R11.4 字节级等价
        # 回退；任一 flag 开启时编排层会先注入 ``set_v3_modules`` 再调用本
        # 方法，14 个 v3 sensor 此时按 design §3.5 / §6.3 一并发布（停用
        # 模块对应 sensor 仍写 ``state = "disabled"``，PR2 兼容契约：既有
        # 20 个 sensor 的 entity_id + attribute schema 逐字保留）。
        if self._v3_modules_loaded:
            await self._publish_v3_sensors()

    # ------------------------------------------------------------------ #
    # v3.0.0 — algorithmic moat sensors                                   #
    # ------------------------------------------------------------------ #

    def set_v3_modules(
        self,
        *,
        bao: Any = None,
        cae_engine: Any = None,
        prior_repo: Any = None,
        predictor: Any = None,
    ) -> None:
        """Register references to the 4 v3 algorithmic moat modules.

        Called once by the orchestrator (Task 8.1) after each module
        finishes ``load_or_init`` / ``try_load`` / ``__init__``.  Any
        argument may be :data:`None` — the corresponding sensor will
        still be published with ``state = "disabled"`` so Lovelace
        cards render consistently regardless of which feature flags
        are turned off (design §3.5 / §6.3).

        Calling this method flips :attr:`_v3_modules_loaded` to
        ``True`` so the orchestrator can guard the publish call with
        ``if publisher.v3_modules_loaded`` (PR2: existing 20-sensor
        publish path is unchanged).
        """
        self._v3_bao = bao
        self._v3_cae_engine = cae_engine
        self._v3_prior_repo = prior_repo
        self._v3_predictor = predictor
        self._v3_modules_loaded = True

    @property
    def v3_modules_loaded(self) -> bool:
        """Return ``True`` once :meth:`set_v3_modules` has been called."""
        return self._v3_modules_loaded

    async def _publish_v3_sensors(
        self,
        *,
        bao: Any = None,
        cae_engine: Any = None,
        prior_repo: Any = None,
        predictor: Any = None,
        attribution_result: Any = None,
        quality_trend: Optional[Dict[str, Any]] = None,
        locked_dimensions: Optional[Dict[str, Any]] = None,
        last_recommendation: Any = None,
        decision_mode: Optional[str] = None,
        prior_weight: Optional[float] = None,
        prior_weight_locked: bool = False,
        bucket_key: Optional[str] = None,
        fallback_level: Optional[int] = None,
        bucket_n_samples: Optional[int] = None,
        optimizer_streak_days: Optional[int] = None,
        optimizer_status_label: Optional[str] = None,
    ) -> None:
        """Publish the 14 v3 algorithmic-moat sensors (design §3.5).

        Any module argument may be :data:`None` (or absent on the
        registered :attr:`_v3_*` attributes); the corresponding sensor
        is still published with ``state = "disabled"`` so Lovelace
        renders the chip consistently in every flag combination
        (design §3.5 / §6.3).

        :param bao: Optional :class:`BayesianOptimizer` reference.
            Falls back to :attr:`_v3_bao` registered via
            :meth:`set_v3_modules`.
        :param cae_engine: Optional :class:`CausalAttributionEngine`.
        :param prior_repo: Optional :class:`PopulationPriorRepository`.
        :param predictor: Optional :class:`StagePredictor`.
        :param attribution_result: Optional latest
            :class:`AttributionResult` from CAE; when ``None`` the
            attribution sensors fall back to a benign ``"nominal"``
            state (CAE healthy, just no fresh result yet).
        :param quality_trend: Optional ``{"slope_score_per_day": float,
            "window_nights": int, "n_observations": int}`` payload
            produced by the orchestrator's 24-h trend job.
        :param locked_dimensions: Optional
            ``{"dimensions": list[str], "expires_at_iso": str}``;
            corresponds to user-pinned axes (R2.5).
        :param last_recommendation: Optional
            :class:`GPRecommendation` whose ``mode`` /
            ``posterior_std`` / ``prior_weight`` populate the BAO
            decision-mode + uncertainty + prior-weight sensors.
        :param decision_mode: Optional override for
            ``sensor.sleep_classifier_decision_mode``; falls back to
            ``last_recommendation.mode`` when absent.
        :param prior_weight: Optional explicit prior-weight value
            (0..1).  Falls back to ``last_recommendation.prior_weight``
            and finally to :data:`None` → state = ``"unknown"``.
        :param prior_weight_locked: ``True`` when the user has pinned
            the prior weight via Web UI (R8.5).
        :param bucket_key: Optional pre-formatted bucket key string
            for ``sensor.sleep_classifier_prior_status`` attributes.
        :param fallback_level: Optional ``int`` (0..3) returned by
            :meth:`PopulationPriorRepository.lookup`.
        :param bucket_n_samples: Optional bucket sample count.
        :param optimizer_streak_days: Optional consecutive-night
            count where the slope ≥ +0.5 score/day (R3.2).
        :param optimizer_status_label: Optional explicit value for
            ``sensor.sleep_classifier_optimizer_status``; falls back
            to ``"learning"`` when not supplied (avoids tripping the
            converging / converged latch from sensor publishes alone).
        """
        # 优先用入参；缺省时回退 ``set_v3_modules`` 注册的实例引用，保证
        # 编排层既可以一次注入然后省略入参，也可以按需在每次 publish 调用
        # 现传当晚最新的 ``AttributionResult`` / ``GPRecommendation``。
        bao = bao if bao is not None else self._v3_bao
        cae_engine = (
            cae_engine if cae_engine is not None else self._v3_cae_engine
        )
        prior_repo = (
            prior_repo if prior_repo is not None else self._v3_prior_repo
        )
        predictor = (
            predictor if predictor is not None else self._v3_predictor
        )

        await self._publish_v3_bao_sensors(
            bao=bao,
            last_recommendation=last_recommendation,
            decision_mode=decision_mode,
            prior_weight=prior_weight,
            prior_weight_locked=prior_weight_locked,
            quality_trend=quality_trend,
            locked_dimensions=locked_dimensions,
            optimizer_streak_days=optimizer_streak_days,
            optimizer_status_label=optimizer_status_label,
        )
        await self._publish_v3_cae_sensors(
            cae_engine=cae_engine,
            attribution_result=attribution_result,
        )
        await self._publish_v3_prior_sensors(
            prior_repo=prior_repo,
            bucket_key=bucket_key,
            fallback_level=fallback_level,
            bucket_n_samples=bucket_n_samples,
        )
        await self._publish_v3_predictor_sensors(predictor=predictor)
        await self._publish_v3_health_summary(
            bao=bao,
            cae_engine=cae_engine,
            prior_repo=prior_repo,
            predictor=predictor,
        )

    # -- BAO sensors ---------------------------------------------------- #

    async def _publish_v3_bao_sensors(
        self,
        *,
        bao: Any,
        last_recommendation: Any,
        decision_mode: Optional[str],
        prior_weight: Optional[float],
        prior_weight_locked: bool,
        quality_trend: Optional[Dict[str, Any]],
        locked_dimensions: Optional[Dict[str, Any]],
        optimizer_streak_days: Optional[int],
        optimizer_status_label: Optional[str],
    ) -> None:
        """Publish the 6 BAO-flavoured sensors (optimizer_* + decision_mode +
        locked_dimensions + quality_trend_14d + prior_weight)."""
        # ---- optimizer_health -----------------------------------------
        attrs = dict(_STATIC_ATTRS_OPTIMIZER_HEALTH)
        if bao is None:
            await self._safe_update(
                ENTITY_OPTIMIZER_HEALTH, "disabled", attrs,
            )
        else:
            error_count = int(getattr(bao, "error_count", 0) or 0)
            attrs["error_count"] = error_count
            last_error = getattr(bao, "last_error", None)
            if last_error:
                attrs["last_error"] = str(last_error)[:_HA_STATE_MAX_LEN]
            health = "degraded" if error_count >= 3 else "healthy"
            await self._safe_update(ENTITY_OPTIMIZER_HEALTH, health, attrs)

        # ---- optimizer_status -----------------------------------------
        attrs = dict(_STATIC_ATTRS_OPTIMIZER_STATUS)
        if bao is None:
            await self._safe_update(
                ENTITY_OPTIMIZER_STATUS, "disabled", attrs,
            )
        else:
            # 默认保持 ``learning``；converging / converged 由编排层维护
            # 7/14 晚 streak 后通过 ``optimizer_status_label`` 显式传入
            # （R3.2，避免在 sensor 内部维护重复的 streak 状态机）。
            status = optimizer_status_label or "learning"
            if status not in ("learning", "converging", "converged"):
                status = "learning"
            if optimizer_streak_days is not None:
                attrs["streak_days"] = int(optimizer_streak_days)
            if quality_trend is not None:
                slope = quality_trend.get("slope_score_per_day")
                if slope is not None:
                    attrs["slope_score_per_day"] = round(float(slope), 3)
            await self._safe_update(ENTITY_OPTIMIZER_STATUS, status, attrs)

        # ---- optimizer_uncertainty ------------------------------------
        attrs = dict(_STATIC_ATTRS_OPTIMIZER_UNCERTAINTY)
        if bao is None:
            await self._safe_update(
                ENTITY_OPTIMIZER_UNCERTAINTY, "disabled", attrs,
            )
        else:
            sigmas = self._extract_posterior_std(bao, last_recommendation)
            if sigmas is None:
                # GP 还没准备好（N<5 / cholesky 失败） — 状态退化但保留
                # 既有 enum schema 不变（PR2）。
                await self._safe_update(
                    ENTITY_OPTIMIZER_UNCERTAINTY, "unknown", attrs,
                )
            else:
                sigma_t, sigma_h, sigma_l = sigmas
                attrs["sigma_temp_c"] = round(float(sigma_t), 3)
                attrs["sigma_humidity_pct"] = round(float(sigma_h), 3)
                attrs["sigma_brightness_pct"] = round(float(sigma_l), 3)
                # state 仅放 σ_T（温度维度，℃），其余两维通过 attribute
                # 暴露，state 长度 ≤ 255 字符（design §3.5 契约）。
                await self._safe_update(
                    ENTITY_OPTIMIZER_UNCERTAINTY,
                    round(float(sigma_t), 3),
                    attrs,
                )

        # ---- decision_mode --------------------------------------------
        attrs = dict(_STATIC_ATTRS_DECISION_MODE)
        if bao is None:
            await self._safe_update(
                ENTITY_DECISION_MODE, "disabled", attrs,
            )
        else:
            mode = decision_mode
            if mode is None and last_recommendation is not None:
                mode = getattr(last_recommendation, "mode", None)
            mode_str = str(mode) if mode else "prior-only"
            # 输入卫生：未知 mode 退回到 ``prior-only`` 而不是污染 enum。
            allowed = {
                "exploit", "explore-temp", "explore-humidity",
                "explore-brightness", "prior-only",
            }
            if mode_str not in allowed:
                mode_str = "prior-only"
            effective_pw = self._resolve_prior_weight(
                bao=bao,
                explicit=prior_weight,
                rec=last_recommendation,
            )
            if effective_pw is not None:
                attrs["prior_weight"] = round(float(effective_pw), 4)
            exploration_rate = getattr(
                bao, "_exploration_rate", None,
            )
            if exploration_rate is None:
                exploration_rate = getattr(bao, "exploration_rate", None)
            if exploration_rate is not None:
                attrs["exploration_rate_effective"] = round(
                    float(exploration_rate), 4,
                )
            await self._safe_update(ENTITY_DECISION_MODE, mode_str, attrs)

        # ---- locked_dimensions ----------------------------------------
        attrs = dict(_STATIC_ATTRS_LOCKED_DIMENSIONS)
        if bao is None:
            await self._safe_update(
                ENTITY_LOCKED_DIMENSIONS, "disabled", attrs,
            )
        else:
            dims: List[str] = []
            expires_iso: Optional[str] = None
            if locked_dimensions is not None:
                raw_dims = locked_dimensions.get("dimensions") or []
                dims = [str(d) for d in raw_dims if d]
                expires_iso = locked_dimensions.get("expires_at_iso")
            state = ",".join(dims) if dims else "none"
            # state 上限 255 字符；超长情况下仅写 ``locked`` 占位符，
            # 完整列表已经在 attribute 中暴露（design §3.5 契约）。
            if len(state) > _HA_STATE_MAX_LEN:
                state = "locked"
            attrs["dimensions"] = dims
            if expires_iso:
                attrs["expires_at_iso"] = str(expires_iso)[:_HA_STATE_MAX_LEN]
            await self._safe_update(ENTITY_LOCKED_DIMENSIONS, state, attrs)

        # ---- quality_trend_14d ----------------------------------------
        attrs = dict(_STATIC_ATTRS_QUALITY_TREND_14D)
        if bao is None:
            await self._safe_update(
                ENTITY_QUALITY_TREND_14D, "disabled", attrs,
            )
        else:
            slope: Optional[float] = None
            if quality_trend is not None:
                raw_slope = quality_trend.get("slope_score_per_day")
                if raw_slope is not None:
                    try:
                        slope = float(raw_slope)
                    except (TypeError, ValueError):
                        slope = None
                window = quality_trend.get("window_nights")
                if window is not None:
                    attrs["window_nights"] = int(window)
                n_obs = quality_trend.get("n_observations")
                if n_obs is not None:
                    attrs["n_observations"] = int(n_obs)
            if slope is None or not math.isfinite(slope):
                await self._safe_update(
                    ENTITY_QUALITY_TREND_14D, "unknown", attrs,
                )
            else:
                await self._safe_update(
                    ENTITY_QUALITY_TREND_14D, round(slope, 3), attrs,
                )

        # ---- prior_weight ---------------------------------------------
        attrs = dict(_STATIC_ATTRS_PRIOR_WEIGHT)
        if bao is None:
            await self._safe_update(
                ENTITY_PRIOR_WEIGHT, "disabled", attrs,
            )
        else:
            effective_pw = self._resolve_prior_weight(
                bao=bao,
                explicit=prior_weight,
                rec=last_recommendation,
            )
            attrs["manually_locked"] = bool(prior_weight_locked)
            if effective_pw is None or not math.isfinite(effective_pw):
                await self._safe_update(
                    ENTITY_PRIOR_WEIGHT, "unknown", attrs,
                )
            else:
                # 裁剪到 [0, 1] — design §3.2.3 契约。
                clipped = max(0.0, min(1.0, float(effective_pw)))
                await self._safe_update(
                    ENTITY_PRIOR_WEIGHT, round(clipped, 4), attrs,
                )

    # -- CAE sensors ---------------------------------------------------- #

    async def _publish_v3_cae_sensors(
        self,
        *,
        cae_engine: Any,
        attribution_result: Any,
    ) -> None:
        """Publish the 2 CAE sensors (attribution + attribution_full)."""
        # ---- attribution ---------------------------------------------
        attrs = dict(_STATIC_ATTRS_ATTRIBUTION)
        if cae_engine is None:
            await self._safe_update(
                ENTITY_ATTRIBUTION, "disabled", attrs,
            )
        else:
            if attribution_result is None:
                # CAE 加载成功但没有最新结果（启动期 / 当晚还未触发 → 默认
                # ``nominal``，与 ``STATUS_NOMINAL`` 语义一致）。
                state = "nominal"
                attrs["explanation_zh"] = ""
            else:
                status = str(
                    getattr(attribution_result, "status", "nominal")
                )
                allowed = {
                    "ok", "nominal", "insufficient_data", "timeout",
                }
                state = status if status in allowed else "nominal"
                top_factor = getattr(
                    attribution_result, "top_factor", None,
                )
                if top_factor:
                    attrs["top_factor"] = str(top_factor)
                top_effect = getattr(
                    attribution_result, "top_effect_pp", None,
                )
                if top_effect is not None:
                    try:
                        attrs["top_effect_pp"] = round(float(top_effect), 3)
                    except (TypeError, ValueError):
                        pass
                cf_score = getattr(
                    attribution_result, "counterfactual_score", None,
                )
                if cf_score is not None:
                    try:
                        attrs["counterfactual_score"] = round(
                            float(cf_score), 2,
                        )
                    except (TypeError, ValueError):
                        pass
                explanation = getattr(
                    attribution_result, "explanation_zh", "",
                )
                # explanation_zh 容易超过 255 字符，因此**只**放 attribute
                # （design §3.5 契约：超长内容仅放 attribute）。
                attrs["explanation_zh"] = (
                    str(explanation)[:_HA_STATE_MAX_LEN]
                )
            await self._safe_update(ENTITY_ATTRIBUTION, state, attrs)

        # ---- attribution_full ----------------------------------------
        attrs = dict(_STATIC_ATTRS_ATTRIBUTION_FULL)
        if cae_engine is None:
            await self._safe_update(
                ENTITY_ATTRIBUTION_FULL, "disabled", attrs,
            )
        else:
            effects_payload: Dict[str, Any] = {}
            if attribution_result is not None:
                for eff in getattr(attribution_result, "effects", ()) or ():
                    factor = getattr(eff, "factor", None)
                    if not factor:
                        continue
                    to_dict = getattr(eff, "to_dict", None)
                    if callable(to_dict):
                        effects_payload[str(factor)] = to_dict()
                    else:
                        # 回退：手动展开 fields，保持 forward-compat。
                        effects_payload[str(factor)] = {
                            "effect_pp": getattr(eff, "effect_pp", None),
                            "ci_low": getattr(eff, "ci_low", None),
                            "ci_high": getattr(eff, "ci_high", None),
                            "n_observations": getattr(
                                eff, "n_observations", None,
                            ),
                            "is_significant": bool(
                                getattr(eff, "is_significant", False)
                            ),
                        }
            attrs["effects"] = effects_payload
            await self._safe_update(ENTITY_ATTRIBUTION_FULL, "ok", attrs)

    # -- PP sensor ------------------------------------------------------ #

    async def _publish_v3_prior_sensors(
        self,
        *,
        prior_repo: Any,
        bucket_key: Optional[str],
        fallback_level: Optional[int],
        bucket_n_samples: Optional[int],
    ) -> None:
        """Publish the 1 PP-flavoured sensor (prior_status).

        ``prior_weight`` belongs to BAO+PP and is published in
        :meth:`_publish_v3_bao_sensors` because the effective value
        depends on ``BayesianOptimizer._compute_prior_weight``."""
        attrs = dict(_STATIC_ATTRS_PRIOR_STATUS)
        if prior_repo is None:
            await self._safe_update(
                ENTITY_PRIOR_STATUS, "disabled", attrs,
            )
            return
        # ``prior_repo is not None`` 表示 pickle 加载成功（PP.load 失败时
        # 主入口直接传 ``None``）。fallback_level 区分 loaded / fallback。
        if bucket_key:
            attrs["bucket_key"] = str(bucket_key)[:_HA_STATE_MAX_LEN]
        if fallback_level is not None:
            attrs["fallback_level"] = int(fallback_level)
        if bucket_n_samples is not None:
            attrs["n_samples"] = int(bucket_n_samples)
        if fallback_level is None:
            # 还没第一次 lookup —— pickle 已加载但状态未知。
            state = "loaded"
        elif int(fallback_level) <= 0:
            state = "loaded"
        else:
            state = "fallback"
        await self._safe_update(ENTITY_PRIOR_STATUS, state, attrs)

    # -- EMST sensors --------------------------------------------------- #

    async def _publish_v3_predictor_sensors(
        self,
        *,
        predictor: Any,
    ) -> None:
        """Publish the 3 EMST-flavoured sensors (predictor_health +
        predictor_status + predictor_hit_rate_7d)."""
        # ---- predictor_health ----------------------------------------
        attrs = dict(_STATIC_ATTRS_PREDICTOR_HEALTH)
        if predictor is None:
            await self._safe_update(
                ENTITY_PREDICTOR_HEALTH, "disabled", attrs,
            )
        else:
            error_count = int(getattr(predictor, "error_count", 0) or 0)
            attrs["error_count"] = error_count
            last_inf_ms = getattr(predictor, "last_inference_ms", None)
            if last_inf_ms is not None:
                try:
                    attrs["last_inference_ms"] = round(
                        float(last_inf_ms), 2,
                    )
                except (TypeError, ValueError):
                    pass
            disabled_until = float(
                getattr(predictor, "disabled_until", 0.0) or 0.0
            )
            if disabled_until > 0 and time.time() < disabled_until:
                health = "degraded"
            elif error_count > 0:
                health = "degraded"
            else:
                health = "healthy"
            await self._safe_update(
                ENTITY_PREDICTOR_HEALTH, health, attrs,
            )

        # ---- predictor_status ----------------------------------------
        attrs = dict(_STATIC_ATTRS_PREDICTOR_STATUS)
        if predictor is None:
            await self._safe_update(
                ENTITY_PREDICTOR_STATUS, "disabled", attrs,
            )
        else:
            raw_status = str(
                getattr(predictor, "predictor_status", "active")
            )
            # 把 EMST 内部 ``healthy`` / ``degraded`` 映射到 sensor 表里
            # 的 ``active`` —— design §3.5 行 12 的 enum 显式只允许
            # ``active / auto_disabled / disabled``。
            if raw_status == "auto_disabled":
                state = "auto_disabled"
                attrs["disabled_reason"] = "hit_rate_below_70pct_3_nights"
            else:
                state = "active"
            disabled_until = float(
                getattr(predictor, "disabled_until", 0.0) or 0.0
            )
            if disabled_until > 0:
                attrs["disabled_until_iso"] = datetime.fromtimestamp(
                    disabled_until, tz=timezone.utc,
                ).isoformat()
            await self._safe_update(
                ENTITY_PREDICTOR_STATUS, state, attrs,
            )

        # ---- predictor_hit_rate_7d -----------------------------------
        attrs = dict(_STATIC_ATTRS_PREDICTOR_HIT_RATE_7D)
        if predictor is None:
            await self._safe_update(
                ENTITY_PREDICTOR_HIT_RATE_7D, "disabled", attrs,
            )
        else:
            hit_rate_fn = getattr(predictor, "hit_rate_7d", None)
            rate: Optional[float] = None
            if callable(hit_rate_fn):
                try:
                    rate = hit_rate_fn()
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "predictor.hit_rate_7d() raised: %s", exc,
                    )
                    rate = None
            n_predictions = getattr(predictor, "n_predictions", None)
            n_hits = getattr(predictor, "n_hits", None)
            per_stage = getattr(predictor, "per_stage_hit_rate", None)
            if n_predictions is not None:
                attrs["n_predictions"] = int(n_predictions)
            if n_hits is not None:
                attrs["n_hits"] = int(n_hits)
            if per_stage is not None:
                attrs["per_stage"] = dict(per_stage)
            if rate is None or not math.isfinite(float(rate)):
                await self._safe_update(
                    ENTITY_PREDICTOR_HIT_RATE_7D, "unknown", attrs,
                )
            else:
                clipped = max(0.0, min(100.0, float(rate)))
                await self._safe_update(
                    ENTITY_PREDICTOR_HIT_RATE_7D,
                    round(clipped, 1),
                    attrs,
                )

    # -- aggregate summary --------------------------------------------- #

    async def _publish_v3_health_summary(
        self,
        *,
        bao: Any,
        cae_engine: Any,
        prior_repo: Any,
        predictor: Any,
    ) -> None:
        """Aggregate the 4 module health states into a single sensor.

        Semantics (revised v3.0.2 — fresh-install friendly):

        * ``green``  — at least 2 modules healthy and no module degraded.
                       Modules disabled because their training artifact
                       (PP pickle / EMST ONNX) is missing are treated as
                       a graceful no-op rather than a fault — typical
                       state on a brand-new install with no offline
                       training run.
        * ``amber``  — at least 1 module reports ``degraded``
                       (error_count ≥ 3 / EMST auto-disabled).
        * ``red``    — fewer than 2 healthy modules and not the
                       all-disabled case below; indicates a real
                       configuration problem (e.g. BAO + CAE both
                       failed to initialise).
        * ``disabled`` — all 4 modules disabled (config-disabled or
                         no artifacts present); equivalent to v2.1.0
                         behaviour.

        Why not "any disabled ⇒ red"?
            On first install PP (cohort prior pickle) and EMST
            (stage_predictor.onnx) are intentionally absent — these
            artefacts ship out-of-band via the optional offline
            training pipeline. BAO + CAE alone still deliver the
            adaptive-learning value proposition; flagging the system
            as ``red`` would scare new users into thinking the add-on
            is broken when it is in fact running its supported
            cold-start configuration.
        """
        statuses: Dict[str, str] = {
            "bao": self._classify_bao_status(bao),
            "cae": self._classify_cae_status(cae_engine),
            "pp": self._classify_pp_status(prior_repo),
            "emst": self._classify_emst_status(predictor),
        }
        n_disabled = sum(1 for s in statuses.values() if s == "disabled")
        n_degraded = sum(1 for s in statuses.values() if s == "degraded")
        n_healthy = sum(1 for s in statuses.values() if s == "healthy")

        if n_disabled == len(statuses):
            state = "disabled"
        elif n_degraded >= 1:
            state = "amber"
        elif n_healthy >= 2:
            state = "green"
        else:
            state = "red"

        attrs = dict(_STATIC_ATTRS_V3_HEALTH_SUMMARY)
        attrs.update(statuses)
        await self._safe_update(ENTITY_V3_HEALTH_SUMMARY, state, attrs)

    # ------------------------------------------------------------------ #
    # v3.0.0 helpers                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_posterior_std(
        bao: Any, last_recommendation: Any,
    ) -> Optional[tuple[float, float, float]]:
        """Return ``(σ_T, σ_H, σ_L)`` from the most recent recommendation.

        Falls back to ``BayesianOptimizer.posterior_uncertainty`` at the
        prior bucket centre (T=21°C, H=50%, L=5%) when no
        recommendation is provided yet.  Returns :data:`None` if the
        GP has too few observations or numerical-error has cleared the
        Cholesky factor.
        """
        if last_recommendation is not None:
            std = getattr(last_recommendation, "posterior_std", None)
            if std is not None and len(std) == 3:
                try:
                    return (
                        float(std[0]), float(std[1]), float(std[2]),
                    )
                except (TypeError, ValueError):
                    pass
        # 回退：直接探询 BAO 在 prior 桶中心点的 σ。注意 ``posterior_uncertainty``
        # 在 N<5 时仍可调用（返回 σ_f），不会抛异常 — 与 design §3.2.4 一致。
        post_fn = getattr(bao, "posterior_uncertainty", None)
        if not callable(post_fn):
            return None
        try:
            triple = post_fn(at=(21.0, 50.0, 5.0))
        except Exception as exc:  # noqa: BLE001
            logger.debug("posterior_uncertainty failed: %s", exc)
            return None
        if triple is None or len(triple) != 3:
            return None
        try:
            return (float(triple[0]), float(triple[1]), float(triple[2]))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _resolve_prior_weight(
        *,
        bao: Any,
        explicit: Optional[float],
        rec: Any,
    ) -> Optional[float]:
        """Resolve the effective prior weight α for sensor publication.

        Priority: explicit kwarg > recommendation.prior_weight >
        ``bao._compute_prior_weight(N=bao.n_observations)`` when
        available.  Returns :data:`None` if no source can be queried.
        """
        if explicit is not None:
            try:
                return float(explicit)
            except (TypeError, ValueError):
                return None
        if rec is not None:
            pw = getattr(rec, "prior_weight", None)
            if pw is not None:
                try:
                    return float(pw)
                except (TypeError, ValueError):
                    pass
        # 没有当晚 recommendation —— 用 BAO 当前 N 推算（仅 sensor 显示用，
        # 不影响真实决策路径）。
        compute_fn = getattr(bao, "_compute_prior_weight", None)
        n_obs = getattr(bao, "n_observations", None)
        if callable(compute_fn) and n_obs is not None:
            try:
                return float(compute_fn(n_obs=int(n_obs), lock=None))
            except Exception as exc:  # noqa: BLE001
                logger.debug("_compute_prior_weight fallback failed: %s", exc)
        return None

    @staticmethod
    def _classify_bao_status(bao: Any) -> str:
        if bao is None:
            return "disabled"
        ec = int(getattr(bao, "error_count", 0) or 0)
        return "degraded" if ec >= 3 else "healthy"

    @staticmethod
    def _classify_cae_status(cae_engine: Any) -> str:
        if cae_engine is None:
            return "disabled"
        ec = int(getattr(cae_engine, "error_count", 0) or 0)
        return "degraded" if ec >= 3 else "healthy"

    @staticmethod
    def _classify_pp_status(prior_repo: Any) -> str:
        if prior_repo is None:
            return "disabled"
        return "healthy"

    @staticmethod
    def _classify_emst_status(predictor: Any) -> str:
        if predictor is None:
            return "disabled"
        raw = str(getattr(predictor, "predictor_status", "healthy"))
        if raw == "auto_disabled":
            return "disabled"
        if raw == "degraded":
            return "degraded"
        return "healthy"

    async def _safe_update(
        self, entity_id: str, state: Any, attrs: Dict[str, Any],
    ) -> None:
        try:
            await self._ha.update_state(entity_id, state, attributes=attrs)
            self.stats.publishes += 1
        except Exception as exc:    # noqa: BLE001
            self.stats.failures += 1
            # Demote to debug after the first failure to avoid log spam if
            # HA is briefly down — the user already sees a single warning.
            level = logging.WARNING if self.stats.failures == 1 else logging.DEBUG
            logger.log(
                level, "Failed to update %s in HA: %s", entity_id, exc,
            )
