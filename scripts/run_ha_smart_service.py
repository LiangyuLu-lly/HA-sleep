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
from src.smart_environment_controller import (
    SmartControlConfig,
    SmartEnvironmentController,
)
from src.training_pipeline import TrainingPipeline

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
        try:
            async for event in ha.iter_state_changes():
                self._route_state_change(event, discovery, engine)
                if self.stop_event.is_set():
                    break
        except asyncio.CancelledError:
            logger.info("WebSocket task cancelled")
            raise
        except Exception as exc:    # noqa: BLE001
            logger.exception("WebSocket task crashed: %s", exc)
            self.stop_event.set()

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
                logger.error(
                    "No heart-rate or movement sensors found in area '%s'. "
                    "Adjust --area or the keywords in config.json.",
                    self.disc_cfg.area_filter,
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
                self._persist_session(controller, partial=False)
        return 0

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
