"""Training pipeline for CNN-BiLSTM sleep stage classification model.

Implements end-to-end training with:
- Data preprocessing (normalization)
- CNN feature extraction
- BiLSTM temporal analysis
- Sleep stage classification
- Early stopping (patience=5 epochs)
- Best model checkpoint saving (HDF5)
- Training/validation history logging

Requirements: 16.1, 16.2, 16.3, 16.4, 16.5, 16.6
"""
import json
import logging
import os
from typing import Dict, List, Optional, Tuple

import numpy as np

from src.data_structures import SleepStage, TrainingSet, TestSet
from src.data_normalizer import DataNormalizer
from src.cnn_extractor import CNNExtractor
from src.bilstm_analyzer import BiLSTMAnalyzer
from src.sleep_classifier import SleepClassifier

logger = logging.getLogger(__name__)


def _softmax(logits: np.ndarray) -> np.ndarray:
    """Numerically stable softmax."""
    shifted = logits - np.max(logits, axis=-1, keepdims=True)
    exp_x = np.exp(shifted)
    return exp_x / np.sum(exp_x, axis=-1, keepdims=True)


def _cross_entropy_loss(probs: np.ndarray, labels: np.ndarray) -> float:
    """Compute mean cross-entropy loss.

    Args:
        probs: Probability array of shape (N, num_classes).
        labels: Integer label array of shape (N,).

    Returns:
        Scalar mean cross-entropy loss.
    """
    n = len(labels)
    # Clip to avoid log(0)
    clipped = np.clip(probs[np.arange(n), labels], 1e-12, 1.0)
    return float(-np.mean(np.log(clipped)))


def _accuracy(probs: np.ndarray, labels: np.ndarray) -> float:
    """Compute classification accuracy."""
    preds = np.argmax(probs, axis=-1)
    return float(np.mean(preds == labels))


