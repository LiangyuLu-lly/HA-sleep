"""Unit tests for PerformanceMetrics class.

Tests cover:
- calculate_accuracy
- calculate_precision_per_class
- calculate_recall_per_class
- calculate_f1_per_class
- generate_confusion_matrix
- save_metrics

Requirements: 19.1, 19.2, 19.3, 19.4, 19.5, 19.6
"""

import json
import os
import tempfile

import numpy as np
import pytest

from src.data_structures import SleepStage
from src.performance_metrics import PerformanceMetrics

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def pm():
    return PerformanceMetrics()


# Perfect predictions: all four classes present
Y_TRUE_PERFECT = np.array([0, 1, 2, 3, 0, 1, 2, 3])
Y_PRED_PERFECT = np.array([0, 1, 2, 3, 0, 1, 2, 3])

# All wrong predictions
Y_TRUE_WRONG = np.array([0, 1, 2, 3])
Y_PRED_WRONG = np.array([1, 2, 3, 0])

# Mixed predictions
Y_TRUE_MIXED = np.array([0, 0, 1, 1, 2, 2, 3, 3])
Y_PRED_MIXED = np.array([0, 1, 1, 2, 2, 3, 3, 0])


# ---------------------------------------------------------------------------
# calculate_accuracy
# ---------------------------------------------------------------------------

class TestCalculateAccuracy:
    def test_perfect_accuracy(self, pm):
        acc = pm.calculate_accuracy(Y_TRUE_PERFECT, Y_PRED_PERFECT)
        assert acc == pytest.approx(1.0)

    def test_zero_accuracy(self, pm):
        acc = pm.calculate_accuracy(Y_TRUE_WRONG, Y_PRED_WRONG)
        assert acc == pytest.approx(0.0)

    def test_partial_accuracy(self, pm):
        # 4 out of 8 correct
        acc = pm.calculate_accuracy(Y_TRUE_MIXED, Y_PRED_MIXED)
        assert acc == pytest.approx(0.5)

    def test_accuracy_in_range(self, pm):
        acc = pm.calculate_accuracy(Y_TRUE_MIXED, Y_PRED_MIXED)
        assert 0.0 <= acc <= 1.0

    def test_single_sample_correct(self, pm):
        assert pm.calculate_accuracy(np.array([2]), np.array([2])) == pytest.approx(1.0)

    def test_single_sample_wrong(self, pm):
        assert pm.calculate_accuracy(np.array([2]), np.array([3])) == pytest.approx(0.0)

    def test_mismatched_lengths_raises(self, pm):
        with pytest.raises(ValueError):
            pm.calculate_accuracy(np.array([0, 1]), np.array([0]))

    def test_invalid_label_raises(self, pm):
        with pytest.raises(ValueError):
            pm.calculate_accuracy(np.array([0, 4]), np.array([0, 1]))


# ---------------------------------------------------------------------------
# calculate_precision_per_class
# ---------------------------------------------------------------------------

class TestCalculatePrecisionPerClass:
    def test_perfect_precision(self, pm):
        prec = pm.calculate_precision_per_class(Y_TRUE_PERFECT, Y_PRED_PERFECT)
        for stage in SleepStage:
            assert prec[stage] == pytest.approx(1.0)

    def test_all_values_in_range(self, pm):
        prec = pm.calculate_precision_per_class(Y_TRUE_MIXED, Y_PRED_MIXED)
        for v in prec.values():
            assert 0.0 <= v <= 1.0

    def test_zero_division_returns_zero(self, pm):
        # Predict only class 0 — classes 1,2,3 have no predicted samples → precision=0
        y_true = np.array([0, 1, 2, 3])
        y_pred = np.array([0, 0, 0, 0])
        prec = pm.calculate_precision_per_class(y_true, y_pred)
        assert prec[SleepStage.LIGHT] == pytest.approx(0.0)
        assert prec[SleepStage.DEEP] == pytest.approx(0.0)
        assert prec[SleepStage.REM] == pytest.approx(0.0)

    def test_known_precision_values(self, pm):
        # AWAKE: TP=1, FP=1 → precision=0.5
        y_true = np.array([0, 0, 1])
        y_pred = np.array([0, 1, 0])
        prec = pm.calculate_precision_per_class(y_true, y_pred)
        assert prec[SleepStage.AWAKE] == pytest.approx(0.5)

    def test_returns_all_four_stages(self, pm):
        prec = pm.calculate_precision_per_class(Y_TRUE_PERFECT, Y_PRED_PERFECT)
        assert set(prec.keys()) == set(SleepStage)


# ---------------------------------------------------------------------------
# calculate_recall_per_class
# ---------------------------------------------------------------------------

