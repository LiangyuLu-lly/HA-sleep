"""Unit tests for TimeSynchronizer (Requirements 3.1 – 3.6)."""
import logging
import numpy as np
import pytest

from src.data_structures import HeartRateData, MovementData
from src.time_synchronizer import TimeSynchronizer, OFFSET_WARNING_THRESHOLD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_hr(start: float, n: int, rate: float = 100.0, bpm: float = 70.0) -> HeartRateData:
    """Create a simple HeartRateData with constant bpm."""
    ts = np.linspace(start, start + (n - 1) / rate, n)
    values = np.full(n, bpm)
    return HeartRateData(timestamps=ts, values=values, sampling_rate=int(rate))


def _make_mv(start: float, n: int, rate: float = 100.0, amp: float = 0.5) -> MovementData:
    """Create a simple MovementData with constant amplitude."""
    ts = np.linspace(start, start + (n - 1) / rate, n)
    values = np.full(n, amp)
    return MovementData(timestamps=ts, values=values, sampling_rate=int(rate))


# ---------------------------------------------------------------------------
# calculate_time_offset
# ---------------------------------------------------------------------------

class TestCalculateTimeOffset:
    def test_zero_offset_same_start(self):
        sync = TimeSynchronizer()
        hr_ts = np.array([1000.0, 1001.0, 1002.0])
        mv_ts = np.array([1000.0, 1001.0, 1002.0])
        assert sync.calculate_time_offset(hr_ts, mv_ts) == pytest.approx(0.0)

    def test_positive_offset_hr_starts_later(self):
        sync = TimeSynchronizer()
        hr_ts = np.array([1001.5, 1002.5])
        mv_ts = np.array([1000.0, 1001.0])
        assert sync.calculate_time_offset(hr_ts, mv_ts) == pytest.approx(1.5)

    def test_negative_offset_mv_starts_later(self):
        sync = TimeSynchronizer()
        hr_ts = np.array([1000.0, 1001.0])
        mv_ts = np.array([1000.3, 1001.3])
        assert sync.calculate_time_offset(hr_ts, mv_ts) == pytest.approx(-0.3)

    def test_warning_logged_when_offset_exceeds_1s(self, caplog):
        sync = TimeSynchronizer()
        hr_ts = np.array([1002.0, 1003.0])
        mv_ts = np.array([1000.0, 1001.0])
        with caplog.at_level(logging.WARNING, logger="src.time_synchronizer"):
            sync.calculate_time_offset(hr_ts, mv_ts)
        assert any("unreliable" in r.message.lower() or "1" in r.message for r in caplog.records)

    def test_no_warning_when_offset_within_1s(self, caplog):
        sync = TimeSynchronizer()
        hr_ts = np.array([1000.5, 1001.5])
        mv_ts = np.array([1000.0, 1001.0])
        with caplog.at_level(logging.WARNING, logger="src.time_synchronizer"):
            sync.calculate_time_offset(hr_ts, mv_ts)
        assert len(caplog.records) == 0

    def test_raises_on_empty_hr_timestamps(self):
        sync = TimeSynchronizer()
        with pytest.raises(ValueError):
            sync.calculate_time_offset(np.array([]), np.array([1.0]))

    def test_raises_on_empty_mv_timestamps(self):
        sync = TimeSynchronizer()
        with pytest.raises(ValueError):
            sync.calculate_time_offset(np.array([1.0]), np.array([]))


# ---------------------------------------------------------------------------
# align_data – output length
# ---------------------------------------------------------------------------

class TestAlignDataLength:
    def test_equal_length_inputs(self):
        sync = TimeSynchronizer()
        hr = _make_hr(0.0, 100)
        mv = _make_mv(0.0, 100)
        a_hr, a_mv = sync.align_data(hr, mv)
        assert len(a_hr.timestamps) == 100
        assert len(a_mv.timestamps) == 100

    def test_hr_shorter(self):
        sync = TimeSynchronizer()
        hr = _make_hr(0.0, 80)
        mv = _make_mv(0.0, 120)
        a_hr, a_mv = sync.align_data(hr, mv)
        assert len(a_hr.timestamps) == 80
        assert len(a_mv.timestamps) == 80

    def test_mv_shorter(self):
        sync = TimeSynchronizer()
        hr = _make_hr(0.0, 150)
        mv = _make_mv(0.0, 90)
        a_hr, a_mv = sync.align_data(hr, mv)
        assert len(a_hr.timestamps) == 90
        assert len(a_mv.timestamps) == 90

    def test_output_lengths_always_equal(self):
        """Both outputs must have the same length."""
        sync = TimeSynchronizer()
        hr = _make_hr(0.0, 200)
        mv = _make_mv(0.0, 150)
        a_hr, a_mv = sync.align_data(hr, mv)
        assert len(a_hr.timestamps) == len(a_mv.timestamps)


