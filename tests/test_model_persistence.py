"""Unit tests for ModelPersistence class.

Tests cover:
- save_model() persists CNN, BiLSTM, and Classifier weights to HDF5
- load_model() restores models from HDF5 file
- validate_model_file() checks file existence and integrity
- FileNotFoundError raised for missing files
- RuntimeError raised for corrupted files
- Metadata (version, timestamp) saved alongside weights
- Round-trip consistency: loaded model produces same output as saved model

Requirements: 17.1, 17.2, 17.3, 17.4, 17.5, 17.6
"""
import os
import tempfile

import numpy as np
import pytest

from src.cnn_extractor import CNNExtractor
from src.bilstm_analyzer import BiLSTMAnalyzer
from src.sleep_classifier import SleepClassifier
from src.model_persistence import ModelPersistence


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_initialized_models():
    """Return CNN, BiLSTM, Classifier instances with weights initialized via a forward pass."""
    cnn = CNNExtractor()
    bilstm = BiLSTMAnalyzer(hidden_units=16, memory_window=60)
    classifier = SleepClassifier(num_classes=4)

    # Trigger lazy weight initialization with a small dummy input
    dummy_input = np.zeros((4, 4, 2), dtype=np.float32)  # tiny, not real shape
    # CNN needs (1024, 128, 2) — use real shape
    cnn_input = np.zeros((1024, 128, 2), dtype=np.float32)
    cnn.extract_features(cnn_input)  # initializes CNN weights

    # BiLSTM: (T, feature_dim)
    bilstm_input = np.zeros((3, 8), dtype=np.float32)
    bilstm.analyze(bilstm_input)  # initializes BiLSTM weights

    # Classifier: (feature_dim,)
    clf_input = np.zeros(8, dtype=np.float32)
    classifier.classify(clf_input)  # initializes classifier weights

    return cnn, bilstm, classifier


# ---------------------------------------------------------------------------
# save_model tests
# ---------------------------------------------------------------------------

class TestSaveModel:
    def test_save_creates_file(self, tmp_path):
        """save_model() should create the HDF5 file at the given path."""
        cnn, bilstm, classifier = _make_initialized_models()
        mp = ModelPersistence()
        file_path = str(tmp_path / "model.h5")

        mp.save_model(cnn, bilstm, classifier, file_path)

        assert os.path.exists(file_path)

    def test_save_creates_parent_directories(self, tmp_path):
        """save_model() should create parent directories if they don't exist."""
        cnn, bilstm, classifier = _make_initialized_models()
        mp = ModelPersistence()
        file_path = str(tmp_path / "nested" / "dir" / "model.h5")

        mp.save_model(cnn, bilstm, classifier, file_path)

        assert os.path.exists(file_path)

    def test_save_writes_hdf5_groups(self, tmp_path):
        """Saved file should contain cnn, bilstm, and classifier groups."""
        import h5py
        cnn, bilstm, classifier = _make_initialized_models()
        mp = ModelPersistence()
        file_path = str(tmp_path / "model.h5")

        mp.save_model(cnn, bilstm, classifier, file_path)

        with h5py.File(file_path, "r") as f:
            assert "cnn" in f
            assert "bilstm" in f
            assert "classifier" in f

    def test_save_writes_metadata_version(self, tmp_path):
        """Saved file should contain a version attribute."""
        import h5py
        cnn, bilstm, classifier = _make_initialized_models()
        mp = ModelPersistence()
        file_path = str(tmp_path / "model.h5")

        mp.save_model(cnn, bilstm, classifier, file_path)

        with h5py.File(file_path, "r") as f:
            assert "version" in f.attrs
            assert f.attrs["version"] == "1.0"

    def test_save_writes_metadata_timestamp(self, tmp_path):
        """Saved file should contain a saved_at timestamp attribute."""
        import h5py
        cnn, bilstm, classifier = _make_initialized_models()
        mp = ModelPersistence()
        file_path = str(tmp_path / "model.h5")

        mp.save_model(cnn, bilstm, classifier, file_path)

        with h5py.File(file_path, "r") as f:
            assert "saved_at" in f.attrs
            assert len(f.attrs["saved_at"]) > 0  # non-empty ISO timestamp


