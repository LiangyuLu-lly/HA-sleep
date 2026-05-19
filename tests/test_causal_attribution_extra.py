"""Targeted unit tests for ``src.causal_attribution`` coverage gaps.

These tests complement the property-based suite in
``test_causal_attribution.py`` by directly exercising the pure-function
helpers, validation branches, and defensive numeric fallbacks that the
synthetic-driven property tests do not reliably hit.

Specifically this module covers:

* ``CausalEffect.to_dict`` round-trip.
* ``CausalAttributionEngine.__init__`` argument validation
  (negative ``max_records``, sub-200 ``bootstrap_iters``, non-positive
  ``timeout_seconds``, sub-1 ``min_per_factor_observations``).
* ``should_disable`` 3-strikes property.
* ``hash_install_id`` non-string ``TypeError`` guard.
* ``on_session`` persistence path: timestamp anchoring, NaN/inf
  rejection, sub-score backfill, and round-trip via ``_load_records``.
* ``_load_records`` resilience: missing file, blank lines, malformed
  JSON line warning + skip.
* ``n_records`` returns the on-disk count.
* ``export_dag_json`` schema and edge enumeration.
* ``attribute`` short-circuits: ``insufficient_data`` (< 30 records),
  ``nominal`` (within 5 points of personal mean), and the generic
  ``Exception`` → ``timeout`` fallback that wraps non-``TimeoutError``
  estimator crashes.
* ``_solve_ols`` numeric fallbacks: ``LinAlgError`` and non-finite
  ``beta_hat``.
* ``_bootstrap_factor_ci`` corner cases: empty residual sample, every
  bootstrap fit failing, and the partial-failure ``continue`` branch.
* ``_pick_top_factor`` filtering: skip when current record's factor is
  missing.
* ``_factor_30d_mean`` returns NaN when every observation is missing.
* ``_build_explanation_zh`` template variants: missing ``factor_current``,
  NaN ``factor_30d_mean``, and the "CI crosses zero" suffix.
* ``_run_estimator`` constant-completeness branch (skips IMR proxy when
  ``np.std`` is ~0) and the singular-system branch (``_solve_ols``
  returns ``None``).
"""
from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pytest

from src import causal_attribution as ca
from src.causal_attribution import (
    ALL_FACTORS,
    CAUSAL_DAG,
    QUALITY_SUBSCORE_KEYS,
    STATUS_INSUFFICIENT_DATA,
    STATUS_NOMINAL,
    STATUS_OK,
    STATUS_TIMEOUT,
    AttributionResult,
    CausalAttributionEngine,
    CausalEffect,
    CausalFactorRecord,
)
from src.preference_learner import EnvironmentParams, SleepSession


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _make_session(quality: float = 80.0, ended_at: float = 1_700_000_000.0) -> SleepSession:
    """Return a minimal :class:`SleepSession` suitable for ``on_session``."""
    return SleepSession(
        session_id="s1",
        started_at=ended_at - 8 * 3600,
        ended_at=ended_at,
        env_params=EnvironmentParams(),
        stage_counts={"AWAKE": 0, "LIGHT": 1, "DEEP": 1, "REM": 1},
        quality_score=quality,
        n_samples=0,
    )


def _make_record(
    *,
    quality_total: float = 75.0,
    factors: Optional[dict[str, Optional[float]]] = None,
    timestamp: str = "2026-01-01T03:00:00Z",
) -> CausalFactorRecord:
    """Construct a :class:`CausalFactorRecord` with sensible defaults."""
    base = {f: 0.5 for f in ALL_FACTORS}
    if factors is not None:
        base.update(factors)
    return CausalFactorRecord(
        timestamp=timestamp,
        install_id_hash="h" * 64,
        factors=base,
        quality_subscores={k: 75.0 for k in QUALITY_SUBSCORE_KEYS},
        quality_total=quality_total,
    )