class TrainingPipeline:
    """End-to-end training pipeline for CNN-BiLSTM sleep stage classification.

    The pipeline:
    1. Normalises input data using DataNormalizer.
    2. Extracts CNN features from time-frequency matrices (or raw signal windows).
    3. Passes CNN features through BiLSTM for temporal context.
    4. Classifies sleep stages with SleepClassifier.
    5. Runs a numpy-based training loop with early stopping.
    6. Saves the best model weights to an HDF5 file.

    Since the CNN and BiLSTM components use fixed random weights (no gradient
    updates in the numpy path), the trainable parameters are the classifier's
    weight matrix W and bias b.  The classifier is trained with mini-batch
    gradient descent using cross-entropy loss.

    Args:
        config_path: Path to config.json.
    """

    def __init__(self, config_path: str = "training_config/config.json") -> None:
        self.config_path = config_path
        self._config = self._load_config(config_path)

        training_cfg = self._config.get("training", {})
        self.batch_size: int = int(training_cfg.get("batch_size", 32))
        self.max_epochs: int = int(training_cfg.get("epochs", 100))
        self.learning_rate: float = float(training_cfg.get("learning_rate", 0.001))
        self.patience: int = int(training_cfg.get("early_stopping_patience", 5))

        # Sliding-window parameters used by `_build_feature_matrix`.  Exposed
        # as instance attributes so that callers (e.g. `scripts/train.py`)
        # can tune them via the CLI without editing the source.
        self.window_size: int = 1024
        self.stride: int = 512

        # Sub-components
        self._normalizer = DataNormalizer(config_path)
        self._cnn = CNNExtractor(config_path=config_path)
        self._bilstm = BiLSTMAnalyzer(config_path=config_path)
        self._classifier = SleepClassifier(config_path=config_path)

        # Training state
        self._best_val_acc: float = -1.0
        self._best_weights: Optional[Dict[str, np.ndarray]] = None

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @staticmethod
    def _load_config(config_path: str) -> Dict:
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:
            logger.warning("Could not load config from %s: %s — using defaults", config_path, exc)
            return {}

    # ------------------------------------------------------------------
    # Feature extraction helpers
    # ------------------------------------------------------------------

    def _extract_features_for_sample(self, hr_window: np.ndarray, mv_window: np.ndarray) -> np.ndarray:
        """Extract a flat feature vector from a pair of signal windows.

        Two complementary feature streams are concatenated:

        * **Deep features** — output of the CNN→BiLSTM stack (mean-pooled
          over time, ``2 * hidden_units`` dimensions).  These capture
          higher-order temporal patterns when the deep net has been trained.
        * **Handcrafted features** — a small bank of statistics computed
          directly on the raw windows (zero-crossing rate, variance of
          first differences, spectral centroid, range, mean absolute value
          and skewness for each channel).  These are robust to global
          z-score normalisation and give the classifier a meaningful
          starting point even before the deep net is fully trained.

        If the windows are shorter than 1024 samples they are zero-padded;
        if longer they are truncated.

        Args:
            hr_window: Heart rate signal window, shape (N,).
            mv_window: Movement signal window, shape (N,).

        Returns:
            1-D feature vector of shape ``(2 * hidden_units + 12,)``.
        """
        target_time = 1024
        target_freq = 128

        def _to_tf_row(signal: np.ndarray) -> np.ndarray:
            """Convert 1-D signal to (target_time, target_freq) matrix via tiling."""
            n = len(signal)
            # Pad or truncate to target_time
            if n < target_time:
                padded = np.zeros(target_time, dtype=np.float32)
                padded[:n] = signal
            else:
                padded = signal[:target_time].astype(np.float32)
            # Tile along frequency axis
            return np.tile(padded[:, np.newaxis], (1, target_freq))  # (1024, 128)

        hr_mat = _to_tf_row(hr_window)
        mv_mat = _to_tf_row(mv_window)

        # Stack into (1024, 128, 2)
        tf_matrix = np.stack([hr_mat, mv_mat], axis=-1)  # (1024, 128, 2)

        # CNN: (1024, 128, 2) → (256, 32, 64)
        cnn_out = self._cnn.extract_features(tf_matrix)  # (256, 32, 64)

        # Reshape CNN output to sequence for BiLSTM: (T, feature_dim)
        # Treat the 256 time steps as the sequence, flatten 32×64 as features
        T = cnn_out.shape[0]
        cnn_seq = cnn_out.reshape(T, -1)  # (256, 32*64=2048)

        # BiLSTM: (256, 2048) → (256, 2*hidden_units)
        bilstm_out = self._bilstm.analyze(cnn_seq)  # (256, 256)

        # Mean-pool over time to get a fixed-size deep feature vector
        deep_feat = bilstm_out.mean(axis=0)  # (2*hidden_units,)

        # Handcrafted statistics computed on the raw windows
        handcrafted = self._handcrafted_features(hr_window, mv_window)

        return np.concatenate([deep_feat, handcrafted]).astype(np.float32)

    @staticmethod
    def _handcrafted_features(
        hr_window: np.ndarray,
        mv_window: np.ndarray,
    ) -> np.ndarray:
        """Compute 12 time-domain / spectral statistics for the two channels.

        Six statistics are computed independently on each channel:

        * Zero-crossing rate (proxy for dominant frequency)
        * Variance of first differences (smoothness / activity)
        * Spectral centroid (mean frequency, normalised to Nyquist)
        * Range (max − min)
        * Mean absolute value
        * Skewness (asymmetry; useful for separating wake / REM from deep)

        Returns:
            1-D array of length 12 ``[hr_features..., mv_features...]``.
        """
        def stats(x: np.ndarray) -> np.ndarray:
            x = np.asarray(x, dtype=np.float64)
            if x.size < 2 or np.std(x) < 1e-12:
                return np.zeros(6, dtype=np.float64)
            # Zero-crossing rate
            zcr = float(np.mean(np.abs(np.diff(np.sign(x - np.mean(x)))) > 0))
            # Smoothness — variance of first differences
            diff_var = float(np.var(np.diff(x)))
            # Spectral centroid (normalised to Nyquist = 1.0)
            spec = np.abs(np.fft.rfft(x))
            freqs = np.fft.rfftfreq(x.size)
            spec_sum = spec.sum()
            centroid = float(np.sum(spec * freqs) / spec_sum) if spec_sum > 0 else 0.0
            # Range and mean absolute value
            rng = float(np.max(x) - np.min(x))
            mav = float(np.mean(np.abs(x)))
            # Skewness (sample skewness, dimensionless)
            mean = np.mean(x)
            std = np.std(x) + 1e-12
            skew = float(np.mean(((x - mean) / std) ** 3))
            return np.array([zcr, diff_var, centroid, rng, mav, skew], dtype=np.float64)

        return np.concatenate([stats(hr_window), stats(mv_window)])

    def _build_feature_matrix(
        self,
        dataset,
        window_size: Optional[int] = None,
        stride: Optional[int] = None,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Build (X, y) arrays from a Dataset using sliding windows.

        Args:
            dataset: Dataset object with heart_rate, movement, sleep_stages.
            window_size: Number of samples per window.  Defaults to
                ``self.window_size`` (1024).
            stride: Step between consecutive windows.  Defaults to
                ``self.stride`` (512).

        Returns:
            X: Feature matrix of shape (N_windows, feature_dim).
            y: Label array of shape (N_windows,) with integer sleep stage indices.
        """
        if window_size is None:
            window_size = self.window_size
        if stride is None:
            stride = self.stride

        hr_values = dataset.heart_rate.values.astype(np.float32)
        mv_values = dataset.movement.values.astype(np.float32)
        stage_values = dataset.sleep_stages.stages.astype(np.int32)

        n = len(hr_values)
        features = []
        labels = []

        for start in range(0, n - window_size + 1, stride):
            end = start + window_size
            hr_win = hr_values[start:end]
            mv_win = mv_values[start:end]

            # Majority vote for label in this window
            window_labels = stage_values[start:end]
            label = int(np.bincount(window_labels, minlength=4).argmax())

            feat = self._extract_features_for_sample(hr_win, mv_win)
            features.append(feat)
            labels.append(label)

        if not features:
            # Fallback: use the whole signal as one window
            feat = self._extract_features_for_sample(hr_values, mv_values)
            label = int(np.bincount(stage_values, minlength=4).argmax())
            features.append(feat)
            labels.append(label)

        return np.array(features, dtype=np.float32), np.array(labels, dtype=np.int32)

    # ------------------------------------------------------------------
    # Classifier weight helpers (numpy path)
    # ------------------------------------------------------------------

    def _get_classifier_weights(self) -> Dict[str, np.ndarray]:
        """Return a copy of the classifier's current numpy weights."""
        if self._classifier._use_keras:
            # Extract weights from Keras dense layer
            dense_layer = self._classifier._model.get_layer("softmax")
            W, b = dense_layer.get_weights()
            return {"W": W.copy(), "b": b.copy()}
        else:
            return {
                "W": self._classifier._W.copy(),
                "b": self._classifier._b.copy(),
            }

    def _set_classifier_weights(self, weights: Dict[str, np.ndarray]) -> None:
        """Restore classifier weights from a saved dict."""
        if self._classifier._use_keras:
            dense_layer = self._classifier._model.get_layer("softmax")
            dense_layer.set_weights([weights["W"], weights["b"]])
        else:
            self._classifier._W = weights["W"].copy()
            self._classifier._b = weights["b"].copy()

    def _classifier_forward(self, X: np.ndarray) -> np.ndarray:
        """Run classifier forward pass, returning probabilities (N, num_classes)."""
        return self._classifier.get_probability_distribution(X)

    def _classifier_update(self, X: np.ndarray, y: np.ndarray) -> None:
        """One mini-batch gradient descent step on the classifier (numpy path).

        Uses cross-entropy loss with softmax output.  Gradient of cross-entropy
        w.r.t. logits is (probs - one_hot) / N.
        """
        if self._classifier._use_keras:
            # For Keras path, use a simple manual gradient step on the dense layer
            dense_layer = self._classifier._model.get_layer("softmax")
            W, b = dense_layer.get_weights()
            probs = self._classifier_forward(X)  # (N, C)
            n = len(y)
            one_hot = np.zeros_like(probs)
            one_hot[np.arange(n), y] = 1.0
            delta = (probs - one_hot) / n  # (N, C)
            dW = X.T @ delta  # (feature_dim, C)
            db = delta.sum(axis=0)  # (C,)
            W -= self.learning_rate * dW
            b -= self.learning_rate * db
            dense_layer.set_weights([W, b])
        else:
            # Ensure classifier is initialised
            if self._classifier._W is None:
                self._classifier._ensure_initialised(X.shape[1])
            probs = self._classifier_forward(X)  # (N, C)
            n = len(y)
            one_hot = np.zeros_like(probs)
            one_hot[np.arange(n), y] = 1.0
            delta = (probs - one_hot) / n  # (N, C)
            dW = X.T @ delta
            db = delta.sum(axis=0)
            self._classifier._W -= self.learning_rate * dW
            self._classifier._b -= self.learning_rate * db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def train(
        self,
        training_set: TrainingSet,
        validation_set: TestSet,
        model_save_path: str = "models/best_model.h5",
    ) -> Dict:
        """Run training loop with early stopping.

        Args:
            training_set: Training data (will be normalised internally).
            validation_set: Validation data for early stopping.
            model_save_path: Path to save best model weights (HDF5).

        Returns:
            Training history dict with keys:
                'epochs': list of epoch numbers (1-indexed)
                'train_loss': list of training losses per epoch
                'train_acc': list of training accuracies per epoch
                'val_loss': list of validation losses per epoch
                'val_acc': list of validation accuracies per epoch
        """
        logger.info("Starting training pipeline")

        # --- Normalise data ---
        logger.info("Fitting normalizer on training data")
        normalised_training = self._normalizer.fit_transform(training_set)
        normalised_val_dataset = self._normalizer.transform(validation_set.dataset)

        # --- Build feature matrices ---
        logger.info("Extracting features from training set")
        X_train, y_train = self._build_feature_matrix(normalised_training.dataset)
        logger.info("Extracting features from validation set")
        X_val, y_val = self._build_feature_matrix(normalised_val_dataset)

        logger.info(
            "Feature shapes — train: %s, val: %s", X_train.shape, X_val.shape
        )

        # Ensure classifier is initialised with correct feature dim
        self._classifier._ensure_initialised(X_train.shape[1])

        # --- Training history ---
        history: Dict[str, List] = {
            "epochs": [],
            "train_loss": [],
            "train_acc": [],
            "val_loss": [],
            "val_acc": [],
        }

        best_val_acc = -1.0
        epochs_without_improvement = 0
        n_train = len(X_train)

        for epoch in range(1, self.max_epochs + 1):
            # Shuffle training data
            perm = np.random.permutation(n_train)
            X_shuffled = X_train[perm]
            y_shuffled = y_train[perm]

            # Mini-batch gradient descent
            for start in range(0, n_train, self.batch_size):
                end = min(start + self.batch_size, n_train)
                X_batch = X_shuffled[start:end]
                y_batch = y_shuffled[start:end]
                self._classifier_update(X_batch, y_batch)

            # --- Epoch metrics ---
            train_probs = self._classifier_forward(X_train)
            train_loss = _cross_entropy_loss(train_probs, y_train)
            train_acc = _accuracy(train_probs, y_train)

            val_probs = self._classifier_forward(X_val)
            val_loss = _cross_entropy_loss(val_probs, y_val)
            val_acc = _accuracy(val_probs, y_val)

            history["epochs"].append(epoch)
            history["train_loss"].append(train_loss)
            history["train_acc"].append(train_acc)
            history["val_loss"].append(val_loss)
            history["val_acc"].append(val_acc)

            logger.info(
                "Epoch %d/%d — train_loss=%.4f train_acc=%.4f "
                "val_loss=%.4f val_acc=%.4f",
                epoch,
                self.max_epochs,
                train_loss,
                train_acc,
                val_loss,
                val_acc,
            )

            # --- Checkpoint best model ---
            if val_acc > best_val_acc:
                best_val_acc = val_acc
                self._best_val_acc = best_val_acc
                self._best_weights = self._get_classifier_weights()
                self._save_model(model_save_path)
                logger.info(
                    "New best val_acc=%.4f — model saved to %s",
                    best_val_acc,
                    model_save_path,
                )
                epochs_without_improvement = 0
            else:
                epochs_without_improvement += 1
                logger.debug(
                    "No improvement for %d epoch(s) (patience=%d)",
                    epochs_without_improvement,
                    self.patience,
                )

            # --- Early stopping ---
            if epochs_without_improvement >= self.patience:
                logger.info(
                    "Early stopping triggered at epoch %d "
                    "(no improvement for %d epochs)",
                    epoch,
                    self.patience,
                )
                break

        # Restore best weights
        if self._best_weights is not None:
            self._set_classifier_weights(self._best_weights)
            logger.info("Restored best model weights (val_acc=%.4f)", best_val_acc)

        logger.info("Training complete — best val_acc=%.4f", best_val_acc)
        return history

    def evaluate(self, test_set: TestSet) -> Dict:
        """Evaluate the model on a test set.

        Args:
            test_set: Test dataset.

        Returns:
            Dict with keys: 'loss', 'accuracy'.
        """
        normalised_dataset = self._normalizer.transform(test_set.dataset)
        X_test, y_test = self._build_feature_matrix(normalised_dataset)

        probs = self._classifier_forward(X_test)
        loss = _cross_entropy_loss(probs, y_test)
        acc = _accuracy(probs, y_test)

        logger.info("Evaluation — loss=%.4f accuracy=%.4f", loss, acc)
        return {"loss": loss, "accuracy": acc}

    # ------------------------------------------------------------------
    # Model persistence
    # ------------------------------------------------------------------

    def _save_model(self, path: str) -> None:
        """Save the full CNN + BiLSTM + classifier to an HDF5 file.

        Uses :class:`~src.model_persistence.ModelPersistence` so that the
        feature-space (CNN/BiLSTM weights) is preserved alongside the
        trainable classifier weights.  This guarantees that reloading the
        model restores identical predictions.

        Args:
            path: Destination file path (e.g. ``"models/best_model.h5"``).
        """
        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        try:
            from src.model_persistence import ModelPersistence
            ModelPersistence().save_model(
                self._cnn, self._bilstm, self._classifier, path
            )
        except RuntimeError as exc:
            # h5py missing — fall back to a .npz dump of just the classifier.
            logger.warning(
                "ModelPersistence failed (%s) — saving classifier weights as .npz", exc
            )
            npz_path = path.replace(".h5", ".npz")
            weights = self._get_classifier_weights()
            np.savez(npz_path, **weights)
            return
        logger.debug("Saved full model to %s", path)

    def load_model(self, path: str) -> None:
        """Load the full CNN + BiLSTM + classifier from an HDF5 file.

        Replaces the pipeline's CNN, BiLSTM, and classifier with the saved
        instances so subsequent feature extraction and prediction match the
        original training run exactly.

        Args:
            path: Source file path.

        Raises:
            FileNotFoundError: If the file does not exist.
            RuntimeError: If the file is corrupted or incompatible.
        """
        if not os.path.exists(path):
            raise FileNotFoundError(f"Model file not found: {path}")

        try:
            from src.model_persistence import ModelPersistence
            cnn, bilstm, classifier = ModelPersistence().load_model(path)
            self._cnn = cnn
            self._bilstm = bilstm
            self._classifier = classifier
            logger.info("Loaded full model from %s", path)
        except (FileNotFoundError, RuntimeError):
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed to load model from {path}: {exc}") from exc
