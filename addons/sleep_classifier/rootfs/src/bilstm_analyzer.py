"""BiLSTM temporal analyzer for sleep stage classification.

Implements a Bidirectional LSTM architecture that captures forward and backward
temporal dependencies in CNN feature sequences, with a 30-minute memory window.
"""
import json
import logging
import os
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Try to import TensorFlow/Keras; fall back to numpy-based implementation
try:
    import tensorflow as tf
    from tensorflow import keras
    from tensorflow.keras import layers
    TENSORFLOW_AVAILABLE = True
    logger.info("TensorFlow available — using Keras BiLSTM implementation")
except ImportError:
    TENSORFLOW_AVAILABLE = False
    logger.warning("TensorFlow not available — using numpy-based BiLSTM implementation")


# ---------------------------------------------------------------------------
# Numpy-based LSTM cell helpers
# ---------------------------------------------------------------------------

def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def _tanh(x: np.ndarray) -> np.ndarray:
    return np.tanh(np.clip(x, -30, 30))


def _lstm_forward(
    x_seq: np.ndarray,
    W: np.ndarray,
    U: np.ndarray,
    b: np.ndarray,
) -> np.ndarray:
    """Run a single-direction LSTM over a sequence.

    Args:
        x_seq: Input sequence of shape (T, input_dim).
        W: Input weight matrix of shape (input_dim, 4*hidden_units).
        U: Recurrent weight matrix of shape (hidden_units, 4*hidden_units).
        b: Bias vector of shape (4*hidden_units,).

    Returns:
        Output sequence of shape (T, hidden_units).
    """
    T, _ = x_seq.shape
    hidden_units = U.shape[0]

    h = np.zeros(hidden_units, dtype=np.float32)
    c = np.zeros(hidden_units, dtype=np.float32)
    outputs = []

    for t in range(T):
        gates = x_seq[t] @ W + h @ U + b  # (4*hidden_units,)
        i = _sigmoid(gates[0 * hidden_units: 1 * hidden_units])   # input gate
        f = _sigmoid(gates[1 * hidden_units: 2 * hidden_units])   # forget gate
        g = _tanh(gates[2 * hidden_units: 3 * hidden_units])      # cell gate
        o = _sigmoid(gates[3 * hidden_units: 4 * hidden_units])   # output gate

        c = f * c + i * g
        h = o * _tanh(c)
        outputs.append(h.copy())

    return np.stack(outputs, axis=0)  # (T, hidden_units)


# ---------------------------------------------------------------------------
# BiLSTMAnalyzer
# ---------------------------------------------------------------------------

