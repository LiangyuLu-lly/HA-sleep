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
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from config.config_loader import load_config
from src.data_structures import SleepStage
from src.device_discovery import DeviceDiscovery, DiscoveryConfig, DiscoveryResult
from src.ha_api_client import (
    HAAPIError,
    HAAuthError,
    HAEntity,
    HomeAssistantClient,
    StateChangeEvent,
)
from src.preference_learner import (
    EnvironmentParams,
    PreferenceConfig,
    PreferenceLearner,
    SleepSession,
    compute_quality_score,
)
from src.sleep_state_publisher import SleepStatePublisher
from src.smart_environment_controller import (
    SmartControlConfig,
    SmartEnvironmentController,
)
from src.training_pipeline import TrainingPipeline

# Buffer persistence path: ``/data`` is the supervisor-managed volume so the
# file survives add-on upgrades.  Outside the add-on we fall back to the
# project root so dev runs work too.
_BUFFER_DIR = Path("/data") if Path("/data").is_dir() else PROJECT_ROOT
_BUFFER_PATH = _BUFFER_DIR / "inference_buffer.npz"
# Don't restore a buffer older than this — stale physiology samples from a
# previous night would mislead the model worse than a fresh cold-start.
_BUFFER_MAX_AGE_S = 6 * 3600   # 6 hours

logger = logging.getLogger("smart_service")


# ---------------------------------------------------------------------------
# Inference engine — rolling buffer + CNN-BiLSTM forward pass
# ---------------------------------------------------------------------------


