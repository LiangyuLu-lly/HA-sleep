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
from src.device_capabilities import Capability, capabilities_of, is_available
from src.device_discovery import ActionableDevices
from src.ha_api_client import HomeAssistantClient
from src.live_state_cache import LiveStateCache
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
# v1.5.0 update: the table below is now a *fallback*.  When the
# preference learner has accumulated enough per-stage history
# (effective sample size ≥ 4), each field is overridden by the
# learned value via :meth:`SmartEnvironmentController._merged_delta`.
# This lets a heavy-duvet user who reliably sleeps best at a flat
# 19 °C across all stages replace the population-average -2 °C DEEP
# delta with their actual ~0 °C delta — without giving up the safe
# defaults during the first weeks of usage.
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


# ---------------------------------------------------------------------------
# Futile-retry suppression (v1.6.4)
# ---------------------------------------------------------------------------
#
# After issuing N consecutive ``set_temperature=19`` calls against the
# same climate entity, if the room temperature hasn't noticeably moved
# we conclude the device is already saturated (e.g. an AC at max cooling
# but outside is 35 °C, or a windowless tiny humidifier trying to hit
# 55 % in a dry winter).  Continuing to hammer the service at every
# tick wastes network + wears out the HA state write path.  Instead
# we pause same-setpoint retries for this entity until the next stage
# transition re-motivates them.
#
# The thresholds below are per-field; a degree Celsius is a much
# bigger signal than a % brightness.

_FUTILE_STREAK_THRESHOLD: int = 3   # retries before we mark saturated

# Minimum env-reading delta we expect to see BETWEEN two consecutive
# same-setpoint actions for it to count as "actually working".
_FUTILE_MIN_EFFECTIVE_DELTA: Dict[str, float] = {
    "temperature_c": 0.3,     # noise floor of most HA temp sensors
    "humidity_pct": 1.5,
    "brightness_pct": 2.0,
    # fan_speed_pct — we don't track; fans either do or don't run,
    # no "environment feedback" loop to check.
}

# Minimum elapsed time between two samples for the delta comparison
# to be meaningful.  Shorter than this and we can't tell whether the
# device just hadn't had time to act.  Matches the longest actuator
# latency in _ACTUATOR_LATENCY_S so climate isn't unfairly marked as
# saturated 60 s after the first attempt.
_FUTILE_MIN_SETTLE_SECONDS: float = 900.0   # 15 min


def _clamp(value: Optional[float], lo: float, hi: float) -> Optional[float]:
    """Clamp ``value`` into ``[lo, hi]``; pass through ``None`` unchanged."""
    if value is None:
        return None
    return max(lo, min(hi, float(value)))


