"""Tests for ``scripts/sync_version.py`` — Property 1: 版本号四处一致.

The script under test enforces the v2.1.0 invariant that the version
string declared in ``pyproject.toml`` (the SoT) propagates to:

* ``setup.py``                       (``version="..."`` keyword arg)
* ``sleep_classifier/config.yaml``   (top-level ``version: "..."``)
* ``src/__init__.py``                (``__version__ = "..."``, optional —
                                     skipped when the line is absent)

The test strategy mirrors design §3.4.2 + tasks 1.4:

1. Build a synthetic but realistic repo skeleton inside ``tmp_path``
   so the script's ``--repo-root`` injection point is exercised in
   isolation (no monkey-patching).
2. Parametrize over a wide grid of "先行不一致初态" — independent
   versions in each manifest, including (a) optional ``__init__.py``
   absent, (b) present but without a ``__version__`` line, and
   (c) present with an inconsistent ``__version__``.
3. For every initial state assert the three legs of Property 1:

   * ``sync(check_only=True)`` returns ``EXIT_DRIFT`` whenever any
     mandatory leg disagrees, else ``EXIT_OK``.
   * ``sync(check_only=False)`` rewrites every reachable target so
     all visible version literals equal the canonical one.
   * A follow-up ``sync(check_only=True)`` then returns ``EXIT_OK``
     (idempotence + convergence in a single shot).

Validates: Requirements 4.5, 4.6
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

import pytest

# Make ``scripts/`` importable so we can call the public API directly
# rather than shelling out — the task explicitly references
# ``read_canonical(repo_root)`` and ``sync(version, *, check_only,
# repo_root)`` as the contract under test.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import sync_version  # noqa: E402  (sys.path manipulation above)


# ---------------------------------------------------------------------------
# Synthetic repo builder
# ---------------------------------------------------------------------------

# Init manifest variants.
# - ``absent``           : ``src/__init__.py`` does not exist.
# - ``no_version_line``  : file exists but has no ``__version__`` literal
#                          (matches the live v2.0.3 layout).
# - ``with_version``     : file exists and declares ``__version__ = "..."``.
InitKind = Literal["absent", "no_version_line", "with_version"]


def _write_pyproject(root: Path, version: str) -> None:
    """Write a minimal but valid PEP 621 ``pyproject.toml``."""
    (root / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools>=68", "wheel"]\n'
        'build-backend = "setuptools.build_meta"\n'
        "\n"
        "[project]\n"
        'name = "sleep-classifier"\n'
        f'version = "{version}"\n'
        'description = "test fixture"\n'
        'requires-python = ">=3.10"\n',
        encoding="utf-8",
    )


def _write_setup(root: Path, version: str) -> None:
    """Mimic the real ``setup.py`` shape (keyword arg + comma)."""
    (root / "setup.py").write_text(
        '"""Synthetic setup.py fixture."""\n'
        "from setuptools import setup\n"
        "\n"
        "setup(\n"
        '    name="sleep-classifier",\n'
        f'    version="{version}",\n'
        '    description="test fixture",\n'
        ")\n",
        encoding="utf-8",
    )


def _write_config_yaml(root: Path, version: str) -> None:
    """Mimic the real Add-on ``config.yaml`` (top-level quoted scalar)."""
    target = root / "sleep_classifier" / "config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "# Synthetic Home Assistant add-on manifest fixture.\n"
        "name: Sleep Classifier\n"
        f'version: "{version}"\n'
        "slug: sleep_classifier\n"
        'homeassistant: "2024.1.0"\n',
        encoding="utf-8",
    )


def _write_init(root: Path, kind: InitKind, version: str | None) -> None:
    """Materialise ``src/__init__.py`` according to the requested variant."""
    src_dir = root / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    target = src_dir / "__init__.py"

    if kind == "absent":
        # Don't create the file at all — exercises the "optional"
        # branch in sync().
        return
    if kind == "no_version_line":
        target.write_text(
            "# Sleep Classifier — runtime modules (synthetic fixture).\n",
            encoding="utf-8",
        )
        return
    if kind == "with_version":
        assert version is not None  # mypy / sanity guard
        target.write_text(
            "# Sleep Classifier — runtime modules (synthetic fixture).\n"
            f'__version__ = "{version}"\n',
            encoding="utf-8",
        )
        return
    raise AssertionError(f"unreachable init kind: {kind!r}")  # pragma: no cover


def _build_repo(
    tmp_path: Path,
    *,
    pyproject_version: str,
    setup_version: str,
    config_version: str,
    init_kind: InitKind,
    init_version: str | None,
) -> Path:
    """Materialise a four-file repo skeleton at ``tmp_path``."""
    _write_pyproject(tmp_path, pyproject_version)
    _write_setup(tmp_path, setup_version)
    _write_config_yaml(tmp_path, config_version)
    _write_init(tmp_path, init_kind, init_version)
    return tmp_path


# ---------------------------------------------------------------------------
# Read-back helpers — mirror the script's parsers but stay independent.
# ---------------------------------------------------------------------------


def _observed_versions(root: Path) -> dict[str, str | None]:
    """Return ``{file_label: version_literal_or_None}``.

    We deliberately re-implement the readers here (instead of calling the
    private helpers in ``sync_version``) so the test would still catch a
    bug where the script's reader and writer fall out of sync.
    """
    import re

    setup_text = (root / "setup.py").read_text(encoding="utf-8")
    setup_match = re.search(
        r'\bversion\s*=\s*"([^"]+)"', setup_text
    )
    setup_value = setup_match.group(1) if setup_match else None

    cfg_text = (root / "sleep_classifier" / "config.yaml").read_text(
        encoding="utf-8"
    )
    cfg_match = re.search(
        r'^version\s*:\s*"([^"]+)"', cfg_text, re.MULTILINE
    )
    cfg_value = cfg_match.group(1) if cfg_match else None

    init_path = root / "src" / "__init__.py"
    init_value: str | None
    if init_path.is_file():
        init_match = re.search(
            r'^__version__\s*=\s*"([^"]+)"',
            init_path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        init_value = init_match.group(1) if init_match else None
    else:
        init_value = None

    return {
        "setup.py": setup_value,
        "sleep_classifier/config.yaml": cfg_value,
        "src/__init__.py": init_value,
    }


# ---------------------------------------------------------------------------
# Parametrised "先行不一致初态" matrix
# ---------------------------------------------------------------------------

# Each row encodes:
#   (pyproject, setup, config, init_kind, init_version)
#
# The grid intentionally covers:
#   * single-leg drift (only one of setup/config/init disagrees);
#   * multi-leg drift (every leg disagrees);
#   * pre-release / build-metadata canonical strings (PEP 440);
#   * init absent vs init present-without-version vs init inconsistent;
#   * fully consistent baselines (regression guard for false positives).
_DRIFT_CASES: list[tuple[str, str, str, InitKind, str | None]] = [
    # --- single-leg drift ---------------------------------------------------
    ("2.1.0", "2.0.3", "2.1.0", "absent",          None),
    ("2.1.0", "2.1.0", "2.0.3", "absent",          None),
    ("2.1.0", "2.1.0", "2.1.0", "with_version",    "2.0.3"),
    # --- multi-leg drift, init absent --------------------------------------
    ("2.1.0", "2.0.3", "1.6.0", "absent",          None),
    # --- multi-leg drift, init present without __version__ -----------------
    ("2.1.0", "1.6.0", "0.0.0", "no_version_line", None),
    # --- multi-leg drift, init present with inconsistent __version__ -------
    ("2.1.0", "0.0.0", "0.0.0", "with_version",    "0.0.0"),
    # --- pre-release SoT ---------------------------------------------------
    ("2.1.0rc1", "2.0.3", "2.0.3", "with_version", "2.0.3"),
    # --- canonical newer than every leg ------------------------------------
    ("3.0.0", "2.0.3", "2.0.3", "with_version",    "2.0.3"),
    # --- only init drifts (mandatory legs already agree) -------------------
    ("2.1.0", "2.1.0", "2.1.0", "with_version",    "1.0.0"),
]

_CONSISTENT_CASES: list[tuple[str, str, str, InitKind, str | None]] = [
    # Every reachable leg already agrees with the canonical SoT — these
    # rows act as a regression guard so ``check_only`` does not falsely
    # report drift.
    ("2.1.0", "2.1.0", "2.1.0", "absent",          None),
    ("2.1.0", "2.1.0", "2.1.0", "no_version_line", None),
    ("2.1.0", "2.1.0", "2.1.0", "with_version",    "2.1.0"),
    ("0.1.0", "0.1.0", "0.1.0", "with_version",    "0.1.0"),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_read_canonical_returns_pyproject_value(tmp_path: Path) -> None:
    """Sanity check: ``read_canonical`` extracts the SoT from pyproject."""
    _build_repo(
        tmp_path,
        pyproject_version="2.1.0",
        setup_version="0.0.0",
        config_version="0.0.0",
        init_kind="with_version",
        init_version="0.0.0",
    )
    assert sync_version.read_canonical(tmp_path) == "2.1.0"


@pytest.mark.parametrize(
    ("pyproject", "setup", "config", "init_kind", "init_version"),
    _DRIFT_CASES,
)
def test_check_only_flags_drift(
    tmp_path: Path,
    pyproject: str,
    setup: str,
    config: str,
    init_kind: InitKind,
    init_version: str | None,
) -> None:
    """``sync(check_only=True)`` must return ``EXIT_DRIFT`` on any drift."""
    _build_repo(
        tmp_path,
        pyproject_version=pyproject,
        setup_version=setup,
        config_version=config,
        init_kind=init_kind,
        init_version=init_version,
    )
    canonical = sync_version.read_canonical(tmp_path)

    rc = sync_version.sync(canonical, check_only=True, repo_root=tmp_path)

    assert rc == sync_version.EXIT_DRIFT, (
        f"expected EXIT_DRIFT for inconsistent state, got {rc}"
    )

    # check_only must not mutate any file — observed values stay put.
    observed = _observed_versions(tmp_path)
    assert observed["setup.py"] == setup
    assert observed["sleep_classifier/config.yaml"] == config
    if init_kind == "with_version":
        assert observed["src/__init__.py"] == init_version
    else:
        assert observed["src/__init__.py"] is None


@pytest.mark.parametrize(
    ("pyproject", "setup", "config", "init_kind", "init_version"),
    _CONSISTENT_CASES,
)
def test_check_only_passes_when_consistent(
    tmp_path: Path,
    pyproject: str,
    setup: str,
    config: str,
    init_kind: InitKind,
    init_version: str | None,
) -> None:
    """``sync(check_only=True)`` must return 0 when every leg agrees."""
    _build_repo(
        tmp_path,
        pyproject_version=pyproject,
        setup_version=setup,
        config_version=config,
        init_kind=init_kind,
        init_version=init_version,
    )
    canonical = sync_version.read_canonical(tmp_path)
    assert canonical == pyproject  # invariant: SoT == pyproject literal

    rc = sync_version.sync(canonical, check_only=True, repo_root=tmp_path)

    assert rc == sync_version.EXIT_OK, (
        f"expected EXIT_OK for consistent state, got {rc}"
    )


@pytest.mark.parametrize(
    ("pyproject", "setup", "config", "init_kind", "init_version"),
    _DRIFT_CASES,
)
def test_sync_propagates_canonical_to_all_targets(
    tmp_path: Path,
    pyproject: str,
    setup: str,
    config: str,
    init_kind: InitKind,
    init_version: str | None,
) -> None:
    """After ``sync(check_only=False)``, every reachable leg must equal SoT.

    This is the core "Property 1" assertion: 版本号四处一致 — across
    pyproject (canonical), setup.py, config.yaml, and (when present)
    src/__init__.py.
    """
    _build_repo(
        tmp_path,
        pyproject_version=pyproject,
        setup_version=setup,
        config_version=config,
        init_kind=init_kind,
        init_version=init_version,
    )
    canonical = sync_version.read_canonical(tmp_path)

    rc = sync_version.sync(canonical, check_only=False, repo_root=tmp_path)
    assert rc == sync_version.EXIT_OK

    observed = _observed_versions(tmp_path)

    # Mandatory legs must always converge to the canonical version.
    assert observed["setup.py"] == canonical
    assert observed["sleep_classifier/config.yaml"] == canonical

    # Optional init leg behaviour:
    #   * absent → file still does not exist, reader returns None.
    #   * no_version_line → file exists but no __version__ literal;
    #     sync must NOT inject one (design §3.4.2 "如存在则同步").
    #   * with_version → existing literal must be rewritten to canonical.
    if init_kind == "absent":
        assert not (tmp_path / "src" / "__init__.py").exists()
        assert observed["src/__init__.py"] is None
    elif init_kind == "no_version_line":
        assert (tmp_path / "src" / "__init__.py").exists()
        assert observed["src/__init__.py"] is None
    else:
        assert observed["src/__init__.py"] == canonical


@pytest.mark.parametrize(
    ("pyproject", "setup", "config", "init_kind", "init_version"),
    _DRIFT_CASES,
)
def test_check_after_sync_reports_no_drift(
    tmp_path: Path,
    pyproject: str,
    setup: str,
    config: str,
    init_kind: InitKind,
    init_version: str | None,
) -> None:
    """``sync(check_only=True)`` after a write-mode ``sync`` must return 0.

    Encodes the convergence half of Property 1: a single ``sync`` call
    is enough to bring an arbitrarily inconsistent initial state into
    full agreement (no second pass required).
    """
    _build_repo(
        tmp_path,
        pyproject_version=pyproject,
        setup_version=setup,
        config_version=config,
        init_kind=init_kind,
        init_version=init_version,
    )
    canonical = sync_version.read_canonical(tmp_path)

    sync_version.sync(canonical, check_only=False, repo_root=tmp_path)

    rc = sync_version.sync(canonical, check_only=True, repo_root=tmp_path)
    assert rc == sync_version.EXIT_OK, (
        "drift detected after sync(check_only=False); the script failed "
        "to converge in one pass"
    )


def test_sync_is_idempotent_on_already_consistent_repo(tmp_path: Path) -> None:
    """A second write-mode ``sync`` must be a no-op (file mtimes preserved).

    Catches accidental unconditional rewrites that would churn the git
    working tree on every CI run even when nothing has changed.
    """
    _build_repo(
        tmp_path,
        pyproject_version="2.1.0",
        setup_version="2.1.0",
        config_version="2.1.0",
        init_kind="with_version",
        init_version="2.1.0",
    )
    canonical = sync_version.read_canonical(tmp_path)

    # First call must be a no-op (already consistent).
    paths = [
        tmp_path / "setup.py",
        tmp_path / "sleep_classifier" / "config.yaml",
        tmp_path / "src" / "__init__.py",
    ]
    before = {p: p.read_bytes() for p in paths}
    rc = sync_version.sync(canonical, check_only=False, repo_root=tmp_path)
    assert rc == sync_version.EXIT_OK
    after = {p: p.read_bytes() for p in paths}
    assert before == after, "sync rewrote already-consistent files"


def test_check_only_does_not_mutate_files(tmp_path: Path) -> None:
    """A failed ``check_only`` run must leave every manifest byte-identical."""
    _build_repo(
        tmp_path,
        pyproject_version="2.1.0",
        setup_version="2.0.3",
        config_version="1.6.0",
        init_kind="with_version",
        init_version="0.0.0",
    )
    paths = [
        tmp_path / "pyproject.toml",
        tmp_path / "setup.py",
        tmp_path / "sleep_classifier" / "config.yaml",
        tmp_path / "src" / "__init__.py",
    ]
    before = {p: p.read_bytes() for p in paths}

    rc = sync_version.sync("2.1.0", check_only=True, repo_root=tmp_path)
    assert rc == sync_version.EXIT_DRIFT

    after = {p: p.read_bytes() for p in paths}
    assert before == after, "check_only mutated at least one file"