class _InferenceEngine:
    """Minimal sliding-window inference engine shared with run_ha_service."""

    _WINDOW = 1024  # samples (must match TrainingPipeline.window_size)

    def __init__(self, model_path: Path, config_path: Path) -> None:
        self._pipeline = TrainingPipeline(config_path=str(config_path))
        if model_path.exists():
            self._pipeline.load_model(str(model_path))
            self.model_loaded = True
        else:
            logger.warning(
                "Model %s not found — running with random classifier weights",
                model_path,
            )
            self.model_loaded = False
        self.hr_buf: Deque[float] = deque(maxlen=self._WINDOW)
        self.mv_buf: Deque[float] = deque(maxlen=self._WINDOW)
        # Bootstrap normaliser so transform() works without a fit() call.
        self._pipeline._normalizer._fitted = True
        self._pipeline._normalizer._hr_mean = 75.0
        self._pipeline._normalizer._hr_std = 15.0
        self._pipeline._normalizer._mv_mean = 0.5
        self._pipeline._normalizer._mv_std = 0.5

    def push_hr(self, value: float) -> None:
        self.hr_buf.append(float(value))

    def push_movement(self, value: float) -> None:
        self.mv_buf.append(float(value))

    def buffer_ready(self) -> bool:
        return len(self.hr_buf) >= self._WINDOW and len(self.mv_buf) >= self._WINDOW

    # ------------------------------------------------------------------ #
    # Persistence: keep buffers warm across add-on restarts.            #
    # ------------------------------------------------------------------ #

    def save_buffers(self, path: Path) -> None:
        """Atomically write the rolling buffers to ``path``.

        Saves to ``<path>.tmp`` first then renames, so a crash during the
        write never leaves a half-written file (which numpy would refuse
        to load on the next boot).
        """
        if not self.hr_buf and not self.mv_buf:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # ``np.savez_compressed`` silently appends ``.npz`` if the
            # destination doesn't already end in that suffix.  We therefore
            # write to an explicit ``<path>.tmp`` *file handle* so the suffix
            # logic doesn't kick in and our atomic rename stays predictable.
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "wb") as fh:
                np.savez_compressed(
                    fh,
                    hr=np.asarray(self.hr_buf, dtype=np.float32),
                    mv=np.asarray(self.mv_buf, dtype=np.float32),
                    saved_at=np.float64(time.time()),
                )
            tmp.replace(path)
            logger.info(
                "inference_buffer saved (hr=%d, mv=%d) → %s",
                len(self.hr_buf), len(self.mv_buf), path,
            )
        except Exception as exc:    # noqa: BLE001
            logger.warning("Could not persist inference buffer: %s", exc)

    def restore_buffers(self, path: Path, max_age_s: float) -> bool:
        """Best-effort restore from ``path`` if the file is fresh enough.

        Returns True iff samples were actually loaded (used by the service
        to bypass the WS warm-up if we were already warm pre-restart).
        """
        if not path.exists():
            return False
        try:
            data = np.load(path, allow_pickle=False)
            saved_at = float(data["saved_at"]) if "saved_at" in data else 0.0
            age = time.time() - saved_at
            if age > max_age_s:
                logger.info(
                    "inference_buffer at %s is %.0fs old (>%.0fs) — ignoring",
                    path, age, max_age_s,
                )
                return False
            for v in data["hr"][-self._WINDOW :]:
                self.hr_buf.append(float(v))
            for v in data["mv"][-self._WINDOW :]:
                self.mv_buf.append(float(v))
            logger.info(
                "inference_buffer restored (hr=%d, mv=%d, age=%.0fs)",
                len(self.hr_buf), len(self.mv_buf), age,
            )
            return True
        except Exception as exc:    # noqa: BLE001
            logger.warning("Could not restore inference buffer: %s", exc)
            return False

    def infer(self) -> tuple[SleepStage, float]:
        if not self.buffer_ready():
            return (SleepStage.LIGHT, 0.25)
        hr = np.asarray(self.hr_buf, dtype=np.float32)
        mv = np.asarray(self.mv_buf, dtype=np.float32)
        hr_n = (hr - self._pipeline._normalizer._hr_mean) / (
            self._pipeline._normalizer._hr_std + 1e-9
        )
        mv_n = (mv - self._pipeline._normalizer._mv_mean) / (
            self._pipeline._normalizer._mv_std + 1e-9
        )
        feats = self._pipeline._extract_features_for_sample(hr_n, mv_n)
        probs = self._pipeline._classifier_forward(feats.reshape(1, -1))[0]
        idx = int(np.argmax(probs))
        return (SleepStage(idx), float(probs[idx]))


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

        # Runtime state
        self.session_id = uuid.uuid4().hex[:8]
        self.session_started_at = time.time()
        self.stage_counts: Dict[str, int] = {
            "AWAKE": 0, "LIGHT": 0, "DEEP": 0, "REM": 0,
        }
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
        engine: _InferenceEngine,
    ) -> None:
        if event.new_state is None:
            return
        eid = event.entity_id
        value = event.new_state.numeric_state()
        if value is None:
            return

        if any(e.entity_id == eid for e in discovery.sensors.heart_rate):
            engine.push_hr(value)
            # Pair the heart-rate sample with the most recent movement so
            # both buffers stay aligned.  If movement hasn't been seen yet,
            # we duplicate the last hr value as a zero-movement placeholder.
            if engine.mv_buf:
                engine.mv_buf.append(engine.mv_buf[-1])
            else:
                engine.push_movement(0.0)
        elif any(e.entity_id == eid for e in discovery.sensors.movement):
            engine.push_movement(value)
            if engine.hr_buf:
                engine.hr_buf.append(engine.hr_buf[-1])
            else:
                engine.push_hr(75.0)
        elif (
            not discovery.sensors.heart_rate
            and any(e.entity_id == eid for e in discovery.sensors.breathing)
        ):
            # mmWave radars expose respiration rate (~12-20 rpm) which is
            # numerically unlike a heart-rate (~50-90 bpm) but carries the
            # same physiological-rhythm signal.  Scale to roughly the HR
            # range so the trained normaliser doesn't clip it before
            # routing it to the HR buffer.  This branch only fires when
            # the user has *not* bound a real HR source.
            engine.push_hr(float(value) * 4.0)
            if engine.mv_buf:
                engine.mv_buf.append(engine.mv_buf[-1])
            else:
                engine.push_movement(0.0)
        elif any(e.entity_id == eid for e in discovery.sensors.temperature):
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
        engine: _InferenceEngine,
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
        engine: _InferenceEngine,
        controller: SmartEnvironmentController,
    ) -> None:
        try:
            while not self.stop_event.is_set():
                stage, conf = engine.infer()
                self.stage_counts[stage.name] = self.stage_counts.get(stage.name, 0) + 1
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

                # Periodic buffer dump so a sudden power loss never wipes
                # more than ``infer_interval`` seconds of warm-up.
                engine.save_buffers(_BUFFER_PATH)

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
        score = compute_quality_score(self.stage_counts)
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
            notes="auto checkpoint" if partial else "session end",
        )
        try:
            self.learner.record_session(session)
            controller.feedback_score(score)
            logger.info(
                "Session %s checkpoint — quality=%.1f (history: %s)",
                session.session_id, score, self.learner.status(),
            )
            # Reflect the latest score on the Lovelace card.  We're inside a
            # sync method here so schedule the coroutine — best-effort.
            if self.publisher is not None:
                try:
                    asyncio.get_event_loop().create_task(
                        self.publisher.publish_quality(score),
                    )
                except RuntimeError:
                    # No running loop (e.g. last call during shutdown). Skip.
                    pass
        except Exception as exc:    # noqa: BLE001
            logger.error("Failed to persist session: %s", exc)

    # ------------------------------------------------------------------ #
    # Main entry                                                         #
    # ------------------------------------------------------------------ #

    async def run(self) -> int:
        config_path = (PROJECT_ROOT / self.args.config).resolve()
        engine = _InferenceEngine(
            model_path=(PROJECT_ROOT / self.args.model).resolve(),
            config_path=config_path,
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

            if not discovery.has_minimum_sensors():
                self._log_binding_help(entities)
                logger.error(
                    "No heart-rate / movement / breathing sensor found. "
                    "Open the add-on Configuration tab and fill the *_source "
                    "slot fields with one of the candidate entity_ids above.",
                )
                return 3

            controller = SmartEnvironmentController(
                config=self.ctrl_cfg,
                ha_client=ha,
                devices=discovery.devices,
                learner=self.learner,
            )

            # Initial environment snapshot
            self._seed_current_env(entities, discovery)

            # Restore any buffer left behind by a previous run.  This
            # avoids the ~10 minute cold-start window after every add-on
            # restart — the most common user complaint.
            engine.restore_buffers(_BUFFER_PATH, max_age_s=_BUFFER_MAX_AGE_S)

            # Diagnostic state publisher — populates HA Lovelace entities.
            self.publisher = SleepStatePublisher(ha)

            await ha.connect_websocket()
            await ha.subscribe_state_changes()

            tasks = [
                asyncio.create_task(
                    self._task_ws_listener(ha, discovery, engine),
                    name="ws_listener",
                ),
                asyncio.create_task(
                    self._task_inference_loop(engine, controller),
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
                # Save buffer first so even a crash during session persistence
                # doesn't lose the warm-up samples.
                engine.save_buffers(_BUFFER_PATH)
                self._persist_session(controller, partial=False)
        return 0

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

    async def _run_dry_with_synthetic_signals(self, engine: _InferenceEngine) -> None:
        """Tiny offline smoke-test path: pushes random HR/movement and prints actions."""
        logger.info("Running offline synthetic loop for %.1fs", self.args.duration or 10)
        import random as _r
        deadline = time.time() + (self.args.duration or 10)
        while time.time() < deadline:
            engine.push_hr(_r.uniform(55, 80))
            engine.push_movement(_r.uniform(0, 1))
            if engine.buffer_ready():
                stage, conf = engine.infer()
                logger.info("stage=%s conf=%.2f", stage.name, conf)
            await asyncio.sleep(0.05)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Deep Home Assistant integration for the CNN-BiLSTM sleep model.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", default="config/config.json")
    p.add_argument("--model", default="models/best_model.h5")
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