def _fan_percent_to_preset(pct: float) -> str:
    """Quantise a 0-100 % fan target to HA's default preset-mode vocabulary.

    HA fans that expose ``preset_modes`` instead of continuous speed
    control (common pattern for Sonoff iFan04 / cheap BLE fans) almost
    always advertise ``low / medium / high`` — that's the tuple the
    HA fan helper uses as the default.  Cut-offs below are
    deliberately asymmetric: we prefer ``low`` in the soft middle so
    an over-cautious learner doesn't blast the user with ``high``.
    """
    p = max(0.0, min(100.0, float(pct)))
    if p < 33.0:
        return "low"
    if p < 66.0:
        return "medium"
    return "high"


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
        live_state: Optional[LiveStateCache] = None,
    ) -> None:
        self.config = config
        self.ha = ha_client
        self.devices = devices
        self.learner = learner
        # v1.7.1 — per-entity live state.  If the caller doesn't
        # provide one, we construct an empty cache so the guards
        # below still work (they'll just never trigger without
        # upstream state push, matching pre-v1.7.1 behaviour).
        self.live_state = live_state or LiveStateCache()

        # Bookkeeping for rate limiting and feedback signal
        self._last_action_ts: Dict[str, float] = {}
        self._actions_log: List[ControlAction] = []
        self._recent_quality: Optional[float] = None
        self._current_targets: Dict[SleepStage, EnvironmentParams] = dict(
            _DEFAULT_TARGETS
        )
        # v1.6.4 — futile-retry suppression.  When we tell an AC
        # ``set_temperature=19`` but the room stays at 26 °C because
        # it's 35 °C outside and the AC is already at max output,
        # there's no point re-sending the same setpoint every
        # ``min_seconds_between_actions``.  Record (for each
        # per-entity action):
        #   * the last N target values,
        #   * the room env reading at that time,
        #   * when the next stage transition happens (so we reset).
        # If N consecutive attempts produce no meaningful change in
        # the relevant environment field, mark the entity "saturated"
        # and skip further setpoint pushes until a stage change.
        self._futile_history: Dict[
            str,    # "<domain>.<entity_id>"
            List[tuple[float, float, float]],   # [(target, observed_env, ts), ...]
        ] = {}
        self._saturated_entities: set[str] = set()
        self._stage_when_saturation_recorded: Optional[SleepStage] = None
        # v1.5.0 — learned per-stage delta cache.  Re-fetched at most
        # once every `_LEARNED_DELTA_TTL` seconds to amortise the
        # weighted-median computation across the high-frequency
        # plan_actions() calls (every ~30 s in production).
        self._learned_deltas_cache: Optional[Dict[str, Dict[str, Any]]] = None
        self._learned_deltas_cached_at: float = 0.0

        # v1.6.1 — capability gating.  HA happily accepts
        # ``climate.set_temperature`` against a climate entity whose
        # ``supported_features`` bitmask does *not* include
        # ``TARGET_TEMPERATURE``: the service exists on the domain, so
        # the REST call returns 200, but the device never moves.  Users
        # then see "Executed climate.set_temperature" in the last_action
        # sensor and wrongly conclude the loop is working.  To close
        # this hole we pre-compute each bound entity's capability set
        # once at construction and gate every action branch on it.  The
        # inspection is pure / cheap (see :func:`capabilities_of`), so
        # caching here just makes the ``plan_actions`` path branch-free.
        self._caps_by_id: Dict[str, set[Capability]] = {}
        for bucket in (
            self.devices.lights, self.devices.climates, self.devices.fans,
            self.devices.humidifiers, self.devices.switches,
            self.devices.media_players,
        ):
            for ent in bucket:
                self._caps_by_id[ent.entity_id] = capabilities_of(ent)
        # Remember which (entity_id, capability) pairs we've already
        # logged a warning for, so a 30 s inference loop doesn't flood
        # the log with the same "climate.xyz does not support
        # SET_TEMPERATURE" message every tick.
        self._missing_cap_warned: set[tuple[str, Capability]] = set()
        # Counter of actions that were *skipped* because the device
        # lacked the required capability.  Surfaced in
        # :meth:`capability_stats` so a diagnostic sensor can show the
        # user "we wanted to adjust your AC but it doesn't support
        # temperature control".
        self._skipped_by_cap: Dict[str, int] = {}

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

    # Cache TTL for the learner's per-stage-delta computation.  120 s
    # is well under our typical 30 s inference cadence × 4 stages, so
    # the same SleepSession history is queried at most once per ~minute
    # of wallclock — cheap enough.
    _LEARNED_DELTA_TTL: float = 120.0

    def _learned_deltas(self) -> Dict[str, Dict[str, Any]]:
        """Cached view of :meth:`PreferenceLearner.recommend_per_stage_deltas`.

        Returns an empty dict when the learner is absent or hasn't
        accumulated enough history yet; callers must treat that as
        "no override, use the clinical default".
        """
        if self.learner is None:
            return {}
        now = time.time()
        if (
            self._learned_deltas_cache is not None
            and now - self._learned_deltas_cached_at < self._LEARNED_DELTA_TTL
        ):
            return self._learned_deltas_cache
        try:
            fresh = self.learner.recommend_per_stage_deltas(now=now)
        except AttributeError:
            # Older learner without the v1.5.0 method — degrade
            # gracefully to clinical-only behaviour.
            fresh = {}
        except Exception:    # noqa: BLE001
            # Corrupted history, IO error, whatever: never let a
            # learner failure break the control loop.
            fresh = {}
        self._learned_deltas_cache = fresh
        self._learned_deltas_cached_at = now
        return fresh

    def _merged_delta(self, stage: SleepStage) -> EnvironmentParams:
        """Per-stage delta to apply on top of the LIGHT-stage baseline.

        Each *field* is independently sourced:

        * If the learner has ≥ ``_MIN_ESS_FOR_DELTA`` effective samples
          for this stage *and* a non-None value for the field, use it.
        * Otherwise fall back to the clinical default in
          :data:`_STAGE_DELTAS`.

        This per-field merge (vs. all-or-nothing per stage) means a
        user who has 30 nights of temperature data but only 2 nights
        of brightness data still gets a personalised temperature
        delta while keeping the safe brightness clinical default.
        """
        clinical = _STAGE_DELTAS[stage]
        learned_all = self._learned_deltas()
        learned = learned_all.get(stage.name) if learned_all else None
        if not learned:
            return clinical

        def _pick(field: str) -> Optional[float]:
            lv = learned.get(field)
            if lv is None:
                return getattr(clinical, field)
            return float(lv)

        return EnvironmentParams(
            temperature_c=_pick("temperature_c"),
            humidity_pct=_pick("humidity_pct"),
            brightness_pct=_pick("brightness_pct"),
            fan_speed_pct=_pick("fan_speed_pct"),
        )

    def _compose(
        self,
        baseline: EnvironmentParams,
        stage: SleepStage,
        fallback_stage: SleepStage,
    ) -> EnvironmentParams:
        """``baseline + delta[stage]`` with safe-range clamps.

        v1.5.0: ``delta`` is now the per-stage *learned* delta (where
        available) merged with the clinical fallback in
        :data:`_STAGE_DELTAS` on a per-field basis via
        :meth:`_merged_delta`.  This used to be a ``@staticmethod`` —
        promoted to an instance method so it can access ``self.learner``.

        ``fallback_stage`` is consulted for any baseline field that is
        ``None`` (e.g. learner had no humidity history yet).
        """
        delta = self._merged_delta(stage)

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

        # v1.6.4 — whenever the effective-control stage changes, reset
        # saturation tracking so a newly-minted setpoint gets a fresh
        # attempt rather than inheriting the previous stage's "this
        # entity is hopeless" verdict.
        self._reset_futility_tracking_on_stage_change(stage)

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
                    if not self._device_supports(c, Capability.SET_TEMPERATURE):
                        # This climate entity is on/off-only or preset-only;
                        # skipping is strictly better than faking success.
                        continue
                    if not self._liveness_guard(c):
                        continue
                    # v1.7.1 — skip if previous attempts haven't moved
                    # the room.  The controller will try again after
                    # the next stage transition.
                    if self._is_entity_saturated(
                        "climate", c.entity_id,
                        target_value=climate_target.temperature_c,
                        current_env_value=current_env.temperature_c,
                        now=now,
                    ):
                        continue
                    # v1.7.1 — if the AC is currently off, inject a
                    # climate.set_hvac_mode=auto (or cool) before the
                    # setpoint so HA actually turns the unit on.  We
                    # can't know whether the user wants heat vs cool
                    # without polling the weather, so we pick the
                    # stage-temperature vs current-temperature delta:
                    # colder target than current → "cool", warmer →
                    # "heat".  Same-temperature falls back to "auto"
                    # which most ACs accept and which lets the unit
                    # decide.
                    if self.live_state.is_off(c.entity_id):
                        mode = self._climate_mode_for_target(
                            climate_target.temperature_c,
                            current_env.temperature_c,
                        )
                        actions.append(ControlAction(
                            domain="climate",
                            service="set_hvac_mode",
                            entity_id=c.entity_id,
                            data={"hvac_mode": mode},
                            reason=(
                                f"stage={stage.name} climate is off; "
                                f"turning on in {mode} mode before setpoint"
                            ),
                        ))
                        self.live_state.count_auto_turn_on(c.entity_id)
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
                    if not self._device_supports(h, Capability.SET_HUMIDITY):
                        continue
                    if not self._liveness_guard(h):
                        continue
                    if self._is_entity_saturated(
                        "humidifier", h.entity_id,
                        target_value=humidifier_target.humidity_pct,
                        current_env_value=current_env.humidity_pct,
                        now=now,
                    ):
                        continue
                    # v1.7.1 — humidifier off → turn_on before set_humidity.
                    # Unlike climate there's no "mode" question; the
                    # humidifier just runs until target is reached.
                    if self.live_state.is_off(h.entity_id):
                        actions.append(ControlAction(
                            domain="humidifier",
                            service="turn_on",
                            entity_id=h.entity_id,
                            data={},
                            reason=(
                                f"stage={stage.name} humidifier is off; "
                                f"turning on before setpoint"
                            ),
                        ))
                        self.live_state.count_auto_turn_on(h.entity_id)
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
                    if not self._liveness_guard(light):
                        continue
                    if light_target.brightness_pct <= 0.5:
                        # turn_off is universal — every HA light entity
                        # answers it — so no capability gate needed.
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
                        # brightness_pct needs SET_BRIGHTNESS.  If the
                        # bulb is on/off-only (Capability.SET_BRIGHTNESS
                        # absent) we degrade to a plain turn_on so the
                        # user at least gets light-on/light-off
                        # behaviour rather than nothing.
                        data: Dict[str, Any] = {}
                        if self._device_supports(light, Capability.SET_BRIGHTNESS):
                            data["brightness_pct"] = int(round(light_target.brightness_pct))
                        # Warm kelvin is a nice-to-have during sleep
                        # stages; silently drop if the bulb can't do
                        # color temp (common for cheap bulbs).
                        if (
                            stage in {SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM}
                            and light.entity_id in self._caps_by_id
                            and Capability.SET_COLOR_TEMP in self._caps_by_id[light.entity_id]
                        ):
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
                    if not self._liveness_guard(f):
                        continue
                    if fan_target.fan_speed_pct <= 1.0:
                        # turn_off is universal — skip the capability gate.
                        actions.append(
                            ControlAction(
                                domain="fan",
                                service="turn_off",
                                entity_id=f.entity_id,
                                data={},
                                reason=f"stage={stage.name} → fan off",
                            )
                        )
                    elif self._device_supports(f, Capability.SET_SPEED_PCT):
                        actions.append(
                            ControlAction(
                                domain="fan",
                                service="set_percentage",
                                entity_id=f.entity_id,
                                data={"percentage": int(round(fan_target.fan_speed_pct))},
                                reason=f"stage={stage.name} target={fan_target.fan_speed_pct}%",
                            )
                        )
                    elif (
                        f.entity_id in self._caps_by_id
                        and Capability.SET_PRESET_MODE
                        in self._caps_by_id[f.entity_id]
                    ):
                        # Preset-mode-only fan (e.g. Sonoff iFan04).
                        # Quantise the target % into low/medium/high
                        # buckets — the three values every preset fan
                        # in the wild supports per HA's defaults.
                        preset = _fan_percent_to_preset(fan_target.fan_speed_pct)
                        actions.append(
                            ControlAction(
                                domain="fan",
                                service="set_preset_mode",
                                entity_id=f.entity_id,
                                data={"preset_mode": preset},
                                reason=(
                                    f"stage={stage.name} "
                                    f"target={fan_target.fan_speed_pct}% "
                                    f"preset={preset} (fan is preset-only)"
                                ),
                            )
                        )
                    # else: fan is turn_on/off only → we already handle
                    # the 0% branch above; the 1%+ path silently drops
                    # (and ``_device_supports`` already logged the
                    # SET_SPEED_PCT miss exactly once).

        return actions

    def _allow_action(self, domain: str, now: float) -> bool:
        """Return True if we have waited long enough since the last action."""
        last = self._last_action_ts.get(domain, 0.0)
        return now - last >= self.config.min_seconds_between_actions

    @staticmethod
    def _climate_mode_for_target(
        target_temp_c: float, current_env_temp_c: Optional[float],
    ) -> str:
        """Pick the HVAC mode string for waking a climate entity.

        When the AC / heat-pump is currently off, HA needs us to set
        ``hvac_mode`` to something other than ``"off"`` before
        ``set_temperature`` will take effect.  We use the simplest
        reliable heuristic:

        * target > ambient → user wants warming → ``"heat"``
        * target < ambient → user wants cooling → ``"cool"``
        * unknown ambient or within 0.5 °C  → ``"auto"`` (most
          modern AC / heat-pumps accept auto; the few that don't
          will log an error in HA and we'll see it in the log).

        We don't try to infer more — vendors diverge on mode names
        (Panasonic ``heat_cool`` vs Mitsubishi ``auto`` vs Daikin
        ``dry``) and guessing wrong is worse than asking HA's auto.
        """
        if current_env_temp_c is None:
            return "auto"
        delta = target_temp_c - current_env_temp_c
        if delta > 0.5:
            return "heat"
        if delta < -0.5:
            return "cool"
        return "auto"

    # ---------------------------------------------------------------- #
    # Futile-retry suppression (v1.6.4)                                #
    # ---------------------------------------------------------------- #

    def _futility_key(self, domain: str, entity_id: str) -> str:
        return f"{domain}.{entity_id}"

    def _reset_futility_tracking_on_stage_change(
        self, stage: SleepStage,
    ) -> None:
        """Clear saturation flags when the stage changes.

        A new stage means a new setpoint, so a previously-saturated
        entity deserves a fresh chance.  Called from plan_actions()
        before any per-domain decision is made.
        """
        if self._stage_when_saturation_recorded == stage:
            return
        if self._saturated_entities:
            logger.info(
                "Stage changed to %s — clearing %d saturation flags so "
                "the new setpoint gets a fresh chance.",
                stage.name, len(self._saturated_entities),
            )
        self._stage_when_saturation_recorded = stage
        self._saturated_entities.clear()
        self._futile_history.clear()

    def _env_field_for_domain(self, domain: str) -> Optional[str]:
        """Which :class:`EnvironmentParams` field should move when we
        issue a service in this domain?  Returns ``None`` for domains
        we don't track (fans, lights after turn_off).
        """
        return {
            "climate": "temperature_c",
            "humidifier": "humidity_pct",
            "light": "brightness_pct",
        }.get(domain)

    def _is_entity_saturated(
        self,
        domain: str,
        entity_id: str,
        target_value: float,
        current_env_value: Optional[float],
        now: float,
    ) -> bool:
        """Check whether hammering this entity again would be futile.

        Strategy: look at the last N (target, observed_env, ts) tuples
        for this entity.  If:

        1. at least ``_FUTILE_STREAK_THRESHOLD`` entries exist,
        2. all targeted the same value (within the field's deadband),
        3. each pair is at least ``_FUTILE_MIN_SETTLE_SECONDS`` apart,
        4. the observed env has moved less than
           ``_FUTILE_MIN_EFFECTIVE_DELTA`` across the whole streak,

        the entity is saturated and we should skip further retries
        until the stage changes.
        """
        field = self._env_field_for_domain(domain)
        if field is None or current_env_value is None:
            # Can't form a feedback loop without an env reading.
            return False
        min_delta = _FUTILE_MIN_EFFECTIVE_DELTA.get(field)
        if min_delta is None:
            return False

        key = self._futility_key(domain, entity_id)
        if key in self._saturated_entities:
            return True
        history = self._futile_history.get(key, [])
        if len(history) < _FUTILE_STREAK_THRESHOLD:
            return False

        # Same-target check — use the field's deadband as the "same"
        # tolerance rather than exact equality (temperature setpoints
        # are ints, but 18.9 and 19.1 are morally the same target).
        target_tolerance = min_delta / 2.0
        if any(
            abs(t - target_value) > target_tolerance
            for t, _, _ in history[-_FUTILE_STREAK_THRESHOLD:]
        ):
            return False

        # Settle-time check — consecutive actions must be at least
        # _FUTILE_MIN_SETTLE_SECONDS apart, else the device hasn't had
        # time to act yet and we can't judge its effectiveness.
        relevant = history[-_FUTILE_STREAK_THRESHOLD:]
        for (_, _, earlier_ts), (_, _, later_ts) in zip(relevant, relevant[1:]):
            if later_ts - earlier_ts < _FUTILE_MIN_SETTLE_SECONDS:
                return False

        # Env-movement check — from first sample to current, was there
        # any useful movement?
        initial_env = relevant[0][1]
        if abs(current_env_value - initial_env) >= min_delta:
            return False

        logger.warning(
            "Futile-retry suppression: %s.%s appears saturated "
            "(target=%.1f, env barely moved from %.1f to %.1f over "
            "%d attempts spanning %.0f min).  Pausing same-setpoint "
            "retries until the stage changes.",
            domain, entity_id, target_value,
            initial_env, current_env_value,
            _FUTILE_STREAK_THRESHOLD,
            (relevant[-1][2] - relevant[0][2]) / 60.0,
        )
        self._saturated_entities.add(key)
        return True

    def _record_action_for_futility(
        self,
        domain: str,
        entity_id: str,
        target_value: Optional[float],
        current_env_value: Optional[float],
        now: float,
    ) -> None:
        """Append a (target, env, ts) tuple to the entity's history.

        Keeps at most ``_FUTILE_STREAK_THRESHOLD`` entries so memory
        doesn't grow over long sessions.
        """
        if target_value is None or current_env_value is None:
            return
        key = self._futility_key(domain, entity_id)
        history = self._futile_history.setdefault(key, [])
        history.append((float(target_value), float(current_env_value), now))
        # Trim to fixed window to cap memory usage.
        if len(history) > _FUTILE_STREAK_THRESHOLD + 1:
            del history[:-(_FUTILE_STREAK_THRESHOLD + 1)]

    def futility_stats(self) -> Dict[str, Any]:
        """Expose saturation bookkeeping so the orchestrator can put
        it on ``sensor.sleep_classifier_last_action``.
        """
        return {
            "saturated_entities": sorted(self._saturated_entities),
            "tracked_entities": sorted(self._futile_history.keys()),
        }

    def _device_supports(
        self, entity: Any, capability: Capability,
    ) -> bool:
        """Return ``True`` iff this entity's cached capability set includes it.

        On the first miss for a given ``(entity_id, capability)`` pair
        we emit a single ``WARNING`` and bump a counter.  Subsequent
        misses are silent — a 30 s inference loop must not flood the
        log with the same message every tick.  Counters are available
        via :meth:`capability_stats` for the diagnostic ``last_action``
        sensor.
        """
        caps = self._caps_by_id.get(entity.entity_id, set())
        if capability in caps:
            return True
        key = (entity.entity_id, capability)
        if key not in self._missing_cap_warned:
            self._missing_cap_warned.add(key)
            logger.warning(
                "Skipping action: %s does not advertise %s in "
                "supported_features (HA would accept the call and "
                "silently no-op). Either bind a capable entity in "
                "Configuration or remove this one.",
                entity.entity_id, capability.value,
            )
        self._skipped_by_cap[capability.value] = (
            self._skipped_by_cap.get(capability.value, 0) + 1
        )
        return False

    def _liveness_guard(self, entity: Any) -> bool:
        """Return ``True`` iff the controller is allowed to dispatch a
        service to this entity right now.

        The three v1.7.1 guards, in priority order:

        1. **Availability.**  If HA shows state ``unavailable`` /
           ``unknown`` / ``""``, the device is physically unreachable
           (dropped off the mesh, lost power, etc.).  Firing a service
           returns 200 but the device won't hear us, and HA may start
           buffering unsent calls.  Skip entirely.
        2. **User override window.**  If the user manually touched this
           entity within the grace period, they win.  The add-on's
           whole point is to ease sleep; fighting the user in the
           middle of the night is the fastest way to get uninstalled.
        3. Otherwise allow.

        Decisions are logged once per entity per state change (via the
        underlying cache's own logging) and counted for the
        diagnostic ``last_action`` sensor attribute.
        """
        eid = entity.entity_id
        if not self.live_state.is_available(eid):
            self.live_state.count_skip_unavailable(eid)
            return False
        if self.live_state.under_user_override(eid):
            self.live_state.count_skip_user_override(eid)
            return False
        return True

    def capability_stats(self) -> Dict[str, int]:
        """Expose skipped-action counts keyed by capability name.

        Used by the orchestrator to drop a breadcrumb into the
        ``sensor.sleep_classifier_last_action`` attribute panel so
        the user can see *why* the loop looks quiet — e.g. their
        fan is a preset-mode-only Sonoff iFan04 and therefore can't
        accept ``set_percentage``.  Pure read of internal state;
        safe to call from any tick.
        """
        return dict(self._skipped_by_cap)

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
            # v1.6.4 — record this action into the futility tracker
            # *before* dispatch.  If the service call fails we still
            # want to see the record; saturation is about "we asked
            # and nothing changed", not "we never asked".
            target_value = self._target_value_from_action(action)
            current_env_value = self._env_value_from_action(action, current_env)
            self._record_action_for_futility(
                action.domain, action.entity_id,
                target_value, current_env_value, now=time.time(),
            )
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
                # v1.7.1 — mark this dispatch so the resulting
                # state_changed echo won't be misclassified as a
                # user override when LiveStateCache sees it.
                self.live_state.record_self_dispatch(action.entity_id)
                logger.info("Executed %s  // %s",
                            action.describe(), action.reason)
            except Exception as exc:    # noqa: BLE001
                logger.error(
                    "Service call failed (%s): %s",
                    action.describe(), exc,
                )

        return actions

    @staticmethod
    def _target_value_from_action(action: ControlAction) -> Optional[float]:
        """Extract the primary numeric target out of an action's data.

        Used by futile-retry tracking — climate/humidifier/light
        carry their setpoint under different keys, and turn_off
        has no setpoint at all (returns None, which disables
        tracking for that action).
        """
        data = action.data
        for key in ("temperature", "humidity", "brightness_pct"):
            if key in data:
                try:
                    return float(data[key])
                except (TypeError, ValueError):
                    return None
        return None

    @staticmethod
    def _env_value_from_action(
        action: ControlAction, current_env: EnvironmentParams,
    ) -> Optional[float]:
        """Pick the env field that corresponds to this action."""
        if action.domain == "climate":
            return current_env.temperature_c
        if action.domain == "humidifier":
            return current_env.humidity_pct
        if action.domain == "light":
            return current_env.brightness_pct
        return None

    @property
    def action_history(self) -> List[ControlAction]:
        return list(self._actions_log)


__all__ = [
    "SmartControlConfig",
    "SmartEnvironmentController",
    "ControlAction",
    "is_in_wind_down",
]