# ---------------------------------------------------------------------------
# load_model tests
# ---------------------------------------------------------------------------

class TestLoadModel:
    def test_load_returns_three_components(self, tmp_path):
        """load_model() should return (CNNExtractor, BiLSTMAnalyzer, SleepClassifier)."""
        cnn, bilstm, classifier = _make_initialized_models()
        mp = ModelPersistence()
        file_path = str(tmp_path / "model.h5")
        mp.save_model(cnn, bilstm, classifier, file_path)

        loaded_cnn, loaded_bilstm, loaded_clf = mp.load_model(file_path)

        assert isinstance(loaded_cnn, CNNExtractor)
        assert isinstance(loaded_bilstm, BiLSTMAnalyzer)
        assert isinstance(loaded_clf, SleepClassifier)

    def test_load_restores_bilstm_hyperparams(self, tmp_path):
        """Loaded BiLSTM should have the same hidden_units and memory_window."""
        cnn, bilstm, classifier = _make_initialized_models()
        mp = ModelPersistence()
        file_path = str(tmp_path / "model.h5")
        mp.save_model(cnn, bilstm, classifier, file_path)

        _, loaded_bilstm, _ = mp.load_model(file_path)

        assert loaded_bilstm.hidden_units == bilstm.hidden_units
        assert loaded_bilstm.memory_window == bilstm.memory_window

    def test_load_restores_classifier_num_classes(self, tmp_path):
        """Loaded classifier should have the same num_classes."""
        cnn, bilstm, classifier = _make_initialized_models()
        mp = ModelPersistence()
        file_path = str(tmp_path / "model.h5")
        mp.save_model(cnn, bilstm, classifier, file_path)

        _, _, loaded_clf = mp.load_model(file_path)

        assert loaded_clf.num_classes == classifier.num_classes

    def test_load_raises_file_not_found(self, tmp_path):
        """load_model() should raise FileNotFoundError for a missing file."""
        mp = ModelPersistence()
        missing_path = str(tmp_path / "nonexistent.h5")

        with pytest.raises(FileNotFoundError) as exc_info:
            mp.load_model(missing_path)

        assert "nonexistent.h5" in str(exc_info.value)

    def test_load_raises_runtime_error_for_corrupted_file(self, tmp_path):
        """load_model() should raise RuntimeError for a corrupted HDF5 file."""
        mp = ModelPersistence()
        corrupt_path = str(tmp_path / "corrupt.h5")
        # Write garbage bytes
        with open(corrupt_path, "wb") as f:
            f.write(b"this is not a valid hdf5 file content at all!!!")

        with pytest.raises(RuntimeError):
            mp.load_model(corrupt_path)

    def test_load_raises_runtime_error_for_missing_group(self, tmp_path):
        """load_model() should raise RuntimeError when required groups are missing."""
        import h5py
        mp = ModelPersistence()
        incomplete_path = str(tmp_path / "incomplete.h5")

        # Create a valid HDF5 file but missing the 'bilstm' group
        with h5py.File(incomplete_path, "w") as f:
            f.attrs["version"] = "1.0"
            f.create_group("cnn")
            # intentionally omit 'bilstm' and 'classifier'

        with pytest.raises(RuntimeError) as exc_info:
            mp.load_model(incomplete_path)

        assert "corrupted" in str(exc_info.value).lower() or "missing" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# validate_model_file tests
# ---------------------------------------------------------------------------

