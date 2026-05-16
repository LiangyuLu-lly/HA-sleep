"""Validate Add-on branding asset dimensions without depending on Pillow.

Parses each PNG file's IHDR chunk using only the standard library (`struct`)
and verifies that:

- ``sleep_classifier/icon.png`` is exactly ``128 x 128``
- ``sleep_classifier/logo.png`` is exactly ``250 x 100``

If either asset is missing, malformed, or has the wrong size the script prints
a descriptive error to stderr and returns a non-zero exit code so the CI
``addon-lint`` job (Requirement 1.6) blocks the merge.

This script intentionally has zero runtime dependencies. The Add-on image
must not pull in Pillow / PIL, so even on the CI side we stay on stdlib only.

Usage::

    python scripts/check_branding.py            # checks repo at CWD
    python scripts/check_branding.py path/to/repo

Acceptance criteria covered: Requirements 1.1, 1.2, 1.6.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Iterable, Tuple


# ---------------------------------------------------------------------------
# PNG layout constants (RFC 2083 / W3C PNG spec):
#   - 8-byte signature
#   - first chunk MUST be IHDR
#   - IHDR data length is fixed at 13 bytes; first 8 bytes are width / height
#     as big-endian unsigned 32-bit integers.
# ---------------------------------------------------------------------------

PNG_SIGNATURE: bytes = b"\x89PNG\r\n\x1a\n"
_IHDR_TYPE: bytes = b"IHDR"
_IHDR_DATA_LEN: int = 13


# ---------------------------------------------------------------------------
# Branding contract — keep aligned with requirements.md §1 and design.md §3.1.
# ---------------------------------------------------------------------------

ICON_PATH: Path = Path("sleep_classifier/icon.png")
ICON_SIZE: Tuple[int, int] = (128, 128)

LOGO_PATH: Path = Path("sleep_classifier/logo.png")
LOGO_SIZE: Tuple[int, int] = (250, 100)

BRANDING_ASSETS: Tuple[Tuple[Path, Tuple[int, int]], ...] = (
    (ICON_PATH, ICON_SIZE),
    (LOGO_PATH, LOGO_SIZE),
)


def read_png_size(path: Path) -> Tuple[int, int]:
    """Return ``(width, height)`` parsed from the PNG IHDR chunk at ``path``.

    Raises :class:`ValueError` for any structural problem (bad signature,
    truncated header, non-IHDR first chunk, malformed length). The caller is
    expected to translate the error into a non-zero exit code.
    """
    with path.open("rb") as fh:
        signature = fh.read(8)
        if signature != PNG_SIGNATURE:
            raise ValueError(f"{path}: not a PNG file (bad signature)")

        chunk_header = fh.read(8)
        if len(chunk_header) != 8:
            raise ValueError(f"{path}: truncated PNG (missing IHDR header)")

        # ">I4s": big-endian uint32 length + 4-byte ASCII chunk type.
        length, chunk_type = struct.unpack(">I4s", chunk_header)
        if chunk_type != _IHDR_TYPE:
            raise ValueError(
                f"{path}: first chunk is {chunk_type!r}, expected IHDR"
            )
        if length != _IHDR_DATA_LEN:
            raise ValueError(
                f"{path}: malformed IHDR length {length} (expected {_IHDR_DATA_LEN})"
            )

        ihdr_head = fh.read(8)
        if len(ihdr_head) != 8:
            raise ValueError(f"{path}: truncated IHDR data")

        width, height = struct.unpack(">II", ihdr_head)
        return int(width), int(height)


def assert_png_size(path: Path, expected: Tuple[int, int]) -> None:
    """Assert that the PNG at ``path`` matches the ``expected`` (w, h) tuple.

    Raises :class:`FileNotFoundError` if the file is missing and
    :class:`ValueError` for any size mismatch or malformed PNG.
    """
    if not path.is_file():
        raise FileNotFoundError(f"{path}: branding asset is missing")

    actual = read_png_size(path)
    if actual != expected:
        raise ValueError(
            f"{path}: expected {expected[0]}x{expected[1]} PNG, "
            f"got {actual[0]}x{actual[1]}"
        )


def _iter_failures(
    repo_root: Path,
    assets: Iterable[Tuple[Path, Tuple[int, int]]],
) -> Iterable[str]:
    """Yield human-readable failure messages for each non-compliant asset."""
    for relative, expected in assets:
        target = repo_root / relative
        try:
            assert_png_size(target, expected)
        except (FileNotFoundError, ValueError, OSError) as exc:
            yield str(exc)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns ``0`` only when **every** branding asset is present and exactly
    matches its declared size. Any failure (missing file, malformed PNG, wrong
    dimensions) maps to a non-zero exit code so CI fails the job.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    repo_root = Path(args[0]).resolve() if args else Path.cwd()

    failures = list(_iter_failures(repo_root, BRANDING_ASSETS))
    if failures:
        for line in failures:
            print(f"check_branding: FAIL {line}", file=sys.stderr)
        return 1

    print(
        "check_branding: OK — icon.png (128x128) and logo.png (250x100) compliant.",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI passthrough
    raise SystemExit(main())
