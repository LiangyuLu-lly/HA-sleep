"""Long-running Home Assistant bridge service.

This is the deployment entry-point: it loads the trained CNN-BiLSTM model,
keeps a sliding window of incoming sensor data, runs inference at a fixed
cadence, and publishes both the result and the raw measurements to a Home
Assistant MQTT broker using the Discovery protocol implemented in
``src/ha_integration.py``.

Two data sources are supported:

``replay`` (default, great for demos and CI)
    The service loads one or more Sleep-EDF subjects and streams the
    recording through the pipeline at *wall-clock* speed (optionally
    accelerated with ``--speedup``).  This is what you would demo at the
    project defence — a full overnight recording compressed into a few
    minutes, with HA reacting to every stage transition.

``mqtt`` (production)
    The service subscribes to the broker's heart-rate / movement topics
    (typically published by a smart band, a Zigbee2MQTT contact sensor, an
    ESPHome device, ...) and runs inference whenever a fresh window of
    samples is available.

Usage
-----
.. code-block:: bash

    # Demo / smoke-test (no broker, no model needed)
    python scripts/run_ha_service.py --dry-run --duration 60 --speedup 60

    # Replay an overnight recording into a real HA broker
    python scripts/run_ha_service.py \\
        --broker 192.168.1.100 --port 1883 \\
        --username homeassistant --password "..." \\
        --source replay --subjects ST7011 --speedup 30

    # Production: subscribe to real-time sensor topics
    python scripts/run_ha_service.py \\
        --broker 192.168.1.100 --source mqtt
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Deque, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from config.config_loader import load_config
from src.data_structures import HeartRateData, MovementData, SleepStage
from src.dataset_loader import DatasetLoader, DatasetLoadError
from src.ha_integration import HAConfig, HomeAssistantBridge
from src.training_pipeline import TrainingPipeline

logger = logging.getLogger("ha_service")

try:
    import paho.mqtt.client as mqtt  # type: ignore[import]
    PAHO_AVAILABLE = True
except ImportError:  # pragma: no cover
    PAHO_AVAILABLE = False
    mqtt = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Inference engine — wraps the trained model and a rolling sensor buffer.
# ---------------------------------------------------------------------------


class _InferenceEngine:
    """Buffers samples and produces a sleep stage / confidence on demand.

    Keeps `_WINDOW` samples in a deque so the most recent ``window_size``
    measurements are always ready for the model.  Falls back to a sensible
    default prediction (LIGHT @ 0.25 confidence) when the buffer is not yet
    full so the HA dashboard never shows ``unknown``.
    """

    _WINDOW = 1024  # must match TrainingPipeline._build_feature_matrix default

    def __init__(self, model_path: Path, config_path: Path) -> None:
        self._pipeline = TrainingPipeline(config_path=str(config_path))
        if model_path.exists():
            self._pipeline.load_model(str(model_path))
            self._model_loaded = True
        else:
            logger.warning(
                "Model file %s not found — service will run with a randomly "
                "initialised classifier (still produces valid MQTT output).",
                model_path,
            )
            self._model_loaded = False
        self.hr_buf: Deque[float] = deque(maxlen=self._WINDOW)
        self.mv_buf: Deque[float] = deque(maxlen=self._WINDOW)
        # Bootstrap the normaliser with neutral statistics so transform()
        # works even when no training set is provided.
        self._pipeline._normalizer._fitted = True
        self._pipeline._normalizer._hr_mean = 75.0
        self._pipeline._normalizer._hr_std = 15.0
        self._pipeline._normalizer._mv_mean = 0.5
        self._pipeline._normalizer._mv_std = 0.5

    def push_samples(self, hr: np.ndarray, mv: np.ndarray) -> None:
        """Append new measurements to the rolling buffers."""
        for v in hr.flat:
            self.hr_buf.append(float(v))
        for v in mv.flat:
            self.mv_buf.append(float(v))

    def buffer_ready(self) -> bool:
        return len(self.hr_buf) >= self._WINDOW

    def latest_means(self) -> tuple[float, float]:
        if not self.hr_buf:
            return (0.0, 0.0)
        return (float(np.mean(self.hr_buf)), float(np.mean(self.mv_buf)))

    def infer(self) -> tuple[SleepStage, float]:
        """Run the CNN-BiLSTM-classifier chain on the rolling buffer."""
        if not self.buffer_ready():
            return (SleepStage.LIGHT, 0.25)

        hr = np.asarray(self.hr_buf, dtype=np.float32)
        mv = np.asarray(self.mv_buf, dtype=np.float32)

        # Normalise using the pipeline's fitted stats (or the bootstrap).
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
# Replay source
# ---------------------------------------------------------------------------


def run_replay(
    engine: _InferenceEngine,
    bridge: HomeAssistantBridge,
    loader: DatasetLoader,
    args: argparse.Namespace,
    stop_event: threading.Event,
) -> None:
    """Stream an EDF recording through the bridge at wall-clock speed."""
    data_dir = (PROJECT_ROOT / args.data_dir).resolve()
    logger.info("Replay source — loading subjects %s from %s",
                args.subjects, data_dir)
    dataset = loader.load_sleep_edf_telemetry(
        str(data_dir),
        subjects=args.subjects,
        target_sampling_rate=args.target_sampling_rate,
    )

    hr_values = dataset.heart_rate.values
    mv_values = dataset.movement.values
    stages = dataset.sleep_stages.stages
    fs = args.target_sampling_rate
    total = len(hr_values)
    logger.info(
        "Streaming %d samples (%.1f minutes) at %dx speedup",
        total, total / fs / 60.0, args.speedup,
    )

    publish_every = max(1, int(args.publish_interval * fs))
    cursor = 0
    next_publish = publish_every
    start = time.time()

    while cursor < total and not stop_event.is_set():
        chunk = min(fs, total - cursor)  # one second of samples per tick
        engine.push_samples(hr_values[cursor : cursor + chunk],
                            mv_values[cursor : cursor + chunk])
        cursor += chunk

        if cursor >= next_publish:
            stage, confidence = engine.infer()
            hr_mean, mv_mean = engine.latest_means()
            true_stage = SleepStage(int(stages[min(cursor - 1, total - 1)]))
            bridge.publish_state(
                sleep_stage=stage,
                confidence=confidence,
                heart_rate=hr_mean,
                movement=mv_mean,
                smoke_alarm=False,
                gas_alarm=False,
                extra={"true_stage": true_stage.name,
                       "cursor_sample": cursor,
                       "elapsed_seconds": time.time() - start},
            )
            next_publish += publish_every
            logger.info(
                "t=%6.1fs  HR=%5.1f  MV=%5.2f  true=%-5s  pred=%-5s  conf=%.2f",
                cursor / fs, hr_mean, mv_mean,
                true_stage.name, stage.name, confidence,
            )

        # Pace the loop: 1 simulated second per (1 / speedup) wall seconds.
        if args.speedup > 0:
            time.sleep(1.0 / args.speedup)

    logger.info("Replay finished — streamed %d samples in %.1fs",
                cursor, time.time() - start)


# ---------------------------------------------------------------------------
# Live MQTT source — subscribe to broker, run inference on rolling window.
# ---------------------------------------------------------------------------


def run_mqtt_source(
    engine: _InferenceEngine,
    bridge: HomeAssistantBridge,
    args: argparse.Namespace,
    stop_event: threading.Event,
) -> None:
    """Subscribe to real sensor topics and react to incoming data."""
    if not PAHO_AVAILABLE:
        raise RuntimeError(
            "paho-mqtt is required for --source mqtt. Install via `pip install paho-mqtt`."
        )

    topics_cfg = args._config["mqtt"]["topics"]
    hr_topic = topics_cfg.get("heart_rate", "sensors/heart_rate")
    mv_topic = topics_cfg.get("movement", "sensors/movement")
    smoke_topic = topics_cfg.get("smoke_sensor", "sensors/smoke")
    gas_topic = topics_cfg.get("gas_sensor", "sensors/gas")

    smoke_threshold = float(args._config["disaster_monitoring"]["smoke_threshold"])
    gas_threshold = float(args._config["disaster_monitoring"]["gas_threshold"])

    last_publish = time.time()
    smoke_on = False
    gas_on = False

    def _on_message(client, userdata, msg):  # noqa: ARG001
        nonlocal smoke_on, gas_on
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception as exc:
            logger.warning("Bad JSON on %s: %s", msg.topic, exc)
            return

        if msg.topic == hr_topic:
            value = float(payload.get("value", payload.get("heart_rate", 75.0)))
            engine.push_samples(np.array([value]), np.array([0.0]))
        elif msg.topic == mv_topic:
            value = float(payload.get("value", payload.get("movement", 0.0)))
            # Pair this movement with the last HR sample so buffers stay aligned.
            last_hr = engine.hr_buf[-1] if engine.hr_buf else 75.0
            engine.push_samples(np.array([last_hr]), np.array([value]))
        elif msg.topic == smoke_topic:
            concentration = float(payload.get("concentration", 0.0))
            smoke_on = concentration > smoke_threshold
        elif msg.topic == gas_topic:
            concentration = float(payload.get("concentration", 0.0))
            gas_on = concentration > gas_threshold

    client = mqtt.Client(client_id=f"{bridge.client_id}-listener")
    if args.username:
        client.username_pw_set(args.username, args.password)
    client.on_message = _on_message
    client.connect(args.broker, args.port, keepalive=60)
    for t in (hr_topic, mv_topic, smoke_topic, gas_topic):
        client.subscribe(t, qos=1)
        logger.info("Subscribed to %s", t)
    client.loop_start()

    try:
        while not stop_event.is_set():
            now = time.time()
            if now - last_publish >= args.publish_interval:
                stage, confidence = engine.infer()
                hr_mean, mv_mean = engine.latest_means()
                bridge.publish_state(
                    sleep_stage=stage,
                    confidence=confidence,
                    heart_rate=hr_mean,
                    movement=mv_mean,
                    smoke_alarm=smoke_on,
                    gas_alarm=gas_on,
                )
                logger.info(
                    "publish stage=%s conf=%.2f hr=%.1f mv=%.2f smoke=%s gas=%s",
                    stage.name, confidence, hr_mean, mv_mean, smoke_on, gas_on,
                )
                last_publish = now
            time.sleep(0.5)
    finally:
        client.loop_stop()
        client.disconnect()


# ---------------------------------------------------------------------------
# CLI / main loop
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Home Assistant bridge for the CNN-BiLSTM sleep classifier.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--source",
        choices=["replay", "mqtt"],
        default="replay",
        help="Where sensor data comes from (default: replay).",
    )
    p.add_argument(
        "--subjects",
        nargs="*",
        default=["ST7011"],
        help="Sleep-EDF subject IDs to replay (replay mode only).",
    )
    p.add_argument(
        "--data-dir",
        default="data/sleep-edf-telemetry",
        help="Directory containing the Sleep-EDF Telemetry EDF files.",
    )
    p.add_argument(
        "--target-sampling-rate",
        type=int,
        default=10,
        help="Down-sampled rate the model expects (default: 10 Hz).",
    )
    p.add_argument(
        "--model",
        default="models/best_model.h5",
        help="Path to the trained model (default: models/best_model.h5).",
    )
    p.add_argument(
        "--config",
        default="config/config.json",
        help="Project configuration file.",
    )
    p.add_argument("--broker", default=None,
                   help="MQTT broker address (overrides config).")
    p.add_argument("--port", type=int, default=None,
                   help="MQTT broker port (overrides config).")
    p.add_argument("--username", default=None,
                   help="MQTT username (overrides config).")
    p.add_argument("--password", default=None,
                   help="MQTT password (overrides config).")
    p.add_argument(
        "--publish-interval",
        type=float,
        default=30.0,
        help="How often (seconds) to push a state update to HA (default: 30).",
    )
    p.add_argument(
        "--speedup",
        type=float,
        default=60.0,
        help="Replay-mode speedup factor (1 = real-time, 60 = 1 minute / sec).",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Stop the service after N seconds (useful for smoke tests).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Don't try to connect to the MQTT broker; only build entities "
            "and print the payloads that would have been published."
        ),
    )
    p.add_argument(
        "--remove-discovery",
        action="store_true",
        help=(
            "Tell HA to delete the registered entities, then exit. "
            "Use this when uninstalling the service."
        ),
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

    config_path = (PROJECT_ROOT / args.config).resolve()
    full_cfg = load_config(str(config_path))
    args._config = full_cfg

    ha_cfg = HAConfig.from_dict(full_cfg.get("home_assistant", {}))
    mqtt_cfg = full_cfg.get("mqtt", {})

    broker = args.broker or mqtt_cfg.get("broker_address", "localhost")
    port = args.port or int(mqtt_cfg.get("broker_port", 1883))
    username = args.username if args.username is not None else mqtt_cfg.get("username", "")
    password = args.password if args.password is not None else mqtt_cfg.get("password", "")

    bridge = HomeAssistantBridge(
        config=ha_cfg,
        broker_address=broker,
        broker_port=port,
        username=username,
        password=password,
    )

    if args.remove_discovery:
        if not args.dry_run:
            if not bridge.connect():
                logger.error("Could not connect to broker — cannot remove discovery.")
                return 2
        bridge.remove_discovery()
        logger.info("Discovery entries cleared.")
        if not args.dry_run:
            bridge.publish_offline()
            bridge.disconnect()
        return 0

    if args.dry_run:
        logger.info("Dry-run mode — no MQTT connection will be opened.")
    else:
        if not bridge.connect():
            logger.error(
                "Could not connect to broker %s:%d. Run with --dry-run to "
                "debug locally.", broker, port,
            )
            return 2

    bridge.publish_discovery()

    engine = _InferenceEngine(
        model_path=(PROJECT_ROOT / args.model).resolve(),
        config_path=config_path,
    )

    stop_event = threading.Event()

    def _handle_signal(signum, _frame):  # noqa: ARG001
        logger.info("Signal %s — shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    if args.duration is not None:
        threading.Timer(args.duration, stop_event.set).start()

    loader = DatasetLoader()
    try:
        if args.source == "replay":
            try:
                run_replay(engine, bridge, loader, args, stop_event)
            except DatasetLoadError as exc:
                logger.error("Replay failed: %s", exc)
                return 2
        else:
            run_mqtt_source(engine, bridge, args, stop_event)
    finally:
        bridge.publish_offline()
        bridge.disconnect()

    return 0


if __name__ == "__main__":
    sys.exit(main())
