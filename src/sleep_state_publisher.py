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

    async def publish_last_action(
        self, summary: str, *, executed: bool,
    ) -> None:
        """Surface the most recent device action (or 'planned only' in dry-run).

        ``summary`` is something short like ``"light.bedroom_main → off"`` so
        Lovelace can show it on a chip.  The full payload is stored as an
        attribute for users who want to dig in.
        """
        attrs = dict(_STATIC_ATTRS_LAST_ACTION)
        attrs["executed"] = bool(executed)
        truncated = (summary or "—")[:255]
        await self._safe_update(ENTITY_LAST_ACTION, truncated, attrs)

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
