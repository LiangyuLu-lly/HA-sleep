"""Atomic-write helpers shared by render_effective_config / web_ui / learner.

Why not use a third-party library:

* ``tech.md`` requires the runtime to depend only on ``aiohttp``.
  Atomic-write is ~30 lines of code — not worth pulling a dependency.

Strategy
--------
``tempfile.mkstemp(prefix=path.name + ".tmp.", dir=path.parent)`` creates
a temporary file on the **same filesystem** as the target (critical for
``os.replace`` to be atomic).  We write → ``fsync`` → ``os.replace``.
A kill anywhere before ``os.replace`` leaves a stale ``.tmp.*`` file but
the main file is intact.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, data: str, *, encoding: str = "utf-8") -> None:
    """Write *data* to *path* atomically.

    :param path: Target file path.
    :param data: String content to write.
    :param encoding: Text encoding (default UTF-8).
    :raises OSError: If the write or replace fails (tmp is cleaned up).

    The sequence is:

    1. ``tempfile.mkstemp`` in the same directory as *path*.
    2. ``fdopen`` → write → ``flush`` → ``fsync``.
    3. ``os.replace(tmp, path)`` — atomic on POSIX same-filesystem.
    4. On exception: ``os.unlink(tmp)`` best-effort cleanup, then re-raise.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".tmp.",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        # Clean up the temporary file so /data doesn't accumulate junk.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj: Any, *, indent: int = 2) -> None:
    """Serialize *obj* as JSON and write to *path* atomically.

    :param path: Target file path.
    :param obj: JSON-serializable object.
    :param indent: JSON indentation (default 2 spaces).

    Delegates to :func:`atomic_write_text` for the actual I/O.
    """
    atomic_write_text(path, json.dumps(obj, indent=indent, ensure_ascii=False))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write *data* (binary blob) to *path* atomically.

    :param path: Target file path.
    :param data: Raw bytes to write (e.g. ``pickle.dumps(...)``).
    :raises OSError: If the write or replace fails (tmp is cleaned up).

    Mirrors :func:`atomic_write_text` but operates on ``bytes`` for binary
    payloads such as the BAO ``pickle`` snapshot at
    ``/data/bao_model.pickle``.  The same tmpfile + ``fsync`` +
    :func:`os.replace` sequence guarantees that a kill anywhere before the
    final replace leaves a stale ``.tmp.*`` file but the main file intact.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".tmp.",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        # Clean up the temporary file so /data doesn't accumulate junk.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_append_jsonl(
    path: Path,
    record: Mapping[str, Any],
    *,
    max_lines: int | None = None,
) -> None:
    """Atomically append one JSON line to *path*.

    :param path: Target ``.jsonl`` file (created if missing).
    :param record: JSON-serializable mapping; serialized as a single line
        with ``ensure_ascii=False`` and **no** trailing whitespace beyond
        the line break.
    :param max_lines: When given (``> 0``), keep only the **last**
        ``max_lines`` records (FIFO truncation).  ``None`` disables the
        cap.
    :raises OSError: If the underlying write or replace fails.
    :raises ValueError: If *max_lines* is given but ``<= 0``.

    Implementation
    --------------
    A true filesystem-level append is non-atomic on partial writes, so we
    rebuild the full file content in memory:

    1. Read existing lines (best-effort; ``FileNotFoundError`` ⇒ empty).
    2. Append the serialized *record*.
    3. If ``max_lines`` is set and total > ``max_lines``, drop oldest
       (FIFO) until equal.
    4. Hand the joined text to :func:`atomic_write_text` (tmpfile +
       ``fsync`` + :func:`os.replace`).

    Per the v3 persistence contract, both ``causal_factors.jsonl``
    (≤ 90 records) and ``predictor_audit.jsonl`` stay well under 100
    lines × 10 KB, so the read-modify-write cost is negligible.
    """
    if max_lines is not None and max_lines <= 0:
        raise ValueError(
            f"max_lines must be a positive int or None, got {max_lines!r}"
        )

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Load existing lines (skip blank tail line if any).
    existing: list[str] = []
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = ""
    if raw:
        existing = [ln for ln in raw.splitlines() if ln]

    # 2. Append the new record (single line, no embedded newline).
    new_line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    existing.append(new_line)

    # 3. FIFO truncate to max_lines.
    if max_lines is not None and len(existing) > max_lines:
        existing = existing[-max_lines:]

    # 4. Atomic rewrite via tmpfile + os.replace.
    atomic_write_text(path, "\n".join(existing) + "\n")
