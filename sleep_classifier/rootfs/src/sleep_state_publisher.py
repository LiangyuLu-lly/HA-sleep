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
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

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
        live_state_stats: Optional[Dict[str, Dict[str, int]]] = None,
    ) -> None:
        """Surface the most recent device action (or 'planned only' in dry-run).

        ``summary`` is something short like ``"light.bedroom_main → off"`` so
        Lovelace can show it on a chip.  The full payload is stored as an
        attribute for users who want to dig in.

        ``skipped_by_capability`` (v1.6.2) is a mapping from
        :class:`src.device_capabilities.Capability` value strings to the
        count of actions the controller declined to issue because the
        bound entity didn't advertise that feature.

        ``live_state_stats`` (v1.7.1) is the dict returned by
        :meth:`src.live_state_cache.LiveStateCache.stats`: three sub-dicts
        keyed by entity_id showing how many times the controller
        skipped a dispatch because the device was unavailable,
        respected a user override, or injected an auto-turn-on.
        Exposing it on the diagnostics sensor lets users see, on
        their Lovelace dashboard, why the system is sometimes
        deliberately silent — "light.bedroom was in user override
        3 times today" is a much clearer signal than "no actions".
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
        if live_state_stats:
            # Only surface sub-dicts that have any entries, to keep
            # the attribute panel uncluttered on healthy installs.
            for key in (
                "skipped_unavailable",
                "skipped_user_override",
                "auto_turn_on_injected",
            ):
                value = live_state_stats.get(key)
                if value:
                    attrs[key] = value
        truncated = (summary or "—")[:255]
        await self._safe_update(ENTITY_LAST_ACTION, truncated, attrs)

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
            ENTITY_LAST_ACTION, "—", _STATIC_ATTRS_LAST_ACTION,
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
