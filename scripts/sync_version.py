"""Synchronise the project version across all manifests.

Single Source of Truth (SoT) for the version string is the
``[project] version`` field of ``pyproject.toml``. The HA Add-on Store, the
PyPI metadata exposed via ``setup.py`` and any ``__version__`` constant in
``src/__init__.py`` must all agree with that value, otherwise users see
inconsistent strings and our release pipeline (CI ``test.yml`` ``--check``
hook + ``release.yml`` auto-commit step, see design §3.4.2) cannot decide
which manifest to trust.

This script provides three capabilities:

* :func:`read_canonical`  — read the SoT version from ``pyproject.toml``.
* :func:`sync`            — propagate / verify the version across:

    1. ``setup.py``                       (``version="..."`` keyword arg)
    2. ``sleep_classifier/config.yaml``   (top-level ``version: "..."``)
    3. ``src/__init__.py``                (``__version__ = "..."``,
                                          *only* synced when that line
                                          already exists — design §3.4.2:
                                          "如存在则同步").

* :func:`main`            — CLI entry point with an optional ``--check``
                            flag.

CLI usage::

    python scripts/sync_version.py            # write SoT into the three targets
    python scripts/sync_version.py --check    # only verify, exit 1 if drift

Acceptance criteria covered: Requirements 4.5, 4.6.

Implementation notes
--------------------
* Pure standard library: ``tomllib`` is used on Python 3.11+; on 3.10 we
  prefer ``tomli`` if available and fall back to a small regex extractor
  on the well-known ``[project] version = "..."`` shape.  No PyYAML
  dependency is introduced — we treat ``config.yaml`` as a line-scanned
  text file and only touch the top-level ``version:`` line, which keeps
  comments / blank lines / quoting style intact.
* All write paths are idempotent: a target file is only rewritten when
  its current value differs from the canonical version.
* On ``--check`` mismatch we print every observed version string (one
  per line, prefixed with the relative path) so CI logs make the drift
  obvious without having to diff the files manually.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable

# ---------------------------------------------------------------------------
# tomllib import strategy (stdlib first, then optional dev dep, then regex).
# ---------------------------------------------------------------------------

try:  # Python 3.11+
    import tomllib as _tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.10
    try:
        import tomli as _tomllib  # type: ignore[import-not-found, no-redef]
    except ModuleNotFoundError:
        _tomllib = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Repo layout constants.
# ---------------------------------------------------------------------------

_REPO_ROOT_DEFAULT: Path = Path(__file__).resolve().parent.parent

_PYPROJECT_RELPATH: Path = Path("pyproject.toml")
_SETUP_RELPATH: Path = Path("setup.py")
_CONFIG_YAML_RELPATH: Path = Path("sleep_classifier") / "config.yaml"
_INIT_RELPATH: Path = Path("src") / "__init__.py"


# ---------------------------------------------------------------------------
# Regular expressions used both for reading and for in-place rewriting.
#
# Each pattern captures the version literal in group 1 and is anchored so
# that a single ``re.subn`` call can replace exactly the version segment
# without disturbing surrounding quoting / whitespace / commas.
# ---------------------------------------------------------------------------

# pyproject.toml fallback (only used when neither tomllib nor tomli are
# importable — i.e. truly broken environment): match ``version = "x.y.z"``
# inside the ``[project]`` table. The pattern requires the [project]
# header to appear earlier in the file via re.DOTALL + lazy match.
_PYPROJECT_VERSION_FALLBACK_RE: re.Pattern[str] = re.compile(
    r"\[project\][^\[]*?\bversion\s*=\s*[\"']([^\"']+)[\"']",
    re.DOTALL,
)

# setup.py: ``version="2.0.3",`` (single or double quotes, optional
# whitespace, optional trailing comma).
_SETUP_VERSION_RE: re.Pattern[str] = re.compile(
    r"(?P<lead>\bversion\s*=\s*[\"'])(?P<value>[^\"']+)(?P<trail>[\"'])",
)

# config.yaml: top-level ``version: "x.y.z"``. We anchor on a line start to
# avoid matching ``homeassistant: "2024.1.0"`` further down. Quoting may be
# double, single, or absent in the source file (HA Supervisor accepts
# bare scalars too); we always rewrite with double quotes for consistency
# with the v2.0.3 baseline.
_CONFIG_VERSION_RE: re.Pattern[str] = re.compile(
    r"^(?P<lead>version\s*:\s*)"
    r"(?P<quote>[\"']?)"
    r"(?P<value>[^\"'\s#]+)"
    r"(?P=quote)"
    r"(?P<trail>\s*(?:#.*)?)$",
    re.MULTILINE,
)

# src/__init__.py: ``__version__ = "x.y.z"`` (PEP 396 style).
_INIT_VERSION_RE: re.Pattern[str] = re.compile(
    r"(?P<lead>^__version__\s*=\s*[\"'])(?P<value>[^\"']+)(?P<trail>[\"'])",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2


# ---------------------------------------------------------------------------
# Public API — read the SoT.
# ---------------------------------------------------------------------------


def read_canonical(repo_root: Path | None = None) -> str:
    """Return the canonical version declared in ``pyproject.toml``.

    Uses :mod:`tomllib` (3.11+) or :mod:`tomli` (3.10 dev) when available,
    falling back to a small regex extractor so that this script runs even
    in minimal environments without a TOML parser.

    Raises :class:`FileNotFoundError` if ``pyproject.toml`` is missing and
    :class:`ValueError` if the ``[project] version`` field is absent or
    not a string.
    """
    root = (repo_root or _REPO_ROOT_DEFAULT).resolve()
    path = root / _PYPROJECT_RELPATH
    if not path.is_file():
        raise FileNotFoundError(f"required file not found: {path}")

    if _tomllib is not None:
        with path.open("rb") as fh:
            data = _tomllib.load(fh)
        try:
            version = data["project"]["version"]
        except (KeyError, TypeError) as exc:
            raise ValueError(
                f"{path}: missing [project] version field"
            ) from exc
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f"{path}: [project] version is not a string")
        return version.strip()

    # Fallback: regex against the raw text. Good enough for a well-known
    # PEP 621 layout — this branch is only taken on broken environments
    # where neither tomllib nor tomli exist.
    text = path.read_text(encoding="utf-8")
    match = _PYPROJECT_VERSION_FALLBACK_RE.search(text)
    if match is None:
        raise ValueError(
            f"{path}: could not locate [project] version (fallback parser)"
        )
    return match.group(1).strip()


# ---------------------------------------------------------------------------
# Internal helpers — read & rewrite each individual target.
# ---------------------------------------------------------------------------


def _read_setup_version(path: Path) -> str | None:
    """Return the version literal in ``setup.py`` or ``None`` if absent."""
    if not path.is_file():
        return None
    match = _SETUP_VERSION_RE.search(path.read_text(encoding="utf-8"))
    return match.group("value") if match else None


def _read_config_yaml_version(path: Path) -> str | None:
    """Return the top-level ``version:`` value in ``config.yaml``."""
    if not path.is_file():
        return None
    match = _CONFIG_VERSION_RE.search(path.read_text(encoding="utf-8"))
    return match.group("value") if match else None


def _read_init_version(path: Path) -> str | None:
    """Return ``__version__`` from ``src/__init__.py`` or ``None`` if absent.

    Per design §3.4.2 ("如存在则同步"), this file is treated as optional:
    the absence of a ``__version__`` assignment is *not* an error.
    """
    if not path.is_file():
        return None
    match = _INIT_VERSION_RE.search(path.read_text(encoding="utf-8"))
    return match.group("value") if match else None


def _rewrite_setup(path: Path, new_version: str) -> bool:
    """Rewrite ``setup.py`` ``version="..."``. Return True iff content changed."""
    text = path.read_text(encoding="utf-8")
    new_text, count = _SETUP_VERSION_RE.subn(
        lambda m: f"{m.group('lead')}{new_version}{m.group('trail')}",
        text,
        count=1,
    )
    if count == 0:
        raise ValueError(
            f"{path}: cannot find a 'version=\"...\"' literal to update"
        )
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def _rewrite_config_yaml(path: Path, new_version: str) -> bool:
    """Rewrite the top-level ``version:`` line in ``config.yaml``.

    Always emits double-quoted form (``version: "x.y.z"``) regardless of
    the existing quoting style, matching the v2.0.3 baseline.
    """
    text = path.read_text(encoding="utf-8")

    def _repl(m: re.Match[str]) -> str:
        return f'{m.group("lead")}"{new_version}"{m.group("trail")}'

    new_text, count = _CONFIG_VERSION_RE.subn(_repl, text, count=1)
    if count == 0:
        raise ValueError(
            f"{path}: cannot find a top-level 'version:' line to update"
        )
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def _rewrite_init(path: Path, new_version: str) -> bool:
    """Rewrite ``__version__`` in ``src/__init__.py`` if it exists."""
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    new_text, count = _INIT_VERSION_RE.subn(
        lambda m: f"{m.group('lead')}{new_version}{m.group('trail')}",
        text,
        count=1,
    )
    if count == 0:
        # No __version__ line — nothing to sync (design §3.4.2 "如存在").
        return False
    if new_text == text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True


def _collect_targets(repo_root: Path) -> list[tuple[Path, str | None]]:
    """Return ``[(path, version_or_None), ...]`` for every target file.

    ``setup.py`` and ``config.yaml`` are mandatory targets — a ``None``
    version reading there indicates a malformed file and is treated as a
    drift (callers will surface the missing-literal as a fault).

    ``src/__init__.py`` is optional: when the ``__version__`` line is
    absent we report ``None`` and ``sync`` skips it without raising.
    """
    return [
        (repo_root / _SETUP_RELPATH, _read_setup_version(repo_root / _SETUP_RELPATH)),
        (
            repo_root / _CONFIG_YAML_RELPATH,
            _read_config_yaml_version(repo_root / _CONFIG_YAML_RELPATH),
        ),
        (repo_root / _INIT_RELPATH, _read_init_version(repo_root / _INIT_RELPATH)),
    ]


# ---------------------------------------------------------------------------
# Public API — sync / check.
# ---------------------------------------------------------------------------


def sync(
    version: str,
    *,
    check_only: bool,
    repo_root: Path | None = None,
) -> int:
    """Propagate (or verify) ``version`` across the three target manifests.

    Parameters
    ----------
    version:
        The canonical version string to enforce — typically the return
        value of :func:`read_canonical`.
    check_only:
        When ``True`` the function never writes; it only diffs each
        target's value against ``version``. When ``False`` it rewrites
        any target whose value differs (idempotent — files already in
        sync are left untouched).
    repo_root:
        Override for the repository root. Defaults to the directory
        containing ``scripts/`` so the script works when invoked from
        any CWD.

    Returns
    -------
    ``EXIT_OK`` (0) when every reachable target matches ``version``,
    ``EXIT_DRIFT`` (1) when at least one target disagrees in ``--check``
    mode (or when a mandatory target file is malformed). In write mode
    a successful sync also returns ``EXIT_OK``; structural problems
    (missing files, missing literals) are surfaced as :class:`ValueError`
    or :class:`FileNotFoundError` so the caller decides the exit code.
    """
    root = (repo_root or _REPO_ROOT_DEFAULT).resolve()

    if not version or not version.strip():
        raise ValueError("version must be a non-empty string")
    version = version.strip()

    targets = _collect_targets(root)

    if check_only:
        drift: list[tuple[Path, str | None]] = []
        for path, current in targets:
            # __init__.py is optional: missing file or missing __version__
            # line both mean "nothing to compare" — no drift.
            if path.name == _INIT_RELPATH.name and current is None:
                continue
            if current != version:
                drift.append((path, current))

        if drift:
            _print_drift_report(root, version, targets)
            return EXIT_DRIFT
        return EXIT_OK

    # Write mode — rewrite anything that's out of sync, leave files at
    # the right version untouched.
    setup_path = root / _SETUP_RELPATH
    config_path = root / _CONFIG_YAML_RELPATH
    init_path = root / _INIT_RELPATH

    if not setup_path.is_file():
        raise FileNotFoundError(f"required file not found: {setup_path}")
    if not config_path.is_file():
        raise FileNotFoundError(f"required file not found: {config_path}")

    if _read_setup_version(setup_path) != version:
        _rewrite_setup(setup_path, version)
    if _read_config_yaml_version(config_path) != version:
        _rewrite_config_yaml(config_path, version)
    # init is optional: only rewrite if the line already exists.
    if init_path.is_file() and _read_init_version(init_path) is not None:
        if _read_init_version(init_path) != version:
            _rewrite_init(init_path, version)

    return EXIT_OK


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def _print_drift_report(
    repo_root: Path,
    canonical: str,
    targets: Iterable[tuple[Path, str | None]],
) -> None:
    """Print one line per inspected manifest plus the canonical version.

    The output is intentionally compact and stable so CI log parsers can
    grep for specific paths::

        sync_version: drift detected
          pyproject.toml          = 2.1.0  (canonical)
          setup.py                = 2.0.3
          sleep_classifier/config.yaml = 2.0.3
          src/__init__.py         = <missing>
    """
    print("sync_version: drift detected", file=sys.stderr)
    print(
        f"  {_PYPROJECT_RELPATH.as_posix():<32} = {canonical}  (canonical)",
        file=sys.stderr,
    )
    for path, value in targets:
        try:
            rel = path.resolve().relative_to(repo_root.resolve()).as_posix()
        except ValueError:
            rel = str(path)
        rendered = value if value is not None else "<missing>"
        print(f"  {rel:<32} = {rendered}", file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronise the project version across pyproject.toml (SoT), "
            "setup.py, sleep_classifier/config.yaml and src/__init__.py."
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Verify only. Exits non-zero when any target disagrees with "
            "pyproject.toml, prints all observed versions to stderr, and "
            "leaves every file untouched."
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT_DEFAULT,
        help=(
            "Repository root. Defaults to the directory containing "
            "scripts/. Useful for tests that drive the script against a "
            "tmp_path fixture."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns one of ``EXIT_OK``/``EXIT_DRIFT``/``EXIT_ERROR``."""
    args = _build_parser().parse_args(argv)
    repo_root: Path = args.repo_root.resolve()

    try:
        canonical = read_canonical(repo_root)
    except (FileNotFoundError, ValueError) as exc:
        print(f"sync_version: error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        return sync(canonical, check_only=args.check, repo_root=repo_root)
    except (FileNotFoundError, ValueError) as exc:
        print(f"sync_version: error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover - CLI passthrough
    raise SystemExit(main())
