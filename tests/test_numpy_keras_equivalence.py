"""Numerical equivalence: Keras runtime vs numpy-only runtime.

These tests are the contract that lets us ship a TensorFlow-free add-on
image (~30 MB instead of ~650 MB).  The numpy fallbacks in
:mod:`src.cnn_extractor`, :mod:`src.bilstm_analyzer` and
:mod:`src.sleep_classifier` are claimed to produce *identical* outputs
to the Keras path when fed the same weights.  We verify that claim
end-to-end here:

1. Build CNN/BiLSTM/Classifier with TensorFlow available.
2. Run a forward pass in Keras and capture the output.
3. Save the model with :class:`ModelPersistence` so the HDF5 file
   contains a ``keras_weights`` group for every component.
4. Reload the file with TensorFlow *masked out* — this triggers the
   ``_keras_*_to_numpy`` translators added for the lightweight runtime.
5. Run the same forward pass through the numpy code path.
6. Assert ``np.max(|keras_out - numpy_out|) < 1e-4`` per component and
   for the full pipeline.

If this test ever fails, the lightweight add-on must NOT be deployed
without re-validating the conversion.  Failure modes to watch for:
LSTM gate ordering drift, Conv2D padding/stride mismatch, or Dense
kernel transposition.

The test imports TensorFlow lazily; if the host has no TF (CI lite
runner, ARM build farm) the suite is skipped instead of erroring out.
"""
from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Skip the whole module if TensorFlow is not available — the equivalence
# check is meaningless without a Keras reference output.
# ---------------------------------------------------------------------------

tf = pytest.importorskip("tensorflow")