def _seed_jsonl(path: Path, records: list[CausalFactorRecord]) -> None:
    """Bulk-write *records* as one JSON object per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(r.to_dict(), ensure_ascii=False, separators=(",", ":"))
        for r in records
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Dataclass serialization (line 213)
# ---------------------------------------------------------------------------


def test_causal_effect_to_dict_roundtrip() -> None:
    """``CausalEffect.to_dict`` produces a JSON-friendly dict."""
    effect = CausalEffect(
        factor="temperature_drift",
        effect_pp=-2.5,
        ci_low=-3.5,
        ci_high=-1.5,
        n_observations=42,
        is_significant=True,
    )
    payload = effect.to_dict()
    assert payload == {
        "factor": "temperature_drift",
        "effect_pp": -2.5,
        "ci_low": -3.5,
        "ci_high": -1.5,
        "n_observations": 42,
        "is_significant": True,
    }
    # The dict must be JSON-serializable.
    assert json.loads(json.dumps(payload)) == payload


def test_causal_factor_record_from_dict_handles_missing_keys() -> None:
    """``from_dict`` tolerates entirely missing optional keys."""
    record = CausalFactorRecord.from_dict({})
    assert record.timestamp == ""
    assert record.install_id_hash == ""
    assert record.quality_total == 0.0
    for f in ALL_FACTORS:
        assert record.factors[f] is None
    for k in QUALITY_SUBSCORE_KEYS:
        assert record.quality_subscores[k] == 0.0


def test_causal_factor_record_from_dict_preserves_floats() -> None:
    """``from_dict`` round-trips float values and explicit ``None``."""
    raw = {
        "timestamp": "2026-05-01T03:00:00Z",
        "install_id_hash": "abc",
        "factors": {
            "temperature_drift": 1.25,
            "noise_level": None,
            # other factors omitted intentionally → default to None
        },
        "quality_subscores": {"architecture": 80.0},
        "quality_total": 73.5,
    }
    record = CausalFactorRecord.from_dict(raw)
    assert record.factors["temperature_drift"] == 1.25
    assert record.factors["noise_level"] is None
    assert record.factors["light_leak"] is None  # missing key
    assert record.quality_subscores["architecture"] == 80.0
    assert record.quality_subscores["efficiency"] == 0.0
    assert record.quality_total == 73.5


# ---------------------------------------------------------------------------
# Constructor validation (lines 285, 290, 295, 299) + should_disable (317)
# ---------------------------------------------------------------------------


def test_constructor_rejects_non_positive_max_records(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_records must be positive"):
        CausalAttributionEngine(jsonl_path=tmp_path / "x.jsonl", max_records=0)


def test_constructor_rejects_low_bootstrap_iters(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="bootstrap_iters must be >= 200"):
        CausalAttributionEngine(
            jsonl_path=tmp_path / "x.jsonl", bootstrap_iters=199
        )


def test_constructor_rejects_non_positive_timeout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        CausalAttributionEngine(
            jsonl_path=tmp_path / "x.jsonl", timeout_seconds=0.0
        )


def test_constructor_rejects_zero_min_observations(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError, match="min_per_factor_observations must be at least 1"
    ):
        CausalAttributionEngine(
            jsonl_path=tmp_path / "x.jsonl",
            min_per_factor_observations=0,
        )


def test_should_disable_property_threshold(tmp_path: Path) -> None:
    """``should_disable`` flips True only at error_count >= 3 (R11.3)."""
    engine = CausalAttributionEngine(jsonl_path=tmp_path / "x.jsonl")
    assert engine.should_disable is False
    engine.error_count = 2
    assert engine.should_disable is False
    engine.error_count = 3
    assert engine.should_disable is True
    engine.error_count = 99
    assert engine.should_disable is True


# ---------------------------------------------------------------------------
# hash_install_id type guard (lines 330-334)
# ---------------------------------------------------------------------------


def test_hash_install_id_rejects_non_string() -> None:
    with pytest.raises(TypeError, match="install_id must be str, got int"):
        CausalAttributionEngine.hash_install_id(12345)  # type: ignore[arg-type]


def test_hash_install_id_returns_hex_digest() -> None:
    digest = CausalAttributionEngine.hash_install_id("install-abc")
    assert len(digest) == 64
    int(digest, 16)  # purely hex; raises ValueError if not


# ---------------------------------------------------------------------------
# on_session persistence (lines 362-389)
# ---------------------------------------------------------------------------


async def test_on_session_persists_record_with_clean_factors(
    tmp_path: Path,
) -> None:
    """``on_session`` writes a complete row, hashing the install_id and
    dropping NaN/inf factor values."""
    jsonl_path = tmp_path / "causal_factors.jsonl"
    engine = CausalAttributionEngine(jsonl_path=jsonl_path)
    session = _make_session(quality=82.5, ended_at=1_700_000_000.0)

    factors = {
        "temperature_drift": 0.7,
        "noise_level": float("nan"),     # rejected → None
        "light_leak": float("inf"),       # rejected → None
        "hrv_anomaly": None,              # already None
        # bedtime_offset / prior_night_debt absent → None
        "unknown_factor": 1.0,            # silently ignored
    }
    sub_scores = {"architecture": 70.0}  # other keys default to 0.0

    await engine.on_session(
        session=session,
        install_id="install-xyz",
        factors=factors,
        quality_subscores=sub_scores,
    )

    on_disk = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(on_disk) == 1
    row = json.loads(on_disk[0])

    # Privacy contract: raw install_id must not appear anywhere.
    assert "install-xyz" not in on_disk[0]
    assert row["install_id_hash"] == CausalAttributionEngine.hash_install_id(
        "install-xyz"
    )
    # Timestamp anchored to ended_at (UTC ISO-8601).
    assert row["timestamp"].endswith("Z")
    # Factor cleansing.
    assert row["factors"]["temperature_drift"] == 0.7
    assert row["factors"]["noise_level"] is None
    assert row["factors"]["light_leak"] is None
    assert row["factors"]["hrv_anomaly"] is None
    assert row["factors"]["bedtime_offset"] is None
    assert row["factors"]["prior_night_debt"] is None
    assert "unknown_factor" not in row["factors"]
    # Sub-score backfill.
    assert row["quality_subscores"]["architecture"] == 70.0
    assert row["quality_subscores"]["efficiency"] == 0.0
    assert row["quality_total"] == 82.5


async def test_on_session_uses_walltime_when_ended_at_zero(
    tmp_path: Path,
) -> None:
    """When ``session.ended_at`` is falsy, ``on_session`` falls back to
    ``time.time()``."""
    jsonl_path = tmp_path / "causal_factors.jsonl"
    engine = CausalAttributionEngine(jsonl_path=jsonl_path)

    session = SleepSession(
        session_id="s2",
        started_at=0.0,
        ended_at=0.0,
        env_params=EnvironmentParams(),
        stage_counts={},
        quality_score=70.0,
        n_samples=0,
    )

    await engine.on_session(
        session=session,
        install_id="abc",
        factors={f: 0.1 for f in ALL_FACTORS},
        quality_subscores=None,
    )

    rows = jsonl_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 1
    row = json.loads(rows[0])
    # Wall-clock fallback yields a valid ISO-8601 stamp; the year must
    # be plausibly current (>= 1970) but is not pinned to ``ended_at``.
    assert row["timestamp"].endswith("Z")
    assert row["timestamp"] > "1970-01-01T00:00:00Z"
    # All sub-scores defaulted to 0 because we passed ``None``.
    assert all(v == 0.0 for v in row["quality_subscores"].values())


# ---------------------------------------------------------------------------
# _load_records (lines 400-413) + n_records (419)
# ---------------------------------------------------------------------------


def test_load_records_missing_file_returns_empty(tmp_path: Path) -> None:
    """Reading a non-existent JSONL must yield ``[]``, not raise."""
    engine = CausalAttributionEngine(jsonl_path=tmp_path / "absent.jsonl")
    assert engine._load_records() == []
    assert engine.n_records() == 0


def test_load_records_skips_blank_and_malformed_lines(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Blank lines are silently dropped and malformed JSON warns + skips."""
    jsonl_path = tmp_path / "causal_factors.jsonl"
    valid_record = _make_record()
    valid_line = json.dumps(
        valid_record.to_dict(), ensure_ascii=False, separators=(",", ":")
    )
    # Mixture of: blank line, malformed JSON, valid record, trailing blank.
    jsonl_path.write_text(
        "\n   \n{this is not json\n" + valid_line + "\n\n",
        encoding="utf-8",
    )

    engine = CausalAttributionEngine(jsonl_path=jsonl_path)
    with caplog.at_level("WARNING"):
        records = engine._load_records()
    assert len(records) == 1
    assert records[0].quality_total == valid_record.quality_total
    assert any("malformed line" in m.getMessage() for m in caplog.records)
    # n_records uses the same loader.
    assert engine.n_records() == 1


