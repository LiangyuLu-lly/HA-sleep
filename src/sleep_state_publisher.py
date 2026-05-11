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
