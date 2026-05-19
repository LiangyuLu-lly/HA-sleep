"""Sanitize user data files for safe sharing in bug reports.

This CLI tool reads JSON / JSONL files from a path (file or directory) and
writes a sanitised copy to ``--out`` so users can attach
``user_preferences.json`` / ``causal_factors.jsonl`` style artifacts to bug
reports without leaking personally-identifiable information.

Sanitisation rules (see ``.kiro/specs/algorithmic-moat-v3.0.0/requirements.md``
R14.5 and ``design.md`` Property 12):

- Any value of a key named ``entity_id`` is replaced by the first 16 hex chars
  of ``sha256(value)``.
- Any string value matching the Home Assistant entity_id pattern
  ``^[a-z_]+\\.[a-zA-Z0-9_]+$`` (e.g. ``sensor.bedroom_temp``) gets the same
  hash treatment, regardless of which key it sits under.
- ISO-8601 timestamp string values under keys
  ``timestamp`` / ``started_at`` / ``ended_at`` / ``time`` keep the hour and
  minute, but the seconds field is zeroed and any fractional seconds are
  dropped (e.g. ``2026-05-18T03:42:17.123Z`` → ``2026-05-18T03:42:00Z``).
- Numeric Unix-second timestamp values under the same keys are rounded down
  to the minute (``int(value) // 60 * 60``).
- Any value of a profile key in
  ``{age_band, sex, chronotype, user_profile_age_band, user_profile_sex,
  user_profile_chronotype}`` is replaced by the literal string ``"redacted"``.

The original file is **never** overwritten: if ``--out`` resolves to the same
path as ``--input``, the tool exits with status 1.

Usage::

    # single file
    python scripts/sanitize_user_data.py --input /data/user_preferences.json \\
        --out /tmp/user_preferences.sanitised.json

    # whole directory (mirrors the relative tree under --out)
    python scripts/sanitize_user_data.py --input /data --out /tmp/data.sanitised

Stdlib only: no third-party dependencies.

:Validates: Requirements 14.4, 14.5
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Keys whose **string** value gets replaced by ``sha256(value)[:16]``.
_ENTITY_ID_KEYS: frozenset[str] = frozenset({"entity_id"})

#: Keys whose **value** gets replaced by the literal ``"redacted"``.
_PROFILE_KEYS: frozenset[str] = frozenset(
    {
        "age_band",
        "sex",
        "chronotype",
        "user_profile_age_band",
        "user_profile_sex",
        "user_profile_chronotype",
    }
)

#: Keys whose ISO-8601 / unix-second value gets minute-precision truncation.
_TIMESTAMP_KEYS: frozenset[str] = frozenset(
    {"timestamp", "started_at", "ended_at", "time"}
)

#: Home Assistant entity_id pattern, e.g. ``sensor.bedroom_temp``.
_ENTITY_ID_RE: re.Pattern[str] = re.compile(r"^[a-z_]+\.[a-zA-Z0-9_]+$")

#: ISO-8601 timestamp with at least seconds; tolerates ``T`` or space and
#: optional fractional + timezone suffix.
_ISO8601_RE: re.Pattern[str] = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})"
    r"(?P<sep>[T ])"
    r"(?P<hm>\d{2}:\d{2})"
    r":(?P<sec>\d{2})"
    r"(?:\.\d+)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)

#: File suffixes we sanitise.
_SUPPORTED_SUFFIXES: frozenset[str] = frozenset({".json", ".jsonl"})


# ---------------------------------------------------------------------------
# Scalar transformations
# ---------------------------------------------------------------------------


def _hash_entity_id(value: str) -> str:
    """Return the first 16 hex chars of ``sha256(value)``.

    Truncated for readability — the full 64-char digest is overkill for the
    bug-report use case and merely needs to be a stable opaque pseudonym.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _redact_iso8601(value: str) -> str | None:
    """Zero out seconds + fractional in an ISO-8601 string.

    Returns ``None`` if ``value`` does not match the expected ISO-8601 shape
    so the caller can fall through to other rules.
    """
    match = _ISO8601_RE.match(value)
    if match is None:
        return None
    tz = match.group("tz") or ""
    return f"{match.group('date')}{match.group('sep')}{match.group('hm')}:00{tz}"


def _round_unix_timestamp_to_minute(value: int | float) -> int | float:
    """Round a Unix-second timestamp down to the nearest minute boundary.

    Booleans are explicitly excluded by the caller because ``bool`` is a
    subclass of ``int`` in Python and we want to leave them untouched.
    """
    floored = int(value) // 60 * 60
    # Preserve numeric type to avoid surprising downstream consumers.
    return float(floored) if isinstance(value, float) else floored


# ---------------------------------------------------------------------------
# Recursive transform
# ---------------------------------------------------------------------------


def _transform(node: Any, parent_key: str | None = None) -> Any:
    """Recursively transform a JSON-decoded structure.

    :param node: The current node (dict / list / scalar).
    :param parent_key: The key of the immediately enclosing dict, propagated
        through lists so that list items inherit the same key context (e.g.
        ``{"entity_id": ["sensor.a", "sensor.b"]}`` hashes both items).
    """
    if isinstance(node, dict):
        return {k: _transform(v, parent_key=k) for k, v in node.items()}
    if isinstance(node, list):
        return [_transform(item, parent_key=parent_key) for item in node]
    return _transform_scalar(node, parent_key=parent_key)


