"""Performance evaluation metrics for sleep stage classification.

Computes accuracy, per-class precision/recall/F1, confusion matrix,
and exports metrics to JSON.

Requirements: 19.1, 19.2, 19.3, 19.4, 19.5, 19.6
"""

import json
import logging
from typing import Dict

import numpy as np

from src.data_structures import SleepStage

logger = logging.getLogger(__name__)

# Ordered sleep stages matching integer labels 0-3
_SLEEP_STAGES = [SleepStage.AWAKE, SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM]
_NUM_CLASSES = 4


class PerformanceMetrics:
    """Compute and export performance metrics for a 4-class sleep stage classifier.

    All metric values are guaranteed to be in [0, 1].
    The confusion matrix is 4×4 with rows = true labels, cols = predicted labels.
    """

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_inputs(y_true: np.ndarray, y_pred: np.ndarray) -> None:
        """Validate that both arrays are 1-D, same length, and contain valid labels."""
        y_true = np.asarray(y_true)
        y_pred = np.asarray(y_pred)
        if y_true.ndim != 1 or y_pred.ndim != 1:
            raise ValueError("y_true and y_pred must be 1-D arrays.")
        if len(y_true) != len(y_pred):
            raise ValueError(
                f"y_true and y_pred must have the same length, "
                f"got {len(y_true)} and {len(y_pred)}."
            )
        valid = set(range(_NUM_CLASSES))
        if not set(np.unique(y_true)).issubset(valid):
            raise ValueError(f"y_true contains labels outside {{0,1,2,3}}.")
        if not set(np.unique(y_pred)).issubset(valid):
            raise ValueError(f"y_pred contains labels outside {{0,1,2,3}}.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def calculate_accuracy(self, y_true: np.ndarray, y_pred: np.ndarray) -> float:
        """Compute overall accuracy: fraction of correctly classified samples.

        Args:
            y_true: Ground-truth integer labels (0-3), shape (n,).
            y_pred: Predicted integer labels (0-3), shape (n,).

        Returns:
            Accuracy in [0, 1].
        """
        y_true = np.asarray(y_true)
        y_pred = np.asarray(y_pred)
        self._validate_inputs(y_true, y_pred)

        if len(y_true) == 0:
            return 0.0

        accuracy = float(np.mean(y_true == y_pred))
        # Clamp to [0, 1] for safety
        return max(0.0, min(1.0, accuracy))

    def calculate_precision_per_class(
        self, y_true: np.ndarray, y_pred: np.ndarray
    ) -> Dict[SleepStage, float]:
        """Compute per-class precision.

        Precision for class c = TP_c / (TP_c + FP_c).
        Returns 0.0 for classes with no predicted samples (zero-division guard).

        Args:
            y_true: Ground-truth integer labels (0-3), shape (n,).
            y_pred: Predicted integer labels (0-3), shape (n,).

        Returns:
            Dict mapping each SleepStage to its precision in [0, 1].
        """
        y_true = np.asarray(y_true)
        y_pred = np.asarray(y_pred)
        self._validate_inputs(y_true, y_pred)

        result: Dict[SleepStage, float] = {}
        for idx, stage in enumerate(_SLEEP_STAGES):
            tp = int(np.sum((y_pred == idx) & (y_true == idx)))
            fp = int(np.sum((y_pred == idx) & (y_true != idx)))
            denom = tp + fp
            precision = float(tp) / float(denom) if denom > 0 else 0.0
            result[stage] = max(0.0, min(1.0, precision))
        return result

    def calculate_recall_per_class(
        self, y_true: np.ndarray, y_pred: np.ndarray
    ) -> Dict[SleepStage, float]:
        """Compute per-class recall.

        Recall for class c = TP_c / (TP_c + FN_c).
        Returns 0.0 for classes absent from y_true (zero-division guard).

        Args:
            y_true: Ground-truth integer labels (0-3), shape (n,).
            y_pred: Predicted integer labels (0-3), shape (n,).

        Returns:
            Dict mapping each SleepStage to its recall in [0, 1].
        """
        y_true = np.asarray(y_true)
        y_pred = np.asarray(y_pred)
        self._validate_inputs(y_true, y_pred)

        result: Dict[SleepStage, float] = {}
        for idx, stage in enumerate(_SLEEP_STAGES):
            tp = int(np.sum((y_pred == idx) & (y_true == idx)))
            fn = int(np.sum((y_pred != idx) & (y_true == idx)))
            denom = tp + fn
            recall = float(tp) / float(denom) if denom > 0 else 0.0
            result[stage] = max(0.0, min(1.0, recall))
        return result

    def calculate_f1_per_class(
        self, y_true: np.ndarray, y_pred: np.ndarray
    ) -> Dict[SleepStage, float]:
        """Compute per-class F1 score.

        F1 = 2 * precision * recall / (precision + recall).
        Returns 0.0 when both precision and recall are 0.

        Args:
            y_true: Ground-truth integer labels (0-3), shape (n,).
            y_pred: Predicted integer labels (0-3), shape (n,).

        Returns:
            Dict mapping each SleepStage to its F1 score in [0, 1].
        """
        precision = self.calculate_precision_per_class(y_true, y_pred)
        recall = self.calculate_recall_per_class(y_true, y_pred)

        result: Dict[SleepStage, float] = {}
        for stage in _SLEEP_STAGES:
            p = precision[stage]
            r = recall[stage]
            denom = p + r
            f1 = 2.0 * p * r / denom if denom > 0.0 else 0.0
            result[stage] = max(0.0, min(1.0, f1))
        return result

    def generate_confusion_matrix(
        self, y_true: np.ndarray, y_pred: np.ndarray
    ) -> np.ndarray:
        """Generate a 4×4 confusion matrix.

        Entry [i, j] = number of samples with true label i predicted as j.

        Args:
            y_true: Ground-truth integer labels (0-3), shape (n,).
            y_pred: Predicted integer labels (0-3), shape (n,).

        Returns:
            Integer ndarray of shape (4, 4).
        """
        y_true = np.asarray(y_true)
        y_pred = np.asarray(y_pred)
        self._validate_inputs(y_true, y_pred)

        matrix = np.zeros((_NUM_CLASSES, _NUM_CLASSES), dtype=np.int64)
        for t, p in zip(y_true, y_pred):
            matrix[int(t), int(p)] += 1
        return matrix

    def save_metrics(self, metrics: Dict, file_path: str) -> None:
        """Export a metrics dictionary to a JSON file.

        SleepStage enum keys are serialised as their name strings (e.g. "AWAKE").
        numpy integers/floats are converted to native Python types.

        Args:
            metrics: Dictionary of metric name → value.  Values may be plain
                     scalars, dicts keyed by SleepStage, or numpy arrays.
            file_path: Destination file path (created or overwritten).
        """

        def _convert(obj):
            if isinstance(obj, SleepStage):
                return obj.name
            if isinstance(obj, np.integer):
                return int(obj)
            if isinstance(obj, np.floating):
                return float(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            if isinstance(obj, dict):
                return {_convert(k): _convert(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_convert(item) for item in obj]
            return obj

        serialisable = _convert(metrics)

        with open(file_path, "w", encoding="utf-8") as fh:
            json.dump(serialisable, fh, indent=2, ensure_ascii=False)

        logger.info("Metrics saved to %s", file_path)
