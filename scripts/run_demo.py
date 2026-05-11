"""End-to-end demonstration of the trained CNN-BiLSTM sleep model.

Two demos are bundled into one script:

1. **Hypnogram demo** — replay an entire night of recorded data through
   the trained model and print the predicted stage every few minutes,
   together with stage-percentage statistics that mirror what a sleep
   tracker app would show.
2. **Live MQTT demo** — wire the trained model into the
   :class:`~src.inference_pipeline.InferencePipeline`, feed it a small
   live-style window of dual-sensor data, and print every MQTT message
   the pipeline would publish (sleep stage + environment control commands)
   without actually connecting to a broker.

Usage
-----
.. code-block:: bash

    # Default: run both demos with subject ST7011
    python scripts/run_demo.py

    # Only the hypnogram replay
    python scripts/run_demo.py --mode hypnogram --subjects ST7011

    # Only the MQTT demo
    python scripts/run_demo.py --mode mqtt --subjects ST7011
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from src.data_structures import HeartRateData, MovementData, SleepStage
from src.dataset_loader import DatasetLoader, DatasetLoadError
from src.inference_pipeline import InferencePipeline
from src.training_pipeline import TrainingPipeline

logger = logging.getLogger("run_demo")

STAGE_NAMES = {0: "AWAKE", 1: "LIGHT", 2: "DEEP", 3: "REM"}
STAGE_GLYPHS = {0: "🟡 W", 1: "🔵 L", 2: "🟣 D", 3: "🟢 R"}


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Demo 1: Hypnogram replay
# ---------------------------------------------------------------------------

def run_hypnogram_demo(
    pipeline: TrainingPipeline,
    loader: DatasetLoader,
    args: argparse.Namespace,
) -> None:
    """Replay an entire night through the trained model and print predictions."""
    logger.info("=" * 70)
    logger.info("Hypnogram Demo — replaying full night through the trained model")
    logger.info("=" * 70)

    # Load just the requested subject(s)
    dataset = loader.load_sleep_edf_telemetry(
        str((PROJECT_ROOT / args.data_dir).resolve()),
        subjects=args.subjects,
        target_sampling_rate=args.target_sampling_rate,
    )

    # Use the training-time normaliser so input distributions match.  We refit
    # on the same training portion the model saw.
    training_set, _ = loader.split_train_test(dataset, test_ratio=args.val_ratio)
    pipeline._normalizer.fit(training_set)

    norm_ds = pipeline._normalizer.transform(dataset)
    X, y_true = pipeline._build_feature_matrix(norm_ds)
    probs = pipeline._classifier_forward(X)
    y_pred = np.argmax(probs, axis=-1).astype(np.int32)

    # Sample-level confidence = max probability per window
    confidences = np.max(probs, axis=-1)

    # Each window covers `window_size / sampling_rate` seconds
    window_size = 1024  # default in TrainingPipeline._build_feature_matrix
    stride = 512
    seconds_per_window = window_size / args.target_sampling_rate  # ≈ 102.4 s
    seconds_per_step = stride / args.target_sampling_rate  # ≈ 51.2 s

    # Print one line every print_every windows so output stays readable
    print_every = max(1, len(y_pred) // 30)
    logger.info(
        "Replaying %d windows (%.1f s each) — printing every %d:",
        len(y_pred), seconds_per_window, print_every,
    )
    print()
    print(f"  {'time':>10s}  {'true':>7s}  {'pred':>7s}  {'conf':>5s}  {'match':>5s}")
    print(f"  {'-'*10}  {'-'*7}  {'-'*7}  {'-'*5}  {'-'*5}")
    for i in range(0, len(y_pred), print_every):
        t_seconds = i * seconds_per_step
        h = int(t_seconds // 3600)
        m = int((t_seconds % 3600) // 60)
        s = int(t_seconds % 60)
        time_str = f"{h:02d}:{m:02d}:{s:02d}"
        match = "✓" if y_pred[i] == y_true[i] else "✗"
        print(
            f"  {time_str:>10s}  "
            f"{STAGE_NAMES[int(y_true[i])]:>7s}  "
            f"{STAGE_NAMES[int(y_pred[i])]:>7s}  "
            f"{confidences[i]*100:4.0f}%  "
            f"{match:>5s}"
        )
    print()

    # Stage-percentage statistics (true vs predicted)
    print(f"  {'Stage':>7s}  {'True %':>10s}  {'Pred %':>10s}")
    print(f"  {'-'*7}  {'-'*10}  {'-'*10}")
    for stage_id in range(4):
        true_pct = 100.0 * np.mean(y_true == stage_id)
        pred_pct = 100.0 * np.mean(y_pred == stage_id)
        print(
            f"  {STAGE_NAMES[stage_id]:>7s}  "
            f"{true_pct:9.1f}%  "
            f"{pred_pct:9.1f}%"
        )
    overall_acc = float(np.mean(y_pred == y_true))
    print()
    logger.info("Overall accuracy on this recording: %.1f%%", 100 * overall_acc)


# ---------------------------------------------------------------------------
# Demo 2: Live MQTT pipeline
# ---------------------------------------------------------------------------

def run_mqtt_demo(
    training_pipeline: TrainingPipeline,
    loader: DatasetLoader,
    args: argparse.Namespace,
) -> None:
    """Wire the trained model into the live inference pipeline with mock MQTT.

    Demonstrates that every MQTT topic the system would publish to during
    real operation receives the right payload.
    """
    logger.info("=" * 70)
    logger.info("Live MQTT Demo — InferencePipeline with mock broker")
    logger.info("=" * 70)

    # Mock subscriber & publisher so no real broker is needed.
    mock_publisher = MagicMock()
    mock_subscriber = MagicMock()

    # Build the inference pipeline, injecting the trained CNN/BiLSTM/classifier.
    pipeline = InferencePipeline(
        config_path=str(PROJECT_ROOT / "config" / "config.json"),
        publisher=mock_publisher,
        subscriber=mock_subscriber,
        cnn_extractor=training_pipeline._cnn,
        bilstm_analyzer=training_pipeline._bilstm,
        sleep_classifier=training_pipeline._classifier,
    )

    # Load real data and pull a representative ~30 s slice as the "live" input.
    dataset = loader.load_sleep_edf_telemetry(
        str((PROJECT_ROOT / args.data_dir).resolve()),
        subjects=args.subjects,
        target_sampling_rate=args.target_sampling_rate,
    )
    fs = args.target_sampling_rate
    window_seconds = 30
    n_samples = window_seconds * fs

    # Pick a slice from the middle of the recording (likely sleep period)
    middle = len(dataset.heart_rate.values) // 2
    start = max(0, middle - n_samples // 2)
    end = start + n_samples
    timestamps = dataset.heart_rate.timestamps[start:end]
    hr_window = HeartRateData(
        timestamps=timestamps.copy(),
        values=dataset.heart_rate.values[start:end],
        sampling_rate=fs,
    )
    mv_window = MovementData(
        timestamps=timestamps.copy(),
        values=dataset.movement.values[start:end],
        sampling_rate=fs,
    )
    true_stage = SleepStage(int(dataset.sleep_stages.stages[start + n_samples // 2]))

    logger.info("Feeding pipeline a %d s slice (HR mean=%.1f, MV mean=%.1f, true stage=%s)",
                window_seconds,
                float(np.mean(hr_window.values)),
                float(np.mean(mv_window.values)),
                true_stage.name)

    predicted_stage = pipeline.process_sensor_data(hr_window, mv_window)
    logger.info("Predicted stage: %s  (true: %s)",
                predicted_stage.name, true_stage.name)
    logger.info("")

    # Show what would have been published to MQTT
    logger.info("MQTT publisher activity (mock):")
    if mock_publisher.publish_sleep_stage.called:
        for call in mock_publisher.publish_sleep_stage.call_args_list:
            logger.info("  → publish_sleep_stage  args=%s  kwargs=%s",
                        call.args, call.kwargs)
    if mock_publisher.publish_environment_control.called:
        for call in mock_publisher.publish_environment_control.call_args_list:
            logger.info("  → publish_environment_control  args=%s  kwargs=%s",
                        call.args, call.kwargs)
    if (
        not mock_publisher.publish_sleep_stage.called
        and not mock_publisher.publish_environment_control.called
    ):
        # InferencePipeline may invoke generic .publish calls; surface those too
        if mock_publisher.method_calls:
            for call in mock_publisher.method_calls:
                logger.info("  → %s", call)
        else:
            logger.info("  (no calls captured)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["both", "hypnogram", "mqtt"],
        default="both",
        help="Which demo to run (default: both).",
    )
    parser.add_argument("--data-dir", default="data/sleep-edf-telemetry")
    parser.add_argument("--subjects", nargs="+", default=["ST7011"])
    parser.add_argument("--target-sampling-rate", type=int, default=10)
    parser.add_argument("--val-ratio", type=float, default=0.2)
    parser.add_argument("--model", default="models/best_model.h5")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    np.random.seed(args.seed)
    try:
        import tensorflow as tf
        tf.random.set_seed(args.seed)
    except ImportError:
        pass

    model_path = (PROJECT_ROOT / args.model).resolve()
    if not model_path.exists():
        logger.error("Trained model not found: %s", model_path)
        logger.error("Hint: run `python scripts/train.py` first.")
        return 2

    # Single shared TrainingPipeline owns the loaded weights so both demos
    # work with the same model instance (and so we only pay loading once).
    training_pipeline = TrainingPipeline(
        config_path=str(PROJECT_ROOT / "config" / "config.json")
    )
    training_pipeline.load_model(str(model_path))
    logger.info("Loaded model from %s", model_path)

    loader = DatasetLoader()

    try:
        if args.mode in ("both", "hypnogram"):
            run_hypnogram_demo(training_pipeline, loader, args)
            print()
        if args.mode in ("both", "mqtt"):
            run_mqtt_demo(training_pipeline, loader, args)
    except DatasetLoadError as exc:
        logger.error("Failed to load dataset: %s", exc)
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