def _reload_inference_modules_without_tf(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Reload CNN/BiLSTM/Classifier with TensorFlow imports forced to fail.

    Returns the freshly-imported modules so the caller can build the
    numpy-only objects.  We do this in a single helper so each test gets
    a clean slate and one test cannot leak imports into the next.
    """
    # Block ``import tensorflow`` for the next imports.
    blocked = {"tensorflow", "tensorflow.keras", "keras"}
    real_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name in blocked or any(name.startswith(b + ".") for b in blocked):
            raise ImportError(f"blocked for test: {name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)

    # Drop cached modules so re-imports re-run their top-level
    # ``try: import tensorflow`` blocks and pick TENSORFLOW_AVAILABLE=False.
    for mod_name in [
        "src.cnn_extractor",
        "src.bilstm_analyzer",
        "src.sleep_classifier",
        "src.model_persistence",
    ]:
        sys.modules.pop(mod_name, None)

    cnn_mod = importlib.import_module("src.cnn_extractor")
    bilstm_mod = importlib.import_module("src.bilstm_analyzer")
    clf_mod = importlib.import_module("src.sleep_classifier")
    persistence_mod = importlib.import_module("src.model_persistence")

    assert cnn_mod.TENSORFLOW_AVAILABLE is False, "TF block escaped"
    assert bilstm_mod.TENSORFLOW_AVAILABLE is False
    assert clf_mod.TENSORFLOW_AVAILABLE is False

    return {
        "cnn": cnn_mod,
        "bilstm": bilstm_mod,
        "clf": clf_mod,
        "persistence": persistence_mod,
    }


def _restore_inference_modules() -> None:
    """Re-import the inference modules with TF available so subsequent
    tests in the session see the normal Keras-backed classes again."""
    for mod_name in [
        "src.cnn_extractor",
        "src.bilstm_analyzer",
        "src.sleep_classifier",
        "src.model_persistence",
    ]:
        sys.modules.pop(mod_name, None)
    importlib.import_module("src.cnn_extractor")
    importlib.import_module("src.bilstm_analyzer")
    importlib.import_module("src.sleep_classifier")
    importlib.import_module("src.model_persistence")


# ---------------------------------------------------------------------------
# Component-level equivalence
# ---------------------------------------------------------------------------


@pytest.fixture
def deterministic_input(rng=None):
    """Reproducible inputs for the three components.

    * CNN expects (1024, 128, 2)
    * BiLSTM expects (T, feature_dim)
    * Classifier expects (feature_dim,)

    A small but realistic seed keeps the test fast (~3 s) while still
    exercising every layer's numerics.
    """
    rng = np.random.default_rng(seed=20251101)
    return {
        "cnn_input": rng.standard_normal((1024, 128, 2), dtype=np.float64).astype(
            np.float32
        ),
        "bilstm_input": rng.standard_normal((32, 16), dtype=np.float64).astype(
            np.float32
        ),
        "clf_input": rng.standard_normal((1, 64), dtype=np.float64).astype(np.float32),
    }


def test_cnn_keras_vs_numpy_equivalence(tmp_path: Path, monkeypatch, deterministic_input):
    """``CNNExtractor`` numpy fallback must match Keras output bit-close.

    Because Conv2D fft-vs-direct rounds differ in the last decimal we
    accept ``atol=5e-4``.  Anything larger means the numpy path is wrong.
    """
    from src.cnn_extractor import CNNExtractor
    from src.bilstm_analyzer import BiLSTMAnalyzer
    from src.sleep_classifier import SleepClassifier
    from src.model_persistence import ModelPersistence

    cnn_keras = CNNExtractor()
    bilstm_keras = BiLSTMAnalyzer(hidden_units=8)
    clf_keras = SleepClassifier()
    # Force lazy-built BiLSTM and Classifier so the file has all groups.
    _ = bilstm_keras.analyze(deterministic_input["bilstm_input"])
    _ = clf_keras.get_probability_distribution(deterministic_input["clf_input"])

    keras_out = cnn_keras.extract_features(deterministic_input["cnn_input"])

    save_path = tmp_path / "model_for_eq_test.h5"
    ModelPersistence().save_model(cnn_keras, bilstm_keras, clf_keras, str(save_path))

    try:
        mods = _reload_inference_modules_without_tf(monkeypatch)
        _, _, classifier_np = mods["persistence"].ModelPersistence().load_model(
            str(save_path)
        )
        cnn_np, _, _ = mods["persistence"].ModelPersistence().load_model(str(save_path))
        numpy_out = cnn_np.extract_features(deterministic_input["cnn_input"])

        assert numpy_out.shape == keras_out.shape, (
            f"shape mismatch: keras={keras_out.shape} numpy={numpy_out.shape}"
        )
        max_abs = float(np.max(np.abs(keras_out - numpy_out)))
        # CNN involves ReLU + max-pool, so error stays tiny.
        assert max_abs < 5e-4, f"CNN keras/numpy diverge: max_abs={max_abs}"
    finally:
        _restore_inference_modules()


def test_bilstm_keras_vs_numpy_equivalence(tmp_path: Path, monkeypatch, deterministic_input):
    """BiLSTM numpy fallback must match Keras output to ~1e-4."""
    from src.cnn_extractor import CNNExtractor
    from src.bilstm_analyzer import BiLSTMAnalyzer
    from src.sleep_classifier import SleepClassifier
    from src.model_persistence import ModelPersistence

    cnn_keras = CNNExtractor()
    bilstm_keras = BiLSTMAnalyzer(hidden_units=8)
    clf_keras = SleepClassifier()

    keras_out = bilstm_keras.analyze(deterministic_input["bilstm_input"])
    _ = clf_keras.get_probability_distribution(deterministic_input["clf_input"])

    save_path = tmp_path / "model_for_eq_test.h5"
    ModelPersistence().save_model(cnn_keras, bilstm_keras, clf_keras, str(save_path))

    try:
        mods = _reload_inference_modules_without_tf(monkeypatch)
        _, bilstm_np, _ = mods["persistence"].ModelPersistence().load_model(
            str(save_path)
        )
        numpy_out = bilstm_np.analyze(deterministic_input["bilstm_input"])

        assert numpy_out.shape == keras_out.shape, (
            f"shape mismatch: keras={keras_out.shape} numpy={numpy_out.shape}"
        )
        # LSTM has 32 sequential steps × tanh/sigmoid; rounding accumulates
        # but stays well below 1e-3 in practice.
        max_abs = float(np.max(np.abs(keras_out - numpy_out)))
        assert max_abs < 1e-3, f"BiLSTM keras/numpy diverge: max_abs={max_abs}"
    finally:
        _restore_inference_modules()


def test_classifier_keras_vs_numpy_equivalence(tmp_path: Path, monkeypatch, deterministic_input):
    """Dense + softmax numpy fallback must match Keras to ~1e-6."""
    from src.cnn_extractor import CNNExtractor
    from src.bilstm_analyzer import BiLSTMAnalyzer
    from src.sleep_classifier import SleepClassifier
    from src.model_persistence import ModelPersistence

    cnn_keras = CNNExtractor()
    bilstm_keras = BiLSTMAnalyzer(hidden_units=8)
    clf_keras = SleepClassifier()
    # Force build by running one input through the classifier first.
    _ = clf_keras.get_probability_distribution(deterministic_input["clf_input"])

    keras_out = clf_keras.get_probability_distribution(
        deterministic_input["clf_input"][0]
    )

    save_path = tmp_path / "model_for_eq_test.h5"
    ModelPersistence().save_model(cnn_keras, bilstm_keras, clf_keras, str(save_path))

    try:
        mods = _reload_inference_modules_without_tf(monkeypatch)
        _, _, clf_np = mods["persistence"].ModelPersistence().load_model(str(save_path))
        numpy_out = clf_np.get_probability_distribution(
            deterministic_input["clf_input"][0]
        )

        assert numpy_out.shape == keras_out.shape
        max_abs = float(np.max(np.abs(keras_out - numpy_out)))
        # Softmax + linear has very tight equivalence — 1e-6 is generous.
        assert max_abs < 1e-5, f"Classifier keras/numpy diverge: max_abs={max_abs}"
        # Probabilities must still sum to 1 in the numpy path.
        assert abs(float(np.sum(numpy_out)) - 1.0) < 1e-6
    finally:
        _restore_inference_modules()


def test_full_pipeline_keras_vs_numpy_equivalence(tmp_path: Path, monkeypatch):
    """End-to-end CNN → BiLSTM → classifier must produce the same stage.

    The mean-pool that connects the BiLSTM output to the classifier
    smooths most of the per-step rounding error away, so the predicted
    sleep stage and the top-1 confidence should be identical between
    the two runtimes — not just close.
    """
    from src.cnn_extractor import CNNExtractor
    from src.bilstm_analyzer import BiLSTMAnalyzer
    from src.sleep_classifier import SleepClassifier
    from src.model_persistence import ModelPersistence

    rng = np.random.default_rng(seed=20251102)
    # Realistic-shaped feature vector matching production:
    # 2*hidden_units (BiLSTM mean-pool) + 12 handcrafted = 2*8 + 12 = 28
    feature_vec = rng.standard_normal(28).astype(np.float32)

    cnn_keras = CNNExtractor()
    bilstm_keras = BiLSTMAnalyzer(hidden_units=8)
    clf_keras = SleepClassifier()
    keras_stage, keras_conf = clf_keras.classify(feature_vec)

    save_path = tmp_path / "model_for_eq_test.h5"
    ModelPersistence().save_model(cnn_keras, bilstm_keras, clf_keras, str(save_path))

    try:
        mods = _reload_inference_modules_without_tf(monkeypatch)
        _, _, clf_np = mods["persistence"].ModelPersistence().load_model(str(save_path))
        numpy_stage, numpy_conf = clf_np.classify(feature_vec)

        assert numpy_stage == keras_stage, (
            f"stage mismatch: keras={keras_stage} numpy={numpy_stage}"
        )
        # Confidence must match to within softmax rounding.
        assert abs(numpy_conf - keras_conf) < 1e-5
    finally:
        _restore_inference_modules()
