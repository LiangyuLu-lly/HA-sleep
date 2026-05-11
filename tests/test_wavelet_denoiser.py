"""Unit tests for WaveletDenoiser.

Tests cover:
- Initialisation (defaults, config override, invalid params)
- denoise() output shape and type
- 50 Hz power-line suppression (Requirement 6.4)
- Soft-thresholding effect on pure noise
- Edge cases (very short signals, single-sample, constant signal)
- Error handling (non-1D input, too-short input)
"""
import numpy as np
import pytest
from hypothesis import given, settings, strategies as st

from src.wavelet_denoiser import WaveletDenoiser, WaveletDenoiserError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FS_DEFAULT = 256.0  # Hz — must be > 100 Hz so 50 Hz is well below Nyquist


def _make_clean_signal(n: int = 1000, fs: float = FS_DEFAULT) -> np.ndarray:
    """Sinusoidal heart-rate-like signal at 1 Hz."""
    t = np.arange(n) / fs
    return 70.0 + 5.0 * np.sin(2 * np.pi * 1.0 * t)


def _add_50hz_noise(signal: np.ndarray, fs: float = FS_DEFAULT, amplitude: float = 5.0) -> np.ndarray:
    """Add a 50 Hz sinusoidal component to the signal."""
    t = np.arange(len(signal)) / fs
    return signal + amplitude * np.sin(2 * np.pi * 50.0 * t)


def _band_energy(signal: np.ndarray, fs: float, f_low: float, f_high: float) -> float:
    """Compute energy in a frequency band using FFT."""
    n = len(signal)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    spectrum = np.abs(np.fft.rfft(signal)) ** 2
    mask = (freqs >= f_low) & (freqs <= f_high)
    return float(np.sum(spectrum[mask]))


# ---------------------------------------------------------------------------
# Initialisation tests
# ---------------------------------------------------------------------------

class TestWaveletDenoiserInit:
    def test_default_params(self):
        wd = WaveletDenoiser()
        assert wd.wavelet == "db5"
        assert wd.level == 5

    def test_custom_params(self):
        """Constructor params are used when no config file is present."""
        wd = WaveletDenoiser(wavelet="db4", level=3, config_path="nonexistent_config.json")
        assert wd.wavelet == "db4"
        assert wd.level == 3

    def test_config_override(self, tmp_path):
        """Config file values should override constructor defaults."""
        import json
        cfg = {
            "data_processing": {
                "wavelet_denoising": {
                    "wavelet_type": "sym8",
                    "decomposition_level": 4,
                }
            }
        }
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps(cfg))
        wd = WaveletDenoiser(config_path=str(cfg_file))
        assert wd.wavelet == "sym8"
        assert wd.level == 4

    def test_invalid_wavelet_raises(self):
        with pytest.raises(WaveletDenoiserError, match="Unknown wavelet"):
            WaveletDenoiser(wavelet="not_a_wavelet")

    def test_invalid_level_raises(self):
        with pytest.raises(WaveletDenoiserError, match="level must be"):
            WaveletDenoiser(level=0)


# ---------------------------------------------------------------------------
# Output shape / type tests
# ---------------------------------------------------------------------------

class TestDenoiseOutputShape:
    def test_output_length_matches_input(self):
        wd = WaveletDenoiser()
        signal = _make_clean_signal(1000)
        result = wd.denoise(signal)
        assert len(result) == len(signal)

    def test_output_is_ndarray(self):
        wd = WaveletDenoiser()
        signal = _make_clean_signal(512)
        result = wd.denoise(signal)
        assert isinstance(result, np.ndarray)

    def test_output_dtype_is_float(self):
        wd = WaveletDenoiser()
        signal = _make_clean_signal(256)
        result = wd.denoise(signal)
        assert np.issubdtype(result.dtype, np.floating)

    def test_non_power_of_two_length(self):
        wd = WaveletDenoiser()
        signal = _make_clean_signal(777)
        result = wd.denoise(signal)
        assert len(result) == 777

    def test_short_signal_level_clamped(self):
        """Very short signals should not raise; level is clamped automatically."""
        wd = WaveletDenoiser(level=5)
        signal = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])
        result = wd.denoise(signal)
        assert len(result) == len(signal)


# ---------------------------------------------------------------------------
# 50 Hz suppression test (Requirement 6.4)
# ---------------------------------------------------------------------------

