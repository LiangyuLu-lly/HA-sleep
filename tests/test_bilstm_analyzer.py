"""Unit tests for BiLSTMAnalyzer (task 7.1).

Tests cover:
- Initialization with default and custom parameters (requirements 9.1)
- Config-file-driven parameters (requirement 15.6)
- Input validation (2-D and 3-D inputs)
- Output shape: (T, 2*hidden_units) for single, (B, T, 2*hidden_units) for batch
- Bidirectional output dimension = 2*hidden_units (requirements 9.5, 9.6)
- Forward and backward context are distinct (requirement 9.5)
- Property 11: BiLSTM output dimension bidirectionality (requirement 9.6)
"""
import json
import os
import tempfile

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.extra.numpy import arrays

from src.bilstm_analyzer import BiLSTMAnalyzer, TENSORFLOW_AVAILABLE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _random_features(shape, seed=0):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(shape).astype(np.float32)


# ---------------------------------------------------------------------------
# 1. Initialization
# ---------------------------------------------------------------------------

class TestInit:
    def test_default_hidden_units(self):
        # Config file sets hidden_units=128; constructor default also 128
        analyzer = BiLSTMAnalyzer()
        assert analyzer.hidden_units == 128

    def test_default_memory_window(self):
        # Config file sets memory_window_seconds=1800; constructor default also 1800
        analyzer = BiLSTMAnalyzer()
        assert analyzer.memory_window == 1800

    def test_custom_hidden_units(self):
        # When no config file is present, constructor value is used
        analyzer = BiLSTMAnalyzer(hidden_units=64, config_path="/nonexistent/config.json")
        assert analyzer.hidden_units == 64

    def test_custom_memory_window(self):
        analyzer = BiLSTMAnalyzer(memory_window=900, config_path="/nonexistent/config.json")
        assert analyzer.memory_window == 900

    def test_invalid_hidden_units_raises(self):
        with pytest.raises(ValueError, match="hidden_units must be positive"):
            BiLSTMAnalyzer(hidden_units=0, config_path="/nonexistent/config.json")

    def test_negative_hidden_units_raises(self):
        with pytest.raises(ValueError, match="hidden_units must be positive"):
            BiLSTMAnalyzer(hidden_units=-1, config_path="/nonexistent/config.json")

    def test_invalid_memory_window_raises(self):
        with pytest.raises(ValueError, match="memory_window must be positive"):
            BiLSTMAnalyzer(memory_window=0, config_path="/nonexistent/config.json")


# ---------------------------------------------------------------------------
# 2. Config file loading (requirement 15.6)
# ---------------------------------------------------------------------------