class TestValidateModelFile:
    def test_validate_returns_true_for_valid_file(self, tmp_path):
        """validate_model_file() should return True for a properly saved model."""
        cnn, bilstm, classifier = _make_initialized_models()
        mp = ModelPersistence()
        file_path = str(tmp_path / "model.h5")
        mp.save_model(cnn, bilstm, classifier, file_path)

        assert mp.validate_model_file(file_path) is True

    def test_validate_returns_false_for_missing_file(self, tmp_path):
        """validate_model_file() should return False when file doesn't exist."""
        mp = ModelPersistence()
        missing_path = str(tmp_path / "missing.h5")

        assert mp.validate_model_file(missing_path) is False

    def test_validate_returns_false_for_corrupted_file(self, tmp_path):
        """validate_model_file() should return False for a corrupted file."""
        mp = ModelPersistence()
        corrupt_path = str(tmp_path / "corrupt.h5")
        with open(corrupt_path, "wb") as f:
            f.write(b"garbage data not hdf5")

        assert mp.validate_model_file(corrupt_path) is False

    def test_validate_returns_false_for_incomplete_file(self, tmp_path):
        """validate_model_file() should return False when required groups are missing."""
        import h5py
        mp = ModelPersistence()
        incomplete_path = str(tmp_path / "incomplete.h5")

        with h5py.File(incomplete_path, "w") as f:
            f.attrs["version"] = "1.0"
            f.create_group("cnn")
            # missing bilstm and classifier

        assert mp.validate_model_file(incomplete_path) is False


# ---------------------------------------------------------------------------
# Round-trip consistency tests (Requirement 17.7)
# ---------------------------------------------------------------------------

class TestRoundTripConsistency:
    def test_numpy_cnn_weights_round_trip(self, tmp_path):
        """CNN numpy weights should be identical after save/load."""
        cnn, bilstm, classifier = _make_initialized_models()
        mp = ModelPersistence()
        file_path = str(tmp_path / "model.h5")

        # Only test numpy path (no TF)
        if cnn._use_keras:
            pytest.skip("Skipping numpy round-trip test when Keras is available")

        original_weights = {k: v.copy() for k, v in cnn._weights.items()}
        mp.save_model(cnn, bilstm, classifier, file_path)
        loaded_cnn, _, _ = mp.load_model(file_path)

        for key in original_weights:
            np.testing.assert_array_equal(
                original_weights[key],
                loaded_cnn._weights[key],
                err_msg=f"CNN weight '{key}' mismatch after round-trip",
            )

    def test_numpy_bilstm_weights_round_trip(self, tmp_path):
        """BiLSTM numpy weights should be identical after save/load."""
        cnn, bilstm, classifier = _make_initialized_models()
        mp = ModelPersistence()
        file_path = str(tmp_path / "model.h5")

        if bilstm._use_keras:
            pytest.skip("Skipping numpy round-trip test when Keras is available")

        original_weights = {k: v.copy() for k, v in bilstm._weights.items()}
        mp.save_model(cnn, bilstm, classifier, file_path)
        _, loaded_bilstm, _ = mp.load_model(file_path)

        for key in original_weights:
            np.testing.assert_array_equal(
                original_weights[key],
                loaded_bilstm._weights[key],
                err_msg=f"BiLSTM weight '{key}' mismatch after round-trip",
            )

    def test_numpy_classifier_weights_round_trip(self, tmp_path):
        """Classifier numpy weights should be identical after save/load."""
        cnn, bilstm, classifier = _make_initialized_models()
        mp = ModelPersistence()
        file_path = str(tmp_path / "model.h5")

        if classifier._use_keras:
            pytest.skip("Skipping numpy round-trip test when Keras is available")

        original_W = classifier._W.copy()
        original_b = classifier._b.copy()
        mp.save_model(cnn, bilstm, classifier, file_path)
        _, _, loaded_clf = mp.load_model(file_path)

        np.testing.assert_array_equal(original_W, loaded_clf._W)
        np.testing.assert_array_equal(original_b, loaded_clf._b)

    def test_classifier_output_consistent_after_round_trip(self, tmp_path):
        """Loaded classifier should produce the same output as the original."""
        cnn, bilstm, classifier = _make_initialized_models()
        mp = ModelPersistence()
        file_path = str(tmp_path / "model.h5")

        test_input = np.random.default_rng(0).standard_normal(8).astype(np.float32)
        original_probs = classifier.get_probability_distribution(test_input)

        mp.save_model(cnn, bilstm, classifier, file_path)
        _, _, loaded_clf = mp.load_model(file_path)

        loaded_probs = loaded_clf.get_probability_distribution(test_input)

        np.testing.assert_allclose(
            original_probs,
            loaded_probs,
            atol=1e-5,
            err_msg="Classifier output changed after save/load round-trip",
        )

    def test_bilstm_output_consistent_after_round_trip(self, tmp_path):
        """Loaded BiLSTM should produce the same output as the original."""
        cnn, bilstm, classifier = _make_initialized_models()
        mp = ModelPersistence()
        file_path = str(tmp_path / "model.h5")

        if bilstm._use_keras:
            pytest.skip("Keras BiLSTM round-trip output test requires deferred weight loading")

        test_input = np.random.default_rng(1).standard_normal((3, 8)).astype(np.float32)
        original_output = bilstm.analyze(test_input)

        mp.save_model(cnn, bilstm, classifier, file_path)
        _, loaded_bilstm, _ = mp.load_model(file_path)

        loaded_output = loaded_bilstm.analyze(test_input)

        np.testing.assert_allclose(
            original_output,
            loaded_output,
            atol=1e-5,
            err_msg="BiLSTM output changed after save/load round-trip",
        )


