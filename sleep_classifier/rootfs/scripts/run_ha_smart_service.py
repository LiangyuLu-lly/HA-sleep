"""Deeply-integrated Home Assistant sleep service.

Pipeline (single ``asyncio`` loop):

1. **Connect** to HA REST API using a Long-Lived Access Token.
2. **Discover** entities: physiological sensors (HR, motion), environment
   sensors (temperature, humidity, illuminance), and actionable devices
   (lights, climates, fans, humidifiers).
3. **Subscribe** to ``state_changed`` over the WebSocket.  Whenever a HR or
   motion sensor publishes a new value, push the sample into the inference
   engine.
4. Every ``infer_interval`` seconds, run the CNN-BiLSTM and produce a stage
   prediction.  The :class:`SmartEnvironmentController` plans + executes the
   matching HA service calls.
5. Every ``session_interval`` (default 30 min), persist a partial
   :class:`SleepSession` so the learner gets ongoing reward signals; record
   the final session on graceful shutdown.

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
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

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
from src.sleep_state_publisher import SleepStatePublisher
from src.smart_environment_controller import (
    SmartControlConfig,
    SmartEnvironmentController,
)
from src.smart_wake import (
    SmartWakePlanner,
    WakeDecision,
    WakeWindow,
    light_ramp_brightness,
)
from src.external_stage_subscriber import ExternalStageSubscriber
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

        # ---- 3. Environment sensors ---------------------------------
        value = event.new_state.numeric_state()
        if value is None:
            return
        if any(e.entity_id == eid for e in discovery.sensors.temperature):
            self.last_env.temperature_c = value
        elif any(e.entity_id == eid for e in discovery.sensors.humidity):
            self.last_env.humidity_pct = value
        elif any(e.entity_id == eid for e in discovery.sensors.illuminance):
            self.last_env.brightness_pct = value

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
            jitter = backoff * 0.2
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=backoff + (jitter * (np.random.random() - 0.5) * 2.0),
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
                self.stage_counts[stage.name] = self.stage_counts.get(stage.name, 0) + 1
                self.stage_sequence.append(stage)
                logger.info(
                    "infer stage=%s conf=%.2f  env(T=%s H=%s)",
                    stage.name, conf,
                    self.last_env.temperature_c, self.last_env.humidity_pct,
                )
                actions = await controller.apply(stage, self.last_env)
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
                        first = actions[0]
                        summary = (
                            f"{first.get('domain', '?')}."
                            f"{first.get('service', '?')} → "
                            f"{first.get('entity_id', '?')}"
                        )
                        await self.publisher.publish_last_action(
                            summary, executed=not self.ctrl_cfg.dry_run,
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
        )
        try:
            self.learner.record_session(session)
            controller.feedback_score(score)
            logger.info(
                "Session %s checkpoint — quality=%.1f (history: %s)",
                session.session_id, score, self.learner.status(),
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
        engine = ExternalStageSubscriber(
            stage_entity_id=self.sleep_stage_source or "sensor.dry_run_stage",
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
            )

            # Initial environment snapshot
            self._seed_current_env(entities, discovery)

            # v1.3.0: no buffer to restore — the subscriber's first real
            # state-changed event will populate the cached stage within
            # one HA WebSocket round-trip (~50 ms typical).

            # Diagnostic state publisher — populates HA Lovelace entities.
            self.publisher = SleepStatePublisher(ha)

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

    async def _publish_debt_and_bedtime(self) -> None:
        """Refresh sensor.sleep_classifier_debt_hours + recommended_bedtime."""
        if self.learner is None or self.publisher is None:
            return
        try:
            sessions = self.learner.sessions()
            tracker = SleepDebtTracker.from_sessions(self.profile, sessions)
            plan = tracker.plan_recovery(wake_window=self._wake_window_strs)
            await self.publisher.publish_debt(
                plan.current_debt_hours,
                severity=plan.severity.value,
                target_hours=self.profile.recommended_total_sleep_hours(),
                nights_to_full_recovery=plan.nights_to_full_recovery,
            )
            await self.publisher.publish_recommended_bedtime(
                plan.tonight_bedtime,
                tonight_target_hours=plan.tonight_target_hours,
                reason=plan.message,
            )
        except Exception as exc:    # noqa: BLE001
            logger.warning("Could not publish debt/bedtime: %s", exc)

    async def _publish_learning_panel(self) -> None:
        """Refresh the four v1.3.0 preference-learning sensors.

        Pulls the latest snapshot from :class:`PreferenceLearner` and
        forwards each piece to the corresponding publisher method.  Any
        exception is logged but never re-raised so a bad learner state
        can't interrupt the inference loop.
        """
        if self.learner is None or self.publisher is None:
            return
        try:
            defaults = EnvironmentParams(
                temperature_c=self.last_env.temperature_c,
                humidity_pct=self.last_env.humidity_pct,
                brightness_pct=self.last_env.brightness_pct,
                fan_speed_pct=self.last_env.fan_speed_pct,
            )
            bedtime = self.learner.recommend_bedtime()
            await self.publisher.publish_learned_bedtime(bedtime)

            knn = self.learner.recommend_knn(
                defaults,
                current_temp_c=self.last_env.temperature_c,
            )
            await self.publisher.publish_learned_environment(
                knn["env"].to_dict(),
                confidence=float(knn.get("confidence", 0.0)),
                n_used=int(knn.get("n_used", 0)),
            )

            explanation = self.learner.explain(
                defaults,
                current_temp_c=self.last_env.temperature_c,
            )
            await self.publisher.publish_recommendation_explain(explanation)
        except Exception as exc:    # noqa: BLE001
            logger.warning("Could not publish learning panel: %s", exc)

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
