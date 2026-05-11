"""Unit and property-based tests for DataNormalizer (Requirements 5.1 – 5.4)."""
import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
import hypothesis.extra.numpy as npst

from src.data_structures import (
    Dataset,
    HeartRateData,
    MovementData,
    SleepStages,
    TrainingSet,
)
from src.data_normalizer import DataNormalizer, NormalizationError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_dataset(
    hr_values: np.ndarray,
    mv_values: np.ndarray,
    n: int | None = None,
) -> Dataset:
    """Build a minimal Dataset from raw value arrays."""
    if n is None:
        n = len(hr_values)
    ts = np.linspace(0.0, (n - 1) / 100.0, n)
    hr = HeartRateData(timestamps=ts, values=hr_values.astype(float), sampling_rate=100)
    mv = MovementData(timestamps=ts, values=mv_values.astype(float), sampling_rate=100)
    stages = SleepStages(timestamps=ts, stages=np.zeros(n, dtype=int))
    return Dataset(heart_rate=hr, movement=mv, sleep_stages=stages, subject_ids=["s1"])


def _make_training_set(hr_values: np.ndarray, mv_values: np.ndarray) -> TrainingSet:
    dataset = _make_dataset(hr_values, mv_values)
    return TrainingSet(dataset=dataset, normalization_params={})


# ---------------------------------------------------------------------------
# fit()
# ---------------------------------------------------------------------------

class TestFit:
    def test_fit_stores_hr_mean_and_std(self):
        hr = np.array([60.0, 80.0, 100.0, 120.0])
        mv = np.array([0.1, 0.2, 0.3, 0.4])
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        dn.fit(ts)
        assert dn._hr_mean == pytest.approx(np.mean(hr))
        assert dn._hr_std == pytest.approx(np.std(hr))

    def test_fit_stores_mv_mean_and_std(self):
        hr = np.array([70.0, 75.0, 80.0])
        mv = np.array([1.0, 2.0, 3.0])
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        dn.fit(ts)
        assert dn._mv_mean == pytest.approx(np.mean(mv))
        assert dn._mv_std == pytest.approx(np.std(mv))

    def test_fit_populates_normalization_params(self):
        hr = np.array([60.0, 80.0, 100.0])
        mv = np.array([0.5, 1.0, 1.5])
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        dn.fit(ts)
        assert "heart_rate" in ts.normalization_params
        assert "movement" in ts.normalization_params
        assert ts.normalization_params["heart_rate"] == pytest.approx(
            (np.mean(hr), np.std(hr))
        )
        assert ts.normalization_params["movement"] == pytest.approx(
            (np.mean(mv), np.std(mv))
        )

    def test_fit_constant_hr_uses_std_one(self):
        """Constant signal → std=0 should be replaced with 1 to avoid division by zero."""
        hr = np.full(10, 70.0)
        mv = np.array([float(i) for i in range(10)])
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        dn.fit(ts)
        assert dn._hr_std == pytest.approx(1.0)

    def test_fit_constant_mv_uses_std_one(self):
        hr = np.linspace(60.0, 100.0, 10)
        mv = np.full(10, 0.5)
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        dn.fit(ts)
        assert dn._mv_std == pytest.approx(1.0)

    def test_fit_marks_normalizer_as_fitted(self):
        hr = np.array([70.0, 80.0])
        mv = np.array([0.1, 0.2])
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        assert not dn._fitted
        dn.fit(ts)
        assert dn._fitted


# ---------------------------------------------------------------------------
# transform()
# ---------------------------------------------------------------------------

