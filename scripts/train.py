"""End-to-end training script for the CNN-BiLSTM sleep stage classifier.

Pipeline
--------
1. Load the Sleep-EDF Telemetry data prepared by ``scripts/download_data.py``
   into a :class:`~src.data_structures.Dataset`.
2. Split into a training set and a held-out validation set (80/20 by time).
3. Run :class:`~src.training_pipeline.TrainingPipeline` to fit the
   classifier head while reusing the lazy CNN/BiLSTM feature extractor.
4. Persist the best checkpoint (CNN + BiLSTM + classifier weights) and a
   JSON training history under ``models/``.

Usage
-----
.. code-block:: bash

    # Default: train on whatever sits in data/sleep-edf-telemetry
    python scripts/train.py

    # Pick specific subjects, smaller window, faster smoke test
    python scripts/train.py --subjects ST7011 --max-epochs 5 --batch-size 32
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

# Add project root to PYTHONPATH so the script runs from anywhere.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from src.data_structures import (
    Dataset, HeartRateData, MovementData, SleepStages, TestSet, TrainingSet,
)
from src.dataset_loader import DatasetLoader, DatasetLoadError
from src.training_pipeline import TrainingPipeline

logger = logging.getLogger("train")


def split_by_time_per_subject(
    dataset: Dataset, test_ratio: float
) -> tuple:
    """Carve a time-based train/val split out of every subject's data.

    Each subject contributes the trailing ``test_ratio`` of its recording
    to the validation set; the remaining lead is used for training.  This
    keeps train and val drawn from the same population, which mirrors the
    "personalised model" deployment scenario better than pure LOSO.
    """
    subject_ids = list(dataset.subject_ids)
    sleep_stages_arr = dataset.sleep_stages.stages
    hr = dataset.heart_rate
    mv = dataset.movement

    train_idx: list = []
    val_idx: list = []
    cur = 0
    while cur < len(subject_ids):
        sid = subject_ids[cur]
        end = cur
        while end < len(subject_ids) and subject_ids[end] == sid:
            end += 1
        n = end - cur
        n_val = max(1, int(n * test_ratio))
        n_train = n - n_val
        train_idx.extend(range(cur, cur + n_train))
        val_idx.extend(range(cur + n_train, end))
        cur = end

    train_idx = np.asarray(train_idx, dtype=np.int64)
    val_idx = np.asarray(val_idx, dtype=np.int64)

    def _slice(idx):
        ds = Dataset(
            heart_rate=HeartRateData(
                timestamps=hr.timestamps[idx],
                values=hr.values[idx],
                sampling_rate=hr.sampling_rate,
            ),
            movement=MovementData(
                timestamps=mv.timestamps[idx],
                values=mv.values[idx],
                sampling_rate=mv.sampling_rate,
            ),
            sleep_stages=SleepStages(
                timestamps=dataset.sleep_stages.timestamps[idx],
                stages=sleep_stages_arr[idx],
            ),
            subject_ids=[subject_ids[i] for i in idx],
        )
        return ds

    train_ds = _slice(train_idx)
    val_ds = _slice(val_idx)

    norm_params = {
        "heart_rate": (
            float(np.mean(train_ds.heart_rate.values)),
            float(np.std(train_ds.heart_rate.values)),
        ),
        "movement": (
            float(np.mean(train_ds.movement.values)),
            float(np.std(train_ds.movement.values)),
        ),
    }
    return (
        TrainingSet(dataset=train_ds, normalization_params=norm_params),
        TestSet(dataset=val_ds),
    )


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        default="data/sleep-edf-telemetry",
        help="Directory holding ST7XXX*-PSG.edf and ST7XXX*-Hypnogram.edf files.",
    )
    parser.add_argument(
        "--subjects",
        nargs="+",
        default=None,
        help="Restrict training to specific subject IDs (default: all available).",
    )
    parser.add_argument(
        "--target-sampling-rate",
        type=int,
        default=10,
        help="Target sampling rate (Hz) after downsampling EOG/EMG (default: 10).",
    )
    parser.add_argument(
        "--val-ratio",
        type=float,
        default=0.2,
        help="Fraction of the dataset reserved for validation (default: 0.2).",
    )
    parser.add_argument(
        "--split-mode",
        choices=["time", "subject"],
        default="time",
        help=(
            "How to carve out the validation set. 'time' (default) uses the "
            "trailing portion of every subject's recording, which mimics "
            "deploying a personalised model after a few nights of data. "
            "'subject' holds out whole subjects (LOSO-style)."
        ),
    )
    parser.add_argument(
        "--max-epochs",
        type=int,
        default=20,
        help="Maximum number of training epochs (default: 20).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=32,
        help="Mini-batch size for classifier SGD (default: 32).",
    )
    parser.add_argument(
        "--learning-rate",
        type=float,
        default=0.01,
        help="Classifier learning rate (default: 0.01).",
    )
    parser.add_argument(
        "--patience",
        type=int,
        default=5,
        help="Early-stopping patience in epochs (default: 5).",
    )
    parser.add_argument(
        "--window-size",
        type=int,
        default=1024,
        help="Sliding-window size in samples for feature extraction (default: 1024).",
    )
    parser.add_argument(
        "--stride",
        type=int,
        default=512,
        help="Sliding-window stride in samples (default: 512).",
    )
    parser.add_argument(
        "--model-out",
        default="models/best_model.h5",
        help="Where to save the best model (default: models/best_model.h5).",
    )
    parser.add_argument(
        "--history-out",
        default="models/training_history.json",
        help="Where to save the training history JSON (default: models/training_history.json).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility (default: 42).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose (DEBUG) logging.",
    )
    args = parser.parse_args()

    _setup_logging(args.verbose)

    np.random.seed(args.seed)
    try:  # Optional: lock TF/Keras seeds when available
        import tensorflow as tf
        tf.random.set_seed(args.seed)
    except ImportError:
        pass

    # ------------------------------------------------------------------ #
    # 1. Load the dataset                                                #
    # ------------------------------------------------------------------ #
    data_dir = (PROJECT_ROOT / args.data_dir).resolve()
    logger.info("=" * 70)
    logger.info("Step 1/3 — Loading Sleep-EDF Telemetry from %s", data_dir)
    logger.info("=" * 70)
    loader = DatasetLoader()
    try:
        dataset = loader.load_sleep_edf_telemetry(
            str(data_dir),
            subjects=args.subjects,
            target_sampling_rate=args.target_sampling_rate,
        )
    except DatasetLoadError as exc:
        logger.error("Failed to load dataset: %s", exc)
        logger.error("Hint: run `python scripts/download_data.py` first.")
        return 2

    n_samples = len(dataset.heart_rate.values)
    logger.info("Loaded %d samples (%.1f hours @ %d Hz) from %d subjects",
                n_samples,
                n_samples / args.target_sampling_rate / 3600.0,
                args.target_sampling_rate,
                len(set(dataset.subject_ids)))

    # ------------------------------------------------------------------ #
    # 2. Split into train / validation                                   #
    # ------------------------------------------------------------------ #
    logger.info("=" * 70)
    logger.info("Step 2/3 — Splitting (val_ratio=%.2f, mode=%s)",
                args.val_ratio, args.split_mode)
    logger.info("=" * 70)
    if args.split_mode == "time":
        training_set, val_set = split_by_time_per_subject(dataset, args.val_ratio)
    else:  # "subject"
        training_set, val_set = loader.split_train_test(
            dataset, test_ratio=args.val_ratio
        )
    logger.info(
        "Train: %d samples — Val: %d samples",
        len(training_set.dataset.heart_rate.values),
        len(val_set.dataset.heart_rate.values),
    )

    # ------------------------------------------------------------------ #
    # 3. Train                                                           #
    # ------------------------------------------------------------------ #
    logger.info("=" * 70)
    logger.info("Step 3/3 — Training (max_epochs=%d, batch=%d, lr=%g, patience=%d)",
                args.max_epochs, args.batch_size, args.learning_rate, args.patience)
    logger.info("=" * 70)

    pipeline = TrainingPipeline(config_path=str(PROJECT_ROOT / "config" / "config.json"))
    # Override config-driven hyperparameters with CLI values
    pipeline.max_epochs = args.max_epochs
    pipeline.batch_size = args.batch_size
    pipeline.learning_rate = args.learning_rate
    pipeline.patience = args.patience
    pipeline.window_size = args.window_size
    pipeline.stride = args.stride

    model_out = (PROJECT_ROOT / args.model_out).resolve()
    history_out = (PROJECT_ROOT / args.history_out).resolve()
    history_out.parent.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    history = pipeline.train(training_set, val_set, model_save_path=str(model_out))
    elapsed = time.time() - t0
    logger.info("Training finished in %.1f s (%.1f min)", elapsed, elapsed / 60.0)

    # Save history JSON
    history_serialisable = {
        k: [float(v) for v in vs] if k != "epochs" else [int(v) for v in vs]
        for k, vs in history.items()
    }
    history_serialisable["best_val_acc"] = float(max(history["val_acc"])) if history["val_acc"] else 0.0
    history_serialisable["best_val_acc_epoch"] = int(
        history["epochs"][int(np.argmax(history["val_acc"]))]
    ) if history["val_acc"] else 0
    history_serialisable["wall_time_seconds"] = elapsed
    history_serialisable["args"] = vars(args)
    with open(history_out, "w") as fh:
        json.dump(history_serialisable, fh, indent=2)
    logger.info("Saved training history to %s", history_out)

    # Final evaluation on validation set
    logger.info("=" * 70)
    logger.info("Final validation metrics")
    logger.info("=" * 70)
    final = pipeline.evaluate(val_set)
    logger.info("Loss: %.4f  |  Accuracy: %.4f", final["loss"], final["accuracy"])

    return 0


if __name__ == "__main__":
    sys.exit(main())