def _transform_scalar(value: Any, parent_key: str | None) -> Any:
    """Apply sanitisation rules to a leaf scalar."""
    # 1. Profile redaction wins regardless of value type.
    if parent_key in _PROFILE_KEYS:
        return "redacted"

    # 2. entity_id key: hash any string value.
    if parent_key in _ENTITY_ID_KEYS and isinstance(value, str):
        return _hash_entity_id(value)

    # 3. Timestamp key: zero seconds for ISO strings, floor to minute for
    #    numeric Unix seconds. ``bool`` is excluded because it is a subclass
    #    of ``int`` but is never a real timestamp.
    if parent_key in _TIMESTAMP_KEYS:
        if isinstance(value, str):
            redacted = _redact_iso8601(value)
            if redacted is not None:
                return redacted
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            return _round_unix_timestamp_to_minute(value)

    # 4. Free-floating entity_id-shaped strings get hashed too.
    if isinstance(value, str) and _ENTITY_ID_RE.match(value):
        return _hash_entity_id(value)

    return value


# ---------------------------------------------------------------------------
# File-level processing
# ---------------------------------------------------------------------------


def _sanitize_json_text(text: str) -> str:
    """Sanitise the contents of a ``.json`` file.

    The file is parsed as a single JSON document; the result is re-serialised
    with ``indent=2`` for readability and ``ensure_ascii=False`` so non-ASCII
    text in the original file (rare but possible in user-supplied notes)
    survives the round-trip.
    """
    payload = json.loads(text)
    sanitised = _transform(payload)
    return json.dumps(sanitised, ensure_ascii=False, indent=2) + "\n"


def _sanitize_jsonl_text(text: str) -> str:
    """Sanitise the contents of a ``.jsonl`` file (one JSON value per line)."""
    out_lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            # Blank lines in JSONL are not part of the format; drop them.
            continue
        record = json.loads(line)
        sanitised = _transform(record)
        out_lines.append(json.dumps(sanitised, ensure_ascii=False))
    return "\n".join(out_lines) + ("\n" if out_lines else "")


def _process_file(src: Path, dst: Path) -> None:
    """Read ``src``, sanitise according to its suffix, write ``dst``."""
    text = src.read_text(encoding="utf-8")
    suffix = src.suffix.lower()
    if suffix == ".jsonl":
        sanitised = _sanitize_jsonl_text(text)
    else:  # ``.json``
        sanitised = _sanitize_json_text(text)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(sanitised, encoding="utf-8")


def _iter_input_files(input_path: Path) -> Iterable[Path]:
    """Yield the files that should be sanitised under ``input_path``.

    Symlinks are followed (``rglob`` default); only regular files whose suffix
    is in :data:`_SUPPORTED_SUFFIXES` are emitted.
    """
    if input_path.is_file():
        if input_path.suffix.lower() in _SUPPORTED_SUFFIXES:
            yield input_path
        return
    for candidate in sorted(input_path.rglob("*")):
        if candidate.is_file() and candidate.suffix.lower() in _SUPPORTED_SUFFIXES:
            yield candidate


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sanitize_user_data",
        description=(
            "Sanitise /data/*.json and /data/*.jsonl artifacts before sharing "
            "them in a bug report (R14.5)."
        ),
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Input file or directory (existing). Directories are walked recursively.",
    )
    parser.add_argument(
        "--out",
        required=True,
        help=(
            "Output file or directory. Must NOT resolve to the same path as "
            "--input; the tool refuses to overwrite the source."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns the process exit code."""
    args = _build_arg_parser().parse_args(argv)

    input_path = Path(args.input)
    out_path = Path(args.out)

    if not input_path.exists():
        print(f"error: --input path does not exist: {input_path}", file=sys.stderr)
        return 2

    # Refuse to clobber the original file/directory. Compare resolved
    # absolute paths so ``./foo`` and ``foo`` map to the same node.
    try:
        if input_path.resolve() == out_path.resolve():
            print(
                "error: --out must not equal --input; refusing to overwrite "
                "the original file.",
                file=sys.stderr,
            )
            return 1
    except OSError as exc:  # pragma: no cover — defensive
        print(f"error: failed to resolve paths: {exc}", file=sys.stderr)
        return 2

    if input_path.is_file():
        # Single-file mode. ``--out`` may be a destination file path, or a
        # directory in which case we mirror the source filename inside it.
        if out_path.exists() and out_path.is_dir():
            dst = out_path / input_path.name
        else:
            dst = out_path
        _process_file(input_path, dst)
        return 0

    if input_path.is_dir():
        # Directory mode: mirror the relative tree under ``--out``.
        any_file = False
        for src in _iter_input_files(input_path):
            any_file = True
            relative = src.relative_to(input_path)
            dst = out_path / relative
            _process_file(src, dst)
        if not any_file:
            # Nothing to do, but ensure the output directory exists so the
            # caller sees an explicit (empty) result.
            out_path.mkdir(parents=True, exist_ok=True)
        return 0

    print(
        f"error: --input is neither a file nor a directory: {input_path}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":  # pragma: no cover — CLI shim
    raise SystemExit(main())
