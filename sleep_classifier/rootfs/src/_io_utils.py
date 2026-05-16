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