# ---------------------------------------------------------------------------
# Error message quality tests
# ---------------------------------------------------------------------------

class TestErrorMessages:
    def test_file_not_found_error_contains_path(self, tmp_path):
        """FileNotFoundError message should include the missing file path."""
        mp = ModelPersistence()
        missing = str(tmp_path / "missing_model.h5")

        with pytest.raises(FileNotFoundError) as exc_info:
            mp.load_model(missing)

        assert "missing_model.h5" in str(exc_info.value)

    def test_file_not_found_error_is_descriptive(self, tmp_path):
        """FileNotFoundError message should be descriptive."""
        mp = ModelPersistence()
        missing = str(tmp_path / "model.h5")

        with pytest.raises(FileNotFoundError) as exc_info:
            mp.load_model(missing)

        # Should mention the file was not found
        msg = str(exc_info.value).lower()
        assert "not found" in msg or "does not exist" in msg or "missing" in msg


# ---------------------------------------------------------------------------
# Property-Based Tests (Requirement 17.7)
# ---------------------------------------------------------------------------

# Feature: cnn-bilstm-sleep-algorithm, Property 15: Model persistence round-trip consistency
from hypothesis import given, settings, strategies as st
import hypothesis.extra.numpy as npst


# Pre-initialise CNN and BiLSTM once (expensive forward passes) so the property
# test only re-creates the lightweight SleepClassifier per example.
_SHARED_CNN, _SHARED_BILSTM, _ = _make_initialized_models()


@given(
    feature_vector=npst.arrays(
        dtype=np.float32,
        shape=st.integers(min_value=1, max_value=64).map(lambda n: (n,)),
        elements=st.floats(
            min_value=-10.0,
            max_value=10.0,
            allow_nan=False,
            allow_infinity=False,
            width=32,
        ),
    )
)
@settings(max_examples=20, deadline=None)
def test_property_15_classifier_output_consistent_after_round_trip(feature_vector):
    """
    **Validates: Requirements 17.7**

    Property 15: Model persistence round-trip consistency —
    For ALL trained models and input data, saving and loading the model SHALL
    produce identical outputs (within floating-point tolerance < 1e-6).
    """
    classifier = SleepClassifier(num_classes=4)
    # Trigger lazy weight initialisation with the generated feature vector
    classifier.classify(feature_vector)

    original_probs = classifier.get_probability_distribution(feature_vector)

    mp = ModelPersistence()

    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = os.path.join(tmpdir, "model_prop15.h5")
        mp.save_model(_SHARED_CNN, _SHARED_BILSTM, classifier, file_path)
        _, _, loaded_clf = mp.load_model(file_path)

    loaded_probs = loaded_clf.get_probability_distribution(feature_vector)

    np.testing.assert_allclose(
        original_probs,
        loaded_probs,
        atol=1e-6,
        err_msg=(
            f"Classifier output changed after save/load round-trip "
            f"for feature_vector shape {feature_vector.shape}"
        ),
    )
