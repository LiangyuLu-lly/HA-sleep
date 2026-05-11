"""Model persistence for CNN-BiLSTM sleep stage classification.

Provides save/load functionality for all three model components
(CNNExtractor, BiLSTMAnalyzer, SleepClassifier) using HDF5 format.

Requirements: 17.1, 17.2, 17.3, 17.4, 17.5, 17.6
"""
import logging
import os
from datetime import datetime, timezone
from typing import Tuple

import numpy as np

from src.cnn_extractor import CNNExtractor
from src.bilstm_analyzer import BiLSTMAnalyzer
from src.sleep_classifier import SleepClassifier

logger = logging.getLogger(__name__)

# Current model file format version
_MODEL_VERSION = "1.0"


class ModelPersistence:
    """Save and load CNN-BiLSTM model weights to/from HDF5 format.

    All three model components (CNN, BiLSTM, Classifier) are persisted in a
    single HDF5 file under separate groups.  Metadata (version, timestamp) is
    stored as file-level attributes so that integrity can be verified on load.

    Requirements satisfied:
    - 17.1: CNN weights saved to HDF5
    - 17.2: BiLSTM weights saved to HDF5
    - 17.3: Classifier weights saved to HDF5
    - 17.4: Model loaded from specified path on startup
    - 17.5: FileNotFoundError raised when file is missing
    - 17.6: RuntimeError raised when file is corrupted
    """

    # Expected top-level HDF5 groups
    _CNN_GROUP = "cnn"
    _BILSTM_GROUP = "bilstm"
    _CLASSIFIER_GROUP = "classifier"
    _REQUIRED_GROUPS = (_CNN_GROUP, _BILSTM_GROUP, _CLASSIFIER_GROUP)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def save_model(
        self,
        cnn: CNNExtractor,
        bilstm: BiLSTMAnalyzer,
        classifier: SleepClassifier,
        file_path: str,
    ) -> None:
        """Save all model weights to an HDF5 file.

        The file contains three groups (cnn, bilstm, classifier) plus
        file-level metadata attributes (version, saved_at).

        Args:
            cnn: Trained CNNExtractor instance.
            bilstm: Trained BiLSTMAnalyzer instance.
            classifier: Trained SleepClassifier instance.
            file_path: Destination path for the HDF5 file.

        Raises:
            RuntimeError: If h5py is not installed or the file cannot be written.
        """
        try:
            import h5py
        except ImportError as exc:
            raise RuntimeError(
                "h5py is required for model persistence. "
                "Install it with: pip install h5py"
            ) from exc

        # Ensure parent directory exists
        parent = os.path.dirname(file_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        try:
            with h5py.File(file_path, "w") as f:
                # File-level metadata
                f.attrs["version"] = _MODEL_VERSION
                f.attrs["saved_at"] = datetime.now(timezone.utc).isoformat()

                # CNN weights
                cnn_grp = f.create_group(self._CNN_GROUP)
                self._save_cnn_weights(cnn, cnn_grp)

                # BiLSTM weights
                bilstm_grp = f.create_group(self._BILSTM_GROUP)
                self._save_bilstm_weights(bilstm, bilstm_grp)

                # Classifier weights
                clf_grp = f.create_group(self._CLASSIFIER_GROUP)
                self._save_classifier_weights(classifier, clf_grp)

        except OSError as exc:
            raise RuntimeError(f"Failed to write model file '{file_path}': {exc}") from exc

        logger.info("Model saved to %s (version=%s)", file_path, _MODEL_VERSION)

    def load_model(
        self,
        file_path: str,
    ) -> Tuple[CNNExtractor, BiLSTMAnalyzer, SleepClassifier]:
        """Load model from an HDF5 file and return initialised component instances.

        Args:
            file_path: Path to the HDF5 model file.

        Returns:
            Tuple of (CNNExtractor, BiLSTMAnalyzer, SleepClassifier) with
            weights restored from the file.

        Raises:
            FileNotFoundError: If the file does not exist.
            RuntimeError: If the file is corrupted or missing required groups.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(
                f"Model file not found: '{file_path}'. "
                "Please provide a valid model file path."
            )

        try:
            import h5py
        except ImportError as exc:
            raise RuntimeError(
                "h5py is required for model persistence. "
                "Install it with: pip install h5py"
            ) from exc

        try:
            with h5py.File(file_path, "r") as f:
                # Validate required groups exist
                for group_name in self._REQUIRED_GROUPS:
                    if group_name not in f:
                        raise RuntimeError(
                            f"Model file '{file_path}' is corrupted: "
                            f"missing required group '{group_name}'."
                        )

                cnn = self._load_cnn_weights(f[self._CNN_GROUP])
                bilstm = self._load_bilstm_weights(f[self._BILSTM_GROUP])
                classifier = self._load_classifier_weights(f[self._CLASSIFIER_GROUP])

        except (OSError, KeyError, ValueError) as exc:
            raise RuntimeError(
                f"Model file '{file_path}' is corrupted or incompatible: {exc}"
            ) from exc

        logger.info("Model loaded from %s", file_path)
        return cnn, bilstm, classifier

    def validate_model_file(self, file_path: str) -> bool:
        """Validate that a model file exists and is structurally intact.

        This is a non-raising convenience method intended for startup checks.
        It returns True only when the file exists, can be opened as HDF5, and
        contains all required groups.

        Args:
            file_path: Path to the HDF5 model file.

        Returns:
            True if the file is valid, False otherwise.
        """
        if not os.path.exists(file_path):
            logger.error("Model file does not exist: '%s'", file_path)
            return False

        try:
            import h5py
            with h5py.File(file_path, "r") as f:
                for group_name in self._REQUIRED_GROUPS:
                    if group_name not in f:
                        logger.error(
                            "Model file '%s' is missing required group '%s'",
                            file_path,
                            group_name,
                        )
                        return False
            return True
        except Exception as exc:
            logger.error("Model file '%s' failed validation: %s", file_path, exc)
            return False

    # ------------------------------------------------------------------
    # CNN weight helpers
    # ------------------------------------------------------------------

    def _save_cnn_weights(self, cnn: CNNExtractor, grp) -> None:
        """Persist CNN weights into an open HDF5 group."""
        grp.attrs["hidden_units_placeholder"] = 0  # structural marker

        if cnn._use_keras and cnn._model is not None:
            keras_grp = grp.create_group("keras_weights")
            for layer in cnn._model.layers:
                layer_weights = layer.get_weights()
                if layer_weights:
                    layer_grp = keras_grp.create_group(layer.name)
                    for idx, w in enumerate(layer_weights):
                        layer_grp.create_dataset(str(idx), data=w)
        else:
            numpy_grp = grp.create_group("numpy_weights")
            for key, arr in cnn._weights.items():
                numpy_grp.create_dataset(key, data=arr)

        # Save architecture params for reconstruction
        grp.attrs["num_filters"] = str(cnn.num_filters)
        grp.attrs["kernel_size"] = str(cnn.kernel_size)
        grp.attrs["pool_size"] = str(cnn.pool_size)

    def _load_cnn_weights(self, grp) -> CNNExtractor:
        """Restore a CNNExtractor from an open HDF5 group.

        Three persistence paths are supported:

        * **Keras checkpoint + Keras runtime** — load directly into the
          `keras.Model` via `set_weights`.
        * **Keras checkpoint + numpy runtime** — translate the per-layer
          arrays into the numpy-fallback layout (`conv1_w/b`, `conv2_w/b`).
          This makes TF-free inference possible from a TF-trained file.
        * **numpy checkpoint + numpy runtime** — copy the dict verbatim.
        """
        cnn = CNNExtractor()

        if "keras_weights" in grp:
            keras_grp = grp["keras_weights"]
            if cnn._use_keras and cnn._model is not None:
                for layer in cnn._model.layers:
                    if layer.name in keras_grp:
                        layer_grp = keras_grp[layer.name]
                        weights = [layer_grp[str(i)][()] for i in range(len(layer_grp))]
                        layer.set_weights(weights)
            else:
                # No TensorFlow at runtime — translate keras weights into the
                # numpy-fallback dict.  Keras Conv2D stores weights as
                # ``[kernel(kH, kW, C_in, C_out), bias(C_out,)]`` which is the
                # exact layout used by ``_conv2d_numpy``; no reshape needed.
                cnn._weights = self._keras_cnn_to_numpy(keras_grp)
        elif "numpy_weights" in grp:
            numpy_grp = grp["numpy_weights"]
            cnn._weights = {key: numpy_grp[key][()] for key in numpy_grp}

        return cnn

    @staticmethod
    def _keras_cnn_to_numpy(keras_grp) -> dict:
        """Convert a Keras CNN HDF5 group to the numpy-fallback weight dict.

        Expected source layout::

            keras_weights/
                conv1/  0 → kernel(3,3,2,32),  1 → bias(32,)
                conv2/  0 → kernel(3,3,32,64), 1 → bias(64,)

        Pool layers carry no trainable weights so they do not appear in
        the file.  Missing layers raise a clear error.
        """
        required = {"conv1": ("conv1_w", "conv1_b"),
                    "conv2": ("conv2_w", "conv2_b")}
        out: dict = {}
        for layer_name, (w_key, b_key) in required.items():
            if layer_name not in keras_grp:
                raise RuntimeError(
                    f"CNN keras_weights missing required layer '{layer_name}'"
                )
            layer = keras_grp[layer_name]
            out[w_key] = layer["0"][()].astype(np.float32)
            out[b_key] = layer["1"][()].astype(np.float32)
        return out

    # ------------------------------------------------------------------
    # BiLSTM weight helpers
    # ------------------------------------------------------------------

    def _save_bilstm_weights(self, bilstm: BiLSTMAnalyzer, grp) -> None:
        """Persist BiLSTM weights into an open HDF5 group."""
        grp.attrs["hidden_units"] = bilstm.hidden_units
        grp.attrs["memory_window"] = bilstm.memory_window

        if bilstm._use_keras and bilstm._model is not None:
            keras_grp = grp.create_group("keras_weights")
            for layer in bilstm._model.layers:
                layer_weights = layer.get_weights()
                if layer_weights:
                    layer_grp = keras_grp.create_group(layer.name)
                    for idx, w in enumerate(layer_weights):
                        layer_grp.create_dataset(str(idx), data=w)
        elif bilstm._weights is not None:
            numpy_grp = grp.create_group("numpy_weights")
            for key, arr in bilstm._weights.items():
                numpy_grp.create_dataset(key, data=arr)

    def _load_bilstm_weights(self, grp) -> BiLSTMAnalyzer:
        """Restore a BiLSTMAnalyzer from an open HDF5 group.

        Mirrors the three-path strategy used for the CNN: keras→keras
        (deferred), keras→numpy (translated here) and numpy→numpy (verbatim).
        """
        hidden_units = int(grp.attrs.get("hidden_units", 128))
        memory_window = int(grp.attrs.get("memory_window", 1800))
        bilstm = BiLSTMAnalyzer(hidden_units=hidden_units, memory_window=memory_window)

        if "keras_weights" in grp:
            keras_grp = grp["keras_weights"]
            # Eagerly read into numpy arrays so they remain valid after the
            # HDF5 file is closed.  Structure mirrors save:
            #   {layer_name: {"0": ndarray, "1": ndarray, ...}}
            pending: dict = {}
            for layer_name in keras_grp:
                layer_grp = keras_grp[layer_name]
                pending[layer_name] = {
                    key: layer_grp[key][()] for key in layer_grp
                }
            if bilstm._use_keras:
                # Defer until the Keras model is built lazily on first call.
                bilstm._pending_keras_weights = pending
            else:
                # Numpy runtime — translate to the {fwd_W, fwd_U, fwd_b,
                # bwd_W, bwd_U, bwd_b} layout consumed by ``_lstm_forward``.
                bilstm._weights = self._keras_bilstm_to_numpy(pending)
                bilstm._feature_dim = bilstm._weights["fwd_W"].shape[0]
        elif "numpy_weights" in grp:
            numpy_grp = grp["numpy_weights"]
            bilstm._weights = {key: numpy_grp[key][()] for key in numpy_grp}
            # Infer feature_dim from forward weight shape: (feature_dim, 4*hidden_units)
            if "fwd_W" in bilstm._weights:
                bilstm._feature_dim = bilstm._weights["fwd_W"].shape[0]

        return bilstm

    @staticmethod
    def _keras_bilstm_to_numpy(pending: dict) -> dict:
        """Convert Keras Bidirectional-LSTM weights to the numpy fallback dict.

        Source structure (produced by ``_save_bilstm_weights``)::

            pending = {
                "<bidir_layer_name>": {
                    "0": kernel(input_dim, 4*h)            # forward
                    "1": recurrent_kernel(h, 4*h)          # forward
                    "2": bias(4*h,)                        # forward
                    "3": kernel(input_dim, 4*h)            # backward
                    "4": recurrent_kernel(h, 4*h)          # backward
                    "5": bias(4*h,)                        # backward
                }
            }

        Keras stores LSTM gates in the order ``[i, f, c, o]`` which matches
        ``_lstm_forward`` exactly, so no slicing or reordering is needed.
        Forget-bias offsets (``unit_forget_bias=True``) are already baked
        into the saved bias arrays.
        """
        if not pending:
            raise RuntimeError("BiLSTM keras_weights group is empty")
        # The Bidirectional wrapper is the sole layer with weights.
        layer_name, layer_dict = next(iter(pending.items()))
        if len(layer_dict) != 6:
            raise RuntimeError(
                f"Expected 6 weight tensors for Bidirectional LSTM "
                f"'{layer_name}', got {len(layer_dict)}"
            )
        return {
            "fwd_W": np.asarray(layer_dict["0"], dtype=np.float32),
            "fwd_U": np.asarray(layer_dict["1"], dtype=np.float32),
            "fwd_b": np.asarray(layer_dict["2"], dtype=np.float32),
            "bwd_W": np.asarray(layer_dict["3"], dtype=np.float32),
            "bwd_U": np.asarray(layer_dict["4"], dtype=np.float32),
            "bwd_b": np.asarray(layer_dict["5"], dtype=np.float32),
        }

    # ------------------------------------------------------------------
    # Classifier weight helpers
    # ------------------------------------------------------------------

    def _save_classifier_weights(self, classifier: SleepClassifier, grp) -> None:
        """Persist SleepClassifier weights into an open HDF5 group."""
        grp.attrs["num_classes"] = classifier.num_classes

        if classifier._use_keras and classifier._model is not None:
            keras_grp = grp.create_group("keras_weights")
            for layer in classifier._model.layers:
                layer_weights = layer.get_weights()
                if layer_weights:
                    layer_grp = keras_grp.create_group(layer.name)
                    for idx, w in enumerate(layer_weights):
                        layer_grp.create_dataset(str(idx), data=w)
        elif classifier._W is not None:
            numpy_grp = grp.create_group("numpy_weights")
            numpy_grp.create_dataset("W", data=classifier._W)
            numpy_grp.create_dataset("b", data=classifier._b)

    def _load_classifier_weights(self, grp) -> SleepClassifier:
        """Restore a SleepClassifier from an open HDF5 group.

        Mirrors the three-path strategy used for the CNN/BiLSTM:
        keras→keras (eagerly applied), keras→numpy (translated here)
        and numpy→numpy (verbatim).
        """
        num_classes = int(grp.attrs.get("num_classes", 4))
        classifier = SleepClassifier(num_classes=num_classes)

        if "keras_weights" in grp:
            keras_grp = grp["keras_weights"]
            # Eagerly materialise weights into numpy arrays so they remain
            # valid after the HDF5 file is closed.  Structure:
            #   {layer_name: {"0": ndarray, "1": ndarray, ...}}
            pending: dict = {}
            for layer_name in keras_grp:
                layer_grp = keras_grp[layer_name]
                pending[layer_name] = {
                    key: layer_grp[key][()] for key in layer_grp
                }
            # Infer feature_dim from the dense kernel shape so the model can
            # be built immediately.  Kernel weight shape is (feature_dim, num_classes).
            feature_dim = None
            for _, weights_dict in pending.items():
                if "0" in weights_dict and weights_dict["0"].ndim == 2:
                    feature_dim = int(weights_dict["0"].shape[0])
                    break

            if classifier._use_keras:
                classifier._pending_keras_weights = pending
                if feature_dim is not None:
                    # Trigger eager build so weights are applied.
                    classifier._ensure_initialised(feature_dim)
            else:
                # Numpy runtime — write directly into _W and _b.  Keras Dense
                # stores ``[kernel(feature_dim, num_classes), bias(num_classes,)]``,
                # which is exactly what ``SleepClassifier._softmax`` expects.
                W, b = self._keras_classifier_to_numpy(pending)
                classifier._W = W
                classifier._b = b
                classifier._feature_dim = W.shape[0]
        elif "numpy_weights" in grp:
            numpy_grp = grp["numpy_weights"]
            W = numpy_grp["W"][()]
            b = numpy_grp["b"][()]
            classifier._W = W
            classifier._b = b
            classifier._feature_dim = W.shape[0]

        return classifier

    @staticmethod
    def _keras_classifier_to_numpy(pending: dict) -> Tuple[np.ndarray, np.ndarray]:
        """Convert a Keras Dense+softmax HDF5 group to ``(W, b)`` numpy arrays.

        The classifier head exposes a single Dense layer whose weights are
        ``[kernel(feature_dim, num_classes), bias(num_classes,)]``.  Layer
        name is ``"softmax"`` in the standard build but we accept any single
        2-tensor layer to stay forward-compatible.
        """
        if not pending:
            raise RuntimeError("Classifier keras_weights group is empty")
        # Find the layer that owns a 2-D kernel + 1-D bias pair.
        for layer_name, layer_dict in pending.items():
            if "0" in layer_dict and "1" in layer_dict:
                W = np.asarray(layer_dict["0"], dtype=np.float32)
                b = np.asarray(layer_dict["1"], dtype=np.float32)
                if W.ndim == 2 and b.ndim == 1 and W.shape[1] == b.shape[0]:
                    return W, b
        raise RuntimeError(
            "Classifier keras_weights does not contain a Dense kernel/bias pair"
        )