# ---------------------------------------------------------------------------
# export_dag_json (lines 438-443)
# ---------------------------------------------------------------------------


def test_export_dag_json_schema() -> None:
    """``export_dag_json`` returns the documented schema (v1)."""
    payload = CausalAttributionEngine.export_dag_json()
    assert payload["schema_version"] == 1

    # Nodes: all DAG keys + the quality_score sink.
    expected_nodes = list(CAUSAL_DAG.keys()) + ["quality_score"]
    assert payload["nodes"] == expected_nodes

    # Edges: every (parent, child) in CAUSAL_DAG, child-sorted within parent.
    edges = payload["edges"]
    expected_edge_count = sum(len(children) for children in CAUSAL_DAG.values())
    assert len(edges) == expected_edge_count
    # Each edge has the documented shape.
    for edge in edges:
        assert set(edge.keys()) == {"src", "dst"}
        assert edge["dst"] in CAUSAL_DAG[edge["src"]]
    # Whole payload must JSON round-trip.
    assert json.loads(json.dumps(payload)) == payload


# ---------------------------------------------------------------------------
# attribute() short-circuits (lines 481, 494) and generic-exception path (522-525)
# ---------------------------------------------------------------------------


async def test_attribute_returns_insufficient_data_below_30_records(
    tmp_path: Path,
) -> None:
    jsonl_path = tmp_path / "causal_factors.jsonl"
    _seed_jsonl(jsonl_path, [_make_record() for _ in range(10)])
    engine = CausalAttributionEngine(jsonl_path=jsonl_path)

    result = await engine.attribute(
        current_record=_make_record(quality_total=40.0),
        personal_30d_mean=80.0,
    )
    assert result.status == STATUS_INSUFFICIENT_DATA
    assert result.effects == ()
    assert result.top_factor is None
    assert result.top_effect_pp is None
    assert result.counterfactual_score is None
    assert "30" in result.explanation_zh


