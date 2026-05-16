"""Tests for ``scripts/check_translations.py`` — Property 10: 翻译合规守护是真的会拦.

The script enforces design §3.2 / Requirement 2.5: the three sets of keys

* ``sleep_classifier/config.yaml``                   ``options:``
* ``sleep_classifier/translations/en.yaml``           ``configuration:``
* ``sleep_classifier/translations/zh-cn.yaml``        ``configuration:``

must be **strictly equal**. Any drift (missing or extra key in either
translation file) must make the script exit non-zero so CI blocks the
merge.

This test suite parametrises a wide grid of mutations applied to the
three YAML inputs (no mutation = baseline pass; any add / drop / rename
in either translation = expected fail) and asserts the CLI behaves the
spec contract through ``main(['--repo-root', tmp_path])``.

Validates: Requirements 2.5
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Mapping

import pytest
import yaml

# Make ``scripts/`` importable so we can call the public API directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import check_translations  # noqa: E402  (sys.path manipulation above)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

# Synthetic option keys — kept small so the parametrised matrix stays cheap
# but realistic (matches the shape of the real config.yaml ``options:`` map).
_BASE_KEYS: tuple[str, ...] = (
    "area",
    "infer_interval",
    "session_interval",
    "dry_run",
    "telemetry_enabled",
    "upgrade_notifications_enabled",
)


def _write_config_yaml(repo_root: Path, keys: Mapping[str, object]) -> None:
    """Write ``sleep_classifier/config.yaml`` with the given ``options:`` keys."""
    target = repo_root / "sleep_classifier" / "config.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": "Sleep Classifier",
        "version": "2.1.0",
        "slug": "sleep_classifier",
        "options": dict(keys),
    }
    target.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _write_translation(
    repo_root: Path,
    locale: str,
    keys: Mapping[str, Mapping[str, str]],
) -> None:
    """Write a translation pack ``configuration:`` block with the given keys."""
    target = repo_root / "sleep_classifier" / "translations" / f"{locale}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"configuration": dict(keys)}
    target.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def _build_baseline(tmp_path: Path) -> None:
    """Materialise a fully-compliant repo skeleton at ``tmp_path``."""
    options = {k: "" for k in _BASE_KEYS}
    en = {
        k: {"name": f"name-en-{k}", "description": f"desc-en-{k}"}
        for k in _BASE_KEYS
    }
    zh = {
        k: {"name": f"name-zh-{k}", "description": f"desc-zh-{k}"}
        for k in _BASE_KEYS
    }
    _write_config_yaml(tmp_path, options)
    _write_translation(tmp_path, "en", en)
    _write_translation(tmp_path, "zh-cn", zh)


# ---------------------------------------------------------------------------
# Mutation matrix
#
# Each row is a callable that, given (config, en, zh), produces a mutated
# triple. The baseline is always the in-sync version; the mutator drives the
# tested drift mode.
#
# We keep the descriptions human-readable so a CI failure log clearly tells
# the maintainer which mutation broke.
# ---------------------------------------------------------------------------


def _drop_key_from(d: dict[str, object], key: str) -> dict[str, object]:
    out = dict(d)
    out.pop(key, None)
    return out


def _add_key_to(d: dict[str, object], key: str, value: object) -> dict[str, object]:
    out = dict(d)
    out[key] = value
    return out


def _rename_key_in(
    d: dict[str, object], old: str, new: str
) -> dict[str, object]:
    out = dict(d)
    if old in out:
        out[new] = out.pop(old)
    return out


_DRIFT_CASES = [
    # --- en.yaml drops a key declared in config.yaml -----------------------
    pytest.param(
        "drop_key_from_en_first",
        lambda c, e, z: (c, _drop_key_from(e, _BASE_KEYS[0]), z),
        id="drop_first_key_from_en",
    ),
    pytest.param(
        "drop_key_from_en_middle",
        lambda c, e, z: (c, _drop_key_from(e, _BASE_KEYS[2]), z),
        id="drop_middle_key_from_en",
    ),
    pytest.param(
        "drop_key_from_en_last",
        lambda c, e, z: (c, _drop_key_from(e, _BASE_KEYS[-1]), z),
        id="drop_last_key_from_en",
    ),
    # --- zh-cn.yaml drops a key declared in config.yaml --------------------
    pytest.param(
        "drop_key_from_zh_first",
        lambda c, e, z: (c, e, _drop_key_from(z, _BASE_KEYS[0])),
        id="drop_first_key_from_zh",
    ),
    pytest.param(
        "drop_key_from_zh_middle",
        lambda c, e, z: (c, e, _drop_key_from(z, _BASE_KEYS[2])),
        id="drop_middle_key_from_zh",
    ),
    # --- en.yaml has an extra key not declared in config.yaml --------------
    pytest.param(
        "extra_key_in_en",
        lambda c, e, z: (
            c,
            _add_key_to(e, "ghost_key_en", {"name": "x", "description": "y"}),
            z,
        ),
        id="extra_key_in_en",
    ),
    # --- zh-cn.yaml has an extra key not declared in config.yaml -----------
    pytest.param(
        "extra_key_in_zh",
        lambda c, e, z: (
            c,
            e,
            _add_key_to(z, "ghost_key_zh", {"name": "x", "description": "y"}),
        ),
        id="extra_key_in_zh",
    ),
    # --- key renamed in one translation only -------------------------------
    pytest.param(
        "rename_in_en_only",
        lambda c, e, z: (
            c,
            _rename_key_in(e, _BASE_KEYS[1], "renamed_in_en"),
            z,
        ),
        id="rename_in_en_only",
    ),
    pytest.param(
        "rename_in_zh_only",
        lambda c, e, z: (
            c,
            e,
            _rename_key_in(z, _BASE_KEYS[1], "renamed_in_zh"),
            ),
        id="rename_in_zh_only",
    ),
    # --- key added to config.yaml but neither translation --------------
    pytest.param(
        "add_to_config_only",
        lambda c, e, z: (_add_key_to(c, "new_option", ""), e, z),
        id="add_to_config_only",
    ),
    # --- key dropped from config.yaml only (translations keep it) ----------
    pytest.param(
        "drop_from_config_only",
        lambda c, e, z: (
            _drop_key_from(c, _BASE_KEYS[3]),
            e,
            z,
        ),
        id="drop_from_config_only",
    ),
    # --- both translations drop the same key (config still declares it) ---
    pytest.param(
        "drop_same_key_from_both_translations",
        lambda c, e, z: (
            c,
            _drop_key_from(e, _BASE_KEYS[2]),
            _drop_key_from(z, _BASE_KEYS[2]),
        ),
        id="drop_same_key_from_both_translations",
    ),
    # --- multiple simultaneous drifts --------------------------------------
    pytest.param(
        "drop_one_each_different_keys",
        lambda c, e, z: (
            c,
            _drop_key_from(e, _BASE_KEYS[0]),
            _drop_key_from(z, _BASE_KEYS[1]),
        ),
        id="drop_one_each_different_keys",
    ),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_main_returns_zero_for_fully_compliant_repo(tmp_path: Path) -> None:
    """Baseline: three sets equal => exit 0 (regression guard)."""
    _build_baseline(tmp_path)
    rc = check_translations.main(["--repo-root", str(tmp_path)])
    assert rc == 0, f"compliant repo should pass, got exit code {rc}"


@pytest.mark.parametrize(("_label", "mutator"), _DRIFT_CASES)
def test_main_returns_nonzero_on_drift(
    tmp_path: Path,
    _label: str,
    mutator,
) -> None:
    """Any mutation that breaks set equality must produce a non-zero exit."""
    options = {k: "" for k in _BASE_KEYS}
    en = {
        k: {"name": f"name-en-{k}", "description": f"desc-en-{k}"}
        for k in _BASE_KEYS
    }
    zh = {
        k: {"name": f"name-zh-{k}", "description": f"desc-zh-{k}"}
        for k in _BASE_KEYS
    }

    new_options, new_en, new_zh = mutator(options, en, zh)
    _write_config_yaml(tmp_path, new_options)
    _write_translation(tmp_path, "en", new_en)
    _write_translation(tmp_path, "zh-cn", new_zh)

    rc = check_translations.main(["--repo-root", str(tmp_path)])
    assert rc != 0, f"drift case {_label!r} unexpectedly passed"


def test_main_returns_nonzero_when_translation_file_missing(
    tmp_path: Path,
) -> None:
    """Missing translation pack is a drift mode and must fail loudly."""
    options = {k: "" for k in _BASE_KEYS}
    en = {
        k: {"name": f"name-en-{k}", "description": f"desc-en-{k}"}
        for k in _BASE_KEYS
    }
    _write_config_yaml(tmp_path, options)
    _write_translation(tmp_path, "en", en)
    # zh-cn.yaml never written.

    rc = check_translations.main(["--repo-root", str(tmp_path)])
    assert rc != 0


def test_main_returns_nonzero_when_config_yaml_missing(tmp_path: Path) -> None:
    """Missing config.yaml is a drift mode and must fail loudly."""
    en = {
        k: {"name": f"name-en-{k}", "description": f"desc-en-{k}"}
        for k in _BASE_KEYS
    }
    zh = {
        k: {"name": f"name-zh-{k}", "description": f"desc-zh-{k}"}
        for k in _BASE_KEYS
    }
    _write_translation(tmp_path, "en", en)
    _write_translation(tmp_path, "zh-cn", zh)

    rc = check_translations.main(["--repo-root", str(tmp_path)])
    assert rc != 0


def test_load_yaml_keys_returns_set_of_top_level_keys(tmp_path: Path) -> None:
    """Direct API smoke test for the pure-functional surface used by main()."""
    _build_baseline(tmp_path)
    keys = check_translations.load_yaml_keys(
        tmp_path / "sleep_classifier" / "translations" / "en.yaml",
        "configuration",
    )
    assert keys == set(_BASE_KEYS)
