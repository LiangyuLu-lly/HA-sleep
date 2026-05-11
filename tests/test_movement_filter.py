"""Unit tests for MovementFilter class.

Tests cover:
- Initialisation (defaults, config override, invalid params)
- Filter disabled → pass-through behaviour (Requirement 7.5)
- Filter enabled → bandpass 0.1–5 Hz (Requirements 7.1, 7.2, 7.3, 7.4)
- High-frequency noise suppression < 20 % of original energy (Requirement 7.6)
- Error handling (bad input shape, too-short signal)
- Property test: Property 9 — high-frequency suppression across random signals
"""
import json
import os

import numpy as np
import pytest
from hypothesis import given, settings, strategies as st

from src.movement_filter import MovementFilter, MovementFilterError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FS = 100.0  # default sampling rate used throughout tests


def _sine(freq_hz: float, n: int = 2000, fs: float = FS, amplitude: float = 1.0) -> np.ndarray:
    """Return a pure sine wave at *freq_hz* Hz."""
    t = np.arange(n) / fs
    return amplitude * np.sin(2 * np.pi * freq_hz * t)


def _band_energy(signal: np.ndarray, fs: float, f_low: float, f_high: float) -> float:
    """Return the fraction of signal energy in [f_low, f_high] Hz."""
    n = len(signal)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    spectrum = np.abs(np.fft.rfft(signal)) ** 2
    mask = (freqs >= f_low) & (freqs <= f_high)
    band = float(np.sum(spectrum[mask]))
    total = float(np.sum(spectrum))
    if total == 0:
        return 0.0
    return band / total


def _high_freq_energy_ratio(original: np.ndarray, filtered: np.ndarray,
                             fs: float = FS, cutoff: float = 10.0) -> float:
    """
    Return (high-freq energy in filtered) / (high-freq energy in original).

    'High-frequency' means > cutoff Hz.
    """
    n = len(original)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    mask = freqs > cutoff

    orig_hf = float(np.sum(np.abs(np.fft.rfft(original)) ** 2 * mask))
    filt_hf = float(np.sum(np.abs(np.fft.rfft(filtered)) ** 2 * mask))

    if orig_hf == 0:
        return 0.0
    return filt_hf / orig_hf


# ---------------------------------------------------------------------------
# Initialisation tests
# ---------------------------------------------------------------------------

class TestMovementFilterInit:
    def test_default_params(self):
        mf = MovementFilter(config_path="nonexistent.json")
        assert mf.enabled is True
        assert mf.cutoff_freq == 10.0
        assert mf.sampling_rate == 100.0

    def test_custom_params(self):
        mf = MovementFilter(enabled=False, cutoff_freq=20.0, sampling_rate=200.0,
                            config_path="nonexistent.json")
        assert mf.enabled is False
        assert mf.cutoff_freq == 20.0
        assert mf.sampling_rate == 200.0

    def test_config_override(self, tmp_path):
        cfg = {
            "data_processing": {
                "movement_filter": {
                    "enabled": False,
                    "cutoff_frequency": 15.0,
                }
            }
        }
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(cfg))
        mf = MovementFilter(config_path=str(cfg_file))
        assert mf.enabled is False
        assert mf.cutoff_freq == 15.0

    def test_invalid_cutoff_raises(self):
        with pytest.raises(MovementFilterError):
            MovementFilter(cutoff_freq=-1.0, config_path="nonexistent.json")

    def test_invalid_sampling_rate_raises(self):
        with pytest.raises(MovementFilterError):
            MovementFilter(sampling_rate=0.0, config_path="nonexistent.json")


# ---------------------------------------------------------------------------
# Disabled filter (pass-through) tests — Requirement 7.5
# ---------------------------------------------------------------------------

class TestMovementFilterDisabled:
    def setup_method(self):
        self.mf = MovementFilter(enabled=False, config_path="nonexistent.json")

    def test_returns_copy_of_input(self):
        signal = np.random.default_rng(0).standard_normal(500)
        result = self.mf.filter(signal)
        np.testing.assert_array_equal(result, signal)

    def test_does_not_modify_original(self):
        signal = np.ones(200)
        original = signal.copy()
        self.mf.filter(signal)
        np.testing.assert_array_equal(signal, original)

    def test_output_length_matches_input(self):
        signal = np.random.default_rng(1).standard_normal(1000)
        assert len(self.mf.filter(signal)) == len(signal)


# ---------------------------------------------------------------------------
# Enabled filter tests — Requirements 7.1, 7.2, 7.3, 7.4
# ---------------------------------------------------------------------------

