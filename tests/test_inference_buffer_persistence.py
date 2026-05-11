"""Tests for the inference-buffer persistence helpers in run_ha_smart_service.

The buffer save/restore flow is the difference between an add-on restart
that picks up where the model left off, and one that has to wait ~10
minutes (1024 samples / ~1.5 Hz) for the rolling window to fill again.
"""
from __future__ import annotations

import importlib
import sys
import time
from pathlib import Path

import numpy as np
import pytest

# Lazy import so the test runs without TensorFlow installed.  The
# TrainingPipeline construction inside _InferenceEngine.__init__ pulls in
# heavy modules; we only need save_buffers/restore_buffers behaviour, so
# we monkey-patch the engine class to expose them on a thin instance.


@pytest.fixture(scope="module")
def engine_module():
    # Adding scripts/ to sys.path lets us load run_ha_smart_service as a
    # plain module despite being executable.  PROJECT_ROOT/scripts is the
    # canonical layout used by the add-on Dockerfile too.
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    try:
        return importlib.import_module("scripts.run_ha_smart_service")
    finally:
        sys.path.pop(0)


@pytest.fixture
def thin_engine(engine_module):
    """A barebones _InferenceEngine instance without loading the model.

    We bypass __init__ because it tries to load best_model.h5 / construct
    a TrainingPipeline.  We only test the buffer-IO methods here, so a
    raw object with the two deque attributes is enough.
    """
    eng = engine_module._InferenceEngine.__new__(engine_module._InferenceEngine)
    from collections import deque
    eng.hr_buf = deque(maxlen=engine_module._InferenceEngine._WINDOW)
    eng.mv_buf = deque(maxlen=engine_module._InferenceEngine._WINDOW)
    return eng


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_save_then_restore_roundtrips_buffers(
    thin_engine, tmp_path: Path,
) -> None:
    """A fresh save followed by a fresh restore reproduces the buffers."""
    rng = np.random.default_rng(42)
    hr = rng.uniform(55, 90, size=300).astype(np.float32)
    mv = rng.uniform(0, 1, size=300).astype(np.float32)
    for v in hr:
        thin_engine.push_hr(v)
    for v in mv:
        thin_engine.push_movement(v)

    path = tmp_path / "buffer.npz"
    thin_engine.save_buffers(path)
    assert path.exists()

    # Build a second blank engine and restore into it.
    other = thin_engine.__class__.__new__(thin_engine.__class__)
    from collections import deque
    other.hr_buf = deque(maxlen=thin_engine.hr_buf.maxlen)
    other.mv_buf = deque(maxlen=thin_engine.mv_buf.maxlen)
    assert other.restore_buffers(path, max_age_s=3600) is True
    assert len(other.hr_buf) == 300
    assert len(other.mv_buf) == 300
    np.testing.assert_array_almost_equal(
        np.asarray(other.hr_buf), hr, decimal=5,
    )


def test_restore_skips_stale_buffer(
    thin_engine, tmp_path: Path,
) -> None:
    path = tmp_path / "buffer.npz"
    # Forge a file with an ancient timestamp.
    np.savez_compressed(
        path,
        hr=np.asarray([72.0], dtype=np.float32),
        mv=np.asarray([0.3], dtype=np.float32),
        saved_at=np.float64(time.time() - 24 * 3600),  # 24 h old
    )
    assert thin_engine.restore_buffers(path, max_age_s=6 * 3600) is False
    assert not thin_engine.hr_buf


def test_restore_returns_false_when_file_missing(
    thin_engine, tmp_path: Path,
) -> None:
    assert thin_engine.restore_buffers(tmp_path / "nope.npz", max_age_s=3600) is False


def test_restore_handles_corrupt_file(
    thin_engine, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "buffer.npz"
    path.write_bytes(b"definitely-not-an-npz")
    with caplog.at_level("WARNING"):
        assert thin_engine.restore_buffers(path, max_age_s=3600) is False
    # Must warn but not raise.
    assert any("Could not restore" in rec.message for rec in caplog.records)


def test_save_is_atomic_with_temp_file(
    thin_engine, tmp_path: Path,
) -> None:
    """The .tmp rename guarantees no half-written buffer ever exists."""
    thin_engine.push_hr(72.0)
    thin_engine.push_movement(0.3)
    path = tmp_path / "buffer.npz"
    thin_engine.save_buffers(path)
    # After a successful save, the .tmp file should have been renamed away.
    assert path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()


def test_save_skips_when_buffers_empty(
    thin_engine, tmp_path: Path,
) -> None:
    """Don't write a useless empty file on a fresh, never-warmed engine."""
    path = tmp_path / "buffer.npz"
    thin_engine.save_buffers(path)
    assert not path.exists()