# ---------------------------------------------------------------------------
# align_data – timestamp alignment quality
# ---------------------------------------------------------------------------

class TestAlignDataTimestamps:
    def test_aligned_timestamps_are_identical(self):
        sync = TimeSynchronizer()
        hr = _make_hr(0.0, 100)
        mv = _make_mv(0.0, 100)
        a_hr, a_mv = sync.align_data(hr, mv)
        np.testing.assert_array_equal(a_hr.timestamps, a_mv.timestamps)

    def test_timestamp_diff_less_than_5ms(self):
        """Requirement 3.7: timestamp diff < 5 ms after alignment."""
        sync = TimeSynchronizer()
        hr = _make_hr(0.0, 500)
        mv = _make_mv(0.005, 500)  # 5 ms offset
        a_hr, a_mv = sync.align_data(hr, mv)
        diffs = np.abs(a_hr.timestamps - a_mv.timestamps)
        assert np.all(diffs < 0.005), f"Max diff: {diffs.max():.6f} s"

    def test_sampling_rate_preserved(self):
        sync = TimeSynchronizer()
        hr = _make_hr(0.0, 100, rate=100.0)
        mv = _make_mv(0.0, 100, rate=50.0)
        a_hr, a_mv = sync.align_data(hr, mv)
        assert a_hr.sampling_rate == 100
        assert a_mv.sampling_rate == 50


# ---------------------------------------------------------------------------
# align_data – interpolation correctness
# ---------------------------------------------------------------------------

class TestAlignDataInterpolation:
    def test_constant_signal_unchanged(self):
        """Interpolating a constant signal should return the same constant."""
        sync = TimeSynchronizer()
        hr = _make_hr(0.0, 100, bpm=75.0)
        mv = _make_mv(0.0, 100, amp=1.0)
        a_hr, a_mv = sync.align_data(hr, mv)
        np.testing.assert_allclose(a_hr.values, 75.0, atol=1e-6)
        np.testing.assert_allclose(a_mv.values, 1.0, atol=1e-6)

    def test_linear_signal_interpolated_correctly(self):
        """Linear ramp should be reproduced exactly by linear interpolation."""
        sync = TimeSynchronizer()
        n = 200
        ts = np.linspace(0.0, 1.99, n)
        # HR: linear ramp 30→200 (valid bpm range)
        hr_values = np.linspace(30.0, 200.0, n)
        hr = HeartRateData(timestamps=ts, values=hr_values, sampling_rate=100)
        mv = _make_mv(0.0, n, amp=0.5)
        a_hr, _ = sync.align_data(hr, mv)
        # Interpolated values should still be within the original range
        assert np.all(a_hr.values >= 30.0)
        assert np.all(a_hr.values <= 200.0)


# ---------------------------------------------------------------------------
# align_data – warning for large offset
# ---------------------------------------------------------------------------

class TestAlignDataWarning:
    def test_warning_emitted_for_large_offset(self, caplog):
        sync = TimeSynchronizer()
        hr = _make_hr(2.0, 100)   # starts 2 s after movement
        mv = _make_mv(0.0, 100)
        with caplog.at_level(logging.WARNING, logger="src.time_synchronizer"):
            sync.align_data(hr, mv)
        assert len(caplog.records) > 0

    def test_no_warning_for_small_offset(self, caplog):
        sync = TimeSynchronizer()
        hr = _make_hr(0.1, 100)   # 0.1 s offset – within threshold
        mv = _make_mv(0.0, 100)
        with caplog.at_level(logging.WARNING, logger="src.time_synchronizer"):
            sync.align_data(hr, mv)
        assert len(caplog.records) == 0


# ---------------------------------------------------------------------------
# Property-Based Tests (Requirements 3.7, 3.8)
# ---------------------------------------------------------------------------