class TestCalculateRecallPerClass:
    def test_perfect_recall(self, pm):
        rec = pm.calculate_recall_per_class(Y_TRUE_PERFECT, Y_PRED_PERFECT)
        for stage in SleepStage:
            assert rec[stage] == pytest.approx(1.0)

    def test_all_values_in_range(self, pm):
        rec = pm.calculate_recall_per_class(Y_TRUE_MIXED, Y_PRED_MIXED)
        for v in rec.values():
            assert 0.0 <= v <= 1.0

    def test_zero_division_returns_zero(self, pm):
        # y_true has no class 3 → recall for REM = 0
        y_true = np.array([0, 1, 2, 0])
        y_pred = np.array([0, 1, 2, 3])
        rec = pm.calculate_recall_per_class(y_true, y_pred)
        assert rec[SleepStage.REM] == pytest.approx(0.0)

    def test_known_recall_values(self, pm):
        # AWAKE: TP=1, FN=1 → recall=0.5
        y_true = np.array([0, 0, 1])
        y_pred = np.array([0, 1, 0])
        rec = pm.calculate_recall_per_class(y_true, y_pred)
        assert rec[SleepStage.AWAKE] == pytest.approx(0.5)

    def test_returns_all_four_stages(self, pm):
        rec = pm.calculate_recall_per_class(Y_TRUE_PERFECT, Y_PRED_PERFECT)
        assert set(rec.keys()) == set(SleepStage)


# ---------------------------------------------------------------------------
# calculate_f1_per_class
# ---------------------------------------------------------------------------

class TestCalculateF1PerClass:
    def test_perfect_f1(self, pm):
        f1 = pm.calculate_f1_per_class(Y_TRUE_PERFECT, Y_PRED_PERFECT)
        for stage in SleepStage:
            assert f1[stage] == pytest.approx(1.0)

    def test_all_values_in_range(self, pm):
        f1 = pm.calculate_f1_per_class(Y_TRUE_MIXED, Y_PRED_MIXED)
        for v in f1.values():
            assert 0.0 <= v <= 1.0

    def test_zero_f1_when_both_zero(self, pm):
        # Class 3 never predicted and never in y_true → precision=0, recall=0 → f1=0
        y_true = np.array([0, 1, 2])
        y_pred = np.array([0, 1, 2])
        f1 = pm.calculate_f1_per_class(y_true, y_pred)
        assert f1[SleepStage.REM] == pytest.approx(0.0)

    def test_known_f1_value(self, pm):
        # precision=0.5, recall=0.5 → f1=0.5
        y_true = np.array([0, 0, 1])
        y_pred = np.array([0, 1, 0])
        f1 = pm.calculate_f1_per_class(y_true, y_pred)
        assert f1[SleepStage.AWAKE] == pytest.approx(0.5)

    def test_returns_all_four_stages(self, pm):
        f1 = pm.calculate_f1_per_class(Y_TRUE_PERFECT, Y_PRED_PERFECT)
        assert set(f1.keys()) == set(SleepStage)


# ---------------------------------------------------------------------------
# generate_confusion_matrix
# ---------------------------------------------------------------------------

class TestGenerateConfusionMatrix:
    def test_shape_is_4x4(self, pm):
        cm = pm.generate_confusion_matrix(Y_TRUE_PERFECT, Y_PRED_PERFECT)
        assert cm.shape == (4, 4)

    def test_perfect_predictions_diagonal(self, pm):
        cm = pm.generate_confusion_matrix(Y_TRUE_PERFECT, Y_PRED_PERFECT)
        # Off-diagonal should be zero
        off_diag = cm - np.diag(np.diag(cm))
        assert np.all(off_diag == 0)

    def test_total_count_equals_n_samples(self, pm):
        cm = pm.generate_confusion_matrix(Y_TRUE_MIXED, Y_PRED_MIXED)
        assert cm.sum() == len(Y_TRUE_MIXED)

    def test_known_confusion_matrix(self, pm):
        y_true = np.array([0, 0, 1, 1])
        y_pred = np.array([0, 1, 0, 1])
        cm = pm.generate_confusion_matrix(y_true, y_pred)
        # Row 0 (AWAKE): 1 correct, 1 predicted as LIGHT
        assert cm[0, 0] == 1
        assert cm[0, 1] == 1
        # Row 1 (LIGHT): 1 predicted as AWAKE, 1 correct
        assert cm[1, 0] == 1
        assert cm[1, 1] == 1

    def test_rows_are_true_labels(self, pm):
        # All true=2, predicted as 3 → row 2, col 3 should be non-zero
        y_true = np.array([2, 2])
        y_pred = np.array([3, 3])
        cm = pm.generate_confusion_matrix(y_true, y_pred)
        assert cm[2, 3] == 2
        assert cm[3, 2] == 0

    def test_non_negative_entries(self, pm):
        cm = pm.generate_confusion_matrix(Y_TRUE_MIXED, Y_PRED_MIXED)
        assert np.all(cm >= 0)


