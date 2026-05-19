"""校验 v3.0.0 训练产物的尺寸与 SHA-256 完整性（CI / prepare 双用）.

This script is the build-time guard for the two embedded model artifacts
that ship inside the Home Assistant Add-on image:

* ``sleep_classifier/rootfs/training_config/population_prior.pickle``
  — Hierarchical Bayesian prior trained on MESA + SHHS PSG data
  (Requirement 7.3, ≤ 8 MB, sha256 must match the value embedded in the
  pickle's :class:`PriorMetadata`).
* ``sleep_classifier/rootfs/training_config/stage_predictor.onnx``
  — INT8 quantised end-side stage transformer (Requirement 9.2, ≤ 80 KB).

Usage::

    # 本地 prepare.sh / prepare.bat — missing files are tolerated (WARN)
    python scripts/check_artifacts.py

    # CI — missing files cause exit 1
    python scripts/check_artifacts.py --strict

The script intentionally has **zero third-party dependencies** (no numpy /
scipy / onnxruntime). It uses only :mod:`pickle`, :mod:`hashlib`,
:mod:`pathlib`, :mod:`argparse`, :mod:`sys` from the standard library so
that it can run inside the minimal CI environment and as part of the
pre-image-build ``prepare`` step before runtime deps are installed.

Exit codes
----------

* ``0`` — all artifacts pass, OR (non-strict) any artifact is missing.
* ``1`` — any size / sha256 check failed (always fatal), OR (strict) any
  artifact is missing.

Acceptance criteria covered: Requirements 7.3, 7.5, 9.2.
"""
from __future__ import annotations

import argparse
import hashlib
import pickle
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Artifact contract — keep aligned with requirements.md §7.3 / §9.2 and
# design.md §3.1 / §3.4. The two paths below are the *image-side* artifact
# locations that get COPY-ed into the Add-on container at build time.
# ---------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
ROOTFS_TRAINING_CONFIG: Path = (
    REPO_ROOT / "sleep_classifier" / "rootfs" / "training_config"
)

PRIOR_PATH: Path = ROOTFS_TRAINING_CONFIG / "population_prior.pickle"
PRIOR_MAX_BYTES: int = 8 * 1024 * 1024  # R7.3 — hard cap, build fails above

ONNX_PATH: Path = ROOTFS_TRAINING_CONFIG / "stage_predictor.onnx"
ONNX_MAX_BYTES: int = 80 * 1024  # R9.2 — INT8 quantised target ≤ 80 KB


# ---------------------------------------------------------------------------
# Make `src.population_prior` reachable when unpickling. The pickle may
# reference the `PopulationPrior` / `PriorMetadata` dataclasses by their
# `(module, qualname)` strings; pickle resolves them via __import__. Adding
# the repo root to sys.path is enough because the package layout is flat.
# This still keeps direct imports stdlib-only (Requirement 12.4 friendly).
# ---------------------------------------------------------------------------

_REPO_ROOT_STR = str(REPO_ROOT)
if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)


def _format_size(n: int) -> str:
    """Pretty-print byte count with adaptive unit (B / KB / MB)."""
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.2f} KB"
    return f"{n} B"


