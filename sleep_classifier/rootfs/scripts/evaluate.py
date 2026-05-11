"""Evaluate a trained CNN-BiLSTM sleep model on a held-out test set.

Loads:
  * the trained model from ``models/best_model.h5``
  * the Sleep-EDF Telemetry dataset (same path used for training)

Splits the dataset with the same ``test_ratio`` as ``scripts/train.py`` so
that the held-out portion never touched during training is used for the
final report.

Outputs:
  * Per-class precision / recall / F1
  * 4×4 confusion matrix
  * JSON report saved to ``models/evaluation_report.json``

Usage
-----
.. code-block:: bash

    python scripts/evaluate.py
    python scripts/evaluate.py --subjects ST7011 ST7022
    python scripts/evaluate.py --model models/best_model.h5 \\
                               --report-out models/evaluation_report.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from src.data_structures import SleepStage
from src.dataset_loader import DatasetLoader, DatasetLoadError
from src.performance_metrics import PerformanceMetrics
from src.training_pipeline import TrainingPipeline
# Re-use train.py's per-subject time split helper so evaluate sees the
# exact same val partition that the model was tuned on.
from scripts.train import split_by_time_per_subject

logger = logging.getLogger("evaluate")

STAGE_NAMES = {0: "AWAKE", 1: "LIGHT", 2: "DEEP", 3: "REM"}


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _format_confusion_matrix(cm: np.ndarray) -> str:
    """Return a pretty-printed 4x4 confusion matrix string."""
    header = "             " + "  ".join(f"{STAGE_NAMES[i]:>7}" for i in range(4))
    lines = [header]
    for i, name in STAGE_NAMES.items():
        row = "  ".join(f"{cm[i, j]:7d}" for j in range(4))
        lines.append(f"  true={name:<6}  {row}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        default="data/sleep-edf-telemetry",
        help="Sleep-EDF Telemetry directory (must match scripts/train.py).",
    )
    parser.add_argument(
        "--subjects",
        nargs="+",
        default=None,
        help="Restrict evaluation to specific subject IDs.",
    )
    parser.add_argument(
        "--target-sampling-rate",
        type=int,
        default=10,
        help="Target sampling rate (must match scripts/train.py; default 10).",
    )
    parser.add_argument(
        "--val-ratio",
        type=float,
        default=0.2,
        help="Same val_ratio used during training, applied with same seed.",
    )
    parser.add_argument(
        "--split-mode",
        choices=["time", "subject"],
        default="time",
        help="Must match scripts/train.py --split-mode (default: time).",
    )
    parser.add_argument(
        "--window-size",
        type=int,
        default=1024,
        help="Sliding-window size in samples (must match training; default 1024).",
    )
    parser.add_argument(
        "--stride",
        type=int,
        default=512,
        help="Sliding-window stride in samples (must match training; default 512).",
    )
    parser.add_argument(
        "--model",
        default="models/best_model.h5",
        help="Path to the saved model (default: models/best_model.h5).",
    )
    parser.add_argument(
        "--report-out",
        default="models/evaluation_report.json",
        help="Where to save the JSON evaluation report.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed (must match scripts/train.py for matching split).",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    np.random.seed(args.seed)
    try:
        import tensorflow as tf
        tf.random.set_seed(args.seed)
    except ImportError:
        pass

    # ------------------------------------------------------------------ #
    # 1. Load dataset                                                    #
    # ------------------------------------------------------------------ #
    data_dir = (PROJECT_ROOT / args.data_dir).resolve()
    logger.info("Loading Sleep-EDF Telemetry from %s", data_dir)
    loader = DatasetLoader()
    try:
        dataset = loader.load_sleep_edf_telemetry(
            str(data_dir),
            subjects=args.subjects,
            target_sampling_rate=args.target_sampling_rate,
        )
    except DatasetLoadError as exc:
        logger.error("Failed to load dataset: %s", exc)
        return 2

    if args.split_mode == "time":
        _, test_set = split_by_time_per_subject(dataset, args.val_ratio)
    else:
        _, test_set = loader.split_train_test(dataset, test_ratio=args.val_ratio)
    logger.info(
        "Test set: %d samples (split=%s)",
        len(test_set.dataset.heart_rate.values), args.split_mode,
    )

    # ------------------------------------------------------------------ #
    # 2. Load model                                                      #
    # ------------------------------------------------------------------ #
    model_path = (PROJECT_ROOT / args.model).resolve()
    if not model_path.exists():
        logger.error("Model not found: %s", model_path)
        logger.error("Hint: run `python scripts/train.py` first.")
        return 2

    logger.info("Loading trained model from %s", model_path)
    pipeline = TrainingPipeline(config_path=str(PROJECT_ROOT / "config" / "config.json"))

    # The pipeline must see training data once so its normaliser fits.
    # We refit on the training portion so that test transformations match.
    if args.split_mode == "time":
        training_set, _ = split_by_time_per_subject(dataset, args.val_ratio)
    else:
        training_set, _ = loader.split_train_test(dataset, test_ratio=args.val_ratio)
    pipeline._normalizer.fit(training_set)
    pipeline.load_model(str(model_path))
    pipeline.window_size = args.window_size
    pipeline.stride = args.stride

    # ------------------------------------------------------------------ #
    # 3. Compute predictions                                             #
    # ------------------------------------------------------------------ #
    logger.info("Extracting features from test set (window=%d, stride=%d)",
                args.window_size, args.stride)
    norm_test = pipeline._normalizer.transform(test_set.dataset)
    X_test, y_true = pipeline._build_feature_matrix(norm_test)

    probs = pipeline._classifier_forward(X_test)
    y_pred = np.argmax(probs, axis=-1).astype(np.int32)
    logger.info("Predicted %d windows", len(y_pred))

    # ------------------------------------------------------------------ #
    # 4. Metrics                                                         #
    # ------------------------------------------------------------------ #
    calc = PerformanceMetrics()
    accuracy = calc.calculate_accuracy(y_true, y_pred)
    precision_per_class = calc.calculate_precision_per_class(y_true, y_pred)
    recall_per_class = calc.calculate_recall_per_class(y_true, y_pred)
    f1_per_class = calc.calculate_f1_per_class(y_true, y_pred)
    confusion_matrix = calc.generate_confusion_matrix(y_true, y_pred)

    # Pretty-print summary
    logger.info("=" * 70)
    logger.info("Evaluation results")
    logger.info("=" * 70)
    logger.info("Overall accuracy : %.4f", accuracy)
    logger.info("")
    logger.info("Per-class metrics:")
    logger.info("  %-7s  %-9s  %-9s  %-9s", "Stage", "Precision", "Recall", "F1")
    for stage in (SleepStage.AWAKE, SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM):
        p = precision_per_class.get(stage, 0.0)
        r = recall_per_class.get(stage, 0.0)
        f = f1_per_class.get(stage, 0.0)
        logger.info("  %-7s  %-9.4f  %-9.4f  %-9.4f", stage.name, p, r, f)
    logger.info("")
    logger.info("Confusion matrix:\n%s", _format_confusion_matrix(confusion_matrix))

    # ------------------------------------------------------------------ #
    # 5. Save JSON report                                                #
    # ------------------------------------------------------------------ #
    report_path = (PROJECT_ROOT / args.report_out).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "accuracy": float(accuracy),
        "precision_per_class": {
            stage.name: float(precision_per_class.get(stage, 0.0))
            for stage in SleepStage
        },
        "recall_per_class": {
            stage.name: float(recall_per_class.get(stage, 0.0))
            for stage in SleepStage
        },
        "f1_per_class": {
            stage.name: float(f1_per_class.get(stage, 0.0))
            for stage in SleepStage
        },
        "confusion_matrix": confusion_matrix.astype(int).tolist(),
        "confusion_matrix_labels": [STAGE_NAMES[i] for i in range(4)],
        "n_test_windows": int(len(y_true)),
        "stage_distribution": {
            STAGE_NAMES[int(u)]: int(c)
            for u, c in zip(*np.unique(y_true, return_counts=True))
        },
        "args": vars(args),
    }
    with open(report_path, "w") as fh:
        json.dump(report, fh, indent=2)
    logger.info("Saved evaluation report to %s", report_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