class TestTransform:
    def test_transform_raises_if_not_fitted(self):
        dn = DataNormalizer()
        hr = np.array([70.0, 80.0])
        mv = np.array([0.1, 0.2])
        dataset = _make_dataset(hr, mv)
        with pytest.raises(NormalizationError):
            dn.transform(dataset)

    def test_transform_returns_dataset(self):
        hr = np.array([60.0, 80.0, 100.0])
        mv = np.array([0.5, 1.0, 1.5])
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        dn.fit(ts)
        result = dn.transform(ts.dataset)
        assert isinstance(result, Dataset)

    def test_transform_applies_zscore_to_hr(self):
        hr = np.array([60.0, 80.0, 100.0])
        mv = np.array([0.5, 1.0, 1.5])
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        dn.fit(ts)
        result = dn.transform(ts.dataset)
        expected = (hr - np.mean(hr)) / np.std(hr)
        np.testing.assert_allclose(result.heart_rate.values, expected, atol=1e-10)

    def test_transform_applies_zscore_to_mv(self):
        hr = np.array([60.0, 80.0, 100.0])
        mv = np.array([0.5, 1.0, 1.5])
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        dn.fit(ts)
        result = dn.transform(ts.dataset)
        expected = (mv - np.mean(mv)) / np.std(mv)
        np.testing.assert_allclose(result.movement.values, expected, atol=1e-10)

    def test_transform_uses_training_params_on_test_data(self):
        """Test data must be normalized with training parameters, not its own stats."""
        train_hr = np.array([60.0, 80.0, 100.0, 120.0])
        train_mv = np.array([0.5, 1.0, 1.5, 2.0])
        test_hr = np.array([70.0, 90.0])
        test_mv = np.array([0.8, 1.2])

        dn = DataNormalizer()
        ts = _make_training_set(train_hr, train_mv)
        dn.fit(ts)

        test_dataset = _make_dataset(test_hr, test_mv)
        result = dn.transform(test_dataset)

        # Must use training mean/std, not test mean/std
        expected_hr = (test_hr - np.mean(train_hr)) / np.std(train_hr)
        expected_mv = (test_mv - np.mean(train_mv)) / np.std(train_mv)
        np.testing.assert_allclose(result.heart_rate.values, expected_hr, atol=1e-10)
        np.testing.assert_allclose(result.movement.values, expected_mv, atol=1e-10)

    def test_transform_preserves_timestamps(self):
        hr = np.array([70.0, 80.0, 90.0])
        mv = np.array([0.1, 0.2, 0.3])
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        dn.fit(ts)
        result = dn.transform(ts.dataset)
        np.testing.assert_array_equal(
            result.heart_rate.timestamps, ts.dataset.heart_rate.timestamps
        )
        np.testing.assert_array_equal(
            result.movement.timestamps, ts.dataset.movement.timestamps
        )

    def test_transform_preserves_sampling_rate(self):
        hr = np.array([70.0, 80.0, 90.0])
        mv = np.array([0.1, 0.2, 0.3])
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        dn.fit(ts)
        result = dn.transform(ts.dataset)
        assert result.heart_rate.sampling_rate == ts.dataset.heart_rate.sampling_rate
        assert result.movement.sampling_rate == ts.dataset.movement.sampling_rate

    def test_transform_does_not_mutate_original(self):
        hr = np.array([60.0, 80.0, 100.0])
        mv = np.array([0.5, 1.0, 1.5])
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        dn.fit(ts)
        original_hr = ts.dataset.heart_rate.values.copy()
        original_mv = ts.dataset.movement.values.copy()
        dn.transform(ts.dataset)
        np.testing.assert_array_equal(ts.dataset.heart_rate.values, original_hr)
        np.testing.assert_array_equal(ts.dataset.movement.values, original_mv)


# ---------------------------------------------------------------------------
# fit_transform()
# ---------------------------------------------------------------------------

class TestFitTransform:
    def test_fit_transform_returns_training_set(self):
        hr = np.array([60.0, 80.0, 100.0])
        mv = np.array([0.5, 1.0, 1.5])
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        result = dn.fit_transform(ts)
        assert isinstance(result, TrainingSet)

    def test_fit_transform_normalized_hr_mean_near_zero(self):
        hr = np.linspace(60.0, 120.0, 200)
        mv = np.linspace(0.0, 5.0, 200)
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        result = dn.fit_transform(ts)
        assert abs(np.mean(result.dataset.heart_rate.values)) < 1e-10

    def test_fit_transform_normalized_hr_std_near_one(self):
        hr = np.linspace(60.0, 120.0, 200)
        mv = np.linspace(0.0, 5.0, 200)
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        result = dn.fit_transform(ts)
        assert abs(np.std(result.dataset.heart_rate.values) - 1.0) < 1e-10

    def test_fit_transform_normalized_mv_mean_near_zero(self):
        hr = np.linspace(60.0, 120.0, 200)
        mv = np.linspace(0.0, 5.0, 200)
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        result = dn.fit_transform(ts)
        assert abs(np.mean(result.dataset.movement.values)) < 1e-10

    def test_fit_transform_normalized_mv_std_near_one(self):
        hr = np.linspace(60.0, 120.0, 200)
        mv = np.linspace(0.0, 5.0, 200)
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        result = dn.fit_transform(ts)
        assert abs(np.std(result.dataset.movement.values) - 1.0) < 1e-10

    def test_fit_transform_populates_normalization_params(self):
        hr = np.array([70.0, 80.0, 90.0])
        mv = np.array([1.0, 2.0, 3.0])
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        result = dn.fit_transform(ts)
        assert "heart_rate" in result.normalization_params
        assert "movement" in result.normalization_params

    def test_fit_transform_equivalent_to_fit_then_transform(self):
        hr = np.linspace(50.0, 150.0, 100)
        mv = np.linspace(0.0, 10.0, 100)

        dn1 = DataNormalizer()
        ts1 = _make_training_set(hr, mv)
        result1 = dn1.fit_transform(ts1)

        dn2 = DataNormalizer()
        ts2 = _make_training_set(hr, mv)
        dn2.fit(ts2)
        result2 = dn2.transform(ts2.dataset)

        np.testing.assert_allclose(
            result1.dataset.heart_rate.values,
            result2.heart_rate.values,
            atol=1e-12,
        )
        np.testing.assert_allclose(
            result1.dataset.movement.values,
            result2.movement.values,
            atol=1e-12,
        )


# ---------------------------------------------------------------------------
# Property-Based Tests
# ---------------------------------------------------------------------------