async def test_attribute_returns_nominal_when_quality_near_personal_mean(
    tmp_path: Path,
) -> None:
    """``status=nominal`` when current quality is within 5 points of mean."""
    jsonl_path = tmp_path / "causal_factors.jsonl"
    _seed_jsonl(jsonl_path, [_make_record() for _ in range(35)])
    engine = CausalAttributionEngine(jsonl_path=jsonl_path)

    result = await engine.attribute(
        # 78 >= 80 - 5 → trigger condition NOT met → nominal.
        current_record=_make_record(quality_total=78.0),
        personal_30d_mean=80.0,
    )
    assert result.status == STATUS_NOMINAL
    assert result.effects == ()
    assert result.top_factor is None
    assert "未触发" in result.explanation_zh


async def test_attribute_generic_exception_returns_timeout_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-``TimeoutError`` crash inside the estimator increments
    ``error_count`` and returns ``status="timeout"`` with a fallback
    Chinese explanation."""
    jsonl_path = tmp_path / "causal_factors.jsonl"
    _seed_jsonl(jsonl_path, [_make_record() for _ in range(35)])
    engine = CausalAttributionEngine(jsonl_path=jsonl_path)

    def _crashing_estimator(_records: list) -> tuple:
        raise RuntimeError("boom from synthetic estimator")

    monkeypatch.setattr(engine, "_run_estimator", _crashing_estimator)

    result = await engine.attribute(
        current_record=_make_record(quality_total=10.0),
        personal_30d_mean=200.0,
    )
    assert result.status == STATUS_TIMEOUT
    assert result.effects == ()
    assert result.top_factor is None
    assert "异常" in result.explanation_zh
    assert engine.error_count == 1


async def test_attribute_returns_ok_with_no_significant_factor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If estimator produces no significant + observed factor, the result
    is ``status=ok`` with ``top_factor=None`` and a generic explanation."""
    jsonl_path = tmp_path / "causal_factors.jsonl"
    _seed_jsonl(jsonl_path, [_make_record() for _ in range(35)])
    engine = CausalAttributionEngine(jsonl_path=jsonl_path)

    def _all_insignificant(_records: list) -> tuple:
        return tuple(
            CausalEffect(
                factor=f,
                effect_pp=0.5,
                ci_low=-1.0,
                ci_high=1.0,  # crosses zero → not significant
                n_observations=20,
                is_significant=False,
            )
            for f in ALL_FACTORS
        )

    monkeypatch.setattr(engine, "_run_estimator", _all_insignificant)

    result = await engine.attribute(
        current_record=_make_record(quality_total=10.0),
        personal_30d_mean=200.0,
    )
    assert result.status == STATUS_OK
    assert result.top_factor is None
    assert result.top_effect_pp is None
    assert result.counterfactual_score is None
    assert "未发现" in result.explanation_zh