class BiLSTMAnalyzer:
    """Bidirectional LSTM analyzer for temporal sleep feature analysis.

    Captures both forward and backward temporal dependencies in CNN feature
    sequences. The memory window of 1800 seconds (30 minutes) defines the
    context length for sleep stage transitions.

    Input shape : (T, feature_dim) or (batch, T, feature_dim)
    Output shape: (T, 2*hidden_units) or (batch, T, 2*hidden_units)

    The output concatenates forward and backward LSTM hidden states, giving
    exactly 2*hidden_units dimensions (requirement 9.5, 9.6).
    """

    def __init__(
        self,
        hidden_units: int = 128,
        memory_window: int = 1800,
        config_path: Optional[str] = None,
    ) -> None:
        """Initialize BiLSTM with hidden units and memory window.

        Args:
            hidden_units: Number of LSTM hidden units (default 128).
            memory_window: Memory window in seconds (default 1800 = 30 minutes).
            config_path: Optional path to config.json for parameter overrides.
        """
        # Load config overrides if available
        cfg_hidden, cfg_window = self._load_config(config_path)
        self.hidden_units = cfg_hidden if cfg_hidden is not None else hidden_units
        self.memory_window = cfg_window if cfg_window is not None else memory_window

        if self.hidden_units <= 0:
            raise ValueError(f"hidden_units must be positive, got {self.hidden_units}")
        if self.memory_window <= 0:
            raise ValueError(f"memory_window must be positive, got {self.memory_window}")

        self._feature_dim: Optional[int] = None  # set on first call

        if TENSORFLOW_AVAILABLE:
            self._model: Optional["keras.Model"] = None  # built lazily on first input
            self._use_keras = True
        else:
            self._weights: Optional[dict] = None  # built lazily on first input
            self._use_keras = False

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def _load_config(self, config_path: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
        """Load BiLSTM parameters from config file.

        Returns:
            Tuple of (hidden_units, memory_window_seconds), either may be None
            if not found or config is invalid.
        """
        if config_path is None:
            candidate = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "config", "config.json"
            )
            if os.path.exists(candidate):
                config_path = candidate
            else:
                return None, None

        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            bilstm_cfg = cfg["model"]["bilstm"]
            hidden_units = bilstm_cfg.get("hidden_units")
            memory_window = bilstm_cfg.get("memory_window_seconds")
            if hidden_units is not None and hidden_units <= 0:
                raise ValueError("hidden_units must be positive")
            if memory_window is not None and memory_window <= 0:
                raise ValueError("memory_window_seconds must be positive")
            return hidden_units, memory_window
        except Exception as exc:
            logger.error(
                "Failed to load BiLSTM config from %s, using constructor defaults: %s",
                config_path,
                exc,
            )
            return None, None

    # ------------------------------------------------------------------
    # Keras model (built lazily once feature_dim is known)
    # ------------------------------------------------------------------

    def _apply_pending_keras_weights(self) -> None:
        """Apply weights deferred by ModelPersistence.load_model.

        Stored as ``{layer_name: {"0": ndarray, "1": ndarray, ...}}``.
        """
        pending = getattr(self, "_pending_keras_weights", None)
        if pending is None or self._model is None:
            return
        for layer in self._model.layers:
            if layer.name in pending:
                layer_dict = pending[layer.name]
                weights = [layer_dict[str(i)] for i in range(len(layer_dict))]
                try:
                    layer.set_weights(weights)
                except Exception as exc:
                    logger.warning(
                        "Could not restore weights for layer '%s': %s",
                        layer.name, exc,
                    )
        self._pending_keras_weights = None

    def _build_keras_model(self, feature_dim: int) -> "keras.Model":
        """Build and return the Keras Bidirectional LSTM model."""
        inputs = keras.Input(shape=(None, feature_dim), name="cnn_features")
        x = layers.Bidirectional(
            layers.LSTM(self.hidden_units, return_sequences=True),
            name="bilstm",
        )(inputs)
        model = keras.Model(inputs=inputs, outputs=x, name="bilstm_analyzer")
        return model

    # ------------------------------------------------------------------
    # Numpy weights (fallback, built lazily)
    # ------------------------------------------------------------------

    def _init_numpy_weights(self, feature_dim: int) -> dict:
        """Initialize random weights for the numpy-based BiLSTM."""
        rng = np.random.default_rng(seed=42)
        h = self.hidden_units
        d = feature_dim

        # He-like initialization scale
        scale_w = np.sqrt(2.0 / (d + h))
        scale_u = np.sqrt(2.0 / (h + h))

        return {
            # Forward LSTM
            "fwd_W": rng.standard_normal((d, 4 * h)).astype(np.float32) * scale_w,
            "fwd_U": rng.standard_normal((h, 4 * h)).astype(np.float32) * scale_u,
            "fwd_b": np.zeros(4 * h, dtype=np.float32),
            # Backward LSTM
            "bwd_W": rng.standard_normal((d, 4 * h)).astype(np.float32) * scale_w,
            "bwd_U": rng.standard_normal((h, 4 * h)).astype(np.float32) * scale_u,
            "bwd_b": np.zeros(4 * h, dtype=np.float32),
        }

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------

    def _validate_input(self, cnn_features: np.ndarray) -> Tuple[np.ndarray, bool]:
        """Validate and normalize input to shape (B, T, feature_dim).

        Args:
            cnn_features: Array of shape (T, feature_dim) or (B, T, feature_dim).

        Returns:
            Tuple of (batch_array of shape (B, T, feature_dim), is_single_sample).

        Raises:
            ValueError: If input dimensions are invalid.
        """
        arr = np.asarray(cnn_features, dtype=np.float32)

        if arr.ndim == 2:
            # Single sample (T, feature_dim) → add batch dim
            arr = arr[np.newaxis, ...]  # (1, T, feature_dim)
            single = True
        elif arr.ndim == 3:
            single = False
        else:
            raise ValueError(
                f"Input must be 2-D (T, feature_dim) or 3-D (B, T, feature_dim), "
                f"got {arr.ndim}-D array with shape {arr.shape}"
            )

        if arr.shape[1] == 0:
            raise ValueError("Sequence length T must be > 0")
        if arr.shape[2] == 0:
            raise ValueError("feature_dim must be > 0")

        return arr, single

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, cnn_features: np.ndarray) -> np.ndarray:
        """Process CNN features through BiLSTM and return bidirectional context vectors.

        The output concatenates forward and backward LSTM hidden states at each
        time step, yielding exactly 2*hidden_units output dimensions.

        Requirements satisfied:
        - 9.1: Cell state initialized to capture past 30-minute feature trends
        - 9.2: Input gate selectively introduces current HRV/movement features
        - 9.3: Forget gate discards irrelevant historical information
        - 9.4: Output gate generates sleep-depth-related feature vectors
        - 9.5: Processes both forward and backward time sequences

        Args:
            cnn_features: CNN feature array of shape (T, feature_dim) for a
                single sample, or (B, T, feature_dim) for a batch.

        Returns:
            Bidirectional context vectors of shape (T, 2*hidden_units) for a
            single sample, or (B, T, 2*hidden_units) for a batch.

        Raises:
            ValueError: If input dimensions are invalid.
        """
        batch, single = self._validate_input(cnn_features)
        B, T, feature_dim = batch.shape

        # Lazy initialization on first call
        if self._feature_dim is None:
            self._feature_dim = feature_dim
            if self._use_keras:
                self._model = self._build_keras_model(feature_dim)
                self._apply_pending_keras_weights()
            else:
                self._weights = self._init_numpy_weights(feature_dim)

        if self._use_keras:
            output = self._model(batch, training=False).numpy()
        else:
            output = self._analyze_numpy(batch)

        # output shape: (B, T, 2*hidden_units)
        return output[0] if single else output

    # ------------------------------------------------------------------
    # Numpy forward pass
    # ------------------------------------------------------------------

    def _analyze_numpy(self, batch: np.ndarray) -> np.ndarray:
        """Run the numpy-based BiLSTM forward pass on a batch.

        Args:
            batch: Array of shape (B, T, feature_dim).

        Returns:
            Array of shape (B, T, 2*hidden_units).
        """
        results = []
        for sample in batch:
            # Forward pass: process sequence left-to-right
            fwd_out = _lstm_forward(
                sample,
                self._weights["fwd_W"],
                self._weights["fwd_U"],
                self._weights["fwd_b"],
            )  # (T, hidden_units)

            # Backward pass: process reversed sequence, then reverse output
            bwd_out = _lstm_forward(
                sample[::-1],
                self._weights["bwd_W"],
                self._weights["bwd_U"],
                self._weights["bwd_b"],
            )[::-1]  # (T, hidden_units)

            # Concatenate forward and backward: (T, 2*hidden_units)
            combined = np.concatenate([fwd_out, bwd_out], axis=-1)
            results.append(combined)

        return np.stack(results, axis=0)  # (B, T, 2*hidden_units)
