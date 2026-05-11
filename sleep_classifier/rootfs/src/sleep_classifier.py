"""Sleep stage classifier using fully connected layer + softmax.

Accepts BiLSTM feature vectors and outputs:
- Sleep stage prediction (SleepStage enum)
- Confidence score (max probability, ∈ [0,1])
- Full probability distribution (sum = 1)

Requirements: 10.1, 10.2, 10.3, 10.4
"""

import json
import logging
import os
from typing import Optional, Tuple

import numpy as np

from src.data_structures import SleepStage

logger = logging.getLogger(__name__)

# Optional TensorFlow/Keras import
try:
    import tensorflow as tf
    from tensorflow import keras

    TENSORFLOW_AVAILABLE = True
except ImportError:  # pragma: no cover
    TENSORFLOW_AVAILABLE = False


class SleepClassifier:
    """Fully-connected + softmax classifier for sleep stage prediction.

    Input : BiLSTM feature vector, shape (feature_dim,) or (batch, feature_dim)
    Output: SleepStage enum + confidence float, or probability array

    The classifier applies a single linear (Dense) layer followed by softmax
    to produce a probability distribution over the four sleep stages:
        0 → AWAKE, 1 → LIGHT, 2 → DEEP, 3 → REM

    Weights are initialised lazily on the first call so that the feature
    dimension does not need to be known at construction time.
    """

    # Ordered list of sleep stages matching class indices 0-3
    SLEEP_STAGES = [SleepStage.AWAKE, SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM]

    def __init__(
        self,
        num_classes: int = 4,
        config_path: Optional[str] = None,
    ) -> None:
        """Initialise the classifier.

        Args:
            num_classes: Number of output classes (default 4).
            config_path: Optional path to config.json for parameter overrides.
        """
        cfg_classes = self._load_config(config_path)
        # Constructor argument takes precedence over config when explicitly provided
        # (config only overrides the default value)
        self.num_classes = num_classes  # use constructor arg first
        if cfg_classes is not None and num_classes == 4:
            # Only apply config override when the caller used the default value
            self.num_classes = cfg_classes

        if self.num_classes <= 0:
            raise ValueError(f"num_classes must be positive, got {self.num_classes}")

        self._feature_dim: Optional[int] = None  # set lazily on first call

        if TENSORFLOW_AVAILABLE:
            self._model: Optional["keras.Model"] = None
            self._use_keras = True
        else:
            # Numpy fallback: weight matrix W (feature_dim, num_classes) + bias b (num_classes,)
            self._W: Optional[np.ndarray] = None
            self._b: Optional[np.ndarray] = None
            self._use_keras = False

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def _load_config(self, config_path: Optional[str]) -> Optional[int]:
        """Load num_classes from config file if available."""
        if config_path is None:
            candidate = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "config", "config.json"
            )
            if os.path.exists(candidate):
                config_path = candidate
            else:
                return None

        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            num_classes = cfg["model"]["classifier"].get("num_classes")
            if num_classes is not None and num_classes <= 0:
                raise ValueError("num_classes must be positive")
            return num_classes
        except Exception as exc:
            logger.error(
                "Failed to load classifier config from %s, using constructor defaults: %s",
                config_path,
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Lazy model initialisation
    # ------------------------------------------------------------------

    def _build_keras_model(self, feature_dim: int) -> "keras.Model":
        """Build a Keras Dense + softmax model."""
        inputs = keras.Input(shape=(feature_dim,), name="bilstm_features")
        outputs = keras.layers.Dense(self.num_classes, activation="softmax", name="softmax")(inputs)
        model = keras.Model(inputs=inputs, outputs=outputs, name="sleep_classifier")
        return model

    def _init_numpy_weights(self, feature_dim: int) -> None:
        """Initialise random weights for the numpy fallback."""
        rng = np.random.default_rng(seed=42)
        self._W = rng.standard_normal((feature_dim, self.num_classes)).astype(np.float32)
        self._b = np.zeros(self.num_classes, dtype=np.float32)

    def _ensure_initialised(self, feature_dim: int) -> None:
        """Lazily build the model once the feature dimension is known."""
        if self._feature_dim is not None:
            return  # already initialised
        self._feature_dim = feature_dim
        if self._use_keras:
            self._model = self._build_keras_model(feature_dim)
            self._apply_pending_keras_weights()
        else:
            self._init_numpy_weights(feature_dim)

    def _apply_pending_keras_weights(self) -> None:
        """Apply weights stored by ModelPersistence.load_model.

        After a Keras model is restored from HDF5, the actual weights cannot
        be applied until the lazily-built model exists.  ModelPersistence
        therefore stashes them in ``_pending_keras_weights`` as a plain
        ``{layer_name: {"0": ndarray, "1": ndarray, ...}}`` dict; this method
        applies them once ``_build_keras_model`` has produced a model.
        """
        pending = getattr(self, "_pending_keras_weights", None)
        if pending is None or self._model is None:
            return
        for layer in self._model.layers:
            if layer.name in pending:
                layer_dict = pending[layer.name]
                weights = [layer_dict[str(i)] for i in range(len(layer_dict))]
                layer.set_weights(weights)
        self._pending_keras_weights = None

    # ------------------------------------------------------------------
    # Softmax helper (numpy path)
    # ------------------------------------------------------------------

    @staticmethod
    def _softmax(logits: np.ndarray) -> np.ndarray:
        """Numerically stable softmax over the last axis."""
        shifted = logits - np.max(logits, axis=-1, keepdims=True)
        exp_x = np.exp(shifted)
        return exp_x / np.sum(exp_x, axis=-1, keepdims=True)

    # ------------------------------------------------------------------
    # Input validation / normalisation
    # ------------------------------------------------------------------

    def _prepare_input(self, bilstm_features: np.ndarray) -> Tuple[np.ndarray, bool]:
        """Validate and reshape input to (batch, feature_dim).

        Returns:
            (batch_array, was_single) where was_single indicates the caller
            passed a 1-D vector (so we should return a scalar result).
        """
        arr = np.asarray(bilstm_features, dtype=np.float32)
        if arr.ndim == 1:
            batch = arr[np.newaxis, :]  # (1, feature_dim)
            was_single = True
        elif arr.ndim == 2:
            batch = arr
            was_single = False
        else:
            raise ValueError(
                f"bilstm_features must be 1-D or 2-D, got shape {arr.shape}"
            )
        feature_dim = batch.shape[1]
        self._ensure_initialised(feature_dim)
        return batch, was_single

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_probability_distribution(self, bilstm_features: np.ndarray) -> np.ndarray:
        """Compute softmax probability distribution over all sleep stages.

        Args:
            bilstm_features: Feature vector of shape (feature_dim,) or
                             (batch, feature_dim).

        Returns:
            Probability array of shape (num_classes,) for single input or
            (batch, num_classes) for batched input.  All values ∈ [0,1] and
            each row sums to 1.
        """
        batch, was_single = self._prepare_input(bilstm_features)

        if self._use_keras and self._model is not None:
            probs = self._model.predict(batch, verbose=0)
        else:
            # Numpy fallback: linear transform + softmax
            logits = batch @ self._W + self._b  # (batch, num_classes)
            probs = self._softmax(logits)

        probs = np.asarray(probs, dtype=np.float64)

        if was_single:
            return probs[0]  # (num_classes,)
        return probs  # (batch, num_classes)

    def classify(self, bilstm_features: np.ndarray) -> Tuple[SleepStage, float]:
        """Classify a single BiLSTM feature vector into a sleep stage.

        Args:
            bilstm_features: Feature vector of shape (feature_dim,) or
                             (1, feature_dim).

        Returns:
            Tuple of (SleepStage, confidence) where confidence = max probability
            and is guaranteed to be in [0, 1].

        Raises:
            ValueError: If input is batched with more than one sample.
        """
        arr = np.asarray(bilstm_features, dtype=np.float32)
        if arr.ndim == 2 and arr.shape[0] != 1:
            raise ValueError(
                "classify() expects a single sample; use get_probability_distribution() "
                f"for batched input (got batch size {arr.shape[0]})"
            )

        probs = self.get_probability_distribution(bilstm_features)
        # Ensure 1-D for single sample
        if probs.ndim == 2:
            probs = probs[0]

        class_idx = int(np.argmax(probs))
        confidence = float(probs[class_idx])

        # Map index to SleepStage; fall back gracefully for non-standard num_classes
        if class_idx < len(self.SLEEP_STAGES):
            stage = self.SLEEP_STAGES[class_idx]
        else:
            stage = SleepStage(class_idx % len(self.SLEEP_STAGES))

        return stage, confidence
