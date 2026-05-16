"""Verify translation packs cover the add-on configuration options.

CI guard for spec ``commercial-readiness-v2.1.0`` Requirement 2 (i18n).
It compares three key sets that **must** be strictly equal:

* ``sleep_classifier/config.yaml``                — top-level ``options:``  keys
* ``sleep_classifier/translations/en.yaml``       — top-level ``configuration:`` keys
* ``sleep_classifier/translations/zh-cn.yaml``    — top-level ``configuration:`` keys

Any drift (missing or extra keys in either translation file) causes the
script to exit non-zero so CI blocks the merge. The diff summary groups
missing/extra keys per file (so a maintainer can fix one file at a time).

Exit codes
----------
* ``0`` — three key sets are strictly equal
* ``1`` — translation drift (missing / extra keys), or one of the
  required input files is missing / malformed. Both failure modes
  collapse to a single non-zero code because the spec only requires
  "三集合不严格相等即返回非零"; the stderr message disambiguates
  for human reviewers.

Pure-functional surface
-----------------------
``load_yaml_keys(path, key) -> set[str]``
    Read ``path`` (YAML mapping) and return the set of keys declared
    under the top-level ``key``. Raises :class:`FileNotFoundError`
    when ``path`` is missing and :class:`ValueError` when the file
    cannot be parsed or the top-level shape is wrong.

``main(argv=None) -> int``
    CLI entry point. Returns 0 on success, non-zero on any failure.

Usage
-----
::

    python scripts/check_translations.py
    python scripts/check_translations.py --repo-root /tmp/fixture-repo

Note
----
The ``sleep_classifier/translations/`` directory is created later by
spec task 2.2. Until then this script is expected to exit non-zero
with a clear "file not found" message — that is the desired CI
behaviour for the wave-0 guard layer.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Mapping

import yaml


_REPO_ROOT_DEFAULT = Path(__file__).resolve().parent.parent
_CONFIG_RELPATH = Path("sleep_classifier") / "config.yaml"
_TRANSLATIONS_RELDIR = Path("sleep_classifier") / "translations"
_TRANSLATION_LOCALES: tuple[str, ...] = ("en", "zh-cn")

_CONFIG_OPTIONS_KEY = "options"
_TRANSLATION_CONFIGURATION_KEY = "configuration"

EXIT_OK = 0
EXIT_FAIL = 1


def _load_yaml_mapping(path: Path) -> Mapping[str, Any]:
    """Load ``path`` as a YAML mapping.

    Raises :class:`FileNotFoundError` when the file does not exist and
    :class:`ValueError` when the file cannot be parsed or its top-level
    shape is not a mapping.
    """
    if not path.is_file():
        raise FileNotFoundError(f"required file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"failed to parse YAML at {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise ValueError(
            f"top-level YAML at {path} is not a mapping (found "
            f"{type(data).__name__})"
        )
    return data


def load_yaml_keys(path: Path, key: str) -> set[str]:
    """Return the set of keys declared under top-level ``key`` in ``path``.

    The pure-functional building block consumed by :func:`main` and by
    unit tests. Raises :class:`FileNotFoundError` when the file is
    missing and :class:`ValueError` when either the file is malformed
    or the value at ``key`` is not a mapping.
    """
    data = _load_yaml_mapping(path)
    section = data.get(key)
    if not isinstance(section, Mapping):
        type_name = type(section).__name__ if section is not None else "missing"
        raise ValueError(
            f"{path} does not contain a top-level '{key}:' mapping "
            f"(found {type_name})"
        )
    return {str(k) for k in section.keys()}


def _format_drift_report(
    config_path: Path,
    config_keys: set[str],
    drift: Mapping[str, Mapping[str, list[str]]],
) -> str:
    """Render a human-readable, per-file drift summary."""
    lines: list[str] = [
        "Translation coverage drift detected.",
        f"Reference: {config_path} ({_CONFIG_OPTIONS_KEY}: {len(config_keys)} keys)",
        "",
    ]
    for label in sorted(drift):
        entry = drift[label]
        lines.append(f"  {label}:")
        if entry["missing"]:
            lines.append(
                "    missing (declared in config.yaml but not translated):"
            )
            for k in entry["missing"]:
                lines.append(f"      - {k}")
        if entry["extra"]:
            lines.append(
                "    extra (translated but not declared in config.yaml):"
            )
            for k in entry["extra"]:
                lines.append(f"      - {k}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check that sleep_classifier/translations/{en,zh-cn}.yaml cover "
            "every options key declared in sleep_classifier/config.yaml."
        )
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT_DEFAULT,
        help=(
            "Repository root containing sleep_classifier/. Defaults to the "
            "checkout this script lives in."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns ``EXIT_OK`` (0) on success, ``EXIT_FAIL`` (1) otherwise.

    All errors are written to ``stderr``; a passing run writes a single
    summary line to ``stdout``.
    """
    args = _build_parser().parse_args(argv)
    repo_root: Path = args.repo_root.resolve()

    config_path = repo_root / _CONFIG_RELPATH
    try:
        config_keys = load_yaml_keys(config_path, _CONFIG_OPTIONS_KEY)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_FAIL

    translation_keys: dict[str, set[str]] = {}
    had_translation_error = False
    for locale in _TRANSLATION_LOCALES:
        relpath = _TRANSLATIONS_RELDIR / f"{locale}.yaml"
        path = repo_root / relpath
        label = str(relpath).replace("\\", "/")
        try:
            translation_keys[label] = load_yaml_keys(
                path, _TRANSLATION_CONFIGURATION_KEY
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            had_translation_error = True

    if had_translation_error:
        return EXIT_FAIL

    drift: dict[str, dict[str, list[str]]] = {}
    for label, keys in translation_keys.items():
        missing = sorted(config_keys - keys)
        extra = sorted(keys - config_keys)
        if missing or extra:
            drift[label] = {"missing": missing, "extra": extra}

    if drift:
        print(
            _format_drift_report(config_path, config_keys, drift),
            file=sys.stderr,
        )
        return EXIT_FAIL

    files_label = ", ".join(sorted(translation_keys))
    print(
        f"OK: {len(config_keys)} options keys are fully translated "
        f"in [{files_label}]."
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
