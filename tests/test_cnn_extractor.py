"""Unit tests for CNNExtractor (task 6.1).

Tests cover:
- Input dimension validation (requirements 8.1, 8.8)
- Architecture output shape (requirements 8.2–8.5)
- Batch processing support
- Config-file-driven architecture parameters (requirement 15.5)
- HRV / movement channel emphasis (requirements 8.6, 8.7)
- CNN feature extraction dimensionality consistency (property 10, requirement 8.9)
"""
import json
import os
import tempfile

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.extra.numpy import arrays

from src.cnn_extractor import CNNExtractor, TENSORFLOW_AVAILABLE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def extractor():
    return CNNExtractor()


def _random_input(shape=(1024, 128, 2), seed=0):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(shape).astype(np.float32)


# ---------------------------------------------------------------------------
# 1. Initialisation
# ---------------------------------------------------------------------------

class TestInit:
    def test_default_input_shape(self, extractor):
        assert extractor.input_shape == (1024, 128, 2)

    def test_default_num_filters(self, extractor):
        assert extractor.num_filters[0] == 32
        assert extractor.num_filters[1] == 64

    def test_default_kernel_size(self, extractor):
        assert extractor.kernel_size == [3, 3]

    def test_default_pool_size(self, extractor):
        assert extractor.pool_size == [2, 2]

    def test_wrong_input_shape_raises(self):
        with pytest.raises(ValueError, match="input_shape must be"):
            CNNExtractor(input_shape=(512, 64, 2))

    def test_wrong_channels_raises(self):
        with pytest.raises(ValueError, match="input_shape must be"):
            CNNExtractor(input_shape=(1024, 128, 1))


# ---------------------------------------------------------------------------
# 2. Input validation
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_valid_single_sample(self, extractor):
        x = _random_input((1024, 128, 2))
        out = extractor.extract_features(x)
        assert out is not None

    def test_valid_batch(self, extractor):
        x = _random_input((4, 1024, 128, 2))
        out = extractor.extract_features(x)
        assert out is not None

    def test_wrong_height_raises(self, extractor):
        x = _random_input((512, 128, 2))
        with pytest.raises(ValueError):
            extractor.extract_features(x)

    def test_wrong_width_raises(self, extractor):
        x = _random_input((1024, 64, 2))
        with pytest.raises(ValueError):
            extractor.extract_features(x)

    def test_wrong_channels_raises(self, extractor):
        x = _random_input((1024, 128, 3))
        with pytest.raises(ValueError):
            extractor.extract_features(x)

    def test_1d_input_raises(self, extractor):
        with pytest.raises(ValueError):
            extractor.extract_features(np.zeros(1024))

    def test_5d_input_raises(self, extractor):
        with pytest.raises(ValueError):
            extractor.extract_features(np.zeros((1, 1, 1024, 128, 2)))

    def test_wrong_batch_spatial_raises(self, extractor):
        x = _random_input((2, 512, 128, 2))
        with pytest.raises(ValueError):
            extractor.extract_features(x)


# ---------------------------------------------------------------------------
# 3. Output shape (requirements 8.2–8.5, 8.9)
# ---------------------------------------------------------------------------

class TestOutputShape:
    def test_single_sample_output_shape(self, extractor):
        """Single sample → (256, 32, 64) after two 2×2 max-pools."""
        x = _random_input((1024, 128, 2))
        out = extractor.extract_features(x)
        assert out.shape == (256, 32, 64), (
            f"Expected (256, 32, 64), got {out.shape}"
        )

    def test_batch_output_shape(self, extractor):
        """Batch of B → (B, 256, 32, 64)."""
        B = 3
        x = _random_input((B, 1024, 128, 2))
        out = extractor.extract_features(x)
        assert out.shape == (B, 256, 32, 64), (
            f"Expected ({B}, 256, 32, 64), got {out.shape}"
        )

    def test_batch_size_1_output_shape(self, extractor):
        x = _random_input((1, 1024, 128, 2))
        out = extractor.extract_features(x)
        assert out.shape == (1, 256, 32, 64)

    def test_output_dtype_float32(self, extractor):
        x = _random_input((1024, 128, 2))
        out = extractor.extract_features(x)
        assert out.dtype == np.float32 or np.issubdtype(out.dtype, np.floating)

    def test_output_is_numpy(self, extractor):
        x = _random_input((1024, 128, 2))
        out = extractor.extract_features(x)
        assert isinstance(out, np.ndarray)


# ---------------------------------------------------------------------------
# 4. Determinism
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_same_input_same_output(self, extractor):
        x = _random_input((1024, 128, 2), seed=7)
        out1 = extractor.extract_features(x)
        out2 = extractor.extract_features(x)
        np.testing.assert_array_equal(out1, out2)

    def test_different_inputs_different_outputs(self, extractor):
        x1 = _random_input((1024, 128, 2), seed=1)
        x2 = _random_input((1024, 128, 2), seed=2)
        out1 = extractor.extract_features(x1)
        out2 = extractor.extract_features(x2)
        assert not np.allclose(out1, out2)


