"""Tests for ``scripts/check_branding.py`` — Property 10: CI 资产守护是真的会拦.

The script enforces design §3.1 / Requirement 1.6: the Add-on branding
assets must match exactly:

* ``sleep_classifier/icon.png`` -> 128 x 128
* ``sleep_classifier/logo.png`` -> 250 x 100

This test suite parametrises a wide grid of ``(icon_size, logo_size)``
pairs — both the compliant baseline and many drift mutations — and
asserts the script returns ``0`` only when every dimension is exact.
The test deliberately uses the public CLI surface (``main([repo_root])``)
so we exercise the full failure path the CI job will hit.

Validates: Requirements 1.6
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

import pytest

# Make ``scripts/`` importable so we can call the public API directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import check_branding  # noqa: E402  (sys.path manipulation above)


# ---------------------------------------------------------------------------
# Minimal PNG builder (stdlib only — Pillow is not a runtime dependency).
# ---------------------------------------------------------------------------

# IHDR data layout (13 bytes):
#   width  : uint32 BE
#   height : uint32 BE
#   bit-depth, color-type, compression, filter, interlace : 1 byte each
# We pick (8, 2, 0, 0, 0) = 8-bit truecolor RGB, deflate compression,
# adaptive filtering, no interlace. The script never reads these fields,
# but writing a real IHDR keeps the fixture honest and readable by other
# tools should we want to debug it.
_IHDR_TAIL: bytes = b"\x08\x02\x00\x00\x00"


def _make_png_bytes(width: int, height: int) -> bytes:
    """Return a minimal but well-formed PNG file with the given size."""
    ihdr_data = struct.pack(">II", width, height) + _IHDR_TAIL
    ihdr_crc = zlib.crc32(b"IHDR" + ihdr_data)
    ihdr_chunk = (
        struct.pack(">I", 13)
        + b"IHDR"
        + ihdr_data
        + struct.pack(">I", ihdr_crc)
    )
    # IEND closes the file. Length 0, type ``IEND``, empty data, CRC fixed.
    iend_crc = zlib.crc32(b"IEND")
    iend_chunk = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", iend_crc)
    return check_branding.PNG_SIGNATURE + ihdr_chunk + iend_chunk


def _materialise_branding(
    repo_root: Path,
    icon_size: tuple[int, int],
    logo_size: tuple[int, int],
) -> None:
    """Write fake icon.png + logo.png with the requested dimensions."""
    target_dir = repo_root / "sleep_classifier"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "icon.png").write_bytes(_make_png_bytes(*icon_size))
    (target_dir / "logo.png").write_bytes(_make_png_bytes(*logo_size))


# ---------------------------------------------------------------------------
# Compliant + drift parametrisations
# ---------------------------------------------------------------------------

# The single compliant pair — used as baseline + as one parametrise row.
_COMPLIANT: tuple[tuple[int, int], tuple[int, int]] = ((128, 128), (250, 100))

_DRIFT_CASES: list[tuple[tuple[int, int], tuple[int, int]]] = [
    # icon drift (logo correct)
    ((127, 128), (250, 100)),  # off by one width
    ((128, 127), (250, 100)),  # off by one height
    ((129, 128), (250, 100)),  # off by one width (other direction)
    ((128, 129), (250, 100)),  # off by one height (other direction)
    ((64, 64), (250, 100)),    # half-size icon
    ((256, 256), (250, 100)),  # double-size icon
    ((250, 100), (250, 100)),  # icon shaped like logo (same w!=h)
    ((128, 250), (250, 100)),  # icon swapped with logo height
    # logo drift (icon correct)
    ((128, 128), (249, 100)),  # off by one width
    ((128, 128), (250, 99)),   # off by one height
    ((128, 128), (251, 100)),  # off by one width (other direction)
    ((128, 128), (250, 101)),  # off by one height (other direction)
    ((128, 128), (100, 250)),  # logo dimensions swapped
    ((128, 128), (128, 128)),  # logo shaped like icon
    ((128, 128), (500, 200)),  # logo scaled up 2x
    # both drift simultaneously
    ((64, 64), (500, 200)),
    ((256, 256), (125, 50)),
    ((127, 127), (249, 99)),
    ((1, 1), (1, 1)),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_main_returns_zero_for_compliant_assets(tmp_path: Path) -> None:
    """Baseline: 128x128 icon + 250x100 logo must pass with exit code 0."""
    icon_size, logo_size = _COMPLIANT
    _materialise_branding(tmp_path, icon_size, logo_size)

    rc = check_branding.main([str(tmp_path)])

    assert rc == 0, (
        "compliant icon (128x128) + logo (250x100) should pass, "
        f"got exit code {rc}"
    )


@pytest.mark.parametrize(("icon_size", "logo_size"), _DRIFT_CASES)
def test_main_returns_nonzero_for_drift(
    tmp_path: Path,
    icon_size: tuple[int, int],
    logo_size: tuple[int, int],
) -> None:
    """Any deviation from the (128x128, 250x100) contract must fail."""
    _materialise_branding(tmp_path, icon_size, logo_size)

    rc = check_branding.main([str(tmp_path)])

    assert rc != 0, (
        f"drift case icon={icon_size} logo={logo_size} unexpectedly passed"
    )


def test_main_returns_nonzero_when_icon_missing(tmp_path: Path) -> None:
    """Missing files are a drift mode and must still return non-zero."""
    target_dir = tmp_path / "sleep_classifier"
    target_dir.mkdir(parents=True, exist_ok=True)
    # Only logo present, icon missing.
    (target_dir / "logo.png").write_bytes(_make_png_bytes(250, 100))

    rc = check_branding.main([str(tmp_path)])
    assert rc != 0


def test_main_returns_nonzero_when_logo_missing(tmp_path: Path) -> None:
    """Symmetric to icon-missing case."""
    target_dir = tmp_path / "sleep_classifier"
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "icon.png").write_bytes(_make_png_bytes(128, 128))

    rc = check_branding.main([str(tmp_path)])
    assert rc != 0


def test_main_returns_nonzero_when_signature_corrupt(tmp_path: Path) -> None:
    """Malformed PNG (bad signature) must be rejected, not silently passed."""
    target_dir = tmp_path / "sleep_classifier"
    target_dir.mkdir(parents=True, exist_ok=True)
    # Strip the PNG signature from an otherwise-correct icon file.
    icon_bytes = _make_png_bytes(128, 128)
    (target_dir / "icon.png").write_bytes(b"\x00" * 8 + icon_bytes[8:])
    (target_dir / "logo.png").write_bytes(_make_png_bytes(250, 100))

    rc = check_branding.main([str(tmp_path)])
    assert rc != 0


def test_assert_png_size_round_trips_compliant_pair(tmp_path: Path) -> None:
    """Direct ``read_png_size`` smoke test — guards against fixture rot."""
    icon = tmp_path / "icon.png"
    logo = tmp_path / "logo.png"
    icon.write_bytes(_make_png_bytes(128, 128))
    logo.write_bytes(_make_png_bytes(250, 100))

    assert check_branding.read_png_size(icon) == (128, 128)
    assert check_branding.read_png_size(logo) == (250, 100)
    # And the higher-level assert helper does not raise on the contract.
    check_branding.assert_png_size(icon, (128, 128))
    check_branding.assert_png_size(logo, (250, 100))
