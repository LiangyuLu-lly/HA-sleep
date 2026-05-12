"""Stage-aware controller that drives Home Assistant devices directly.

This is the **adaptive control** half of the add-on's value: pick the
right bedroom environment for *the user's current sleep stage*, diff it
against the current room state, then call the matching HA services
through :class:`src.ha_api_client.HomeAssistantClient`.

Phases of regulation
--------------------
The controller doesn't just learn one "ideal" env and apply it
constantly — that would defeat the purpose of having a stage signal in
the first place.  Instead it composes two layers:

1. A **personalised baseline** from
   :class:`src.preference_learner.PreferenceLearner` (when there is
   enough history) — answers the question *"what room conditions did
   you sleep best in?"*.
2. **Stage-relative deltas** (the ``_STAGE_DELTAS`` table below) —
   answers *"how should those conditions be modulated across the
   night?"*: warmer + brighter pre-sleep, cooler + dark during DEEP,
   gentle bedside lamp at the wake window.

So if the learner discovers you sleep best at 20 °C (slightly cooler
than the 21 °C default), the controller will aim for ~22 °C while you
wind down, ~20 °C during LIGHT, ~18 °C during DEEP — preserving the
medically-motivated stage variation while shifting the *midpoint*
toward your personal preference.

A configurable **deadband** prevents the controller from flapping
(e.g. don't bump the HVAC for a 0.1 °C change), and an explore knob
lets the learner probe nearby setpoints after a poor night.

Closing the loop
----------------
The controller exposes :meth:`feedback_score` so the orchestrator (the
service main loop) can report how good the *last* stretch of sleep was; the
controller uses that to decide whether to lean on personalisation (when the
recent feedback is good) or to invite the learner to explore (when bad).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.data_structures import SleepStage
from src.device_discovery import ActionableDevices
from src.ha_api_client import HomeAssistantClient
from src.preference_learner import (
    EnvironmentParams,
    PreferenceLearner,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default targets per stage (used as fallback before any learning has happened)
# ---------------------------------------------------------------------------


_DEFAULT_TARGETS: Dict[SleepStage, EnvironmentParams] = {
    SleepStage.AWAKE: EnvironmentParams(
        temperature_c=23.0, humidity_pct=50.0, brightness_pct=40.0, fan_speed_pct=20.0,
    ),
    SleepStage.LIGHT: EnvironmentParams(
        temperature_c=21.0, humidity_pct=55.0, brightness_pct=8.0,  fan_speed_pct=15.0,
    ),
    SleepStage.DEEP: EnvironmentParams(
        temperature_c=19.0, humidity_pct=55.0, brightness_pct=0.0,  fan_speed_pct=10.0,
    ),
    SleepStage.REM: EnvironmentParams(
        temperature_c=19.5, humidity_pct=55.0, brightness_pct=0.0,  fan_speed_pct=10.0,
    ),
}


# ---------------------------------------------------------------------------
# Per-stage deltas applied on top of the *learned* baseline (since v1.3.1)
# ---------------------------------------------------------------------------
#
# The deltas below are derived from ``_DEFAULT_TARGETS`` by subtracting
# the LIGHT row from each other row.  LIGHT is the reference because it
# is the longest stage of a healthy night, so we treat the learner's
# recommendation as "your LIGHT-stage preference" and modulate around it.
#
# Why deltas rather than per-stage learning?
#   * A recorded session contains a mix of all stages — we observe one
#     env per night, not one per stage.  So the learner cannot directly
#     learn "your DEEP temperature" without first attributing the
#     in-night env trace to each stage, which we don't store yet.
#   * The clinical literature is consistent on the *direction* of stage
#     variation (T drops into DEEP, brightness drops with sleep onset,
#     etc.) even if individual midpoints vary.  Locking the deltas to
#     the clinical consensus gives us a safe-by-default policy that
#     still personalises the midpoint.
#
# Future work (v1.4): also learn the deltas from per-session
# stage-segmented env traces.  Tracked in docs/BACKLOG.md.
_STAGE_DELTAS: Dict[SleepStage, EnvironmentParams] = {
    SleepStage.AWAKE: EnvironmentParams(
        temperature_c=+2.0, humidity_pct=-5.0,
        brightness_pct=+32.0, fan_speed_pct=+5.0,
    ),
    SleepStage.LIGHT: EnvironmentParams(
        temperature_c=0.0, humidity_pct=0.0,
        brightness_pct=0.0, fan_speed_pct=0.0,
    ),
    SleepStage.DEEP: EnvironmentParams(
        temperature_c=-2.0, humidity_pct=0.0,
        brightness_pct=-8.0, fan_speed_pct=-5.0,
    ),
    SleepStage.REM: EnvironmentParams(
        temperature_c=-1.5, humidity_pct=0.0,
        brightness_pct=-8.0, fan_speed_pct=-5.0,
    ),
}


# ---------------------------------------------------------------------------
# Anticipatory control (v1.4.0)
# ---------------------------------------------------------------------------
#
# Real-world problem: ``climate.set_temperature(19)`` does not cool the
# bedroom to 19 °C instantly.  Typical actuator response times:
#
#   * lights      ≈ 0 s        (LED dim is electrical)
#   * fans        ≈ 0 s        (mechanical, but air mixes in seconds)
#   * humidifiers ≈ 300 s      (5 min to noticeably shift RH)
#   * climate     ≈ 900 s      (15 min for a split AC to drop 2 °C in
#                               a closed bedroom; longer in summer)
#
# If we wait until the user enters DEEP before lowering the climate
# target, the room is still ~21 °C halfway through DEEP — exactly when
# the literature says we should already be at 19 °C.  We have to *lead*
# the user.
#
# Implementation: each actuator's target is blended with the *next*
# stage's target proportional to ``actuator_latency / typical_stage_duration``.
#
# Typical stage duration of 30 min (1800 s) is the median dwell time
# we see in our PSG data + commercial wearable hypnograms.  A bigger
# number under-anticipates (climate lags behind reality); smaller
# over-anticipates (room is already cold during AWAKE wind-down).
_TYPICAL_STAGE_DURATION_S: float = 1800.0     # 30 min

_ACTUATOR_LATENCY_S: Dict[str, float] = {
    "climate":    900.0,        # 15 min
    "humidifier": 300.0,        # 5 min
    "fan":          0.0,        # instant
    "light":        0.0,        # instant
}

# ``_NEXT_STAGE`` encodes the most likely next stage given the current
# one.  Sleep architecture is non-deterministic (DEEP can return to
# LIGHT, REM can briefly visit AWAKE) but these defaults match the
# canonical NREM cycle and keep the anticipation logic predictable.
_NEXT_STAGE: Dict[SleepStage, SleepStage] = {
    SleepStage.AWAKE: SleepStage.LIGHT,    # AWAKE → fall-asleep → LIGHT
    SleepStage.LIGHT: SleepStage.DEEP,     # NREM 1/2 → SWS
    SleepStage.DEEP:  SleepStage.REM,      # SWS → REM (or back to LIGHT)
    SleepStage.REM:   SleepStage.LIGHT,    # REM → cycle back into LIGHT
}


# Safe clamp ranges so a runaway delta + outlier baseline can't push
# a device to a dangerous or simply-broken setpoint (e.g. negative
# brightness, 5 °C in summer, 100 % humidity in a Pi-controlled
# bedroom).  Picked conservatively; widen via config if you have a
# medical reason to.
_SAFE_RANGES = {
    "temperature_c": (16.0, 28.0),
    "humidity_pct": (30.0, 70.0),
    "brightness_pct": (0.0, 100.0),
    "fan_speed_pct": (0.0, 100.0),
}


def _clamp(value: Optional[float], lo: float, hi: float) -> Optional[float]:
    """Clamp ``value`` into ``[lo, hi]``; pass through ``None`` unchanged."""
    if value is None:
        return None
    return max(lo, min(hi, float(value)))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class SmartControlConfig:
    enabled: bool = True
    min_seconds_between_actions: float = 120.0
    deadband_temperature_c: float = 0.5
    deadband_humidity_pct: float = 5.0
    deadband_brightness_pct: float = 10.0
    dry_run: bool = False
    # v1.4.0 — minutes before the learned bedtime to start treating the
    # user as already in the LIGHT stage for *environment* control
    # purposes.  This makes the AC start pre-cooling toward sleep
    # setpoints while the user is still winding down on the couch.  The
    # stage *sensor* in HA still reflects the truthful AWAKE — only
    # the controller substitutes.  Set to 0 to disable.
    wind_down_minutes: int = 30
    # v1.4.0 — debounce window for the ExternalStageSubscriber.  Lives
    # on this config so it can be tuned from the add-on Configuration
    # form alongside the rest of the smart-control behaviour, even
    # though the orchestrator routes it to the subscriber rather than
    # the controller.  A new candidate stage must hold for this many
    # seconds before the subscriber's ``current()`` reports it.  Set
    # to 0 to disable (controller acts on every observation).
    min_stage_dwell_seconds: float = 60.0

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SmartControlConfig":
        valid = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in valid})


# ---------------------------------------------------------------------------
# Wind-down helper (v1.4.0)
# ---------------------------------------------------------------------------


def is_in_wind_down(
    now: "datetime.datetime",                # noqa: F821 - forward-ref for circular import
    bedtime_recommendation: Dict[str, Any],
    wind_down_minutes: int,
) -> bool:
    """Return True iff ``now`` is within ``wind_down_minutes`` of bedtime.

    Inputs:
        now: a local-time naive ``datetime`` (use :func:`src._time_utils.now_local`).
        bedtime_recommendation: payload from
            :meth:`src.preference_learner.PreferenceLearner.recommend_bedtime`.
            Specifically reads the ``"next_bedtime"`` field as ``"HH:MM"``.
        wind_down_minutes: window length in minutes.  ``0`` disables.

    Why this lives in the controller module:
        wind-down is *the* trigger that decides whether the controller
        should treat the user as already-in-LIGHT.  Keeping the function
        next to ``SmartControlConfig`` lets the orchestrator stay thin.

    Edge cases:
        * If ``recommend_bedtime`` couldn't fill the bucket yet
          (``next_bedtime is None``) we return False — no wind-down
          until we have enough history to predict a bedtime.
        * If the bedtime is e.g. 00:30 and now is 23:55, the wrap-around
          is handled by computing the *signed* minute distance modulo
          24 h and treating only forward-in-time differences as
          "approaching bedtime".
    """
    if wind_down_minutes <= 0:
        return False
    raw = bedtime_recommendation.get("next_bedtime")
    if not raw:
        return False
    try:
        hh, mm = (int(s) for s in str(raw).split(":", 1))
    except (ValueError, AttributeError):
        return False

    bedtime_minutes_of_day = hh * 60 + mm
    now_minutes_of_day = now.hour * 60 + now.minute
    # Forward distance to bedtime modulo a day (in minutes).  E.g. if
    # now is 23:55 and bedtime is 00:30, distance is 35 min — within
    # the window.  If bedtime was 12 h ago, distance is 12 h — outside.
    delta = (bedtime_minutes_of_day - now_minutes_of_day) % (24 * 60)
    return 0 <= delta <= wind_down_minutes


@dataclass
class ControlAction:
    """A single planned HA service call (for logging / testing)."""

    domain: str
    service: str
    entity_id: str
    data: Dict[str, Any]
    reason: str = ""

    def describe(self) -> str:
        kvs = ", ".join(f"{k}={v}" for k, v in self.data.items() if k != "entity_id")
        return f"{self.domain}.{self.service}({self.entity_id}{', ' + kvs if kvs else ''})"


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class SmartEnvironmentController:
    """Pull current state from HA, decide target, push commands back."""

    def __init__(
        self,
        config: SmartControlConfig,
        ha_client: HomeAssistantClient,
        devices: ActionableDevices,
        learner: Optional[PreferenceLearner] = None,
    ) -> None:
        self.config = config
        self.ha = ha_client
        self.devices = devices
        self.learner = learner

        # Bookkeeping for rate limiting and feedback signal
        self._last_action_ts: Dict[str, float] = {}
        self._actions_log: List[ControlAction] = []
        self._recent_quality: Optional[float] = None
        self._current_targets: Dict[SleepStage, EnvironmentParams] = dict(
            _DEFAULT_TARGETS
        )

    # ------------------------------------------------------------------ #
    # Target selection                                                   #
    # ------------------------------------------------------------------ #

    def feedback_score(self, score: float) -> None:
        """Report the *most recent* sleep-quality score (0-100).

        Bad scores enable exploration in the learner.
        """
        self._recent_quality = float(score)

    def _should_explore(self) -> bool:
        if self._recent_quality is None:
            return False
        return self._recent_quality < 50.0   # below median → try something new

    def _baseline(self) -> EnvironmentParams:
        """The learner's "midpoint" recommendation (LIGHT-stage preference).

        Returns the LIGHT defaults if the learner is absent or has no
        history yet.  Exploration is opt-in via :meth:`_should_explore`.
        """
        defaults = _DEFAULT_TARGETS[SleepStage.LIGHT]
        if self.learner is None:
            return defaults
        return self.learner.recommend(
            defaults, explore=self._should_explore(),
        )

    @staticmethod
    def _compose(
        baseline: EnvironmentParams,
        stage: SleepStage,
        fallback_stage: SleepStage,
    ) -> EnvironmentParams:
        """``baseline + _STAGE_DELTAS[stage]`` with safe-range clamps.

        ``fallback_stage`` is consulted for any baseline field that is
        ``None`` (e.g. learner had no humidity history yet).
        """
        delta = _STAGE_DELTAS[stage]

        def _shift(
            base: Optional[float], d: Optional[float], field: str,
        ) -> Optional[float]:
            if base is None:
                fb = getattr(_DEFAULT_TARGETS[fallback_stage], field)
                return _clamp(fb, *_SAFE_RANGES[field])
            shifted = float(base) + float(d or 0.0)
            return _clamp(shifted, *_SAFE_RANGES[field])

        return EnvironmentParams(
            temperature_c=_shift(
                baseline.temperature_c, delta.temperature_c, "temperature_c",
            ),
            humidity_pct=_shift(
                baseline.humidity_pct, delta.humidity_pct, "humidity_pct",
            ),
            brightness_pct=_shift(
                baseline.brightness_pct, delta.brightness_pct, "brightness_pct",
            ),
            fan_speed_pct=_shift(
                baseline.fan_speed_pct, delta.fan_speed_pct, "fan_speed_pct",
            ),
        )

    def target_for(self, stage: SleepStage) -> EnvironmentParams:
        """Return the *current-stage* environment target.

        Composition (v1.3.1):

        1. **Baseline** — :meth:`_baseline` returns the env that
           historically correlated with the user's best sleep, falling
           back to LIGHT-stage defaults when there isn't enough history
           yet.
        2. **Stage delta** — :data:`_STAGE_DELTAS[stage]` is added on
           top, so AWAKE comes out warmer / brighter and DEEP comes out
           cooler / dark relative to that baseline.
        3. **Safe clamp** — every field is clipped into the conservative
           range in :data:`_SAFE_RANGES`.

        Note: this is the *non-anticipatory* target.  For actual device
        actuation we use :meth:`target_for_actuator` which leads the
        user by the actuator's response time so the room is at the right
        setpoint by the time the stage transition completes.
        """
        return self._compose(self._baseline(), stage, fallback_stage=stage)

    def target_for_actuator(
        self,
        stage: SleepStage,
        domain: str,
    ) -> EnvironmentParams:
        """Return the target *for a given actuator domain*, anticipating ahead.

        The blend factor is
        ``min(0.6, actuator_latency / typical_stage_duration)`` —
        capped at 0.6 so anticipation can never fully override the
        current stage's setpoint (which would defeat the
        stage-aware design and confuse the user).

        Example: at 30-min typical stage duration with a 15-min climate
        latency, the climate target = 0.5 * current + 0.5 * next.  An
        AC therefore starts cooling toward the DEEP setpoint as soon as
        the user enters LIGHT, reaching the DEEP target ~15 min later —
        exactly when the user actually transitions.  Lights (latency 0)
        get pure current-stage targets so they don't pre-dim.
        """
        baseline = self._baseline()
        current = self._compose(baseline, stage, fallback_stage=stage)
        latency = _ACTUATOR_LATENCY_S.get(domain, 0.0)
        if latency <= 0.0:
            return current
        next_stage = _NEXT_STAGE[stage]
        future = self._compose(baseline, next_stage, fallback_stage=stage)
        # Cap at 0.6 to keep stage variation visible even for high-latency
        # devices.  Otherwise a 30-min-latency under-floor heating system
        # would render the controller's stage signal meaningless.
        alpha = min(0.6, latency / _TYPICAL_STAGE_DURATION_S)

        def _blend(c: Optional[float], n: Optional[float]) -> Optional[float]:
            if c is None and n is None:
                return None
            if c is None:
                return n
            if n is None:
                return c
            return (1.0 - alpha) * float(c) + alpha * float(n)

        return EnvironmentParams(
            temperature_c=_blend(current.temperature_c, future.temperature_c),
            humidity_pct=_blend(current.humidity_pct, future.humidity_pct),
            brightness_pct=_blend(current.brightness_pct, future.brightness_pct),
            fan_speed_pct=_blend(current.fan_speed_pct, future.fan_speed_pct),
        )

    # ------------------------------------------------------------------ #
    # Planning                                                           #
    # ------------------------------------------------------------------ #

    def plan_actions(
        self,
        stage: SleepStage,
        current_env: EnvironmentParams,
    ) -> List[ControlAction]:
        """Compute the list of HA service calls to issue for the new stage.

        Args:
            stage: the freshly classified sleep stage.
            current_env: temperature / humidity / brightness as last reported
                by HA.  Fields that are ``None`` are treated as unknown and
                always trigger an action.

        Returns:
            List of :class:`ControlAction` objects.  Empty when no change is
            needed or when the rate-limit is still active.
        """
        if not self.config.enabled:
            return []

        actions: List[ControlAction] = []
        now = time.time()

        # Each actuator domain gets its *own* target so high-latency
        # devices (climate, humidifier) can anticipate the next stage
        # while instantaneous ones (light, fan) act on the current
        # stage only.  See :meth:`target_for_actuator`.
        climate_target = self.target_for_actuator(stage, "climate")
        humidifier_target = self.target_for_actuator(stage, "humidifier")
        light_target = self.target_for_actuator(stage, "light")
        fan_target = self.target_for_actuator(stage, "fan")

        # --- Temperature: climate.set_temperature ------------------------
        if climate_target.temperature_c is not None and self.devices.climates:
            outside_dead = (
                current_env.temperature_c is None
                or abs(current_env.temperature_c - climate_target.temperature_c)
                > self.config.deadband_temperature_c
            )
            if outside_dead and self._allow_action("climate", now):
                for c in self.devices.climates:
                    actions.append(
                        ControlAction(
                            domain="climate",
                            service="set_temperature",
                            entity_id=c.entity_id,
                            data={"temperature": round(climate_target.temperature_c, 1)},
                            reason=(
                                f"stage={stage.name} anticipating={_NEXT_STAGE[stage].name} "
                                f"curr={current_env.temperature_c}"
                            ),
                        )
                    )

        # --- Humidity: humidifier.set_humidity ---------------------------
        if humidifier_target.humidity_pct is not None and self.devices.humidifiers:
            outside_dead = (
                current_env.humidity_pct is None
                or abs(current_env.humidity_pct - humidifier_target.humidity_pct)
                > self.config.deadband_humidity_pct
            )
            if outside_dead and self._allow_action("humidifier", now):
                for h in self.devices.humidifiers:
                    actions.append(
                        ControlAction(
                            domain="humidifier",
                            service="set_humidity",
                            entity_id=h.entity_id,
                            data={"humidity": int(round(humidifier_target.humidity_pct))},
                            reason=(
                                f"stage={stage.name} anticipating={_NEXT_STAGE[stage].name} "
                                f"curr={current_env.humidity_pct}"
                            ),
                        )
                    )

        # --- Brightness: light.turn_on / turn_off ------------------------
        # Lights have zero latency → no anticipation; they switch crisply
        # at the stage boundary.
        if light_target.brightness_pct is not None and self.devices.lights:
            outside_dead = (
                current_env.brightness_pct is None
                or abs(current_env.brightness_pct - light_target.brightness_pct)
                > self.config.deadband_brightness_pct
            )
            if outside_dead and self._allow_action("light", now):
                for light in self.devices.lights:
                    if light_target.brightness_pct <= 0.5:
                        actions.append(
                            ControlAction(
                                domain="light",
                                service="turn_off",
                                entity_id=light.entity_id,
                                data={},
                                reason=f"stage={stage.name} → lights off",
                            )
                        )
                    else:
                        # Use warm kelvin for sleep stages so the user isn't
                        # blasted with daylight white.
                        data: Dict[str, Any] = {
                            "brightness_pct": int(round(light_target.brightness_pct)),
                        }
                        if stage in {SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM}:
                            data["kelvin"] = 2200
                        actions.append(
                            ControlAction(
                                domain="light",
                                service="turn_on",
                                entity_id=light.entity_id,
                                data=data,
                                reason=f"stage={stage.name} target={light_target.brightness_pct}%",
                            )
                        )

        # --- Fan: fan.set_percentage -------------------------------------
        if fan_target.fan_speed_pct is not None and self.devices.fans:
            if self._allow_action("fan", now):
                for f in self.devices.fans:
                    if fan_target.fan_speed_pct <= 1.0:
                        actions.append(
                            ControlAction(
                                domain="fan",
                                service="turn_off",
                                entity_id=f.entity_id,
                                data={},
                                reason=f"stage={stage.name} → fan off",
                            )
                        )
                    else:
                        actions.append(
                            ControlAction(
                                domain="fan",
                                service="set_percentage",
                                entity_id=f.entity_id,
                                data={"percentage": int(round(fan_target.fan_speed_pct))},
                                reason=f"stage={stage.name} target={fan_target.fan_speed_pct}%",
                            )
                        )

        return actions

    def _allow_action(self, domain: str, now: float) -> bool:
        """Return True if we have waited long enough since the last action."""
        last = self._last_action_ts.get(domain, 0.0)
        return now - last >= self.config.min_seconds_between_actions

    # ------------------------------------------------------------------ #
    # Execution                                                          #
    # ------------------------------------------------------------------ #

    async def apply(
        self,
        stage: SleepStage,
        current_env: EnvironmentParams,
    ) -> List[ControlAction]:
        """Plan + execute the actions for ``stage``.

        Returns the list of actions that were planned (whether actually
        executed depends on ``dry_run``).
        """
        actions = self.plan_actions(stage, current_env)
        if not actions:
            return []

        for action in actions:
            self._actions_log.append(action)
            if self.config.dry_run:
                logger.info("[dry-run] would call %s  // %s",
                            action.describe(), action.reason)
                continue
            try:
                await self.ha.call_service(
                    action.domain,
                    action.service,
                    entity_id=action.entity_id,
                    **action.data,
                )
                self._last_action_ts[action.domain] = time.time()
                logger.info("Executed %s  // %s",
                            action.describe(), action.reason)
            except Exception as exc:    # noqa: BLE001
                logger.error(
                    "Service call failed (%s): %s",
                    action.describe(), exc,
                )

        return actions

    @property
    def action_history(self) -> List[ControlAction]:
        return list(self._actions_log)


__all__ = [
    "SmartControlConfig",
    "SmartEnvironmentController",
    "ControlAction",
    "is_in_wind_down",
]