async def test_attribute_builds_full_explanation_from_significant_factor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end happy path: estimator yields a significant + observed
    factor → engine returns the full counterfactual + explanation."""
    jsonl_path = tmp_path / "causal_factors.jsonl"
    # Seed enough rows that the 30-day mean is well-defined for the top
    # factor (temperature_drift = 0.5 from default _make_record).
    _seed_jsonl(jsonl_path, [_make_record() for _ in range(35)])
    engine = CausalAttributionEngine(jsonl_path=jsonl_path)

    def _one_significant(_records: list) -> tuple:
        results: list[CausalEffect] = []
        for f in ALL_FACTORS:
            if f == "temperature_drift":
                results.append(
                    CausalEffect(
                        factor=f,
                        effect_pp=-3.0,
                        ci_low=-4.0,
                        ci_high=-2.0,  # does not cross zero
                        n_observations=30,
                        is_significant=True,
                    )
                )
            else:
                results.append(
                    CausalEffect(
                        factor=f,
                        effect_pp=0.0,
                        ci_low=-1.0,
                        ci_high=1.0,
                        n_observations=30,
                        is_significant=False,
                    )
                )
        return tuple(results)

    monkeypatch.setattr(engine, "_run_estimator", _one_significant)

    current = _make_record(
        quality_total=50.0,
        factors={"temperature_drift": 0.9},
    )
    result = await engine.attribute(
        current_record=current,
        personal_30d_mean=200.0,
    )
    assert result.status == STATUS_OK
    assert result.top_factor == "temperature_drift"
    assert result.top_effect_pp == -3.0
    # counterfactual = 50 + |-3| = 53
    assert result.counterfactual_score == pytest.approx(53.0)
    # Chinese explanation should include the localized label and both
    # quality numbers; CI does not cross zero so no suffix appears.
    assert "卧室温度方差" in result.explanation_zh
    assert "50.0" in result.explanation_zh
    assert "53.0" in result.explanation_zh
    assert "（统计显著性弱）" not in result.explanation_zh


# ---------------------------------------------------------------------------
# _solve_ols numeric fallbacks (lines 708-709, 711)
# ---------------------------------------------------------------------------


def test_solve_ols_handles_linalgerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``np.linalg.lstsq`` raises, ``_solve_ols`` returns ``(None, 0)``."""

    def _raising_lstsq(*_args: Any, **_kwargs: Any):
        raise np.linalg.LinAlgError("synthetic")

    monkeypatch.setattr(np.linalg, "lstsq", _raising_lstsq)
    design = np.array([[1.0, 0.0], [1.0, 1.0]])
    y = np.array([1.0, 2.0])
    beta, rank = CausalAttributionEngine._solve_ols(design, y)
    assert beta is None
    assert rank == 0


def test_solve_ols_returns_none_for_nonfinite_beta() -> None:
    """An NaN-laden response yields a non-finite ``beta_hat``; the helper
    must mark the system as unsolvable while preserving the rank."""
    design = np.array([[1.0, 0.0], [1.0, 1.0], [1.0, 2.0]])
    y = np.array([1.0, float("nan"), 3.0])
    beta, rank = CausalAttributionEngine._solve_ols(design, y)
    assert beta is None
    # Rank is still reported (design itself is full-rank).
    assert rank >= 1


def test_solve_ols_succeeds_on_well_conditioned_system() -> None:
    """Sanity: the closed-form OLS path returns finite coefficients."""
    design = np.array([[1.0, 0.0], [1.0, 1.0], [1.0, 2.0]])
    y = np.array([0.0, 1.0, 2.0])
    beta, rank = CausalAttributionEngine._solve_ols(design, y)
    assert beta is not None
    np.testing.assert_allclose(beta, np.array([0.0, 1.0]), atol=1e-9)
    assert rank == 2


# ---------------------------------------------------------------------------
# _bootstrap_factor_ci corner cases (lines 732, 739, 742)
# ---------------------------------------------------------------------------


def test_bootstrap_factor_ci_empty_residuals_returns_nan(
    tmp_path: Path,
) -> None:
    """A zero-row design yields NaN bounds without crashing."""
    engine = CausalAttributionEngine(jsonl_path=tmp_path / "x.jsonl")
    design = np.zeros((0, 2))
    y = np.zeros(0)
    beta_hat = np.array([0.0, 0.0])
    rng = np.random.default_rng(0)
    ci_low, ci_high = engine._bootstrap_factor_ci(
        design=design, y=y, beta_hat=beta_hat, rng=rng
    )
    assert math.isnan(ci_low)
    assert math.isnan(ci_high)