class TestMovementFilterEnabled:
    def setup_method(self):
        self.mf = MovementFilter(enabled=True, config_path="nonexistent.json")

    def test_output_length_matches_input(self):
        signal = _sine(1.0)
        assert len(self.mf.filter(signal)) == len(signal)

    def test_output_is_ndarray(self):
        signal = _sine(2.0)
        result = self.mf.filter(signal)
        assert isinstance(result, np.ndarray)

    def test_output_dtype_is_float(self):
        signal = _sine(1.0)
        assert self.mf.filter(signal).dtype == float

    def test_passband_signal_preserved(self):
        """A 2 Hz sine (inside 0.1–5 Hz passband) should be largely preserved."""
        signal = _sine(2.0, n=3000)
        filtered = self.mf.filter(signal)
        # Energy in 1–3 Hz band should still dominate after filtering
        energy_ratio = _band_energy(filtered, FS, 1.0, 3.0)
        assert energy_ratio > 0.5, f"Passband energy ratio too low: {energy_ratio:.3f}"

    def test_high_freq_signal_attenuated(self):
        """A 20 Hz sine (above cutoff) should be attenuated relative to original."""
        signal = _sine(20.0, n=3000)
        filtered = self.mf.filter(signal)
        # The filtered output energy should be much less than the input energy
        orig_energy = float(np.sum(signal ** 2))
        filt_energy = float(np.sum(filtered ** 2))
        ratio = filt_energy / orig_energy if orig_energy > 0 else 0.0
        assert ratio < 0.20, f"Filtered energy ratio too high: {ratio:.3f}"

    def test_list_input_accepted(self):
        signal = list(_sine(1.0, n=200))
        result = self.mf.filter(signal)
        assert len(result) == 200


# ---------------------------------------------------------------------------
# High-frequency noise suppression — Requirement 7.6
# ---------------------------------------------------------------------------

class TestHighFreqSuppression:
    def setup_method(self):
        self.mf = MovementFilter(enabled=True, config_path="nonexistent.json")

    def test_hf_energy_below_20_percent(self):
        """
        After filtering, high-frequency (>10 Hz) energy should be < 20 % of
        the original high-frequency energy (Requirement 7.6).
        """
        rng = np.random.default_rng(42)
        # Signal with significant high-frequency content
        n = 4000
        t = np.arange(n) / FS
        low_freq = np.sin(2 * np.pi * 1.0 * t)
        high_freq_noise = 2.0 * rng.standard_normal(n)  # broadband noise
        signal = low_freq + high_freq_noise

        filtered = self.mf.filter(signal)
        ratio = _high_freq_energy_ratio(signal, filtered, FS, cutoff=10.0)
        assert ratio < 0.20, f"HF energy ratio {ratio:.3f} exceeds 20 %"

    def test_pure_high_freq_strongly_suppressed(self):
        """A pure 20 Hz tone should be suppressed to < 20 % energy."""
        signal = _sine(20.0, n=4000, amplitude=3.0)
        filtered = self.mf.filter(signal)
        ratio = _high_freq_energy_ratio(signal, filtered, FS, cutoff=10.0)
        assert ratio < 0.20, f"HF energy ratio {ratio:.3f} exceeds 20 %"


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------

class TestMovementFilterErrors:
    def setup_method(self):
        self.mf = MovementFilter(enabled=True, config_path="nonexistent.json")

    def test_2d_input_raises(self):
        with pytest.raises(MovementFilterError):
            self.mf.filter(np.ones((10, 10)))

    def test_single_sample_raises(self):
        with pytest.raises(MovementFilterError):
            self.mf.filter(np.array([1.0]))

    def test_empty_array_raises(self):
        with pytest.raises(MovementFilterError):
            self.mf.filter(np.array([]))

    def test_too_short_signal_raises(self):
        # Less than 3 * order + 1 = 13 samples
        with pytest.raises(MovementFilterError):
            self.mf.filter(np.ones(5))


# ---------------------------------------------------------------------------
# Property test — Property 9: Movement filter high-frequency suppression
# Validates: Requirements 7.6
# ---------------------------------------------------------------------------

@settings(max_examples=50)
@given(
    n=st.integers(min_value=2000, max_value=5000),
    hf_amplitude=st.floats(min_value=0.5, max_value=5.0),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
def test_movement_filter_hf_suppression_property(n, hf_amplitude, seed):
    """
    Property 9: Movement filter high-frequency suppression.

    For any movement signal containing high-frequency noise (>10 Hz),
    the filtered signal's high-frequency energy should be < 20 % of the
    original high-frequency energy.

    Validates: Requirements 7.6
    """
    rng = np.random.default_rng(seed)
    t = np.arange(n) / FS

    # Construct signal: low-freq component + high-freq noise
    low_component = np.sin(2 * np.pi * 1.5 * t)
    high_noise = hf_amplitude * rng.standard_normal(n)
    signal = low_component + high_noise

    mf = MovementFilter(enabled=True, config_path="nonexistent.json")
    filtered = mf.filter(signal)

    ratio = _high_freq_energy_ratio(signal, filtered, FS, cutoff=10.0)
    assert ratio < 0.20, (
        f"Property 9 violated: HF energy ratio {ratio:.4f} >= 0.20 "
        f"(n={n}, hf_amplitude={hf_amplitude:.3f}, seed={seed})"
    )