class TestPowerLineSuppression:
    """
    After denoising, the 50 Hz band energy must be < 10 % of the original.
    Requirement 6.4 / Property 8.
    """

    def test_50hz_energy_suppressed(self):
        fs = FS_DEFAULT
        n = 2048
        signal_clean = _make_clean_signal(n, fs)
        signal_noisy = _add_50hz_noise(signal_clean, fs, amplitude=10.0)

        wd = WaveletDenoiser()
        signal_denoised = wd.denoise(signal_noisy)

        energy_before = _band_energy(signal_noisy, fs, f_low=48.0, f_high=52.0)
        energy_after = _band_energy(signal_denoised, fs, f_low=48.0, f_high=52.0)

        assert energy_before > 0, "Noisy signal should have non-zero 50 Hz energy"
        ratio = energy_after / energy_before
        assert ratio < 0.10, (
            f"50 Hz energy ratio {ratio:.4f} exceeds 10 % threshold. "
            "Denoiser did not suppress power-line interference sufficiently."
        )

    def test_50hz_suppression_large_amplitude(self):
        """Even with a large 50 Hz component the suppression should hold."""
        fs = FS_DEFAULT
        n = 4096
        signal_clean = _make_clean_signal(n, fs)
        signal_noisy = _add_50hz_noise(signal_clean, fs, amplitude=20.0)

        wd = WaveletDenoiser()
        signal_denoised = wd.denoise(signal_noisy)

        energy_before = _band_energy(signal_noisy, fs, f_low=48.0, f_high=52.0)
        energy_after = _band_energy(signal_denoised, fs, f_low=48.0, f_high=52.0)

        ratio = energy_after / energy_before
        assert ratio < 0.10, f"50 Hz energy ratio {ratio:.4f} exceeds 10 %"


# ---------------------------------------------------------------------------
# Denoising quality tests
# ---------------------------------------------------------------------------

class TestDenoiseQuality:
    def test_pure_noise_reduced(self):
        """Denoising white noise should reduce its overall energy."""
        rng = np.random.default_rng(42)
        noise = rng.normal(0, 1, 1024)
        wd = WaveletDenoiser()
        denoised = wd.denoise(noise)
        assert np.var(denoised) < np.var(noise)

    def test_constant_signal_preserved(self):
        """A constant signal should remain (approximately) constant after denoising."""
        signal = np.full(512, 70.0)
        wd = WaveletDenoiser()
        denoised = wd.denoise(signal)
        np.testing.assert_allclose(denoised, 70.0, atol=1e-6)

    def test_low_frequency_signal_preserved(self):
        """Low-frequency content (1 Hz) should be largely preserved."""
        fs = 100.0
        n = 2048
        t = np.arange(n) / fs
        signal = 70.0 + 5.0 * np.sin(2 * np.pi * 1.0 * t)
        wd = WaveletDenoiser()
        denoised = wd.denoise(signal)
        # Mean should be close to 70
        assert abs(np.mean(denoised) - 70.0) < 1.0


# ---------------------------------------------------------------------------
# Error handling tests
# ---------------------------------------------------------------------------

class TestDenoiseErrors:
    def test_2d_input_raises(self):
        wd = WaveletDenoiser()
        with pytest.raises(WaveletDenoiserError, match="1-D"):
            wd.denoise(np.ones((10, 10)))

    def test_single_sample_raises(self):
        wd = WaveletDenoiser()
        with pytest.raises(WaveletDenoiserError, match="at least 2"):
            wd.denoise(np.array([1.0]))

    def test_empty_array_raises(self):
        wd = WaveletDenoiser()
        with pytest.raises(WaveletDenoiserError):
            wd.denoise(np.array([]))

    def test_list_input_accepted(self):
        """Python lists should be accepted and converted internally."""
        wd = WaveletDenoiser()
        result = wd.denoise([70.0] * 100)
        assert len(result) == 100


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------

# **Validates: Requirements 6.1, 6.2, 6.3**
@given(
    n=st.integers(min_value=32, max_value=4096),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=50)
def test_output_length_property(n, seed):
    """
    For any signal of length n, denoise() must return a signal of the same length.

    Validates: Requirements 6.1, 6.2, 6.3
    """
    rng = np.random.default_rng(seed)
    signal = 70.0 + rng.normal(0, 2, n)
    wd = WaveletDenoiser()
    result = wd.denoise(signal)
    assert len(result) == n


# **Validates: Requirements 6.4**
@given(
    n=st.integers(min_value=256, max_value=4096),
    amplitude=st.floats(min_value=1.0, max_value=20.0),
    seed=st.integers(min_value=0, max_value=2**31 - 1),
)
@settings(max_examples=30)
def test_50hz_suppression_property(n, amplitude, seed):
    """
    For any heart-rate signal with 50 Hz interference, denoising must reduce
    the 50 Hz band energy to < 10 % of the original.

    Validates: Requirements 6.4
    """
    fs = FS_DEFAULT
    rng = np.random.default_rng(seed)
    t = np.arange(n) / fs
    # Low-frequency base signal (heart rate ~70 bpm)
    base = 70.0 + 5.0 * np.sin(2 * np.pi * 1.0 * t) + rng.normal(0, 0.5, n)
    # Add 50 Hz power-line interference
    noisy = base + amplitude * np.sin(2 * np.pi * 50.0 * t)

    wd = WaveletDenoiser()
    denoised = wd.denoise(noisy)

    energy_before = _band_energy(noisy, fs, f_low=48.0, f_high=52.0)
    energy_after = _band_energy(denoised, fs, f_low=48.0, f_high=52.0)

    if energy_before > 0:
        ratio = energy_after / energy_before
        assert ratio < 0.10, (
            f"50 Hz energy ratio {ratio:.4f} >= 10 % for n={n}, amplitude={amplitude}"
        )