class TestConfigFile:
    def test_custom_hidden_units_from_config(self):
        cfg = {"model": {"bilstm": {"hidden_units": 64, "memory_window_seconds": 900}}}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as fh:
            json.dump(cfg, fh)
            tmp_path = fh.name
        try:
            analyzer = BiLSTMAnalyzer(config_path=tmp_path)
            assert analyzer.hidden_units == 64
            assert analyzer.memory_window == 900
        finally:
            os.unlink(tmp_path)

    def test_invalid_config_falls_back_to_constructor_defaults(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as fh:
            fh.write("not valid json {{{")
            tmp_path = fh.name
        try:
            analyzer = BiLSTMAnalyzer(hidden_units=32, memory_window=600, config_path=tmp_path)
            assert analyzer.hidden_units == 32
            assert analyzer.memory_window == 600
        finally:
            os.unlink(tmp_path)

    def test_missing_config_uses_constructor_defaults(self):
        analyzer = BiLSTMAnalyzer(
            hidden_units=16, memory_window=300, config_path="/nonexistent/path.json"
        )
        assert analyzer.hidden_units == 16
        assert analyzer.memory_window == 300


# ---------------------------------------------------------------------------
# 3. Input validation
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_valid_2d_input(self):
        analyzer = BiLSTMAnalyzer(config_path="/nonexistent/config.json")
        x = _random_features((10, 64))
        out = analyzer.analyze(x)
        assert out is not None

    def test_valid_3d_input(self):
        analyzer = BiLSTMAnalyzer(config_path="/nonexistent/config.json")
        x = _random_features((4, 10, 64))
        out = analyzer.analyze(x)
        assert out is not None

    def test_1d_input_raises(self):
        analyzer = BiLSTMAnalyzer(config_path="/nonexistent/config.json")
        with pytest.raises(ValueError):
            analyzer.analyze(np.zeros(10))

    def test_4d_input_raises(self):
        analyzer = BiLSTMAnalyzer(config_path="/nonexistent/config.json")
        with pytest.raises(ValueError):
            analyzer.analyze(np.zeros((2, 3, 10, 64)))

    def test_empty_sequence_raises(self):
        analyzer = BiLSTMAnalyzer(config_path="/nonexistent/config.json")
        with pytest.raises(ValueError):
            analyzer.analyze(np.zeros((0, 64)))


# ---------------------------------------------------------------------------
# 4. Output shape (requirements 9.5, 9.6)
# ---------------------------------------------------------------------------

class TestOutputShape:
    def test_single_sample_output_shape(self):
        """Single (T, feature_dim) → (T, 2*hidden_units)."""
        analyzer = BiLSTMAnalyzer(hidden_units=32, config_path="/nonexistent/config.json")
        T, feature_dim = 20, 64
        x = _random_features((T, feature_dim))
        out = analyzer.analyze(x)
        assert out.shape == (T, 2 * 32), f"Expected ({T}, 64), got {out.shape}"

    def test_batch_output_shape(self):
        """Batch (B, T, feature_dim) → (B, T, 2*hidden_units)."""
        analyzer = BiLSTMAnalyzer(hidden_units=32, config_path="/nonexistent/config.json")
        B, T, feature_dim = 3, 20, 64
        x = _random_features((B, T, feature_dim))
        out = analyzer.analyze(x)
        assert out.shape == (B, T, 2 * 32), f"Expected ({B}, {T}, 64), got {out.shape}"

    def test_output_dim_is_2x_hidden_units(self):
        """Output last dimension must be exactly 2*hidden_units."""
        for h in [16, 32, 64, 128]:
            analyzer = BiLSTMAnalyzer(hidden_units=h, config_path="/nonexistent/config.json")
            x = _random_features((10, 32))
            out = analyzer.analyze(x)
            assert out.shape[-1] == 2 * h, (
                f"hidden_units={h}: expected output dim {2*h}, got {out.shape[-1]}"
            )

    def test_output_dtype_float32(self):
        analyzer = BiLSTMAnalyzer(hidden_units=16, config_path="/nonexistent/config.json")
        x = _random_features((10, 32))
        out = analyzer.analyze(x)
        assert np.issubdtype(out.dtype, np.floating)

    def test_output_is_numpy(self):
        analyzer = BiLSTMAnalyzer(hidden_units=16, config_path="/nonexistent/config.json")
        x = _random_features((10, 32))
        out = analyzer.analyze(x)
        assert isinstance(out, np.ndarray)

    def test_batch_size_1_output_shape(self):
        analyzer = BiLSTMAnalyzer(hidden_units=16, config_path="/nonexistent/config.json")
        x = _random_features((1, 10, 32))
        out = analyzer.analyze(x)
        assert out.shape == (1, 10, 2 * 16)

    def test_default_hidden_units_output_dim(self):
        """Default hidden_units=128 → output dim 256."""
        analyzer = BiLSTMAnalyzer()
        x = _random_features((5, 64))
        out = analyzer.analyze(x)
        assert out.shape[-1] == 256


# ---------------------------------------------------------------------------
# 5. Bidirectionality (requirement 9.5)
# ---------------------------------------------------------------------------

class TestBidirectionality:
    def test_forward_backward_halves_differ(self):
        """The first and second halves of the output should differ (fwd vs bwd)."""
        analyzer = BiLSTMAnalyzer(hidden_units=32, config_path="/nonexistent/config.json")
        x = _random_features((15, 32), seed=7)
        out = analyzer.analyze(x)  # (15, 64)
        h = 32
        fwd_half = out[:, :h]
        bwd_half = out[:, h:]
        # Forward and backward should not be identical
        assert not np.allclose(fwd_half, bwd_half), (
            "Forward and backward halves should differ"
        )

    def test_different_inputs_different_outputs(self):
        analyzer = BiLSTMAnalyzer(hidden_units=16, config_path="/nonexistent/config.json")
        x1 = _random_features((10, 32), seed=1)
        x2 = _random_features((10, 32), seed=2)
        out1 = analyzer.analyze(x1)
        out2 = analyzer.analyze(x2)
        assert not np.allclose(out1, out2)

    def test_determinism(self):
        """Same input should produce same output."""
        analyzer = BiLSTMAnalyzer(hidden_units=16, config_path="/nonexistent/config.json")
        x = _random_features((10, 32), seed=42)
        out1 = analyzer.analyze(x)
        out2 = analyzer.analyze(x)
        np.testing.assert_array_almost_equal(out1, out2)


# ---------------------------------------------------------------------------
# 6. Property 11: BiLSTM output dimension bidirectionality (requirement 9.6)
# ---------------------------------------------------------------------------

class TestProperty11:
    # Feature: cnn-bilstm-sleep-algorithm, Property 11: BiLSTM output dimension bidirectionality
    @given(
        T=st.integers(min_value=1, max_value=50),
        feature_dim=st.integers(min_value=1, max_value=32),
        hidden_units=st.integers(min_value=4, max_value=32),
    )
    @settings(max_examples=30, deadline=None)
    def test_property_11_output_dimension_bidirectionality(
        self, T: int, feature_dim: int, hidden_units: int
    ):
        """**Validates: Requirements 9.6**

        Property 11: For ALL time series inputs, the BiLSTM output vector
        dimension SHALL be 2*hidden_units (containing both forward and backward
        context information).
        """
        analyzer = BiLSTMAnalyzer(
            hidden_units=hidden_units, config_path="/nonexistent/config.json"
        )
        rng = np.random.default_rng(seed=T * 1000 + feature_dim)
        x = rng.standard_normal((T, feature_dim)).astype(np.float32)
        out = analyzer.analyze(x)

        assert out.shape == (T, 2 * hidden_units), (
            f"Expected output shape ({T}, {2 * hidden_units}), got {out.shape}"
        )