# ---------------------------------------------------------------------------
# save_metrics
# ---------------------------------------------------------------------------

class TestSaveMetrics:
    def test_saves_json_file(self, pm, tmp_path):
        metrics = {"accuracy": 0.9}
        path = str(tmp_path / "metrics.json")
        pm.save_metrics(metrics, path)
        assert os.path.exists(path)

    def test_json_is_valid(self, pm, tmp_path):
        metrics = {"accuracy": 0.85, "note": "test"}
        path = str(tmp_path / "metrics.json")
        pm.save_metrics(metrics, path)
        with open(path) as f:
            loaded = json.load(f)
        assert loaded["accuracy"] == pytest.approx(0.85)

    def test_sleep_stage_keys_serialised_as_names(self, pm, tmp_path):
        metrics = {
            "precision": {SleepStage.AWAKE: 0.9, SleepStage.REM: 0.8}
        }
        path = str(tmp_path / "metrics.json")
        pm.save_metrics(metrics, path)
        with open(path) as f:
            loaded = json.load(f)
        assert "AWAKE" in loaded["precision"]
        assert "REM" in loaded["precision"]

    def test_numpy_array_serialised(self, pm, tmp_path):
        cm = np.eye(4, dtype=np.int64)
        metrics = {"confusion_matrix": cm}
        path = str(tmp_path / "metrics.json")
        pm.save_metrics(metrics, path)
        with open(path) as f:
            loaded = json.load(f)
        assert loaded["confusion_matrix"] == cm.tolist()

    def test_numpy_scalar_serialised(self, pm, tmp_path):
        metrics = {"accuracy": np.float64(0.75)}
        path = str(tmp_path / "metrics.json")
        pm.save_metrics(metrics, path)
        with open(path) as f:
            loaded = json.load(f)
        assert loaded["accuracy"] == pytest.approx(0.75)

    def test_full_metrics_roundtrip(self, pm, tmp_path):
        """Save a complete metrics dict and verify it round-trips correctly."""
        y_true = Y_TRUE_MIXED
        y_pred = Y_PRED_MIXED
        metrics = {
            "accuracy": pm.calculate_accuracy(y_true, y_pred),
            "precision": pm.calculate_precision_per_class(y_true, y_pred),
            "recall": pm.calculate_recall_per_class(y_true, y_pred),
            "f1": pm.calculate_f1_per_class(y_true, y_pred),
            "confusion_matrix": pm.generate_confusion_matrix(y_true, y_pred),
        }
        path = str(tmp_path / "full_metrics.json")
        pm.save_metrics(metrics, path)
        with open(path) as f:
            loaded = json.load(f)
        assert "accuracy" in loaded
        assert "precision" in loaded
        assert "recall" in loaded
        assert "f1" in loaded
        assert "confusion_matrix" in loaded


# ---------------------------------------------------------------------------
# Property-Based Tests
# ---------------------------------------------------------------------------

# Feature: cnn-bilstm-sleep-algorithm, Property 17: Evaluation metrics range constraint
from hypothesis import given, settings
import hypothesis.strategies as st


# Strategy: non-empty lists of labels in {0, 1, 2, 3}
_label_arrays = st.lists(
    st.integers(min_value=0, max_value=3),
    min_size=1,
    max_size=200,
).map(np.array)


@given(y_true=_label_arrays, y_pred=_label_arrays)
@settings(max_examples=100)
def test_property17_evaluation_metrics_range_constraint(y_true, y_pred):
    """Property 17: Evaluation metrics range constraint.

    For ALL prediction results and true labels, all evaluation metrics
    (accuracy, precision, recall, F1) SHALL be in [0, 1].

    Validates: Requirements 19.7
    """
    # Ensure same length by truncating to the shorter one
    n = min(len(y_true), len(y_pred))
    y_true = y_true[:n]
    y_pred = y_pred[:n]

    if n == 0:
        return  # skip empty arrays

    pm = PerformanceMetrics()

    accuracy = pm.calculate_accuracy(y_true, y_pred)
    assert 0.0 <= accuracy <= 1.0, f"accuracy {accuracy} out of [0,1]"

    precision = pm.calculate_precision_per_class(y_true, y_pred)
    for stage, val in precision.items():
        assert 0.0 <= val <= 1.0, f"precision[{stage}]={val} out of [0,1]"

    recall = pm.calculate_recall_per_class(y_true, y_pred)
    for stage, val in recall.items():
        assert 0.0 <= val <= 1.0, f"recall[{stage}]={val} out of [0,1]"

    f1 = pm.calculate_f1_per_class(y_true, y_pred)
    for stage, val in f1.items():
        assert 0.0 <= val <= 1.0, f"f1[{stage}]={val} out of [0,1]"
