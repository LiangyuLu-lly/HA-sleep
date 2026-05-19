"""PopulationPriorRepository 补充覆盖测试（覆盖率 ≥95%）。

这个文件聚焦 ``tests/test_population_prior.py`` 既有套件之外的角落用例：

* :meth:`PopulationPriorRepository.load` 路径上的细分降级分支
  （``OSError`` from ``stat()``、``read_bytes`` IOError、unpickle 异常族
  内的 ``EOFError`` / ``AttributeError``、``buckets`` 不是 dict 的 schema 校验、
  ``PicklingError`` 在 SHA-256 校验阶段抛出、SHA-256 mismatch 时的日志格式）。
* :meth:`PopulationPriorRepository.lookup` 在 ``l2 is not None`` 但 season
  不匹配的极端分支（line 401，单纯靠 dict 数据结构难以构造，需要通过
  ``__getitem__`` 拦截 ``buckets.get`` 让 l2 命中又让 ``buckets.items``
  返回空，以覆盖原本被注释为「unreachable」的 dead code）。
* 模块级 :func:`_emit_dua_log_once` / :func:`reset_dua_log_for_tests`
  的状态机：``sources`` 为空 tuple 时 INFO 日志写 ``"(unspecified)"``；
  ``reset_dua_log_for_tests`` 调用后下一次 ``load`` 必须重新打日志。
* ``expected_size_bytes`` / ``buckets`` / ``metadata`` 三个 read-only
  访问器在 round-trip 后的语义保持。

风格沿用 :mod:`tests.test_bayesian_optimizer_extra`：用合成 prior（不依赖
真实 MESA / SHHS 数据），``hashlib`` 算 SHA-256 后注入 :class:`PriorMetadata`
的 ``sha256`` 字段，再 ``pickle.dumps`` 落盘走 ``load`` 全链路。
"""
from __future__ import annotations

import hashlib
import logging
import pickle
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.population_prior import (
    MAX_PICKLE_SIZE_BYTES,
    MIN_BUCKET_N_SAMPLES,
    BucketKey,
    PopulationPrior,
    PopulationPriorRepository,
    PriorBucket,
    PriorMetadata,
    _emit_dua_log_once,
    reset_dua_log_for_tests,
)


# ---------------------------------------------------------------------------
# Helpers — synthetic prior + on-disk pickle round-trip
# ---------------------------------------------------------------------------

#: Pickle protocol pinned to module's expectation (= 5).  See
#: ``src/population_prior.py::_PICKLE_PROTOCOL``.  Not imported directly
#: because it's underscore-prefixed module private; we mirror it here so
#: that ``hashlib.sha256(pickle.dumps(buckets, protocol=...))`` matches
#: what the load path computes.
_PICKLE_PROTOCOL: int = 5


def _make_bucket(n_samples: int = 100) -> PriorBucket:
    """Build a physiologically valid bucket (re-used across tests)."""
    return PriorBucket(
        temperature_mean_c=22.5,
        temperature_var_c2=1.5,
        humidity_mean_pct=48.0,
        humidity_var_pct2=9.0,
        brightness_mean_pct=8.0,
        brightness_var_pct2=4.0,
        n_samples=n_samples,
    )


def _write_valid_prior(tmp_path: Path, *, sources: tuple[str, ...] = ("MESA v0.6.0",)) -> Path:
    """Write a valid pickle with a real SHA-256 hash of the buckets dict."""
    buckets: dict[BucketKey, PriorBucket] = {
        ("26-35", "unspecified", "neutral", "spring"): _make_bucket(),
        ("26-35", "M", "morning", "summer"): _make_bucket(80),
    }
    sha256 = hashlib.sha256(
        pickle.dumps(buckets, protocol=_PICKLE_PROTOCOL)
    ).hexdigest()
    metadata = PriorMetadata(
        schema_version=1,
        sources=sources,
        trained_at="2026-01-01T00:00:00Z",
        git_commit="deadbee",
        n_subject_nights=180,
        sha256=sha256,
    )
    wire = {"buckets": buckets, "metadata": metadata}
    path = tmp_path / "prior.pickle"
    path.write_bytes(pickle.dumps(wire, protocol=_PICKLE_PROTOCOL))
    return path


