"""Data normalization module for CNN-BiLSTM Sleep Algorithm"""
import logging
import numpy as np
from typing import Optional

from src.data_structures import Dataset, TrainingSet
from training_config.config_loader import load_config

logger = logging.getLogger(__name__)


class NormalizationError(Exception):
    """Raised when normalization fails"""
    pass


class DataNormalizer:
    """
    Z-score normalizer for dual-channel (heart_rate, movement) sleep data.

    Workflow:
        1. Call fit() on training data to compute per-channel mean and std.
        2. Call transform() on any Dataset (train or test) to apply normalization.
        3. Or call fit_transform() as a convenience shortcut for training data.

    Requirements: 5.1, 5.2, 5.3, 5.4
    """

    def __init__(self, config_path: str = "training_config/config.json") -> None:
        config = load_config(config_path)
        self._method: str = (
            config.get("data_processing", {})
            .get("normalization", {})
            .get("method", "z-score")
        )
        # Fitted parameters: (mean, std) per channel
        self._hr_mean: Optional[float] = None
        self._hr_std: Optional[float] = None
        self._mv_mean: Optional[float] = None
        self._mv_std: Optional[float] = None
        self._fitted: bool = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fit(self, training_data: TrainingSet) -> None:
        """
        Compute mean and std from training data for both channels.

        Parameters
        ----------
        training_data : TrainingSet
            The training dataset used to derive normalization parameters.
        """
        hr_values = training_data.dataset.heart_rate.values.astype(float)
        mv_values = training_data.dataset.movement.values.astype(float)

        self._hr_mean = float(np.mean(hr_values))
        self._hr_std = float(np.std(hr_values))
        self._mv_mean = float(np.mean(mv_values))
        self._mv_std = float(np.std(mv_values))

        # Guard against zero or near-zero std (constant or near-constant signal).
        # Use a relative threshold: std must be at least 1e-8 times the mean
        # (or 1e-8 in absolute terms) to be considered non-degenerate.
        _min_std = 1e-8
        if self._hr_std < _min_std:
            logger.warning("Heart rate std is near 0; using std=1 to avoid division by zero")
            self._hr_std = 1.0
        if self._mv_std < _min_std:
            logger.warning("Movement std is near 0; using std=1 to avoid division by zero")
            self._mv_std = 1.0

        self._fitted = True
        logger.info(
            "DataNormalizer fitted — "
            f"HR: mean={self._hr_mean:.4f}, std={self._hr_std:.4f} | "
            f"MV: mean={self._mv_mean:.4f}, std={self._mv_std:.4f}"
        )

        # Store params back into the TrainingSet dataclass
        training_data.normalization_params = {
            "heart_rate": (self._hr_mean, self._hr_std),
            "movement": (self._mv_mean, self._mv_std),
        }

    def transform(self, data: Dataset) -> Dataset:
        """
        Apply Z-score normalization using training parameters.

        Parameters
        ----------
        data : Dataset
            Dataset to normalize (may be train or test split).

        Returns
        -------
        Dataset
            A new Dataset with normalized heart_rate and movement values.
        """
        if not self._fitted:
            raise NormalizationError(
                "DataNormalizer has not been fitted yet. Call fit() first."
            )

        normalized_hr_values = self._zscore(
            data.heart_rate.values.astype(float),
            self._hr_mean,
            self._hr_std,
        )
        normalized_mv_values = self._zscore(
            data.movement.values.astype(float),
            self._mv_mean,
            self._mv_std,
        )

        # Build new data objects with normalized values, preserving metadata.
        # Normalized values are outside the physical [30, 200] bpm range, so we
        # bypass __post_init__ validation by using object.__setattr__ directly.
        from src.data_structures import HeartRateData, MovementData
        import dataclasses

        new_hr = HeartRateData.__new__(HeartRateData)
        object.__setattr__(new_hr, "timestamps", data.heart_rate.timestamps.copy())
        object.__setattr__(new_hr, "values", normalized_hr_values)
        object.__setattr__(new_hr, "sampling_rate", data.heart_rate.sampling_rate)

        new_mv = MovementData.__new__(MovementData)
        object.__setattr__(new_mv, "timestamps", data.movement.timestamps.copy())
        object.__setattr__(new_mv, "values", normalized_mv_values)
        object.__setattr__(new_mv, "sampling_rate", data.movement.sampling_rate)

        return Dataset(
            heart_rate=new_hr,
            movement=new_mv,
            sleep_stages=data.sleep_stages,
            subject_ids=list(data.subject_ids),
        )

    def fit_transform(self, training_data: TrainingSet) -> TrainingSet:
        """
        Fit on training data and return a new TrainingSet with normalized values.

        Parameters
        ----------
        training_data : TrainingSet
            The training dataset.

        Returns
        -------
        TrainingSet
            A new TrainingSet whose dataset contains normalized values and
            whose normalization_params are populated.
        """
        self.fit(training_data)
        normalized_dataset = self.transform(training_data.dataset)
        return TrainingSet(
            dataset=normalized_dataset,
            normalization_params=dict(training_data.normalization_params),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _zscore(values: np.ndarray, mean: float, std: float) -> np.ndarray:
        """Apply Z-score formula: (x - mean) / std"""
        return (values - mean) / std