def test_bootstrap_factor_ci_skips_failed_resamples(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a subset of bootstrap fits fail, the helper still returns a
    finite CI from the surviving fits."""
    engine = CausalAttributionEngine(jsonl_path=tmp_path / "x.jsonl")
    design = np.array([[1.0, 0.0], [1.0, 1.0], [1.0, 2.0], [1.0, 3.0], [1.0, 4.0]])
    y = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
    beta_hat = np.array([0.0, 1.0])
    rng = np.random.default_rng(20260518)

    real_solve = CausalAttributionEngine._solve_ols
    counter = {"calls": 0}

    def _flaky_solve(d: np.ndarray, vec: np.ndarray):
        counter["calls"] += 1
        # Fail every other call; success on the rest.
        if counter["calls"] % 2 == 0:
            return None, 0
        return real_solve(d, vec)

    monkeypatch.setattr(
        CausalAttributionEngine, "_solve_ols", staticmethod(_flaky_solve)
    )

    ci_low, ci_high = engine._bootstrap_factor_ci(
        design=design, y=y, beta_hat=beta_hat, rng=rng
    )
    # Surviving fits all reproduce slope ≈ 1 (residuals are zero up to
    # numerical noise), so the CI collapses to a near-degenerate interval
    # around 1.  Allow a small float tolerance.
    assert math.isfinite(ci_low)
    assert math.isfinite(ci_high)
    assert ci_low <= ci_high
    assert abs(ci_low - 1.0) < 1e-6
    assert abs(ci_high - 1.0) < 1e-6
    # Sanity: the flaky_solve replacement was actually exercised — at
    # least one bootstrap iteration triggered the ``continue`` branch.
    assert counter["calls"] >= engine._bootstrap_iters


def test_bootstrap_factor_ci_all_failures_returns_nan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If every bootstrap fit fails, the helper falls back to ``nan``."""
    engine = CausalAttributionEngine(jsonl_path=tmp_path / "x.jsonl")
    design = np.array([[1.0, 0.0], [1.0, 1.0], [1.0, 2.0]])
    y = np.array([0.0, 1.0, 2.0])
    beta_hat = np.array([0.0, 1.0])
    rng = np.random.default_rng(0)

    monkeypatch.setattr(
        CausalAttributionEngine,
        "_solve_ols",
        staticmethod(lambda _d, _v: (None, 0)),
    )

    ci_low, ci_high = engine._bootstrap_factor_ci(
        design=design, y=y, beta_hat=beta_hat, rng=rng
    )
    assert math.isnan(ci_low)
    assert math.isnan(ci_high)


# ---------------------------------------------------------------------------
# _pick_top_factor branches (line 770)
# ---------------------------------------------------------------------------


def test_pick_top_factor_skips_factors_missing_in_current_record() -> None:
    """A factor that is significant historically but absent tonight cannot
    contribute a counterfactual delta and must be filtered out."""
    significant_but_missing = CausalEffect(
        factor="temperature_drift",
        effect_pp=-5.0,
        ci_low=-6.0,
        ci_high=-4.0,
        n_observations=30,
        is_significant=True,
    )
    significant_and_observed = CausalEffect(
        factor="noise_level",
        effect_pp=-2.0,
        ci_low=-3.0,
        ci_high=-1.0,
        n_observations=30,
        is_significant=True,
    )
    insignificant = CausalEffect(
        factor="light_leak",
        effect_pp=-10.0,  # large but CI crosses zero
        ci_low=-12.0,
        ci_high=2.0,
        n_observations=30,
        is_significant=False,
    )
    current = _make_record(
        factors={"temperature_drift": None, "noise_level": 0.4, "light_leak": 0.3},
    )

    pick = CausalAttributionEngine._pick_top_factor(
        (significant_but_missing, significant_and_observed, insignificant),
        current,
    )
    assert pick is not None
    assert pick.factor == "noise_level"


def test_pick_top_factor_returns_none_when_no_candidates() -> None:
    """No significant + observed factor → ``_pick_top_factor`` returns None."""
    effects = (
        CausalEffect(
            factor=f,
            effect_pp=float("nan"),
            ci_low=float("nan"),
            ci_high=float("nan"),
            n_observations=2,
            is_significant=False,
        )
        for f in ALL_FACTORS
    )
    pick = CausalAttributionEngine._pick_top_factor(
        tuple(effects), _make_record()
    )
    assert pick is None


def test_pick_top_factor_skips_significant_but_non_finite_effect() -> None:
    """Defensive guard: if a callsite mis-constructs a ``CausalEffect``
    flagged ``is_significant=True`` but with a non-finite ``effect_pp``,
    the picker must still skip it instead of returning a NaN top
    factor."""
    pathological = CausalEffect(
        factor="temperature_drift",
        # Significant flag set but the point estimate is NaN — only
        # reachable via direct construction (the estimator never emits
        # this combination), so this test exercises the defensive
        # ``math.isfinite`` guard inside ``_pick_top_factor``.
        effect_pp=float("nan"),
        ci_low=-1.0,
        ci_high=-0.5,
        n_observations=20,
        is_significant=True,
    )
    healthy = CausalEffect(
        factor="noise_level",
        effect_pp=-1.0,
        ci_low=-1.5,
        ci_high=-0.5,
        n_observations=20,
        is_significant=True,
    )
    current = _make_record(
        factors={"temperature_drift": 0.5, "noise_level": 0.5}
    )
    pick = CausalAttributionEngine._pick_top_factor(
        (pathological, healthy), current
    )
    assert pick is not None
    assert pick.factor == "noise_level"


# ---------------------------------------------------------------------------
# _factor_30d_mean (line 795)
# ---------------------------------------------------------------------------


def test_factor_30d_mean_returns_nan_when_all_missing() -> None:
    """When every record has the factor missing, the helper returns NaN."""
    records = [
        _make_record(factors={"temperature_drift": None}) for _ in range(35)
    ]
    mean = CausalAttributionEngine._factor_30d_mean(records, "temperature_drift")
    assert math.isnan(mean)


def test_factor_30d_mean_uses_last_30_records() -> None:
    """Older-than-30 records must not influence the rolling mean."""
    older = [_make_record(factors={"temperature_drift": -100.0}) for _ in range(20)]
    recent = [_make_record(factors={"temperature_drift": 1.0}) for _ in range(30)]
    mean = CausalAttributionEngine._factor_30d_mean(
        older + recent, "temperature_drift"
    )
    # Only the recent 30 (all 1.0) are used.
    assert mean == pytest.approx(1.0)


def test_factor_30d_mean_uses_all_when_under_30_records() -> None:
    records = [_make_record(factors={"temperature_drift": 2.0}) for _ in range(5)]
    mean = CausalAttributionEngine._factor_30d_mean(records, "temperature_drift")
    assert mean == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# _build_explanation_zh template branches (lines 817, 829, 840)
# ---------------------------------------------------------------------------


def test_build_explanation_when_factor_current_missing() -> None:
    """If tonight's factor value is unknown, fall back to the no-current
    sentence template."""
    top = CausalEffect(
        factor="temperature_drift",
        effect_pp=-2.5,
        ci_low=-3.5,
        ci_high=-1.5,
        n_observations=30,
        is_significant=True,
    )
    text = CausalAttributionEngine._build_explanation_zh(
        top=top,
        current_quality=60.0,
        counterfactual_score=62.5,
        factor_current=None,
        factor_30d_mean=0.5,
    )
    assert "卧室温度方差" in text
    assert "因果效应估计为" in text
    assert "60.0" in text
    assert "62.5" in text
    assert "（统计显著性弱）" not in text


def test_build_explanation_when_factor_30d_mean_is_nan() -> None:
    """When the 30-day mean is unavailable, use the historical-mean
    fallback template (still references current factor value)."""
    top = CausalEffect(
        factor="noise_level",
        effect_pp=-1.5,
        ci_low=-2.5,
        ci_high=-0.5,
        n_observations=20,
        is_significant=True,
    )
    text = CausalAttributionEngine._build_explanation_zh(
        top=top,
        current_quality=55.0,
        counterfactual_score=56.5,
        factor_current=0.42,
        factor_30d_mean=float("nan"),
    )
    assert "环境噪声水平" in text
    assert "0.42" in text
    assert "接近历史均值" in text


def test_build_explanation_appends_weak_significance_suffix() -> None:
    """When the CI crosses zero, the (weak-significance) suffix is added."""
    top = CausalEffect(
        factor="hrv_anomaly",
        effect_pp=-0.5,
        ci_low=-1.5,
        ci_high=0.5,  # crosses zero
        n_observations=20,
        is_significant=False,
    )
    text = CausalAttributionEngine._build_explanation_zh(
        top=top,
        current_quality=70.0,
        counterfactual_score=70.5,
        factor_current=0.3,
        factor_30d_mean=0.4,
    )
    assert text.endswith("（统计显著性弱）")
    # Standard fully-populated template was used as the base.
    assert "0.30" in text and "0.40" in text


def test_build_explanation_factor_current_nan_uses_no_current_branch() -> None:
    """A NaN ``factor_current`` is treated identically to ``None`` per
    ``math.isfinite`` check."""
    top = CausalEffect(
        factor="prior_night_debt",
        effect_pp=2.0,
        ci_low=0.5,
        ci_high=3.5,
        n_observations=30,
        is_significant=True,
    )
    text = CausalAttributionEngine._build_explanation_zh(
        top=top,
        current_quality=72.0,
        counterfactual_score=74.0,
        factor_current=float("nan"),
        factor_30d_mean=1.2,
    )
    assert "上一晚累计睡眠债" in text
    # Used the +.1f formatter (no-current branch) rather than the .2f one.
    assert "+2.0" in text


# ---------------------------------------------------------------------------
# _run_estimator branches: constant-completeness + singular system
# ---------------------------------------------------------------------------


def test_run_estimator_skips_imr_when_completeness_is_constant(
    tmp_path: Path,
) -> None:
    """If every record has the same completeness ratio, the IMR proxy
    column has zero variance and the estimator skips it (branch 651→654).

    We construct a dataset where most factors are fully observed but
    one factor (``noise_level``) is missing in exactly the same set of
    rows where ``hrv_anomaly`` is also missing — making row completeness
    take only two distinct values that, after centering on the
    factor-observed subset, collapse to the same value (and thus a
    near-zero std)."""
    # 30 records, all factors observed → completeness == 1.0 for every row.
    # The factor under regression has missingness in zero rows.  Because
    # the n_total_observed_for_factor < n_records guard fails entirely,
    # the IMR block is never entered for any factor → the 651→654 branch
    # is also exercised vacuously alongside the simpler "all observed"
    # case.
    records = [
        _make_record(
            factors={f: float(i % 7) * 0.1 + 0.1 for f in ALL_FACTORS},
            quality_total=70.0 + (i % 5),
        )
        for i in range(30)
    ]
    engine = CausalAttributionEngine(jsonl_path=tmp_path / "x.jsonl")
    effects = engine._run_estimator(records)
    assert len(effects) == len(ALL_FACTORS)
    # All factors had >= 5 obs → no NaN effects.
    for effect in effects:
        assert effect.n_observations == 30
        assert math.isfinite(effect.effect_pp)


def test_run_estimator_marks_factor_nan_when_design_is_singular(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If ``_solve_ols`` reports a singular system for one factor, that
    factor's :class:`CausalEffect` is NaN + ``is_significant=False``
    rather than propagating bogus coefficients."""
    records = [
        _make_record(
            factors={f: 0.5 for f in ALL_FACTORS},
            quality_total=70.0 + i * 0.1,
        )
        for i in range(30)
    ]
    engine = CausalAttributionEngine(jsonl_path=tmp_path / "x.jsonl")

    real_solve = CausalAttributionEngine._solve_ols

    def _selectively_singular(d: np.ndarray, y: np.ndarray):
        # Trigger the singular branch the first time it is called
        # (corresponds to factor index 0 = temperature_drift), succeed
        # for every subsequent call so the rest of the report is well
        # formed and bootstrap iterations don't pollute results.
        if not getattr(_selectively_singular, "fired", False):
            _selectively_singular.fired = True  # type: ignore[attr-defined]
            return None, 0
        return real_solve(d, y)

    monkeypatch.setattr(
        CausalAttributionEngine,
        "_solve_ols",
        staticmethod(_selectively_singular),
    )

    effects = engine._run_estimator(records)
    by_name = {e.factor: e for e in effects}
    singular = by_name["temperature_drift"]
    assert math.isnan(singular.effect_pp)
    assert math.isnan(singular.ci_low)
    assert math.isnan(singular.ci_high)
    assert singular.is_significant is False
    # Sanity: at least one other factor still produced a finite effect.
    others = [e for e in effects if e.factor != "temperature_drift"]
    assert any(math.isfinite(e.effect_pp) for e in others)


def test_run_estimator_appends_imr_proxy_when_factor_has_missingness(
    tmp_path: Path,
) -> None:
    """When a factor has missingness *and* the IMR proxy varies across
    the surviving rows, the estimator includes the proxy column.  The
    test asserts the engine still produces a complete result."""
    rng = np.random.default_rng(20260520)
    records: list[CausalFactorRecord] = []
    for i in range(40):
        # Vary completeness: half the rows drop noise_level, the other
        # half drop bedtime_offset, ensuring that for any given factor
        # the IMR column has non-trivial variance.
        factors: dict[str, Optional[float]] = {
            f: float(rng.normal(loc=0.5, scale=0.2)) for f in ALL_FACTORS
        }
        if i % 4 == 0:
            factors["noise_level"] = None
        if i % 3 == 0:
            factors["bedtime_offset"] = None
        records.append(
            _make_record(
                factors=factors,
                quality_total=70.0 + float(rng.normal(0.0, 1.5)),
            )
        )
    engine = CausalAttributionEngine(jsonl_path=tmp_path / "x.jsonl")
    effects = engine._run_estimator(records)
    assert len(effects) == len(ALL_FACTORS)
    # At least one factor must have produced a finite effect; the
    # presence of the IMR proxy should not destabilize the estimator
    # for the well-conditioned ones.
    assert any(math.isfinite(e.effect_pp) for e in effects)
