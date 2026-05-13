"""Deeply-integrated Home Assistant sleep service.

Pipeline (single ``asyncio`` loop):

1. **Connect** to HA REST API (Long-Lived Access Token in dev,
   Supervisor proxy token in Add-on mode).
2. **Discover** entities: the bound sleep-stage sensor, environment
   sensors (temperature, humidity, illuminance), and actionable
   devices (lights, climates, fans, humidifiers, media_players).
3. **Subscribe** to ``state_changed`` over the WebSocket.  The
   configured sleep-stage entity is routed through
   :class:`ExternalStageSubscriber` (with debounce); environment
   sensors snapshot into ``self.last_env``; the optional feedback
   ``input_number`` is forwarded to the
   :class:`SubjectiveFeedbackListener`.
4. Every ``infer_interval`` seconds, read the current (debounced)
   stage from the subscriber.  The
   :class:`SmartEnvironmentController` plans + executes the matching
   HA service calls (per-stage deltas + per-actuator anticipation +
   wind-down pre-cool).
5. Every ``session_interval`` (default 30 min), persist a partial
   :class:`SleepSession` so the learner gets ongoing reward signals;
   record the final session on graceful shutdown.

Run examples
------------

.. code-block:: bash

    # Dry run against a local mock HA — prints planned actions
    python scripts/run_ha_smart_service.py --dry-run --duration 60

    # Real Pi 4B deployment
    HA_TOKEN="..." python scripts/run_ha_smart_service.py \\
        --base-url http://homeassistant.local:8123 \\
        --area bedroom --infer-interval 30 --session-interval 1800

The token can be supplied via ``--token``, the ``HA_TOKEN`` env var, or the
``home_assistant.api.access_token`` field in ``config/config.json``.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional


def _L(en: str, zh: str) -> str:
    """Return zh if LANG contains 'zh', else en. Used for user-facing log messages."""
    return zh if "zh" in os.environ.get("LANG", "").lower() else en

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from training_config.config_loader import load_config
from src.data_structures import SleepStage
from src.device_discovery import DeviceDiscovery, DiscoveryConfig, DiscoveryResult
from src.ha_api_client import (
    HAAPIError,
    HAAuthError,
    HAEntity,
    HomeAssistantClient,
    StateChangeEvent,
)
from src._time_utils import now_local
from src.feedback_input import SubjectiveFeedbackListener
from src.preference_learner import (
    EnvironmentParams,
    PreferenceConfig,
    PreferenceLearner,
    SleepSession,
)
from src.preference_learner import compute_quality_score as _legacy_quality_score
from src.sleep_debt import NightRecord, SleepDebtTracker
from src.sleep_quality_score import (
    blend_subjective,
    compute_metrics,
    compute_objective_quality,
)
from src.learning_panel_publisher import LearningPanelPublisher
from src.sleep_state_publisher import SleepStatePublisher
from src.smart_environment_controller import (
    SmartControlConfig,
    SmartEnvironmentController,
    is_in_wind_down,
)
from src.smart_wake import (
    SmartWakePlanner,
    WakeDecision,
    WakeWindow,
    light_ramp_brightness,
)
from src.external_stage_subscriber import ExternalStageSubscriber
from src.apnea_wiring import ApneaWiring, ApneaWiringConfig
from src.live_state_cache import LiveStateCache
from src.user_profile import UserProfile, UserProfileStore
from src.whitenoise_matcher import Soundscape, WhiteNoiseMatcher

# Persistence root: ``/data`` is the supervisor-managed volume so files
# survive add-on upgrades.  Outside the add-on we fall back to the
# project root so dev runs work too.
#
# v1.3.0: the legacy CNN-BiLSTM ``inference_buffer.npz`` is gone — the
# stage now arrives as a single HA entity update, so there's nothing to
# warm up across restarts.  We keep ``_BUFFER_DIR`` only as the natural
# parent for ``user_profile.json`` so the existing on-disk layout stays
# stable for users upgrading from v1.2.x.
_BUFFER_DIR = Path("/data") if Path("/data").is_dir() else PROJECT_ROOT

# Natural-sleep persistence paths (v1.2.0).
_PROFILE_PATH = _BUFFER_DIR / "user_profile.json"

# Type alias kept short so the ``_route_state_change`` /
# ``_task_inference_loop`` signatures stay scannable.
_Engine = ExternalStageSubscriber

logger = logging.getLogger("smart_service")


# ---------------------------------------------------------------------------
# Service orchestration
# ---------------------------------------------------------------------------


class SmartSleepService:
    """Tie HA client + discovery + inference + controller + learner together."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.full_cfg = load_config(str((PROJECT_ROOT / args.config).resolve()))
        ha_cfg = self.full_cfg.get("home_assistant", {})
        api_cfg = ha_cfg.get("api", {})

        # When running as a Home Assistant add-on, the supervisor injects
        # SUPERVISOR_TOKEN automatically — we honour it as a fallback so the
        # user never has to create a Long-Lived Access Token manually.
        token = (
            args.token
            or os.environ.get("HA_TOKEN")
            or os.environ.get("SUPERVISOR_TOKEN")
            or api_cfg.get("access_token", "")
        )
        if not token and not args.dry_run:
            raise SystemExit(
                "No HA access token provided. Use --token, HA_TOKEN env var, "
                "or set home_assistant.api.access_token in config.json."
            )

        self.base_url = args.base_url or api_cfg.get(
            "base_url", "http://homeassistant.local:8123"
        )
        self.token = token
        self.verify_ssl = bool(api_cfg.get("verify_ssl", True))

        # Discovery configuration
        disc_cfg = DiscoveryConfig.from_dict(api_cfg)
        if args.area:
            disc_cfg.area_filter = args.area
        self.disc_cfg = disc_cfg

        # Preference learner
        pref_cfg = PreferenceConfig.from_dict(
            ha_cfg.get("preference_learner", {})
        )
        pref_cfg.history_path = str(
            (PROJECT_ROOT / pref_cfg.history_path).resolve()
        )
        self.learner = (
            PreferenceLearner(pref_cfg) if pref_cfg.enabled else None
        )

        # Smart controller config
        self.ctrl_cfg = SmartControlConfig.from_dict(
            ha_cfg.get("smart_control", {})
        )
        if args.dry_run:
            self.ctrl_cfg.dry_run = True

        # v1.6.3 — session-lifecycle knobs.  Read from
        # ``home_assistant.session_lifecycle`` so they can be surfaced
        # in the add-on Configuration UI later without changing the
        # dataclass shape.  Defaults derive from the literature:
        # 5 min of non-AWAKE persistence is a standard sleep-onset
        # criterion in PSG scoring (AASM §2); 10 min of continuous
        # AWAKE is long enough that a stir-then-back-to-sleep doesn't
        # close the session prematurely.
        lifecycle_cfg = ha_cfg.get("session_lifecycle", {})
        self._session_onset_dwell_seconds: float = float(
            lifecycle_cfg.get("onset_dwell_seconds", 300.0),
        )
        self._session_wake_dwell_seconds: float = float(
            lifecycle_cfg.get("wake_dwell_seconds", 600.0),
        )
        # v1.8.0 — minimum session duration (minutes) to feed the
        # learner.  Sessions shorter than this (e.g. naps) are still
        # scored and published but NOT recorded into the preference
        # learner, preventing short naps from polluting the overnight
        # recommendation model.
        self._min_session_minutes: float = float(
            lifecycle_cfg.get("min_session_minutes", 60.0),
        )

        # v1.7.0 — apnea trend detector.  Disabled by default; when a
        # breathing-rate source is bound via Configuration the wiring
        # layer subscribes to it, accumulates samples per session, and
        # publishes a coarse red/amber/green trend AFTER an explicit
        # consent toggle.  No numeric AHI is ever surfaced.
        apnea_cfg = ApneaWiringConfig.from_dict(
            ha_cfg.get("apnea", {}),
        )
        # Make the baseline path relative to PROJECT_ROOT only when
        # it's a bare filename (tests); leave absolute paths (e.g.
        # /data/apnea_baseline.json in the Add-on) untouched.
        if apnea_cfg.baseline_path and not Path(apnea_cfg.baseline_path).is_absolute():
            apnea_cfg.baseline_path = str(
                (PROJECT_ROOT / apnea_cfg.baseline_path).resolve(),
            )
        self.apnea = ApneaWiring(apnea_cfg)

        # v1.7.1 — shared per-entity live-state cache.  Populated by
        # the WebSocket listener; queried by the controller's
        # liveness guard + off-state auto-turn-on logic.  One
        # instance lives on the service so both routing and
        # controller see the same data.
        live_cfg = ha_cfg.get("live_state", {})
        self.live_state = LiveStateCache(
            user_override_grace_seconds=float(
                live_cfg.get("user_override_grace_seconds", 600.0),
            ),
        )

        # --- Natural-sleep modules (v1.2.0) ----------------------------- #
        # All of these are *optional*: if the user didn't configure the
        # relevant fields we leave the attribute as None and the main
        # loop simply skips the corresponding publish / action.
        natural_cfg = ha_cfg.get("natural_sleep", {})
        # ``profile_path`` is normally the supervisor /data volume; tests
        # override it to keep their fixtures isolated from each other and
        # from the host's real ``user_profile.json``.
        profile_path = Path(
            natural_cfg.get("profile_path") or _PROFILE_PATH,
        )
        self.profile_store = UserProfileStore(profile_path)
        self.profile = self.profile_store.load(
            user_id=natural_cfg.get("user_id", "default"),
        )
        # Keep config-driven overrides that don't belong in the on-disk
        # profile (user can edit birth_year in Configuration without us
        # clobbering the learned posterior).
        if natural_cfg.get("birth_year"):
            try:
                self.profile.birth_year = int(natural_cfg["birth_year"])
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring invalid birth_year %r", natural_cfg["birth_year"],
                )
        if natural_cfg.get("chronotype"):
            self.profile.chronotype = str(natural_cfg["chronotype"])

        # Wake window (two "HH:MM" strings from the add-on Config tab).
        wake_start = natural_cfg.get("wake_window_start") or ""
        wake_end = natural_cfg.get("wake_window_end") or ""
        self._wake_window_strs: Optional[tuple[str, str]] = (
            (str(wake_start), str(wake_end))
            if wake_start and wake_end else None
        )
        self.wake_planner: Optional[SmartWakePlanner] = None
        self._wake_light_targets: List[str] = list(
            natural_cfg.get("wake_light_targets", []) or []
        )

        # Soundscape matcher.
        self.sound_matcher: Optional[WhiteNoiseMatcher] = None
        media_target = natural_cfg.get("whitenoise_target") or ""
        if media_target:
            self.sound_matcher = WhiteNoiseMatcher(
                media_player_entity=str(media_target),
                user_overrides=natural_cfg.get("whitenoise_overrides") or {},
                volume_scale=float(natural_cfg.get("whitenoise_volume_scale", 1.0)),
                track_overrides=natural_cfg.get("whitenoise_track_overrides") or {},
                is_pre_wake=self._is_pre_wake,
            )
        self._last_soundscape: Optional[str] = None

        # Subjective-feedback listener.
        self.feedback: Optional[SubjectiveFeedbackListener] = None
        fb_entity = natural_cfg.get("feedback_entity") or ""
        if fb_entity:
            self.feedback = SubjectiveFeedbackListener(
                str(fb_entity),
                scale=int(natural_cfg.get("feedback_scale", 5)),
            )

        # v1.9.0 — user temperature override input_number.  When the
        # user sets a value via this entity, the controller's _baseline()
        # uses it instead of the learner's recommended temperature_c.
        self._temperature_override_entity: str = str(
            natural_cfg.get("temperature_override_entity") or ""
        ).strip()

        # v2.0.0 — white noise volume one-click feedback.  When the
        # user presses this input_button, the WhiteNoiseMatcher's
        # volume_scale is reduced by 30 % (multiplied by 0.7).
        self._whitenoise_volume_feedback_entity: str = str(
            natural_cfg.get("whitenoise_volume_feedback_entity") or ""
        ).strip()

        # v1.3.0: stage now comes from an external HA entity (Mi Band /
        # Apple Watch / Withings / etc) instead of a local CNN-BiLSTM
        # forward pass.  The entity_id is mandatory in live mode but the
        # constructor must not raise here so dry-run / unit-test paths
        # can still build the service; we re-check the binding in run().
        self.sleep_stage_source: str = str(
            api_cfg.get("sleep_stage_source", "")
        ).strip()
        # Allow the user's literal '""' placeholder (the add-on's
        # Configuration form keeps quotes around the empty default).
        if self.sleep_stage_source in ('""', "''"):
            self.sleep_stage_source = ""

        # Runtime state
        self.session_id = uuid.uuid4().hex[:8]
        self.session_started_at = time.time()
        self.stage_counts: Dict[str, int] = {
            "AWAKE": 0, "LIGHT": 0, "DEEP": 0, "REM": 0,
        }
        # Sequence of (SleepStage) per inference for SE/WASO/SOL scoring.
        self.stage_sequence: List[SleepStage] = []
        self.last_env = EnvironmentParams()
        # v1.6.4 — per-field freshness timestamps so we don't feed the
        # controller a 6-hour-old temperature reading if the sensor
        # dropped off the network.  A field with ``ts=0`` means "never
        # observed"; a field whose ts is older than
        # ``_env_freshness_window_seconds`` is treated as None at read
        # time, forcing the controller to treat it as unknown rather
        # than stale (causing deadband to greenlight an action that
        # might be fighting imaginary drift).  See _safe_last_env().
        self._env_ts: Dict[str, float] = {
            "temperature_c": 0.0,
            "humidity_pct": 0.0,
            "brightness_pct": 0.0,
        }
        self._env_freshness_window_seconds: float = float(
            ha_cfg.get("env_freshness_window_seconds", 900.0),
        )
        # Which env fields are currently expired — updated on each
        # inference tick.  Exposed via status() for the diagnostic
        # last_action sensor so users see "temperature_c stale" on
        # Lovelace.
        self._env_stale_fields: set[str] = set()
        # v1.5.0 — per-stage env snapshots.  Updated by
        # :meth:`_track_per_stage_env` once per inference tick when the
        # *raw* (debounced) stage changes.  Empty until the user moves
        # through more than one stage; populates the matching
        # SleepSession field at checkpoint time so
        # :meth:`PreferenceLearner.recommend_per_stage_deltas` can
        # learn each user's idiosyncratic stage-to-stage offsets.
        self.env_by_stage: Dict[str, EnvironmentParams] = {}
        # Track the last stage we sampled so we don't overwrite the
        # snapshot on every tick — only on genuine stage transitions.
        self._last_sampled_stage: Optional[SleepStage] = None

        # v1.6.3 — proper session-lifecycle bookkeeping.  Previously the
        # five fields above were initialised *once* in __init__ and never
        # reset, so an add-on running for a month produced one
        # 30-day-long "session" with a meaningless quality score.  The
        # real semantics are:
        #   * A session STARTS the first time we observe a non-AWAKE
        #     stage that holds for >= ``session_onset_dwell_seconds``.
        #   * A session ENDS after ``session_wake_dwell_seconds`` of
        #     continuous AWAKE.
        # While ``_in_session`` is False we still tick the controller
        # (so setpoints track daytime comfort too) but we don't append
        # to stage_sequence or stage_counts and don't persist anything.
        self._in_session: bool = False
        self._consecutive_awake_ticks: int = 0
        self._consecutive_non_awake_ticks: int = 0
        # Published once per staleness-state change so the log doesn't
        # flood when a wearable is off for 8 hours.
        self._stage_source_was_stale: bool = False
        # v1.6.0 — recommend_bedtime() runs a weighted-median over
        # every session in history.  At the default 30 s inference
        # cadence with 60 sessions that's ~120 K redundant computes a
        # night.  Cache for 60 s; the bedtime forecast doesn't change
        # faster than that anyway.
        self._bedtime_cache: Optional[Dict[str, Any]] = None
        self._bedtime_cached_at: float = 0.0
        self.stop_event = asyncio.Event()
        # The publisher is created once HA is reachable; until then it stays
        # ``None`` so dry-run and unit-test paths don't accidentally try to
        # POST to a non-existent server.
        self.publisher: Optional[SleepStatePublisher] = None

    # ------------------------------------------------------------------ #
    # State change handler                                               #
    # ------------------------------------------------------------------ #

    def _route_state_change(
        self,
        event: StateChangeEvent,
        discovery: DiscoveryResult,
        engine: _Engine,
    ) -> None:
        """Fan out one HA ``state_changed`` event to the right consumer.

        v1.3.0 fan-out (top priority first):

        1.  The bound *sleep-stage* entity → forward to the subscriber
            (raw value + attributes; the subscriber owns parsing).
        2.  The feedback ``input_number`` (if configured) → consume the
            raw event so we don't try to coerce a string to a number.
        3.  Environment sensors (temperature / humidity / illuminance)
            → snapshot into ``self.last_env`` so the controller has a
            current reading next time the inference loop ticks.

        Heart-rate / movement / breathing sensors are no longer routed
        anywhere — the external sleep tracker is the authoritative
        source for stage, and feeding redundant physiology would only
        add noise to the learner without affecting the decision.
        """
        if event.new_state is None:
            return
        eid = event.entity_id

        # ---- 1. Sleep-stage forwarding (the new hot path) ------------
        # Use ``observe`` so the subscriber owns the vocabulary mapping
        # (numeric / English / Chinese / "unknown" all handled there).
        if eid == self.sleep_stage_source:
            engine.observe(
                eid,
                event.new_state.state,
                attributes=event.new_state.attributes,
            )
            return

        # ---- 2. Feedback helper (raw string state) -------------------
        # Must come before the numeric coerce below because feedback
        # entities sometimes hold non-numeric text (e.g. "skipped").
        if self.feedback is not None and eid == self.feedback.entity_id:
            self.feedback.on_state_change(event)
            return

        # ---- 2a. Temperature override input_number (v1.9.0) ----------
        # When the user sets a value via this entity, the controller's
        # _baseline() uses it instead of the learner's temperature_c.
        if self._temperature_override_entity and eid == self._temperature_override_entity:
            try:
                value = float(event.new_state.state)
                self.ctrl_cfg.user_temperature_override_c = value
                logger.info(
                    "User temperature override updated: %.1f °C", value,
                )
            except (TypeError, ValueError):
                # Non-numeric state (e.g. "unavailable") — clear override.
                self.ctrl_cfg.user_temperature_override_c = None
                logger.debug(
                    "Temperature override cleared (state=%r)",
                    event.new_state.state,
                )
            return

        # ---- 2c. White noise volume feedback button (v2.0.0) ---------
        # When the user presses this input_button, reduce the
        # WhiteNoiseMatcher's volume_scale by 30 %.
        if (
            self._whitenoise_volume_feedback_entity
            and eid == self._whitenoise_volume_feedback_entity
        ):
            if self.sound_matcher is not None:
                self.sound_matcher.volume_scale *= 0.7
                logger.info(
                    _L(
                        "White noise volume reduced by user feedback",
                        "用户反馈：白噪音音量已降低",
                    ),
                )
            return

        # ---- 2b. Apnea wiring (v1.7.0) ------------------------------
        # The consent input_boolean carries a non-numeric state, so
        # route the raw event.  Breathing-rate / chest-amplitude
        # entities carry numeric values; we pass both the string state
        # and the parsed numeric value so the wiring can choose.
        numeric = event.new_state.numeric_state()
        if self.apnea.on_state_change(
            eid, event.new_state.state, numeric_value=numeric,
        ):
            return

        # ---- 3. Environment sensors ---------------------------------
        value = event.new_state.numeric_state()
        # HA returns ``"unavailable"`` / ``"unknown"`` as the state
        # string when a sensor drops off the mesh; ``numeric_state()``
        # returns None in that case.  Previously we just returned and
        # kept ``last_env`` at whatever value we had before, so a dead
        # sensor's last reading could haunt the controller for hours.
        # v1.6.4 makes this explicit: drop the update here but DON'T
        # refresh the freshness timestamp, so _safe_last_env() will
        # start returning None for that field after the grace window.
        if value is None:
            # v1.7.1 — still let the live_state cache see the event
            # so e.g. a light going to "unavailable" is properly
            # detected before the next plan_actions tick.
            self._maybe_update_live_state(
                eid, discovery, event.new_state.state,
                event.new_state.attributes,
            )
            return
        now = time.time()
        if any(e.entity_id == eid for e in discovery.sensors.temperature):
            self.last_env.temperature_c = value
            self._env_ts["temperature_c"] = now
        elif any(e.entity_id == eid for e in discovery.sensors.humidity):
            self.last_env.humidity_pct = value
            self._env_ts["humidity_pct"] = now
        elif any(e.entity_id == eid for e in discovery.sensors.illuminance):
            self.last_env.brightness_pct = value
            self._env_ts["brightness_pct"] = now
        else:
            # v1.7.1 — numeric state of a NON-sensor entity (rare:
            # some fans report percentage as the state itself).
            # Forward to live state for on/off + override tracking.
            self._maybe_update_live_state(
                eid, discovery, event.new_state.state,
                event.new_state.attributes,
            )
            return

        # Sensor updates also feed the live-state cache for
        # availability/override tracking even though they don't carry
        # on/off semantics — a temperature sensor going unavailable
        # is useful signal for the diagnostic sensor.
        self._maybe_update_live_state(
            eid, discovery, event.new_state.state,
            event.new_state.attributes,
        )

    def _maybe_update_live_state(
        self,
        entity_id: str,
        discovery: DiscoveryResult,
        new_state: str,
        attributes: Optional[Dict[str, Any]],
    ) -> None:
        """Forward a state_changed to :attr:`live_state` iff the entity
        is bound to us (either as an actionable device or a sensor
        we read).  Random entities we don't touch shouldn't expand
        the cache indefinitely.
        """
        bound_ids = set()
        for bucket in (
            discovery.devices.lights, discovery.devices.climates,
            discovery.devices.fans, discovery.devices.humidifiers,
            discovery.devices.switches, discovery.devices.media_players,
            discovery.sensors.temperature, discovery.sensors.humidity,
            discovery.sensors.illuminance,
        ):
            for e in bucket:
                bound_ids.add(e.entity_id)
        if entity_id not in bound_ids:
            return
        self.live_state.on_state_change(
            entity_id, new_state, attributes,
        )

    # ------------------------------------------------------------------ #
    # Tasks                                                              #
    # ------------------------------------------------------------------ #

    async def _task_ws_listener(
        self,
        ha: HomeAssistantClient,
        discovery: DiscoveryResult,
        engine: _Engine,
    ) -> None:
        """Stream state-changed events; reconnect with exponential backoff.

        Previously a single WS error tore down the whole service, which on
        a flaky home network meant the add-on "randomly stopped" every
        few hours.  We now treat the WS as a best-effort transport: any
        recoverable error retries after a sleep that doubles up to 5
        minutes, with a small uniform jitter to avoid synchronised
        reconnect storms after a HA restart.
        """
        backoff = 1.0
        max_backoff = 300.0
        while not self.stop_event.is_set():
            try:
                async for event in ha.iter_state_changes():
                    self._route_state_change(event, discovery, engine)
                    if self.stop_event.is_set():
                        break
                    backoff = 1.0   # any successful event resets backoff
                if self.stop_event.is_set():
                    return
                # iter_state_changes returned cleanly but stop_event isn't
                # set — HA closed the WS.  Try to reconnect.
                logger.warning("HA WebSocket closed gracefully — reconnecting")
            except asyncio.CancelledError:
                logger.info("WebSocket task cancelled")
                raise
            except (HAAuthError, HAAPIError) as exc:
                # Auth errors won't fix themselves; give up loudly.
                logger.error("WebSocket auth/API error: %s — stopping service", exc)
                self.stop_event.set()
                return
            except Exception as exc:    # noqa: BLE001
                logger.warning(
                    "WebSocket transport error (%s); reconnecting in %.1fs",
                    exc, backoff,
                )
            # Sleep with jitter, but bail out fast if a shutdown signal lands.
            # ``random.uniform(-j, +j)`` is the textbook 1-line way to
            # spread reconnects across the cluster; dropping numpy from
            # this module means one less heavyweight import at startup.
            jitter = backoff * 0.2
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=backoff + random.uniform(-jitter, jitter),
                )
                return
            except asyncio.TimeoutError:
                pass
            backoff = min(max_backoff, backoff * 2.0)
            try:
                await ha.connect_websocket()
                await ha.subscribe_state_changes()
            except Exception as exc:    # noqa: BLE001
                logger.warning("Reconnect attempt failed: %s", exc)

    async def _task_inference_loop(
        self,
        engine: _Engine,
        controller: SmartEnvironmentController,
        ha: "HomeAssistantClient",
    ) -> None:
        try:
            while not self.stop_event.is_set():
                stage, conf = engine.infer()

                # v1.6.3 — stale stage-source guard.  If the bound HA
                # entity hasn't reported a new state in a long time
                # (wearable dead, integration broken, user took the
                # watch off), the subscriber keeps returning the last
                # known stage forever.  Without this guard the
                # controller would lock the bedroom into e.g. DEEP
                # setpoints for a whole day; the session-lifecycle
                # state machine would miss the wake transition and
                # never close the session.  When stale we:
                #   1) skip updating stage_counts / stage_sequence —
                #      we don't know what's really happening;
                #   2) skip the effective-stage substitution + apply()
                #      — don't push new setpoints based on stale data;
                #   3) still publish to HA (with is_stale=true) so the
                #      user sees "tracker not reporting" on the chip;
                #   4) log once per edge transition, not every tick.
                is_stale = engine.is_stale()
                if is_stale and not self._stage_source_was_stale:
                    logger.warning(
                        _L(
                            "Stage source %s has not updated for > %d s; "
                            "pausing control loop until the tracker comes "
                            "back online.",
                            "Stage source stale / 睡眠阶段源已断开: %s has not updated for > %d s; "
                            "pausing control loop.",
                        ),
                        engine.stage_entity_id,
                        int(engine._stale_after),
                    )
                    self._stage_source_was_stale = True
                elif not is_stale and self._stage_source_was_stale:
                    logger.info(
                        _L(
                            "Stage source %s is live again — resuming control.",
                            "Stage source live again / 睡眠阶段源已恢复: %s — resuming control.",
                        ),
                        engine.stage_entity_id,
                    )
                    self._stage_source_was_stale = False

                if is_stale:
                    # Still publish the diagnostic sensor so HA shows
                    # the staleness, but skip every mutation.
                    if self.publisher is not None:
                        await self.publisher.publish_stage(
                            stage, conf,
                            env_temperature_c=self.last_env.temperature_c,
                            env_humidity_pct=self.last_env.humidity_pct,
                            env_brightness_pct=self.last_env.brightness_pct,
                        )
                    try:
                        await asyncio.wait_for(
                            self.stop_event.wait(),
                            timeout=self.args.infer_interval,
                        )
                        return     # stop_event fired — exit the task
                    except asyncio.TimeoutError:
                        continue   # next tick — re-check is_stale

                # v1.6.3 — session lifecycle state machine.  Runs BEFORE
                # counts are updated so the first tick past onset/wake
                # doesn't bleed into the next session.  Note that
                # _maybe_advance_session_lifecycle may call
                # _persist_session + _reset_session_state mid-tick
                # when it detects a wake.
                self._maybe_advance_session_lifecycle(stage, controller)

                if self._in_session:
                    self.stage_counts[stage.name] = (
                        self.stage_counts.get(stage.name, 0) + 1
                    )
                    self.stage_sequence.append(stage)
                    # v1.5.0 — snapshot the env at every stage *entry*
                    # so the preference learner can later compute
                    # personalised AWAKE/LIGHT/DEEP/REM deltas.
                    self._track_per_stage_env(stage)
                    # v1.7.0 — append a breathing-signal sample per
                    # tick so the apnea detector has ~30 s hops to
                    # look at.  No-op when apnea disabled or the
                    # relevant rate/amplitude entities aren't bound.
                    self.apnea.tick()

                logger.info(
                    "infer stage=%s conf=%.2f  env(T=%s H=%s)%s",
                    stage.name, conf,
                    self.last_env.temperature_c, self.last_env.humidity_pct,
                    "" if self._in_session else "  [pre-onset]",
                )
                # v1.4.0 — wind-down substitution.  When the user is
                # still AWAKE but we are within `wind_down_minutes` of
                # the learned bedtime, the controller treats them as
                # already in LIGHT so the AC starts pre-cooling before
                # they actually lie down.  The published sensor still
                # reflects the truthful AWAKE — only the control path
                # substitutes, so users aren't confused by a sensor
                # that lies.
                effective_stage = self._effective_control_stage(stage)
                if effective_stage is not stage:
                    logger.info(
                        "  wind-down active: controlling as %s instead of %s",
                        effective_stage.name, stage.name,
                    )
                # v1.6.4 — feed the controller a freshness-masked copy
                # of last_env.  Stale sensors appear as None and the
                # deadband logic already treats None as "unknown"
                # (always-outside-deadband), so we fall back to stage
                # defaults rather than fighting a reading that's no
                # longer reflective of the room.
                safe_env = self._safe_last_env()
                actions = await controller.apply(effective_stage, safe_env)
                if actions:
                    logger.info("  → %d HA action(s) planned", len(actions))

                # Mirror diagnostics back to HA so users see the result on
                # their Lovelace dashboard.  Failures are swallowed inside
                # the publisher so they never break the inference loop.
                if self.publisher is not None:
                    await self.publisher.publish_stage(
                        stage, conf,
                        env_temperature_c=self.last_env.temperature_c,
                        env_humidity_pct=self.last_env.humidity_pct,
                        env_brightness_pct=self.last_env.brightness_pct,
                    )
                    await self.publisher.publish_duration(
                        time.time() - self.session_started_at,
                    )
                    if actions:
                        # ``actions`` is a list of ControlAction dataclasses
                        # (see src/smart_environment_controller.py), not a
                        # list of dicts — calling .get() on a dataclass
                        # raises AttributeError.  A dataclass fallback is
                        # robust against any future field renames.
                        first = actions[0]
                        summary = (
                            f"{first.domain}.{first.service} → "
                            f"{first.entity_id}"
                        )
                        await self.publisher.publish_last_action(
                            summary,
                            executed=not self.ctrl_cfg.dry_run,
                            # v1.6.2: let the user see *why* some
                            # devices aren't being controlled even
                            # though they're bound in Configuration.
                            skipped_by_capability=controller.capability_stats(),
                            # v1.7.1: show unavailability / override /
                            # auto-turn-on counts so the user can
                            # diagnose "why didn't my AC move"
                            # without reading the Supervisor log.
                            live_state_stats=self.live_state.stats(),
                        )

                # --- Smart-wake tick -------------------------------- #
                await self._wake_tick(ha, stage, conf)

                # --- Soundscape tick -------------------------------- #
                await self._soundscape_tick(ha, stage, conf)

                # v1.3.0: no rolling buffer to checkpoint anymore — the
                # subscriber holds a single most-recent stage so a power
                # loss costs at most one ``infer_interval`` tick.

                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(),
                        timeout=self.args.infer_interval,
                    )
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise

    async def _task_session_checkpoint(
        self,
        controller: SmartEnvironmentController,
    ) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(),
                        timeout=self.args.session_interval,
                    )
                    return
                except asyncio.TimeoutError:
                    pass
                self._persist_session(controller, partial=True)
        except asyncio.CancelledError:
            raise

    def _persist_session(
        self,
        controller: SmartEnvironmentController,
        *,
        partial: bool,
    ) -> None:
        if self.learner is None:
            return
        if sum(self.stage_counts.values()) == 0:
            return

        # ---- Polysomnography-grade scoring (v1.2.0) ----------------- #
        # Compute SE / WASO / SOL from the full stage sequence and blend
        # in the user's subjective rating (if any).  Falls back to the
        # legacy architecture-only score if we have < 10 epochs of data.
        subj_snap = self.feedback.consume() if self.feedback is not None else None
        subj_score = subj_snap.score if subj_snap is not None else None
        metrics = None
        if len(self.stage_sequence) >= 10:
            metrics = compute_metrics(
                self.stage_sequence,
                epoch_seconds=float(self.args.infer_interval),
            )
            sub_scores = compute_objective_quality(metrics)
            score = blend_subjective(
                sub_scores["composite"], subj_score,
            )
        else:
            score = _legacy_quality_score(self.stage_counts)
            if subj_score is not None:
                score = blend_subjective(score, subj_score)

        # Record subjective notes on the session for traceability.
        notes = "auto checkpoint" if partial else "session end"
        if subj_score is not None:
            notes += f" (subjective={subj_score:.1f})"

        session = SleepSession(
            session_id=self.session_id + ("-partial" if partial else ""),
            started_at=self.session_started_at,
            ended_at=time.time(),
            env_params=EnvironmentParams(
                temperature_c=self.last_env.temperature_c,
                humidity_pct=self.last_env.humidity_pct,
                brightness_pct=self.last_env.brightness_pct,
                fan_speed_pct=self.last_env.fan_speed_pct,
            ),
            stage_counts=dict(self.stage_counts),
            quality_score=score,
            n_samples=sum(self.stage_counts.values()),
            notes=notes,
            # v1.5.0 — hand off the per-stage env trace collected by
            # _track_per_stage_env during the night.  Empty dict for a
            # session that never crossed a stage boundary (e.g. an
            # extremely short nap); the learner tolerates that and
            # falls back to clinical deltas.
            env_by_stage=dict(self.env_by_stage),
        )
        try:
            # v1.8.0 — nap filter: skip learner recording for sessions
            # shorter than min_session_minutes (default 60) when this
            # is a final persist (partial=False).
            session_duration_min = (
                session.ended_at - session.started_at
            ) / 60.0
            if not partial and session_duration_min < self._min_session_minutes:
                logger.warning(
                    _L(
                        "Session too short (%.0f min), not feeding to learner",
                        "Session too short / 会话过短，未记录 (%.0f min)",
                    ),
                    session_duration_min,
                )
            else:
                self.learner.record_session(session)
            controller.feedback_score(score)
            logger.info(
                "Session %s checkpoint — quality=%.1f (history: %s)",
                session.session_id, score, self.learner.status(),
            )

            # v1.9.0 — first-night diagnostic report.  After the very
            # first complete session, log a summary so the user knows
            # the system is working and what to expect next.
            if not partial and self.learner.n_sessions() == 1:
                duration_h = (session.ended_at - session.started_at) / 3600.0
                total_epochs = sum(session.stage_counts.values()) or 1
                stage_pcts = {
                    k: f"{v / total_epochs * 100:.1f}%"
                    for k, v in session.stage_counts.items()
                }
                logger.info(
                    "═══ 首晚诊断报告 ═══\n"
                    "  session 时长: %.1f 小时\n"
                    "  quality_score: %.1f\n"
                    "  stage 分布: %s\n"
                    "  环境快照: temperature=%.1f°C humidity=%.0f%% brightness=%.0f%%\n"
                    "  系统已开始学习您的偏好，预计 3 晚后开始个性化推荐",
                    duration_h,
                    score,
                    stage_pcts,
                    self.last_env.temperature_c or 0.0,
                    self.last_env.humidity_pct or 0.0,
                    self.last_env.brightness_pct or 0.0,
                )
            # Feed the same evidence into the user profile so the
            # recommended-sleep-hours estimate tracks the user's
            # actual "good night" distribution.
            try:
                actual_hours = max(
                    0.0, (session.ended_at - session.started_at) / 3600.0,
                )
                self.profile.record_quality_session(
                    actual_hours,
                    objective_score=score,
                    subjective_score=subj_score,
                )
                self.profile_store.save(self.profile)
            except Exception as exc:    # noqa: BLE001
                logger.warning("Profile update skipped: %s", exc)

            # Reflect the latest score on the Lovelace card.  We're inside a
            # sync method here so schedule the coroutine — best-effort.
            if self.publisher is not None:
                try:
                    loop = asyncio.get_event_loop()
                    loop.create_task(self.publisher.publish_quality(score))
                    # v1.8.0 — publish quality sub-scores when available.
                    if metrics is not None:
                        loop.create_task(
                            self.publisher.publish_quality_sub_scores(sub_scores),
                        )
                    # Also refresh debt + bedtime entities because the
                    # new session just became history.
                    loop.create_task(self._publish_debt_and_bedtime())
                    # And refresh the v1.3.0 learning panel — the new
                    # session changes the decay-weighted recommendations.
                    loop.create_task(self._publish_learning_panel())
                except RuntimeError:
                    # No running loop (e.g. last call during shutdown). Skip.
                    pass
        except Exception as exc:    # noqa: BLE001
            logger.error("Failed to persist session: %s", exc)

    # ------------------------------------------------------------------ #
    # Main entry                                                         #
    # ------------------------------------------------------------------ #

    async def run(self) -> int:
        if not self.sleep_stage_source and not self.args.dry_run:
            logger.error(
                "sleep_stage_source is empty.  Open the add-on Configuration "
                "tab and bind a sleep-stage entity (e.g. "
                "sensor.mi_band_sleep_stage)."
            )
            return 4
        # The subscriber needs a non-empty entity_id to validate; in the
        # dry-run-without-binding path we substitute a placeholder so the
        # synthetic loop can still drive the controller.
        #
        # v1.4.0: the subscriber's dwell guard reads
        # ``smart_control.min_stage_dwell_seconds`` from add-on config so
        # users can tighten / loosen the debounce without rebuilding.
        min_dwell = float(
            getattr(self.ctrl_cfg, "min_stage_dwell_seconds", None) or 60.0
        )
        engine = ExternalStageSubscriber(
            stage_entity_id=self.sleep_stage_source or "sensor.dry_run_stage",
            min_stage_dwell_seconds=min_dwell,
        )

        # ----- Dry-run path: no HA ---------------------------------------
        if self.args.dry_run and not self.token:
            logger.warning(
                "Dry-run without token — discovery and live HA calls are skipped."
            )
            await self._run_dry_with_synthetic_signals(engine)
            return 0

        async with HomeAssistantClient(
            self.base_url, self.token, verify_ssl=self.verify_ssl,
        ) as ha:
            if not await ha.ping():
                logger.error("HA REST ping failed — check base_url and token.")
                return 2

            logger.info("Fetching entity registry from HA …")
            entities = await ha.get_states()
            logger.info("HA exposes %d entities", len(entities))

            discovery = DeviceDiscovery(self.disc_cfg).discover(entities)
            discovery.log_summary()

            # v1.3.0: HR / movement / breathing are no longer required.
            # The only mandatory binding is ``sleep_stage_source`` (checked
            # at the top of run()).  Environment sensors stay optional but
            # we surface a one-line summary so the user can spot the gap.
            if not (
                discovery.sensors.temperature
                or discovery.sensors.humidity
                or discovery.sensors.illuminance
            ):
                logger.warning(
                    "No environment sensors discovered (temperature / humidity "
                    "/ illuminance).  Learning continues on stage data alone, "
                    "but the controller can't learn the best (T, RH, lux) "
                    "combo without them."
                )

            controller = SmartEnvironmentController(
                config=self.ctrl_cfg,
                ha_client=ha,
                devices=discovery.devices,
                learner=self.learner,
                live_state=self.live_state,
            )

            # Initial environment snapshot
            self._seed_current_env(entities, discovery)

            # v1.7.1 — seed the live-state cache with the current
            # state of every actionable entity so the very first
            # plan_actions() tick has accurate on/off + availability
            # data, not just whatever state_changed events have
            # arrived since the WebSocket connected.
            self._seed_live_state(entities, discovery)

            # v1.3.0: no buffer to restore — the subscriber's first real
            # state-changed event will populate the cached stage within
            # one HA WebSocket round-trip (~50 ms typical).

            # Diagnostic state publisher — populates HA Lovelace entities.
            self.publisher = SleepStatePublisher(ha)

            # v1.9.0 — delay before first publish to let HA core's REST
            # API fully initialise after a restart.  Without this, the
            # first POST /api/states/<entity> can 502 if the add-on
            # starts faster than HA core's internal state machine.
            await asyncio.sleep(2.0)

            # Seed every entity at boot so Lovelace cards stop showing
            # "Entity not available" for the first ~10 minutes.
            try:
                await self.publisher.publish_initial_placeholders()
            except Exception as exc:    # noqa: BLE001
                logger.warning("Initial placeholder publish failed: %s", exc)

            # Publish an initial debt/bedtime snapshot so Lovelace has
            # something better than the placeholder once history exists.
            try:
                await self._publish_debt_and_bedtime()
            except Exception as exc:    # noqa: BLE001
                logger.warning("Initial debt/bedtime publish failed: %s", exc)

            # Same for the v1.3.0 learning panel.
            try:
                await self._publish_learning_panel()
            except Exception as exc:    # noqa: BLE001
                logger.warning("Initial learning-panel publish failed: %s", exc)

            await ha.connect_websocket()
            await ha.subscribe_state_changes()

            tasks = [
                asyncio.create_task(
                    self._task_ws_listener(ha, discovery, engine),
                    name="ws_listener",
                ),
                asyncio.create_task(
                    self._task_inference_loop(engine, controller, ha),
                    name="inference_loop",
                ),
                asyncio.create_task(
                    self._task_session_checkpoint(controller),
                    name="session_checkpoint",
                ),
            ]

            if self.args.duration is not None:
                async def _stop_after(seconds: float) -> None:
                    await asyncio.sleep(seconds)
                    self.stop_event.set()

                tasks.append(asyncio.create_task(
                    _stop_after(self.args.duration), name="duration_timer",
                ))

            try:
                await self.stop_event.wait()
            finally:
                for t in tasks:
                    t.cancel()
                for t in tasks:
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
                self._persist_session(controller, partial=False)
        return 0

    # ================================================================== #
    # Natural-sleep helpers (v1.2.0)                                      #
    # ================================================================== #

    def _is_pre_wake(self, now: Any) -> bool:
        """True iff the current time is inside the light-ramp window.

        Used by :class:`WhiteNoiseMatcher` to override the stage-based
        soundscape with the dawn-chorus once we're approaching wake.
        """
        if self.wake_planner is None:
            return False
        from datetime import datetime as _dt, timedelta
        if not isinstance(now, _dt):
            return False
        ramp_start = self.wake_planner.window.end - timedelta(
            minutes=self.wake_planner.light_ramp_min,
        )
        return ramp_start <= now <= self.wake_planner.window.end

    def _ensure_wake_planner(self) -> Optional[SmartWakePlanner]:
        """Build or refresh ``self.wake_planner`` for tonight's window.

        Called lazily on each tick so the window always refers to the
        *next* occurrence; once a wake fires we mark the planner as
        woken and null it out so the next pass builds tomorrow's.
        """
        if self._wake_window_strs is None:
            return None
        if self.wake_planner is not None and not self.wake_planner._woken:
            return self.wake_planner
        try:
            window = WakeWindow.from_strings(*self._wake_window_strs)
        except Exception as exc:     # noqa: BLE001
            logger.warning("Bad wake window %r: %s", self._wake_window_strs, exc)
            return None
        self.wake_planner = SmartWakePlanner(window)
        logger.info(
            "Smart wake planner armed: window %s .. %s",
            window.start.isoformat(), window.end.isoformat(),
        )
        return self.wake_planner

    async def _wake_tick(
        self,
        ha: "HomeAssistantClient",
        stage: SleepStage,
        conf: float,
    ) -> None:
        """Advance the smart-wake state machine by one inference tick."""
        planner = self._ensure_wake_planner()
        if planner is None:
            return
        planner.observe_stage(stage, conf)
        plan = planner.tick(now=now_local())

        # Publish the decision regardless so the user sees progress.
        if self.publisher is not None:
            await self.publisher.publish_wake_decision(
                plan.decision.value,
                reason=plan.reason,
                alarm_time=plan.alarm_time,
                light_ramp_start=plan.light_ramp_start,
                matched_stage=plan.matched_stage,
            )

        # Light ramp: gentle brightness curve over the ramp window.
        if plan.decision in (WakeDecision.PRE_RAMP, WakeDecision.OPEN_WINDOW):
            if plan.light_ramp_start and plan.alarm_time and self._wake_light_targets:
                brightness = light_ramp_brightness(
                    now=now_local(),
                    ramp_start=plan.light_ramp_start,
                    ramp_end=plan.alarm_time,
                )
                if not self.ctrl_cfg.dry_run:
                    for eid in self._wake_light_targets:
                        try:
                            await ha.call_service(
                                "light", "turn_on",
                                entity_id=eid,
                                brightness_pct=max(1.0, brightness),
                            )
                        except Exception as exc:    # noqa: BLE001
                            logger.warning(
                                "Light ramp failed for %s: %s", eid, exc,
                            )

        if plan.decision == WakeDecision.FIRE_NOW:
            logger.info(
                "Smart wake: firing alarm (reason=%s stage=%s)",
                plan.reason, plan.matched_stage,
            )
            if not self.ctrl_cfg.dry_run:
                for eid in self._wake_light_targets:
                    try:
                        await ha.call_service(
                            "light", "turn_on",
                            entity_id=eid, brightness_pct=100,
                        )
                    except Exception as exc:    # noqa: BLE001
                        logger.warning("Wake light %s failed: %s", eid, exc)
            planner.mark_woken()

    async def _soundscape_tick(
        self,
        ha: "HomeAssistantClient",
        stage: SleepStage,
        conf: float,
    ) -> None:
        """Push the stage-appropriate soundscape to the user's speaker."""
        if self.sound_matcher is None:
            return
        policy = self.sound_matcher.policy_for(
            stage, conf, now=now_local(),
        )
        current_id = f"{policy.soundscape.value}@{int(policy.volume_pct)}"
        # Only act on genuine transitions.
        if current_id == self._last_soundscape:
            return
        self._last_soundscape = current_id

        if self.publisher is not None:
            await self.publisher.publish_soundscape(
                policy.soundscape.value,
                volume_pct=policy.volume_pct,
                reason=policy.reason,
            )

        if self.ctrl_cfg.dry_run:
            return
        target = self.sound_matcher.media_player_entity
        if not target:
            return
        try:
            if policy.soundscape == Soundscape.OFF:
                await ha.call_service(
                    "media_player", "media_stop", entity_id=target,
                )
                return
            url = self.sound_matcher.media_url(policy.soundscape)
            if url:
                await ha.call_service(
                    "media_player", "play_media",
                    entity_id=target,
                    media_content_id=url,
                    media_content_type="music",
                )
            await ha.call_service(
                "media_player", "volume_set",
                entity_id=target,
                volume_level=max(0.0, min(1.0, policy.volume_pct / 100.0)),
            )
        except Exception as exc:    # noqa: BLE001
            logger.warning("Soundscape control failed: %s", exc)

    # ------------------------------------------------------------------ #
    # Session lifecycle (v1.6.3)                                         #
    # ------------------------------------------------------------------ #
    #
    # Previously everything in ``__init__`` under "Runtime state" was
    # a one-shot assignment — the orchestrator would start a "session"
    # on boot and keep adding to its stage_counts until process exit,
    # producing one 30-day-long session per month of uptime.  The real
    # semantics of a "session" are:
    #
    #   * STARTS: the first time the debounced stage stays non-AWAKE
    #     for >= ``onset_dwell_seconds`` (default 5 min).  That's the
    #     standard PSG sleep-onset criterion.
    #   * ENDS:   after ``wake_dwell_seconds`` of continuous AWAKE
    #     (default 10 min) — long enough that a brief nighttime stir
    #     followed by falling back asleep doesn't close the session.
    #
    # Outside a session the controller still runs (so daytime comfort
    # is still adjusted by deadband + learned baseline), but:
    #
    #   - stage_counts / stage_sequence / env_by_stage are NOT updated
    #   - _persist_session does nothing
    #   - the quality score for a session is therefore bounded by what
    #     actually happened during that session, not all of last month

    def _reset_session_state(self) -> None:
        """Wipe per-session accumulators back to a fresh-session baseline.

        Called after ``_persist_session(partial=False)`` finalises a
        session, so the next sleep starts with zero'd counts + a new
        session id.  ``last_env`` is intentionally preserved because
        environment sensors live on a different timescale than
        sessions — the latest T/RH/lux reading is still valid for
        tomorrow night.
        """
        self.session_id = uuid.uuid4().hex[:8]
        self.session_started_at = time.time()
        self.stage_counts = {
            "AWAKE": 0, "LIGHT": 0, "DEEP": 0, "REM": 0,
        }
        self.stage_sequence = []
        self.env_by_stage = {}
        self._last_sampled_stage = None
        self._in_session = False
        self._consecutive_awake_ticks = 0
        self._consecutive_non_awake_ticks = 0

    def _maybe_advance_session_lifecycle(
        self,
        stage: SleepStage,
        controller: "SmartEnvironmentController",
    ) -> None:
        """Run the onset / wake-up state machine one tick.

        Must be called *every* inference tick (not only when the user
        is inside a session) so dwell counters track correctly across
        AWAKE ↔ non-AWAKE transitions.  Returns nothing; mutates
        ``_in_session`` and — on session end — calls
        :meth:`_persist_session` + :meth:`_reset_session_state`.
        """
        interval = max(1.0, float(self.args.infer_interval))
        # Convert dwell thresholds from seconds to tick counts so
        # integer-comparisons are cheap.
        onset_ticks = max(1, int(self._session_onset_dwell_seconds / interval))
        wake_ticks = max(1, int(self._session_wake_dwell_seconds / interval))

        if stage is SleepStage.AWAKE:
            self._consecutive_awake_ticks += 1
            self._consecutive_non_awake_ticks = 0
        else:
            self._consecutive_non_awake_ticks += 1
            self._consecutive_awake_ticks = 0

        if not self._in_session:
            # Onset detection — enough non-AWAKE dwell = session began.
            if self._consecutive_non_awake_ticks >= onset_ticks:
                self._in_session = True
                # Back-date the session start so we don't lose the
                # onset dwell window's worth of data from the record.
                self.session_started_at = time.time() - (
                    self._consecutive_non_awake_ticks * interval
                )
                logger.info(
                    _L(
                        "Session %s started (non-AWAKE held for >= %d s).",
                        "Session %s started / 睡眠会话已开始 (non-AWAKE held for >= %d s).",
                    ),
                    self.session_id, int(self._session_onset_dwell_seconds),
                )
                # v1.7.0 — kick off the apnea sample buffer at the
                # same boundary so respiratory data gets grouped with
                # the right session.  No-op when apnea disabled.
                self.apnea.begin_session()
            return

        # Already in a session — check for wake-up.
        if self._consecutive_awake_ticks >= wake_ticks:
            logger.info(
                _L(
                    "Session %s ending (AWAKE held for >= %d s).",
                    "Session %s ending / 睡眠会话结束 (AWAKE held for >= %d s).",
                ),
                self.session_id, int(self._session_wake_dwell_seconds),
            )
            self._persist_session(controller, partial=False)
            # v1.7.0 — finalise the apnea buffer + publish the trend.
            # Done BEFORE _reset_session_state so the publish task can
            # still read self.apnea state; the wiring owns its own
            # session bookkeeping so our reset doesn't touch it.
            self._finalise_apnea_session_async()
            self._reset_session_state()

    def _finalise_apnea_session_async(self) -> None:
        """Ask ``self.apnea`` for the final trend and schedule a publish.

        Lives on its own method so tests can stub it.  Failure modes
        (missing publisher, no baseline yet, apnea disabled) are all
        swallowed — apnea diagnostics must never block the main
        session-end flow, which carries the much more critical
        quality-score + learner persistence.
        """
        try:
            trend = self.apnea.end_session()
        except Exception as exc:    # noqa: BLE001
            logger.warning("Apnea end_session failed: %s", exc)
            return
        if trend is None or self.publisher is None:
            return
        try:
            loop = asyncio.get_event_loop()
            loop.create_task(self.publisher.publish_apnea_index(
                trend.value, status=self.apnea.status(),
            ))
        except RuntimeError:
            # No running loop (shutdown path) — skip silently.
            pass

    def _track_per_stage_env(self, raw_stage: SleepStage) -> None:
        """Record the current env as the snapshot for ``raw_stage``
        on the first tick we see this stage in the current session.

        We snapshot **on entry** rather than on exit because:

        * The user's body is reacting to the conditions they entered
          into; that's the causal signal we want to learn from.
        * Exit-time conditions are corrupted by the controller's own
          response — the AC may have already adjusted toward the *next*
          stage's setpoint thanks to v1.4.0 anticipation.

        Subsequent ticks within the same stage are skipped so we don't
        overwrite the entry snapshot.  A future occurrence of the same
        stage (e.g. wake-LIGHT-wake-LIGHT) **does** re-snapshot, since
        the env may have changed and the new entry is the freshest
        signal we have.
        """
        if self._last_sampled_stage is raw_stage:
            return
        # Capture a *copy* of last_env so subsequent env updates don't
        # mutate this stage's recorded snapshot via shared reference.
        # v1.6.4 note: we intentionally snapshot the RAW last_env
        # (not _safe_last_env) because env_by_stage is downstream
        # evidence for the learner — if the temperature reading was
        # stale on entry, the learner should know that happened on
        # that stage rather than seeing a None.  Staleness is still
        # visible via the env_ts timestamps carried alongside.
        self.env_by_stage[raw_stage.name] = EnvironmentParams(
            temperature_c=self.last_env.temperature_c,
            humidity_pct=self.last_env.humidity_pct,
            brightness_pct=self.last_env.brightness_pct,
            fan_speed_pct=self.last_env.fan_speed_pct,
        )
        self._last_sampled_stage = raw_stage

    def _safe_last_env(
        self, *, now: Optional[float] = None,
    ) -> EnvironmentParams:
        """Return a copy of ``last_env`` with stale fields masked to ``None``.

        v1.6.4 — HA's ``state_changed`` event is fire-and-forget: when
        a temperature sensor drops off the mesh the add-on doesn't get
        notified.  Without freshness tracking the controller would
        keep comparing to a 6-hour-old reading and either:

        * stop acting (the deadband thinks we're already at setpoint), or
        * act wrongly (fighting a reading that no longer reflects reality).

        This method consults the per-field update timestamps written
        by :meth:`_route_state_change` and substitutes ``None`` for
        any field older than ``_env_freshness_window_seconds`` (default
        15 min).  The controller treats ``None`` as "unknown" which
        its deadband logic already handles safely — an unknown field
        always falls outside any deadband so the stage's default
        setpoint gets reapplied, but the *current* reading doesn't
        contaminate future-stage anticipation.

        Also populates ``self._env_stale_fields`` so the diagnostic
        ``last_action`` sensor can surface which sensors went dark.
        """
        now = time.time() if now is None else now
        window = self._env_freshness_window_seconds
        stale: set[str] = set()

        def _pick(field_name: str, value: Optional[float]) -> Optional[float]:
            ts = self._env_ts.get(field_name, 0.0)
            if ts == 0.0:
                # Never observed — controller falls back to stage
                # default anyway.  Don't count as "stale"; it's
                # "uninitialised", which is semantically different.
                return None if value is None else value
            if now - ts > window:
                stale.add(field_name)
                return None
            return value

        env = EnvironmentParams(
            temperature_c=_pick(
                "temperature_c", self.last_env.temperature_c,
            ),
            humidity_pct=_pick(
                "humidity_pct", self.last_env.humidity_pct,
            ),
            brightness_pct=_pick(
                "brightness_pct", self.last_env.brightness_pct,
            ),
            # fan_speed_pct is never a sensor input — it's always a
            # controller output, so freshness doesn't apply.
            fan_speed_pct=self.last_env.fan_speed_pct,
        )
        self._env_stale_fields = stale
        return env

    def _effective_control_stage(self, raw_stage: SleepStage) -> SleepStage:
        """Map the *observed* stage to the stage the controller should act on.

        Currently the only transformation is **wind-down substitution**:
        if the user is still AWAKE but we're within
        ``ctrl_cfg.wind_down_minutes`` of their learned bedtime,
        substitute LIGHT so the bedroom is pre-cooled by the time they
        actually lie down.  Any other stage passes through unchanged.

        Returning ``raw_stage`` (identity) is the safe default whenever
        the prerequisites for wind-down aren't met (no learner, no
        history yet, or `wind_down_minutes=0`).
        """
        if raw_stage is not SleepStage.AWAKE:
            return raw_stage
        if self.learner is None or self.ctrl_cfg is None:
            return raw_stage
        wind_down_minutes = getattr(self.ctrl_cfg, "wind_down_minutes", 0) or 0
        if wind_down_minutes <= 0:
            return raw_stage
        bedtime = self._bedtime_recommendation_cached()
        if bedtime is None:
            return raw_stage
        if is_in_wind_down(now_local(), bedtime, wind_down_minutes):
            return SleepStage.LIGHT
        return raw_stage

    # 60 s — same time-scale as user perception of "is it bedtime yet";
    # any finer is wasted CPU.
    _BEDTIME_CACHE_TTL_SECONDS: float = 60.0

    def _bedtime_recommendation_cached(self) -> Optional[Dict[str, Any]]:
        """Return the learner's bedtime recommendation, cached for 60 s.

        ``recommend_bedtime`` walks every session in history doing a
        weighted-median, so calling it on every 30 s inference tick
        (and again from the wind-down check) burns CPU for no benefit.
        Returns ``None`` if the learner is missing or raised — callers
        treat that as "no wind-down".
        """
        if self.learner is None:
            return None
        now = time.time()
        if (
            self._bedtime_cache is not None
            and now - self._bedtime_cached_at < self._BEDTIME_CACHE_TTL_SECONDS
        ):
            return self._bedtime_cache
        try:
            fresh = self.learner.recommend_bedtime(now=now_local())
        except Exception as exc:    # noqa: BLE001
            # Corrupted history, IO error, etc.  Don't poison the
            # control loop; return None so wind-down stays off.
            logger.debug("wind-down: recommend_bedtime failed: %s", exc)
            return None
        self._bedtime_cache = fresh
        self._bedtime_cached_at = now
        return fresh

    # ------------------------------------------------------------------ #
    # Learning panel facade (v1.6.0)                                     #
    # ------------------------------------------------------------------ #
    #
    # The two public methods below are thin shims onto
    # :class:`LearningPanelPublisher`.  Pre-v1.6 they were ~50-line
    # methods inline on this class; extracting them dropped this file
    # off the BACKLOG's biggest-file hotspot and makes the panel
    # independently unit-testable.

    @property
    def _panel(self) -> LearningPanelPublisher:
        """Lazy-initialised because the publisher doesn't exist at __init__.

        Recreated whenever the publisher reference flips (e.g. after a
        WebSocket reconnect rebuilds the publisher), so the panel never
        holds a stale reference.
        """
        cached = getattr(self, "_panel_cache", None)
        if cached is not None and cached.publisher is self.publisher:
            return cached
        wake_strs = list(self._wake_window_strs or [])
        new_panel = LearningPanelPublisher(
            learner=self.learner,
            publisher=self.publisher,
            profile=self.profile,
            wake_window_strs=wake_strs,
            env_provider=lambda: self.last_env,
        )
        self._panel_cache = new_panel
        return new_panel

    async def _publish_debt_and_bedtime(self) -> None:
        """Refresh sensor.sleep_classifier_debt_hours + recommended_bedtime."""
        await self._panel.publish_debt_and_bedtime()

    async def _publish_learning_panel(self) -> None:
        """Refresh the v1.3 + v1.5 preference-learning sensors."""
        await self._panel.publish_learning_panel()

    def _log_binding_help(self, entities: List[HAEntity]) -> None:
        """Print top candidate entity_ids per slot to help the user bind them.

        Triggered when discovery finds no usable sensor.  The candidates are
        produced by re-running discovery with the area filter disabled, so
        the user sees suggestions from the entire HA registry even if they
        haven't assigned anything to an area yet.
        """
        try:
            suggestions = DeviceDiscovery.suggest_candidates(
                entities, self.disc_cfg, limit_per_bucket=5,
            )
        except Exception as exc:    # noqa: BLE001
            logger.warning("Could not build suggestion list: %s", exc)
            return
        if not any(suggestions.values()):
            logger.warning(
                "No candidate sensors anywhere in HA either. The most likely "
                "causes are (a) your sensors aren't yet integrated into HA, "
                "or (b) their entity_ids and friendly_names don't contain any "
                "of the bilingual keywords. You can extend the keyword lists "
                "in the add-on Configuration tab, or just paste the entity_id "
                "of your sensor into the matching *_source field.",
            )
            return
        logger.error("=" * 60)
        logger.error("Suggested entity_ids — paste them into the matching ")
        logger.error("slot fields in the add-on Configuration tab:")
        for slot, ids in suggestions.items():
            if ids:
                logger.error("  %-13s → %s", slot, ids)
        logger.error("=" * 60)

    def _seed_current_env(
        self,
        entities: List[HAEntity],
        discovery: DiscoveryResult,
    ) -> None:
        """Populate ``self.last_env`` from the most recent HA state values."""
        by_id = {e.entity_id: e for e in entities}
        if discovery.sensors.temperature:
            v = by_id.get(discovery.sensors.temperature[0].entity_id)
            if v is not None:
                self.last_env.temperature_c = v.numeric_state()
        if discovery.sensors.humidity:
            v = by_id.get(discovery.sensors.humidity[0].entity_id)
            if v is not None:
                self.last_env.humidity_pct = v.numeric_state()
        if discovery.sensors.illuminance:
            v = by_id.get(discovery.sensors.illuminance[0].entity_id)
            if v is not None:
                self.last_env.brightness_pct = v.numeric_state()
        logger.info(
            "Initial environment: T=%s°C  H=%s%%  bright=%s",
            self.last_env.temperature_c, self.last_env.humidity_pct,
            self.last_env.brightness_pct,
        )

    def _seed_live_state(
        self,
        entities: List[HAEntity],
        discovery: DiscoveryResult,
    ) -> None:
        """Seed :attr:`live_state` with the HA-registry snapshot so the
        very first plan_actions() tick has accurate on/off + availability
        data.

        Without this, the controller would see every bound entity as
        "unknown → optimistic → let's dispatch!" for the first
        state_changed event's worth of time (up to ``infer_interval``
        seconds), which is long enough to send a climate.set_temperature
        to an AC that's been off all day and have nothing happen.

        Timestamps are back-dated to the entity's ``last_changed`` from
        HA where available so the user-override grace window doesn't
        fire spuriously for an entity that last changed hours ago.
        """
        now = time.time()
        bound_ids: set[str] = set()
        for bucket in (
            discovery.devices.lights, discovery.devices.climates,
            discovery.devices.fans, discovery.devices.humidifiers,
            discovery.devices.switches, discovery.devices.media_players,
        ):
            for e in bucket:
                bound_ids.add(e.entity_id)
        by_id = {e.entity_id: e for e in entities}
        seeded = 0
        for eid in bound_ids:
            ent = by_id.get(eid)
            if ent is None:
                continue
            self.live_state.seed_from_registry(
                eid, ent.state, ent.attributes, now=now,
            )
            seeded += 1
        if seeded:
            logger.info(
                "Live-state cache seeded with %d actionable entities "
                "from the HA registry.",
                seeded,
            )

    async def _run_dry_with_synthetic_signals(self, engine: _Engine) -> None:
        """Tiny offline smoke-test path: cycles synthetic stages and prints actions.

        v1.3.0: with the model gone, the dry-run loop just rotates
        through AWAKE → LIGHT → DEEP → REM at a fixed cadence so an
        operator can validate routing / publishing without standing up a
        real HA instance.
        """
        logger.info("Running offline synthetic loop for %.1fs", self.args.duration or 10)
        cycle = [SleepStage.AWAKE, SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM]
        deadline = time.time() + (self.args.duration or 10)
        i = 0
        while time.time() < deadline:
            engine.observe(
                engine.stage_entity_id,
                cycle[i % len(cycle)].name,
                attributes={"confidence": 0.9},
            )
            stage, conf = engine.current()
            logger.info("stage=%s conf=%.2f", stage.name, conf)
            i += 1
            await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Home Assistant integration for the Sleep Classifier add-on "
            "(v1.3.0+).  Subscribes to an external sleep-stage entity and "
            "learns the user's optimal sleep environment over time."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", default="training_config/config.json")
    p.add_argument("--base-url", default=None,
                   help="HA base URL (overrides config). e.g. http://homeassistant.local:8123")
    p.add_argument("--token", default=None,
                   help="Long-Lived Access Token (overrides config + HA_TOKEN env var).")
    p.add_argument("--area", default=None,
                   help="Restrict discovery to entities in this area / keyword.")
    p.add_argument(
        "--infer-interval", type=float, default=30.0,
        help="Seconds between inference + control actions (default: 30).",
    )
    p.add_argument(
        "--session-interval", type=float, default=1800.0,
        help="Seconds between learner checkpoints (default: 1800 = 30 min).",
    )
    p.add_argument(
        "--duration", type=float, default=None,
        help="Stop after N seconds (smoke tests).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Plan actions but never call HA services (still uses live state).",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    service = SmartSleepService(args)

    def _shutdown(signum, _frame):    # noqa: ARG001
        logger.info("Signal %s — shutting down", signum)
        try:
            asyncio.get_event_loop().call_soon_threadsafe(service.stop_event.set)
        except RuntimeError:
            pass

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    try:
        return asyncio.run(service.run())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