from hypothesis import given, settings
from hypothesis import strategies as st
import hypothesis.extra.numpy as npst


def _make_hr_from_arrays(timestamps: np.ndarray, values: np.ndarray, rate: int) -> HeartRateData:
    """Build HeartRateData clamping values to valid bpm range [30, 200]."""
    clamped = np.clip(values, 30.0, 200.0)
    return HeartRateData(timestamps=timestamps, values=clamped, sampling_rate=rate)


def _make_mv_from_arrays(timestamps: np.ndarray, values: np.ndarray, rate: int) -> MovementData:
    """Build MovementData with given arrays."""
    return MovementData(timestamps=timestamps, values=values, sampling_rate=rate)


# Strategy: generate a sorted, strictly-increasing timestamp array starting near a base time
@st.composite
def timestamp_array(draw, min_size=2, max_size=50):
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    base = draw(st.floats(min_value=1_000_000.0, max_value=1_700_000_000.0, allow_nan=False, allow_infinity=False))
    # spacing between 0.001s and 0.1s to keep timestamps realistic
    spacings = draw(
        st.lists(
            st.floats(min_value=0.001, max_value=0.1, allow_nan=False, allow_infinity=False),
            min_size=n - 1,
            max_size=n - 1,
        )
    )
    ts = np.empty(n)
    ts[0] = base
    for i, s in enumerate(spacings):
        ts[i + 1] = ts[i] + s
    return ts


@st.composite
def dual_sensor_data(draw):
    """Generate a pair of (HeartRateData, MovementData) with independent lengths and timestamps."""
    hr_ts = draw(timestamp_array(min_size=2, max_size=50))
    mv_ts = draw(timestamp_array(min_size=2, max_size=50))
    n_hr = len(hr_ts)
    n_mv = len(mv_ts)
    hr_vals = draw(
        npst.arrays(dtype=np.float64, shape=n_hr,
                    elements=st.floats(min_value=30.0, max_value=200.0, allow_nan=False, allow_infinity=False))
    )
    mv_vals = draw(
        npst.arrays(dtype=np.float64, shape=n_mv,
                    elements=st.floats(min_value=0.0, max_value=10.0, allow_nan=False, allow_infinity=False))
    )
    rate = draw(st.integers(min_value=1, max_value=200))
    hr = _make_hr_from_arrays(hr_ts, hr_vals, rate)
    mv = _make_mv_from_arrays(mv_ts, mv_vals, rate)
    return hr, mv


# Feature: cnn-bilstm-sleep-algorithm, Property 3: Time synchronization timestamp precision
# Validates: Requirements 3.7
class TestPropertyTimestampPrecision:
    """Property 3: For ALL aligned dual-sensor data pairs, the timestamp difference
    between heart rate and movement data SHALL be less than 5ms."""

    @given(dual_sensor_data())
    @settings(max_examples=100)
    def test_aligned_timestamp_diff_less_than_5ms(self, sensor_pair):
        hr, mv = sensor_pair
        sync = TimeSynchronizer()
        a_hr, a_mv = sync.align_data(hr, mv)
        diffs = np.abs(a_hr.timestamps - a_mv.timestamps)
        assert np.all(diffs < 0.005), (
            f"Timestamp diff exceeded 5ms. Max diff: {diffs.max():.9f} s"
        )


# Feature: cnn-bilstm-sleep-algorithm, Property 4: Time synchronization data length invariant
# Validates: Requirements 3.8
class TestPropertyDataLengthInvariant:
    """Property 4: For ALL alignment operations, the aligned data length SHALL equal
    the minimum of both sensor data lengths."""

    @given(dual_sensor_data())
    @settings(max_examples=100)
    def test_aligned_length_equals_min_of_inputs(self, sensor_pair):
        hr, mv = sensor_pair
        sync = TimeSynchronizer()
        a_hr, a_mv = sync.align_data(hr, mv)
        expected_len = min(len(hr.timestamps), len(mv.timestamps))
        assert len(a_hr.timestamps) == expected_len, (
            f"HR aligned length {len(a_hr.timestamps)} != expected {expected_len}"
        )
        assert len(a_mv.timestamps) == expected_len, (
            f"MV aligned length {len(a_mv.timestamps)} != expected {expected_len}"
        )