def _get_attr_or_key(obj: Any, name: str) -> Any:
    """Duck-typed accessor that supports both dataclass attrs and mappings.

    The forward-compat wire format (design §3.1.3) only relies on
    ``dataclass + dict + tuple + str/int/float``; this helper lets the
    checker work whether the pickle was produced from a frozen dataclass
    instance or from a plain dict.
    """
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def check_population_prior(path: Path) -> tuple[bool, str]:
    """Validate ``population_prior.pickle`` size + embedded SHA-256.

    The embedded SHA-256 (``metadata.sha256``) is recomputed against
    ``pickle.dumps(buckets_dict, protocol=pickle.HIGHEST_PROTOCOL)`` to
    match the layout chosen by ``PopulationPrior`` (task 2.1).

    :returns: ``(ok, message)`` — ``ok`` is ``True`` only when both the
              size cap and the sha256 check pass.
    """
    size = path.stat().st_size
    if size > PRIOR_MAX_BYTES:
        return False, (
            f"size {_format_size(size)} exceeds limit "
            f"{_format_size(PRIOR_MAX_BYTES)} (R7.3)"
        )

    try:
        with path.open("rb") as fh:
            obj = pickle.load(fh)
    except Exception as exc:  # noqa: BLE001 — surface any unpickle error
        return False, f"pickle.load failed: {exc.__class__.__name__}: {exc}"

    buckets = _get_attr_or_key(obj, "buckets")
    metadata = _get_attr_or_key(obj, "metadata")
    if buckets is None or metadata is None:
        return False, (
            "pickle layout invalid: expected attributes/keys "
            "'buckets' and 'metadata'"
        )

    embedded_sha = _get_attr_or_key(metadata, "sha256")
    if not isinstance(embedded_sha, str) or len(embedded_sha) != 64:
        return False, "metadata.sha256 missing or malformed (expected 64-char hex)"

    # Match task 2.1: sha256 is computed over pickle bytes of the *buckets*
    # mapping using HIGHEST_PROTOCOL (forward-compat with v3.1.0 federated
    # aggregation wire format).
    buckets_dict = buckets if isinstance(buckets, dict) else dict(buckets)
    digest = hashlib.sha256(
        pickle.dumps(buckets_dict, protocol=pickle.HIGHEST_PROTOCOL)
    ).hexdigest()

    if digest != embedded_sha:
        return False, (
            f"sha256 mismatch: embedded={embedded_sha[:16]}… "
            f"recomputed={digest[:16]}… (R7.3)"
        )

    return True, f"OK ({_format_size(size)}, sha256 {embedded_sha[:16]}…)"


def check_stage_predictor(path: Path) -> tuple[bool, str]:
    """Validate ``stage_predictor.onnx`` size only (R9.2).

    Functional / inference-time validation (latency, output shape) is the
    job of ``scripts/train_stage_predictor.py``'s post-train smoke test —
    here we only enforce the static size cap.
    """
    size = path.stat().st_size
    if size > ONNX_MAX_BYTES:
        return False, (
            f"size {_format_size(size)} exceeds limit "
            f"{_format_size(ONNX_MAX_BYTES)} (R9.2)"
        )
    return True, f"OK ({_format_size(size)})"


def _emit_missing(path: Path, *, strict: bool) -> int:
    """Print a missing-file diagnostic and return the appropriate rc."""
    try:
        rel = path.relative_to(REPO_ROOT)
    except ValueError:
        rel = path
    if strict:
        print(
            f"check_artifacts: FAIL {rel}: missing (strict mode)",
            file=sys.stderr,
        )
        return 1
    print(
        f"check_artifacts: WARN {rel}: missing — produced by training "
        f"scripts (tasks 10.1 / 10.2). Skipping in non-strict mode.",
        file=sys.stderr,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns ``0`` on success, ``1`` on any size / sha256 violation, and (in
    ``--strict`` mode) ``1`` for any missing artifact too.
    """
    parser = argparse.ArgumentParser(
        prog="check_artifacts",
        description=(
            "Validate v3.0.0 training artifact size + sha256 integrity. "
            "Use --strict in CI; default tolerates missing files for local "
            "prepare runs."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "Treat missing artifacts as a hard failure (exit 1). Without "
            "this flag missing files are reported as WARN and the script "
            "exits 0 — useful for local 'prepare' runs before the training "
            "pipeline has produced any artifacts."
        ),
    )
    args = parser.parse_args(argv)

    overall_rc = 0

    # 1. population_prior.pickle (R7.3)
    if not PRIOR_PATH.is_file():
        overall_rc = max(overall_rc, _emit_missing(PRIOR_PATH, strict=args.strict))
    else:
        ok, message = check_population_prior(PRIOR_PATH)
        rel = PRIOR_PATH.relative_to(REPO_ROOT)
        if ok:
            print(f"check_artifacts: PASS {rel}: {message}")
        else:
            print(f"check_artifacts: FAIL {rel}: {message}", file=sys.stderr)
            overall_rc = 1

    # 2. stage_predictor.onnx (R9.2)
    if not ONNX_PATH.is_file():
        overall_rc = max(overall_rc, _emit_missing(ONNX_PATH, strict=args.strict))
    else:
        ok, message = check_stage_predictor(ONNX_PATH)
        rel = ONNX_PATH.relative_to(REPO_ROOT)
        if ok:
            print(f"check_artifacts: PASS {rel}: {message}")
        else:
            print(f"check_artifacts: FAIL {rel}: {message}", file=sys.stderr)
            overall_rc = 1

    if overall_rc == 0:
        print("check_artifacts: all checks passed.")
    return overall_rc


if __name__ == "__main__":  # pragma: no cover - CLI passthrough
    raise SystemExit(main())
