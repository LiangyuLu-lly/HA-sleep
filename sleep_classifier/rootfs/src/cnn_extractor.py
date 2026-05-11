"""CNN feature extractor for dual-channel time-frequency matrices.

Implements a two-layer CNN architecture for extracting sleep-relevant features
from 1024×128×2 time-frequency matrices (heart rate + movement channels).
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
    logger.info("TensorFlow available — using Keras CNN implementation")
except ImportError:
    TENSORFLOW_AVAILABLE = False
    logger.warning("TensorFlow not available — using numpy-based CNN implementation")


# ---------------------------------------------------------------------------
# Numpy-based fallback helpers
# ---------------------------------------------------------------------------

def _relu(x: np.ndarray) -> np.ndarray:
    return np.maximum(0.0, x)


def _conv2d_numpy(
    x: np.ndarray,
    filters: np.ndarray,
    biases: np.ndarray,
) -> np.ndarray:
    """Same-padding 2-D convolution using scipy for speed.

    Args:
        x: Input array of shape (H, W, C_in).
        filters: Kernel array of shape (kH, kW, C_in, C_out).
        biases: Bias array of shape (C_out,).

    Returns:
        Output array of shape (H, W, C_out)  (same padding).
    """
    from scipy.signal import fftconvolve

    kH, kW, C_in, C_out = filters.shape
    H, W, _ = x.shape
    out = np.zeros((H, W, C_out), dtype=np.float32)

    for c_out in range(C_out):
        acc = np.zeros((H, W), dtype=np.float32)
        for c_in in range(C_in):
            # fftconvolve with 'same' mode — flip kernel to get cross-correlation
            kernel = filters[:, :, c_in, c_out][::-1, ::-1]
            acc += fftconvolve(x[:, :, c_in], kernel, mode="same").astype(np.float32)
        out[:, :, c_out] = acc + biases[c_out]
    return out


def _maxpool2d_numpy(x: np.ndarray, pool_size: Tuple[int, int] = (2, 2)) -> np.ndarray:
    """Non-overlapping max-pooling.

    Args:
        x: Input array of shape (H, W, C).
        pool_size: (pH, pW) pooling window.

    Returns:
        Output array of shape (H // pH, W // pW, C).
    """
    pH, pW = pool_size
    H, W, C = x.shape
    out_H = H // pH
    out_W = W // pW
    out = np.zeros((out_H, out_W, C), dtype=np.float32)
    for i in range(out_H):
        for j in range(out_W):
            out[i, j, :] = x[i * pH : (i + 1) * pH, j * pW : (j + 1) * pW, :].max(
                axis=(0, 1)
            )
    return out


# ---------------------------------------------------------------------------
# CNNExtractor
# ---------------------------------------------------------------------------

class CNNExtractor:
    """Dual-channel CNN feature extractor for sleep stage classification.

    Architecture (per requirements 8.2–8.5):
        Conv2D(32, 3×3, relu) → MaxPool(2×2) →
        Conv2D(64, 3×3, relu) → MaxPool(2×2)

    Input shape : (1024, 128, 2)  — time × frequency × [heart-rate, movement]
    Output shape: (256, 32, 64)   — after two 2×2 max-pooling operations

    The heart-rate channel (index 0) emphasises 0.1–0.4 Hz HRV features;
    the movement channel (index 1) emphasises 0.1–5 Hz movement features.
    These frequency bands correspond to specific rows in the 128-bin frequency
    axis of the time-frequency matrix and are naturally captured by the
    convolutional kernels during training.

    Supports batch processing: input may be (1024, 128, 2) or (B, 1024, 128, 2).
    """

    INPUT_SHAPE: Tuple[int, int, int] = (1024, 128, 2)

    def __init__(
        self,
        input_shape: Tuple[int, int, int] = (1024, 128, 2),
        config_path: Optional[str] = None,
    ) -> None:
        """Initialise the CNN extractor.

        Args:
            input_shape: Expected input shape (H, W, C).  Must be (1024, 128, 2).
            config_path: Optional path to config.json.  When provided the CNN
                architecture parameters (num_filters, kernel_size, pool_size)
                are read from the file.
        """
        if input_shape != self.INPUT_SHAPE:
            raise ValueError(
                f"input_shape must be {self.INPUT_SHAPE}, got {input_shape}"
            )
        self.input_shape = input_shape

        # Load architecture parameters from config (or use defaults)
        num_filters, kernel_size, pool_size = self._load_config(config_path)
        self.num_filters = num_filters      # e.g. [32, 64]
        self.kernel_size = kernel_size      # e.g. [3, 3]
        self.pool_size = pool_size          # e.g. [2, 2]

        # Build the model
        if TENSORFLOW_AVAILABLE:
            self._model = self._build_keras_model()
            self._use_keras = True
        else:
            self._weights = self._init_numpy_weights()
            self._use_keras = False

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def _load_config(
        self, config_path: Optional[str]
    ) -> Tuple[list, list, list]:
        """Load CNN architecture parameters from config file.

        Falls back to defaults if the file is missing or invalid.
        """
        defaults = ([32, 64], [3, 3], [2, 2])
        if config_path is None:
            # Try the standard project location
            candidate = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "config", "config.json"
            )
            if os.path.exists(candidate):
                config_path = candidate
            else:
                return defaults

        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            cnn_cfg = cfg["model"]["cnn"]
            num_filters = cnn_cfg.get("num_filters", defaults[0])
            kernel_size = cnn_cfg.get("kernel_size", defaults[1])
            pool_size = cnn_cfg.get("pool_size", defaults[2])
            # Basic validation
            if not (isinstance(num_filters, list) and len(num_filters) >= 2):
                raise ValueError("num_filters must be a list with at least 2 elements")
            if not (isinstance(kernel_size, list) and len(kernel_size) == 2):
                raise ValueError("kernel_size must be a list of 2 integers")
            if not (isinstance(pool_size, list) and len(pool_size) == 2):
                raise ValueError("pool_size must be a list of 2 integers")
            return num_filters, kernel_size, pool_size
        except Exception as exc:
            logger.error(
                "Failed to load CNN config from %s, using defaults: %s",
                config_path,
                exc,
            )
            return defaults

    # ------------------------------------------------------------------
    # Keras model
    # ------------------------------------------------------------------

    def _build_keras_model(self) -> "keras.Model":
        """Build and return the Keras CNN model."""
        kH, kW = self.kernel_size
        pH, pW = self.pool_size
        f1, f2 = self.num_filters[0], self.num_filters[1]

        inputs = keras.Input(shape=self.input_shape, name="time_frequency_input")

        # Layer 1: Conv2D(32, 3×3, relu) → MaxPool(2×2)
        x = layers.Conv2D(
            filters=f1,
            kernel_size=(kH, kW),
            activation="relu",
            padding="same",
            name="conv1",
        )(inputs)
        x = layers.MaxPooling2D(pool_size=(pH, pW), name="pool1")(x)

        # Layer 2: Conv2D(64, 3×3, relu) → MaxPool(2×2)
        x = layers.Conv2D(
            filters=f2,
            kernel_size=(kH, kW),
            activation="relu",
            padding="same",
            name="conv2",
        )(x)
        x = layers.MaxPooling2D(pool_size=(pH, pW), name="pool2")(x)

        model = keras.Model(inputs=inputs, outputs=x, name="cnn_extractor")
        return model

    # ------------------------------------------------------------------
    # Numpy weights (fallback)
    # ------------------------------------------------------------------

    def _init_numpy_weights(self) -> dict:
        """Initialise random weights for the numpy-based CNN."""
        rng = np.random.default_rng(seed=42)
        kH, kW = self.kernel_size
        f1, f2 = self.num_filters[0], self.num_filters[1]
        C_in = self.input_shape[2]  # 2 channels

        # He initialisation scale
        scale1 = np.sqrt(2.0 / (kH * kW * C_in))
        scale2 = np.sqrt(2.0 / (kH * kW * f1))

        return {
            "conv1_w": rng.standard_normal((kH, kW, C_in, f1)).astype(np.float32) * scale1,
            "conv1_b": np.zeros(f1, dtype=np.float32),
            "conv2_w": rng.standard_normal((kH, kW, f1, f2)).astype(np.float32) * scale2,
            "conv2_b": np.zeros(f2, dtype=np.float32),
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _validate_input(self, time_frequency_matrix: np.ndarray) -> np.ndarray:
        """Validate and normalise input to shape (B, 1024, 128, 2).

        Args:
            time_frequency_matrix: Array of shape (1024, 128, 2) or
                (B, 1024, 128, 2).

        Returns:
            Array of shape (B, 1024, 128, 2).

        Raises:
            ValueError: If the spatial/channel dimensions are not (1024, 128, 2).
        """
        arr = np.asarray(time_frequency_matrix, dtype=np.float32)

        if arr.ndim == 3:
            # Single sample — add batch dimension
            if arr.shape != self.INPUT_SHAPE:
                raise ValueError(
                    f"Input shape must be {self.INPUT_SHAPE}, got {arr.shape}"
                )
            arr = arr[np.newaxis, ...]  # (1, 1024, 128, 2)
        elif arr.ndim == 4:
            if arr.shape[1:] != self.INPUT_SHAPE:
                raise ValueError(
                    f"Input spatial/channel shape must be {self.INPUT_SHAPE}, "
                    f"got {arr.shape[1:]}"
                )
        else:
            raise ValueError(
                f"Input must be 3-D (1024, 128, 2) or 4-D (B, 1024, 128, 2), "
                f"got {arr.ndim}-D array with shape {arr.shape}"
            )
        return arr

    def extract_features(self, time_frequency_matrix: np.ndarray) -> np.ndarray:
        """Process a time-frequency matrix through the CNN and return feature maps.

        The heart-rate channel (channel 0) captures 0.1–0.4 Hz HRV features;
        the movement channel (channel 1) captures 0.1–5 Hz movement features.
        After two 2×2 max-pooling operations the spatial dimensions are reduced
        from 1024×128 to 256×32, yielding an output of shape (256, 32, 64) for
        a single sample.

        Args:
            time_frequency_matrix: Array of shape (1024, 128, 2) for a single
                sample, or (B, 1024, 128, 2) for a batch.

        Returns:
            Feature maps of shape (256, 32, 64) for a single sample, or
            (B, 256, 32, 64) for a batch.

        Raises:
            ValueError: If the input dimensions are not (1024, 128, 2).
        """
        batch = self._validate_input(time_frequency_matrix)
        single = batch.shape[0] == 1 and np.asarray(time_frequency_matrix).ndim == 3

        if self._use_keras:
            features = self._model(batch, training=False).numpy()
        else:
            features = self._extract_numpy(batch)

        return features[0] if single else features

    # ------------------------------------------------------------------
    # Numpy forward pass
    # ------------------------------------------------------------------

    def _extract_numpy(self, batch: np.ndarray) -> np.ndarray:
        """Run the numpy-based CNN forward pass on a batch.

        Args:
            batch: Array of shape (B, 1024, 128, 2).

        Returns:
            Array of shape (B, 256, 32, 64).
        """
        pH, pW = self.pool_size
        results = []
        for sample in batch:
            # Conv1 + ReLU + MaxPool
            x = _relu(
                _conv2d_numpy(sample, self._weights["conv1_w"], self._weights["conv1_b"])
            )
            x = _maxpool2d_numpy(x, (pH, pW))

            # Conv2 + ReLU + MaxPool
            x = _relu(
                _conv2d_numpy(x, self._weights["conv2_w"], self._weights["conv2_b"])
            )
            x = _maxpool2d_numpy(x, (pH, pW))

            results.append(x)
        return np.stack(results, axis=0)