# ---------------------------------------------------------------------------
# 5. Config-file-driven architecture (requirement 15.5)
# ---------------------------------------------------------------------------

class TestConfigFile:
    def test_custom_filters_from_config(self):
        cfg = {
            "model": {
                "cnn": {
                    "num_filters": [16, 32],
                    "kernel_size": [3, 3],
                    "pool_size": [2, 2],
                }
            }
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as fh:
            json.dump(cfg, fh)
            tmp_path = fh.name

        try:
            ext = CNNExtractor(config_path=tmp_path)
            assert ext.num_filters[0] == 16
            assert ext.num_filters[1] == 32
        finally:
            os.unlink(tmp_path)

    def test_invalid_config_falls_back_to_defaults(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as fh:
            fh.write("not valid json {{{")
            tmp_path = fh.name

        try:
            ext = CNNExtractor(config_path=tmp_path)
            # Should fall back to defaults
            assert ext.num_filters == [32, 64]
        finally:
            os.unlink(tmp_path)

    def test_missing_config_uses_defaults(self):
        ext = CNNExtractor(config_path="/nonexistent/path/config.json")
        assert ext.num_filters == [32, 64]


# ---------------------------------------------------------------------------
# 6. Channel emphasis (requirements 8.6, 8.7)
# ---------------------------------------------------------------------------

class TestChannelEmphasis:
    """Verify that the two input channels produce distinct feature responses.

    We cannot test learned frequency-band emphasis without training, but we
    can verify that the extractor treats each channel independently and that
    zeroing one channel changes the output.
    """

    def test_zeroing_hr_channel_changes_output(self, extractor):
        x = _random_input((1024, 128, 2), seed=42)
        out_full = extractor.extract_features(x)

        x_no_hr = x.copy()
        x_no_hr[:, :, 0] = 0.0  # zero out heart-rate channel
        out_no_hr = extractor.extract_features(x_no_hr)

        assert not np.allclose(out_full, out_no_hr), (
            "Zeroing the HR channel should change the output"
        )

    def test_zeroing_movement_channel_changes_output(self, extractor):
        x = _random_input((1024, 128, 2), seed=42)
        out_full = extractor.extract_features(x)

        x_no_mv = x.copy()
        x_no_mv[:, :, 1] = 0.0  # zero out movement channel
        out_no_mv = extractor.extract_features(x_no_mv)

        assert not np.allclose(out_full, out_no_mv), (
            "Zeroing the movement channel should change the output"
        )

    def test_both_channels_zero_gives_zero_output(self, extractor):
        """All-zero input → all-zero output (ReLU + zero bias)."""
        x = np.zeros((1024, 128, 2), dtype=np.float32)
        out = extractor.extract_features(x)
        np.testing.assert_array_equal(out, np.zeros_like(out))


# ---------------------------------------------------------------------------
# 7. Dimensionality reduction ratio (property 10 / requirement 8.9)
# ---------------------------------------------------------------------------

class TestDimensionalityReduction:
    def test_height_reduced_by_factor_4(self, extractor):
        """1024 → 256 after two 2×2 max-pools (factor 4)."""
        x = _random_input((1024, 128, 2))
        out = extractor.extract_features(x)
        assert out.shape[0] == 1024 // 4

    def test_width_reduced_by_factor_4(self, extractor):
        """128 → 32 after two 2×2 max-pools (factor 4)."""
        x = _random_input((1024, 128, 2))
        out = extractor.extract_features(x)
        assert out.shape[1] == 128 // 4

    def test_output_channels_equal_second_filter_count(self, extractor):
        """Output channels should equal num_filters[1] = 64."""
        x = _random_input((1024, 128, 2))
        out = extractor.extract_features(x)
        assert out.shape[2] == extractor.num_filters[1]

    # Feature: cnn-bilstm-sleep-algorithm, Property 10: CNN feature extraction dimensionality consistency
    @given(
        data=arrays(
            dtype=np.float32,
            shape=(1024, 128, 2),
            elements=st.floats(
                min_value=-10.0,
                max_value=10.0,
                allow_nan=False,
                allow_infinity=False,
                width=32,
            ),
        )
    )
    @settings(max_examples=5, deadline=None)
    def test_property_10_dimensionality_consistency(self, data):
        """**Validates: Requirements 8.9**

        Property 10: For ALL input matrices of shape (1024, 128, 2), the output
        feature maps SHALL have shape (256, 32, 64) after two 2×2 max-pooling
        operations.
        """
        extractor = CNNExtractor()
        out = extractor.extract_features(data)
        assert out.shape == (256, 32, 64), (
            f"Expected output shape (256, 32, 64), got {out.shape}"
        )