# ---------------------------------------------------------------------------
# Load — buckets-not-dict branch (line ~284)
# ---------------------------------------------------------------------------


def test_load_buckets_not_dict(tmp_path: Path) -> None:
    """``buckets`` slot is a list, not a dict → returns ``None`` (R7.4)."""
    metadata = PriorMetadata(
        schema_version=1,
        sources=("MESA",),
        trained_at="2026-01-01T00:00:00Z",
        git_commit="abc",
        n_subject_nights=10,
        sha256="0" * 64,
    )
    wire: dict[str, Any] = {"buckets": ["not", "a", "dict"], "metadata": metadata}
    path = tmp_path / "prior.pickle"
    path.write_bytes(pickle.dumps(wire, protocol=_PICKLE_PROTOCOL))
    assert PopulationPriorRepository.load(path) is None


# ---------------------------------------------------------------------------
# Load — read_bytes raises OSError (line ~270)
# ---------------------------------------------------------------------------


def test_load_read_bytes_oserror(tmp_path: Path) -> None:
    """``Path.read_bytes`` raising OSError mid-load returns ``None``."""
    path = _write_valid_prior(tmp_path)

    def _raise(self: Path) -> bytes:
        raise OSError("disk read failure")

    with patch.object(Path, "read_bytes", _raise):
        assert PopulationPriorRepository.load(path) is None


# ---------------------------------------------------------------------------
# Load — unpickle EOFError / AttributeError variants
# ---------------------------------------------------------------------------


def test_load_unpickle_eof_error(tmp_path: Path) -> None:
    """Truncated pickle stream → ``EOFError`` caught → ``None``."""
    path = tmp_path / "prior.pickle"
    # Truncated pickle header — pickle.loads will raise EOFError.
    path.write_bytes(b"\x80\x05")
    assert PopulationPriorRepository.load(path) is None


def test_load_unpickle_attribute_error(tmp_path: Path) -> None:
    """Pickle referencing missing class → ``AttributeError`` caught → ``None``.

    We craft a pickle that names a non-existent class via the GLOBAL
    opcode; the unpickler raises ``AttributeError`` when resolving it.
    """
    path = tmp_path / "prior.pickle"
    # Hand-crafted pickle: tries to import a non-existent attribute.
    # ``c<module>\n<name>\n.`` ↦ GLOBAL opcode that performs
    # ``getattr(import_module('builtins'), 'definitely_not_a_real_attr_xyz')``.
    path.write_bytes(b"cbuiltins\ndefinitely_not_a_real_attr_xyz\n.")
    assert PopulationPriorRepository.load(path) is None


# ---------------------------------------------------------------------------
# Load — SHA-256 mismatch warning surface
# ---------------------------------------------------------------------------