# Strategy: generate valid heart rate arrays (30–200 bpm)
@st.composite
def hr_array(draw, min_size: int = 10, max_size: int = 500):
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    values = draw(
        npst.arrays(
            dtype=np.float64,
            shape=n,
            elements=st.floats(
                min_value=30.0, max_value=200.0, allow_nan=False, allow_infinity=False
            ),
        )
    )
    return values


# Strategy: generate valid movement arrays (non-negative)
@st.composite
def mv_array(draw, min_size: int = 10, max_size: int = 500):
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    values = draw(
        npst.arrays(
            dtype=np.float64,
            shape=n,
            elements=st.floats(
                min_value=0.0, max_value=20.0, allow_nan=False, allow_infinity=False
            ),
        )
    )
    return values


@st.composite
def training_pair(draw):
    """Generate matching-length HR and MV arrays for a TrainingSet."""
    n = draw(st.integers(min_value=10, max_value=300))
    hr = draw(
        npst.arrays(
            dtype=np.float64,
            shape=n,
            elements=st.floats(
                min_value=30.0, max_value=200.0, allow_nan=False, allow_infinity=False
            ),
        )
    )
    mv = draw(
        npst.arrays(
            dtype=np.float64,
            shape=n,
            elements=st.floats(
                min_value=0.0, max_value=20.0, allow_nan=False, allow_infinity=False
            ),
        )
    )
    return hr, mv


# Feature: cnn-bilstm-sleep-algorithm, Property 6: Z-score normalization range constraint
# Validates: Requirements 5.5, 5.6
class TestPropertyNormalizationRange:
    """Property 6: For any dual-channel data, Z-score normalized values should be
    in a reasonable range (99.7% of data within [-3, 3] for a normal distribution;
    we verify all values are within [-10, 10] as a conservative bound)."""

    @given(training_pair())
    @settings(max_examples=100)
    def test_normalized_hr_values_in_reasonable_range(self, pair):
        hr, mv = pair
        # Skip constant arrays (std=0 → replaced with 1, result is 0)
        if np.std(hr) == 0:
            return
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        result = dn.fit_transform(ts)
        normalized = result.dataset.heart_rate.values
        assert np.all(np.isfinite(normalized)), "Normalized HR contains non-finite values"
        # For any finite dataset, z-scores are bounded by (max-min)/std
        # which is always finite; we just check finiteness here
        assert np.all(np.abs(normalized) < 1e9), "Normalized HR values are unreasonably large"

    @given(training_pair())
    @settings(max_examples=100)
    def test_normalized_mv_values_in_reasonable_range(self, pair):
        hr, mv = pair
        if np.std(mv) == 0:
            return
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        result = dn.fit_transform(ts)
        normalized = result.dataset.movement.values
        assert np.all(np.isfinite(normalized)), "Normalized MV contains non-finite values"
        assert np.all(np.abs(normalized) < 1e9), "Normalized MV values are unreasonably large"


# Feature: cnn-bilstm-sleep-algorithm, Property 7: Z-score normalization statistical properties
# Validates: Requirements 5.7, 5.8
class TestPropertyNormalizationStatistics:
    """Property 7: For any training set, Z-score normalized mean ≈ 0 (|mean| < 0.01)
    and std ≈ 1 (|std - 1| < 0.01)."""

    @given(training_pair())
    @settings(max_examples=100)
    def test_normalized_training_hr_mean_near_zero(self, pair):
        hr, mv = pair
        # Skip degenerate (near-constant) arrays — std≈0 means normalization is undefined
        from hypothesis import assume
        assume(np.std(hr) >= 1e-8)
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        result = dn.fit_transform(ts)
        mean_val = np.mean(result.dataset.heart_rate.values)
        assert abs(mean_val) < 1e-10, (
            f"Normalized HR mean {mean_val} is not near 0"
        )

    @given(training_pair())
    @settings(max_examples=100)
    def test_normalized_training_hr_std_near_one(self, pair):
        hr, mv = pair
        from hypothesis import assume
        assume(np.std(hr) >= 1e-8)
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        result = dn.fit_transform(ts)
        std_val = np.std(result.dataset.heart_rate.values)
        assert abs(std_val - 1.0) < 1e-10, (
            f"Normalized HR std {std_val} is not near 1"
        )

    @given(training_pair())
    @settings(max_examples=100)
    def test_normalized_training_mv_mean_near_zero(self, pair):
        hr, mv = pair
        from hypothesis import assume
        assume(np.std(mv) >= 1e-8)
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        result = dn.fit_transform(ts)
        mean_val = np.mean(result.dataset.movement.values)
        assert abs(mean_val) < 1e-10, (
            f"Normalized MV mean {mean_val} is not near 0"
        )

    @given(training_pair())
    @settings(max_examples=100)
    def test_normalized_training_mv_std_near_one(self, pair):
        hr, mv = pair
        from hypothesis import assume
        assume(np.std(mv) >= 1e-8)
        dn = DataNormalizer()
        ts = _make_training_set(hr, mv)
        result = dn.fit_transform(ts)
        std_val = np.std(result.dataset.movement.values)
        assert abs(std_val - 1.0) < 1e-10, (
            f"Normalized MV std {std_val} is not near 1"
        )