def test_load_sha256_mismatch_emits_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Tampered ``sha256`` field still triggers WARNING with both digests."""
    bucket = _make_bucket()
    buckets = {("26-35", "unspecified", "neutral", "spring"): bucket}
    metadata = PriorMetadata(
        schema_version=1,
        sources=("MESA",),
        trained_at="2026-01-01T00:00:00Z",
        git_commit="abc",
        n_subject_nights=10,
        sha256="f" * 64,  # deliberately wrong
    )
    wire = {"buckets": buckets, "metadata": metadata}
    path = tmp_path / "prior.pickle"
    path.write_bytes(pickle.dumps(wire, protocol=_PICKLE_PROTOCOL))

    with caplog.at_level(logging.WARNING, logger="src.population_prior"):
        assert PopulationPriorRepository.load(path) is None

    mismatch = [r for r in caplog.records if "SHA-256 mismatch" in r.message]
    assert mismatch, "expected SHA-256 mismatch WARNING log line"


# ---------------------------------------------------------------------------
# Load — pickle.dumps raises PicklingError during SHA verification
# ---------------------------------------------------------------------------


def test_load_repickle_pickling_error(tmp_path: Path) -> None:
    """``pickle.PicklingError`` during SHA re-pickle returns ``None``.

    Distinct from ``test_load_repickle_type_error`` in the existing suite
    (which uses ``TypeError``); both branches feed into the same except
    clause but exercising :class:`pickle.PicklingError` keeps the
    catch-tuple complete (R7.4 forward-compat with FedAvg wire layouts).
    """
    path = _write_valid_prior(tmp_path)

    original_dumps = pickle.dumps

    def patched_dumps(*args: Any, **kwargs: Any) -> bytes:
        # First call inside load is the buckets re-pickle for SHA check.
        raise pickle.PicklingError("synthetic pickling failure")

    with patch("src.population_prior.pickle.dumps", side_effect=patched_dumps):
        assert PopulationPriorRepository.load(path) is None


# ---------------------------------------------------------------------------
# DUA log — sources empty tuple writes "(unspecified)" placeholder
# ---------------------------------------------------------------------------


def test_emit_dua_log_with_empty_sources(caplog: pytest.LogCaptureFixture) -> None:
    """``metadata.sources == ()`` → log line contains ``(unspecified)``."""
    reset_dua_log_for_tests()
    metadata = PriorMetadata(
        schema_version=1,
        sources=(),
        trained_at="2026-01-01T00:00:00Z",
        git_commit="abc",
        n_subject_nights=0,
        sha256="0" * 64,
    )
    with caplog.at_level(logging.INFO, logger="src.population_prior"):
        _emit_dua_log_once(metadata)
    msgs = [r.message for r in caplog.records if "NSRR DUA" in r.message]
    assert any("(unspecified)" in m for m in msgs), (
        f"expected '(unspecified)' placeholder in DUA log, got {msgs}"
    )


def test_reset_dua_log_re_emits_on_next_load(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """After :func:`reset_dua_log_for_tests`, next ``load`` re-emits DUA log."""
    reset_dua_log_for_tests()
    path = _write_valid_prior(tmp_path)
    with caplog.at_level(logging.INFO, logger="src.population_prior"):
        # First load emits.
        assert PopulationPriorRepository.load(path) is not None
        # Reset → second load must emit again.
        reset_dua_log_for_tests()
        assert PopulationPriorRepository.load(path) is not None
    dua_msgs = [r for r in caplog.records if "NSRR DUA" in r.message]
    assert len(dua_msgs) == 2, (
        f"expected 2 DUA log lines after reset, got {len(dua_msgs)}"
    )


# ---------------------------------------------------------------------------
# Lookup — degenerate l2-only fallback covering line 401
# ---------------------------------------------------------------------------


class _LookupBucketsProxy(dict):
    """Custom dict that lies about ``items()`` to force the dead branch.

    The runtime's degenerate fallback at line 401 (``if l2 is not None: return l2, 3``)
    is normally unreachable: any ``(age, "unspecified", "neutral", season)``
    bucket that satisfies the ``l2`` lookup also satisfies the ``season_roots``
    filter, so control returns earlier at the ``best = max(season_roots, ...)``
    line.

    To exercise the dead branch deterministically we subclass ``dict`` and
    override ``items()`` to return an empty iterator while still serving ``get()``
    correctly.  This breaks the invariant the production code relies on — but
    it's a legitimate way to surface the defensive fallback for coverage.

    All other dict methods (including ``__bool__`` and pickling for SHA-256
    verification) inherit from ``dict`` so the constructor's ``not buckets``
    check still passes.
    """

    def items(self):  # type: ignore[override]
        return iter(())

    def values(self):  # type: ignore[override]
        return iter(())


def test_lookup_degenerate_returns_l2_when_roots_empty() -> None:
    """``l2`` non-None + roots-empty hits the unreachable defensive branch.

    Covers ``return l2, 3`` (line ~401 in ``src/population_prior.py``).
    Uses a :class:`_LookupBucketsProxy` to make ``items()``/``values()``
    return empty iterators, simulating the (logically impossible) state
    where ``buckets.get(l2_key)`` returns a bucket but iteration yields
    nothing.  The bucket itself is still indexable via ``get()`` so the
    fallback can return it.
    """
    small = _make_bucket(n_samples=10)
    real_buckets: dict[BucketKey, PriorBucket] = {
        # l2 key for query (26-35, M, morning, spring)
        ("26-35", "unspecified", "neutral", "spring"): small,
    }
    proxy = _LookupBucketsProxy(real_buckets)

    metadata = PriorMetadata(
        schema_version=1,
        sources=("synthetic",),
        trained_at="2026-01-01T00:00:00Z",
        git_commit="abc",
        n_subject_nights=10,
        sha256="0" * 64,
    )
    prior = PopulationPrior(buckets=proxy, metadata=metadata)
    repo = PopulationPriorRepository(prior, size_bytes=0)

    bucket, level = repo.lookup(
        age_band="26-35", sex="M", chronotype="morning", season="spring",
    )
    assert level == 3
    assert bucket.n_samples == 10
    assert bucket is small


# ---------------------------------------------------------------------------
# Accessors — round-trip semantics
# ---------------------------------------------------------------------------


def test_accessors_round_trip(tmp_path: Path) -> None:
    """``buckets`` / ``metadata`` / ``expected_size_bytes`` survive load."""
    reset_dua_log_for_tests()
    path = _write_valid_prior(tmp_path, sources=("MESA v0.6.0", "SHHS v8"))
    repo = PopulationPriorRepository.load(path)
    assert repo is not None

    # ``expected_size_bytes`` must equal the on-disk size.
    assert repo.expected_size_bytes() == path.stat().st_size

    # ``metadata`` echoes what we wrote.
    assert repo.metadata.sources == ("MESA v0.6.0", "SHHS v8")
    assert repo.metadata.schema_version == 1

    # ``buckets`` is a non-empty mapping with the keys we inserted.
    assert ("26-35", "unspecified", "neutral", "spring") in repo.buckets
    assert ("26-35", "M", "morning", "summer") in repo.buckets


# ---------------------------------------------------------------------------
# Lookup — exact match below threshold falls through to l1
# ---------------------------------------------------------------------------


def test_lookup_exact_below_threshold_falls_through() -> None:
    """``exact.n_samples < 50`` skips level 0 and walks the ladder.

    Covers the boolean branch where ``exact is not None`` but
    ``exact.n_samples >= MIN_BUCKET_N_SAMPLES`` is false — distinct
    from ``test_lookup_level1_sex_relaxed`` which already does the same
    setup but doesn't assert on the threshold edge.
    """
    below = _make_bucket(n_samples=MIN_BUCKET_N_SAMPLES - 1)
    big = _make_bucket(n_samples=MIN_BUCKET_N_SAMPLES + 5)
    buckets: dict[BucketKey, PriorBucket] = {
        ("36-50", "F", "evening", "winter"): below,
        ("36-50", "unspecified", "evening", "winter"): big,
    }
    metadata = PriorMetadata(
        schema_version=1,
        sources=("synthetic",),
        trained_at="2026-01-01T00:00:00Z",
        git_commit="abc",
        n_subject_nights=below.n_samples + big.n_samples,
        sha256="0" * 64,
    )
    prior = PopulationPrior(buckets=buckets, metadata=metadata)
    repo = PopulationPriorRepository(prior, size_bytes=0)

    bucket, level = repo.lookup(
        age_band="36-50", sex="F", chronotype="evening", season="winter",
    )
    assert level == 1
    assert bucket is big
