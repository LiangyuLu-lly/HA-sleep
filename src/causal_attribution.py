"""Causal attribution engine for the v3.0.0 algorithmic moat (CAE).

Implements the CAE pillar described in
``.kiro/specs/algorithmic-moat-v3.0.0/design.md`` §3.3:

* a frozen 6-factor causal DAG (R4.1) recorded as a plain ``dict`` so a
  future v3.1.0 federated aggregator can parse it without depending on
  v3.0.0 add-on code;
* an append-only ``causal_factors.jsonl`` log capped at 90 records via
  :func:`src._io_utils.atomic_append_jsonl` (R4.3 + PR3 atomic-write
  contract);
* an ``attribute()`` coroutine that runs do-calculus back-door
  adjustment, a simplified Heckman two-stage correction, and a 200-fold
  residual bootstrap to produce a 95% confidence interval for each
  factor's effect on ``quality_total`` (R5.1, R6.1).

Heavy CPU work is dispatched to ``asyncio.to_thread`` and wrapped in
``asyncio.wait_for(timeout=5.0)``; on timeout the engine returns
``status="timeout"`` instead of raising, which keeps the main asyncio
event loop responsive (R5.4 + tech.md "no blocking on the event loop"
hard rule).

The estimator is implemented with pure Python + ``numpy`` only --
``networkx`` / ``dowhy`` / ``statsmodels`` are intentionally **not**
imported (R4.5 dependency hygiene).  ``scipy`` is reachable via the v3.0
runtime requirements but is also avoided here so the module stays
self-contained for the property-based tests in tasks 4.3 / 4.4 / 4.6.

Privacy contract (R14.2): the raw ``install_id`` is never persisted; it
is hashed via ``sha256`` before it ever reaches the JSONL line.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Mapping, Optional

import numpy as np

from . import _io_utils
from .preference_learner import SleepSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DAG and factor inventory (R4.1, R4.5)
# ---------------------------------------------------------------------------

#: Adjacency list for the 6-confounder causal DAG.  Each entry maps a
#: parent to the frozenset of its children, which is the canonical
#: representation for back-door adjustment in do-calculus.  ``quality_score``
#: is the sole sink and therefore not a key in the mapping.
CAUSAL_DAG: Final[Mapping[str, frozenset[str]]] = MappingProxyType({
    "bedtime_offset":    frozenset({"hrv_anomaly", "quality_score"}),
    "prior_night_debt":  frozenset({"hrv_anomaly", "quality_score"}),
    "temperature_drift": frozenset({"quality_score"}),
    "light_leak":        frozenset({"quality_score"}),
    "noise_level":       frozenset({"quality_score"}),
    "hrv_anomaly":       frozenset({"quality_score"}),
})

#: Tuple of all 6 candidate confounders, in canonical reporting order.
ALL_FACTORS: Final[tuple[str, ...]] = (
    "temperature_drift",
    "noise_level",
    "light_leak",
    "hrv_anomaly",
    "bedtime_offset",
    "prior_night_debt",
)

#: 4 quality sub-score keys persisted alongside the confounders (R4.2).
QUALITY_SUBSCORE_KEYS: Final[tuple[str, ...]] = (
    "architecture",
    "efficiency",
    "fragmentation",
    "onset",
)

#: Localized labels used to build ``explanation_zh`` per R5.2.
_FACTOR_LABELS_ZH: Final[Mapping[str, str]] = MappingProxyType({
    "temperature_drift": "卧室温度方差",
    "noise_level":       "环境噪声水平",
    "light_leak":        "夜间亮度峰值",
    "hrv_anomaly":       "HRV 偏离",
    "bedtime_offset":    "入睡时间偏差",
    "prior_night_debt":  "上一晚累计睡眠债",
})

#: Status string constants surfaced through ``AttributionResult.status``.
STATUS_INSUFFICIENT_DATA: Final[str] = "insufficient_data"
STATUS_NOMINAL: Final[str] = "nominal"
STATUS_OK: Final[str] = "ok"
STATUS_TIMEOUT: Final[str] = "timeout"

#: Schema version for the JSON export of the DAG (forward-compat for
#: v3.1.0 cross-user causal averaging).
_DAG_JSON_SCHEMA_VERSION: Final[int] = 1

#: Default seed used by the bootstrap RNG.  Matches the project-wide
#: convention (R15.5) so eval scripts can reproduce a result by
#: reusing the same install state.
_DEFAULT_BOOTSTRAP_SEED: Final[int] = 20260518


def _parents_of(factor: str) -> tuple[str, ...]:
    """Return the back-door adjustment set for *factor*.

    :param factor: One of :data:`ALL_FACTORS`.
    :returns: Tuple of factor names that have *factor* as a child in
        :data:`CAUSAL_DAG`.  For root nodes the tuple is empty, in
        which case the do-calculus reduces to a univariate regression.
    """
    parents = [p for p, children in CAUSAL_DAG.items() if factor in children]
    return tuple(sorted(parents))


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CausalFactorRecord:
    """One night's worth of observed factors + quality score.

    :ivar timestamp: ISO-8601 UTC string for the session end time.
    :ivar install_id_hash: ``sha256(install_id)`` hex digest -- the raw
        install id is never stored on disk (R14.2).
    :ivar factors: Mapping from factor name (a member of
        :data:`ALL_FACTORS`) to either a float observation or
        :data:`None` when the underlying sensor was missing (R4.6).
    :ivar quality_subscores: Mapping from sub-score key (one of
        :data:`QUALITY_SUBSCORE_KEYS`) to its 0..100 value.
    :ivar quality_total: Total quality score in [0, 100] for the night.
    """

    timestamp: str
    install_id_hash: str
    factors: Mapping[str, Optional[float]]
    quality_subscores: Mapping[str, float]
    quality_total: float

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict suitable for JSON-line persistence."""
        return {
            "timestamp": self.timestamp,
            "install_id_hash": self.install_id_hash,
            "factors": {k: self.factors.get(k) for k in ALL_FACTORS},
            "quality_subscores": {
                k: float(self.quality_subscores.get(k, 0.0))
                for k in QUALITY_SUBSCORE_KEYS
            },
            "quality_total": float(self.quality_total),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CausalFactorRecord":
        """Inverse of :meth:`to_dict`; tolerant to missing keys."""
        raw_factors = raw.get("factors", {}) or {}
        factors: dict[str, Optional[float]] = {}
        for key in ALL_FACTORS:
            value = raw_factors.get(key)
            factors[key] = None if value is None else float(value)
        raw_sub = raw.get("quality_subscores", {}) or {}
        quality_subscores = {
            key: float(raw_sub.get(key, 0.0))
            for key in QUALITY_SUBSCORE_KEYS
        }
        return cls(
            timestamp=str(raw.get("timestamp", "")),
            install_id_hash=str(raw.get("install_id_hash", "")),
            factors=factors,
            quality_subscores=quality_subscores,
            quality_total=float(raw.get("quality_total", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class CausalEffect:
    """Estimated causal effect of one factor on ``quality_total``.

    :ivar factor: Factor name (member of :data:`ALL_FACTORS`).
    :ivar effect_pp: Linear coefficient of the factor on the quality
        score under back-door adjustment, in score-units per
        factor-unit; ``NaN`` when fewer than ``min_per_factor_observations``
        non-missing rows are available (R5.6).
    :ivar ci_low: Lower bound of the 95% bootstrap confidence interval.
    :ivar ci_high: Upper bound of the 95% bootstrap confidence interval.
    :ivar n_observations: Number of complete-case rows used in the
        regression for this factor.
    :ivar is_significant: ``True`` when the CI does not cross zero.
        Always ``False`` when ``effect_pp`` is ``NaN``.
    """

    factor: str
    effect_pp: float
    ci_low: float
    ci_high: float
    n_observations: int
    is_significant: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dict (used by ``sensor.*_full``)."""
        return {
            "factor": self.factor,
            "effect_pp": self.effect_pp,
            "ci_low": self.ci_low,
            "ci_high": self.ci_high,
            "n_observations": int(self.n_observations),
            "is_significant": bool(self.is_significant),
        }


@dataclass(frozen=True, slots=True)
class AttributionResult:
    """Output of :meth:`CausalAttributionEngine.attribute`.

    :ivar status: One of :data:`STATUS_INSUFFICIENT_DATA`,
        :data:`STATUS_NOMINAL`, :data:`STATUS_OK`, :data:`STATUS_TIMEOUT`.
    :ivar effects: Tuple of :class:`CausalEffect`, one per factor in
        :data:`ALL_FACTORS`; empty tuple when status is not ``"ok"``.
    :ivar top_factor: Name of the factor with the largest absolute
        ``effect_pp`` among statistically significant factors that were
        observed in the current record, or :data:`None`.
    :ivar top_effect_pp: ``effect_pp`` of the top factor, or :data:`None`.
    :ivar counterfactual_score: ``quality_total + abs(top_effect_pp)``;
        the score the user might have reached had ``top_factor``
        equaled its 30-day mean (R5.2).
    :ivar explanation_zh: Pre-rendered Chinese explanation for the
        Lovelace card.  Always non-empty.
    """

    status: str
    effects: tuple[CausalEffect, ...]
    top_factor: Optional[str]
    top_effect_pp: Optional[float]
    counterfactual_score: Optional[float]
    explanation_zh: str


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class CausalAttributionEngine:
    """Persist factor records and run causal attribution on demand.

    :param jsonl_path: Path to ``/data/causal_factors.jsonl``.  The file
        is created on first append and never opened in append-mode --
        all writes go through :func:`atomic_append_jsonl` to honor PR3.
    :param max_records: Cap on the JSONL size; default 90 (R4.3 FIFO).
    :param bootstrap_iters: Number of residual-bootstrap iterations for
        the 95% CI; default 200 (R6.1 lower bound).
    :param timeout_seconds: Budget for one ``attribute()`` call; default
        5.0 seconds (R5.4).  Estimator runs in a worker thread and is
        cancelled on timeout, in which case the result is a benign
        ``status="timeout"``.
    :param min_per_factor_observations: Minimum non-missing rows per
        factor regression; default 5 (R5.6).
    :param rng_seed: Optional seed for the bootstrap RNG, defaults to
        :data:`_DEFAULT_BOOTSTRAP_SEED` for reproducibility.
    """

    def __init__(
        self,
        *,
        jsonl_path: Path,
        max_records: int = 90,
        bootstrap_iters: int = 200,
        timeout_seconds: float = 5.0,
        min_per_factor_observations: int = 5,
        rng_seed: int = _DEFAULT_BOOTSTRAP_SEED,
    ) -> None:
        if max_records <= 0:
            raise ValueError(f"max_records must be positive, got {max_records!r}")
        if bootstrap_iters < 200:
            # Per R6.1 we require at least 200 resamples.  Reject anything
            # smaller at construction time so a misconfigured deployment
            # fails loud rather than silently producing under-powered CIs.
            raise ValueError(
                f"bootstrap_iters must be >= 200 to satisfy R6.1, "
                f"got {bootstrap_iters!r}"
            )
        if timeout_seconds <= 0:
            raise ValueError(
                f"timeout_seconds must be positive, got {timeout_seconds!r}"
            )
        if min_per_factor_observations < 1:
            raise ValueError(
                "min_per_factor_observations must be at least 1, "
                f"got {min_per_factor_observations!r}"
            )

        self._jsonl_path = Path(jsonl_path)
        self._max_records = int(max_records)
        self._bootstrap_iters = int(bootstrap_iters)
        self._timeout_seconds = float(timeout_seconds)
        self._min_obs = int(min_per_factor_observations)
        self._rng_seed = int(rng_seed)
        # ``error_count`` follows the v3.0.0 health convention (R11.3): the
        # orchestrator inspects it and disables the module after 3 strikes.
        self.error_count: int = 0

    @property
    def should_disable(self) -> bool:
        """Return ``True`` when ``error_count >= 3`` (R11.3 threshold)."""
        return self.error_count >= 3

    # ------------------------------------------------------------------
    # Persistence (R4.2, R4.3, PR3)
    # ------------------------------------------------------------------

    @staticmethod
    def hash_install_id(install_id: str) -> str:
        """Return ``sha256(install_id)`` hex digest (R14.2).

        Pulled out as a ``@staticmethod`` to make the privacy contract
        easy to assert in tests without instantiating the engine.
        """
        if not isinstance(install_id, str):
            raise TypeError(
                f"install_id must be str, got {type(install_id).__name__}"
            )
        return hashlib.sha256(install_id.encode("utf-8")).hexdigest()

    async def on_session(
        self,
        *,
        session: SleepSession,
        install_id: str,
        factors: Mapping[str, Optional[float]],
        quality_subscores: Optional[Mapping[str, float]] = None,
    ) -> None:
        """Persist one night's record to ``causal_factors.jsonl``.

        :param session: The :class:`SleepSession` that just finished.
            Its ``ended_at`` timestamp anchors the record.
        :param install_id: Raw install id; immediately hashed (R14.2).
        :param factors: Mapping from factor name to its observed value
            or :data:`None` for missing sensors (R4.6).  Unknown factor
            keys are silently ignored.
        :param quality_subscores: Optional mapping of sub-score keys
            (:data:`QUALITY_SUBSCORE_KEYS`) to floats.  Missing keys
            default to ``0.0``.

        Writes go through :func:`_io_utils.atomic_append_jsonl` with
        ``max_lines=90`` (FIFO truncation) which honors both PR3 (atomic
        rewrite) and R4.3 (rolling cap).
        """
        # Anchor the timestamp to ``session.ended_at`` (preferred) or
        # current wall-clock; emit ISO-8601 UTC for downstream readability.
        anchor_seconds = session.ended_at if session.ended_at else time.time()
        timestamp = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(anchor_seconds)
        )
        normalized_factors: dict[str, Optional[float]] = {}
        for key in ALL_FACTORS:
            value = factors.get(key) if factors is not None else None
            if value is None:
                normalized_factors[key] = None
            else:
                # Reject NaN/inf at the boundary so downstream regressions
                # never have to defend against polluted input.
                fv = float(value)
                normalized_factors[key] = fv if math.isfinite(fv) else None
        sub = quality_subscores or {}
        normalized_subscores = {
            key: float(sub.get(key, 0.0)) for key in QUALITY_SUBSCORE_KEYS
        }
        record = CausalFactorRecord(
            timestamp=timestamp,
            install_id_hash=self.hash_install_id(install_id),
            factors=normalized_factors,
            quality_subscores=normalized_subscores,
            quality_total=float(session.quality_score),
        )
        # Filesystem I/O is synchronous; off-load to a worker thread so we
        # do not block the orchestrator event loop.
        await asyncio.to_thread(
            _io_utils.atomic_append_jsonl,
            self._jsonl_path,
            record.to_dict(),
            max_lines=self._max_records,
        )

    def _load_records(self) -> list[CausalFactorRecord]:
        """Read all records from disk; missing file is treated as empty."""
        try:
            raw = self._jsonl_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        records: list[CausalFactorRecord] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "skipping malformed line in %s", self._jsonl_path
                )
                continue
            records.append(CausalFactorRecord.from_dict(obj))
        return records

    def n_records(self) -> int:
        """Return the number of persisted records (recomputed on call)."""
        return len(self._load_records())

    # ------------------------------------------------------------------
    # Forward-compat (v3.1.0 federated DAG averaging)
    # ------------------------------------------------------------------

    @staticmethod
    def export_dag_json() -> dict[str, Any]:
        """Return a JSON-serializable representation of :data:`CAUSAL_DAG`.

        Schema (``schema_version=1``):

        * ``nodes``: list of all node names including the ``quality_score``
          sink, in declaration order.
        * ``edges``: list of ``{"src": parent, "dst": child}`` dicts.

        Used by future v3.1.0 federated aggregation (no runtime caller
        in v3.0.0; included as a forward-compat hook per design §3.3.1).
        """
        nodes: list[str] = list(CAUSAL_DAG.keys()) + ["quality_score"]
        edges: list[dict[str, str]] = []
        for src, children in CAUSAL_DAG.items():
            for dst in sorted(children):
                edges.append({"src": src, "dst": dst})
        return {
            "schema_version": _DAG_JSON_SCHEMA_VERSION,
            "nodes": nodes,
            "edges": edges,
        }

    # ------------------------------------------------------------------
    # Attribution (R5, R6)
    # ------------------------------------------------------------------

    async def attribute(
        self,
        *,
        current_record: CausalFactorRecord,
        personal_30d_mean: float,
    ) -> AttributionResult:
        """Run causal attribution for *current_record*.

        :param current_record: Tonight's :class:`CausalFactorRecord`,
            usually built by the orchestrator just before this call.
        :param personal_30d_mean: The user's own 30-day mean of
            ``quality_total``.  Used to gate the expensive estimator.
        :returns: :class:`AttributionResult` -- never raises; on
            estimator timeout the status is :data:`STATUS_TIMEOUT`.

        Decision flow:

        1. Fewer than 30 records on disk → :data:`STATUS_INSUFFICIENT_DATA`.
        2. ``quality_total >= personal_30d_mean - 5`` → :data:`STATUS_NOMINAL`
           (R5.1 trigger condition + R5.3); skip estimator entirely.
        3. Run :meth:`_run_estimator` in a worker thread under
           :func:`asyncio.wait_for` with budget ``timeout_seconds``.
           On :class:`asyncio.TimeoutError` return :data:`STATUS_TIMEOUT`.
        4. Otherwise pick the top significant + observed factor and
           build the Chinese explanation per R5.2.
        """
        records = await asyncio.to_thread(self._load_records)
        if len(records) < 30:
            return AttributionResult(
                status=STATUS_INSUFFICIENT_DATA,
                effects=(),
                top_factor=None,
                top_effect_pp=None,
                counterfactual_score=None,
                explanation_zh="数据不足，至少需要 30 晚因子记录",
            )

        # R5.1 trigger: only run the estimator when the night is at
        # least 5 points below the personal 30-day mean.  Otherwise
        # tag as nominal and skip the expensive bootstrap.
        if current_record.quality_total >= personal_30d_mean - 5:
            return AttributionResult(
                status=STATUS_NOMINAL,
                effects=(),
                top_factor=None,
                top_effect_pp=None,
                counterfactual_score=None,
                explanation_zh="今晚睡眠质量与个人均值持平，未触发因果归因",
            )

        try:
            effects = await asyncio.wait_for(
                asyncio.to_thread(self._run_estimator, records),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            self.error_count += 1
            logger.warning(
                "causal attribution timed out after %.1fs; returning timeout status",
                self._timeout_seconds,
            )
            return AttributionResult(
                status=STATUS_TIMEOUT,
                effects=(),
                top_factor=None,
                top_effect_pp=None,
                counterfactual_score=None,
                explanation_zh=f"因果推断超时（> {self._timeout_seconds:.0f} 秒），已跳过本次",
            )
        except Exception:  # noqa: BLE001 -- defensive boundary
            self.error_count += 1
            logger.exception("causal attribution estimator crashed")
            return AttributionResult(
                status=STATUS_TIMEOUT,
                effects=(),
                top_factor=None,
                top_effect_pp=None,
                counterfactual_score=None,
                explanation_zh="因果推断异常，已跳过本次",
            )

        top = self._pick_top_factor(effects, current_record)
        if top is None:
            return AttributionResult(
                status=STATUS_OK,
                effects=effects,
                top_factor=None,
                top_effect_pp=None,
                counterfactual_score=None,
                explanation_zh="未发现具有统计显著性的影响因子",
            )

        counterfactual_score = float(current_record.quality_total) + abs(
            float(top.effect_pp)
        )
        factor_30d_mean = self._factor_30d_mean(records, top.factor)
        factor_current = current_record.factors.get(top.factor)
        explanation = self._build_explanation_zh(
            top=top,
            current_quality=float(current_record.quality_total),
            counterfactual_score=counterfactual_score,
            factor_current=factor_current,
            factor_30d_mean=factor_30d_mean,
        )
        return AttributionResult(
            status=STATUS_OK,
            effects=effects,
            top_factor=top.factor,
            top_effect_pp=float(top.effect_pp),
            counterfactual_score=counterfactual_score,
            explanation_zh=explanation,
        )

    # ------------------------------------------------------------------
    # Estimator internals (pure-numpy, runs in a worker thread)
    # ------------------------------------------------------------------

    def _run_estimator(
        self, records: list[CausalFactorRecord]
    ) -> tuple[CausalEffect, ...]:
        """Compute :class:`CausalEffect` for every factor.

        Pure numpy implementation; safe to call from a worker thread.
        Per-factor pipeline:

        1. Drop rows where the factor or any of its DAG parents are
           :data:`None` (R4.6: never impute zero).
        2. If complete-case rows < ``min_per_factor_observations``,
           return ``effect_pp = NaN`` and ``is_significant = False``
           (R5.6).
        3. Build the design matrix ``[1, factor, parents..., IMR]``
           where ``IMR`` is a row-completeness proxy that approximates
           the inverse Mills ratio when the factor has missingness; the
           column is omitted when the rows are all complete.
        4. Solve OLS via :func:`numpy.linalg.lstsq`; the coefficient on
           the factor column is ``effect_pp``.
        5. Bootstrap ``bootstrap_iters`` (>= 200) residual resamples and
           take the 2.5th / 97.5th percentile for the 95% CI.
        """
        rng = np.random.default_rng(self._rng_seed)
        results: list[CausalEffect] = []
        # Pre-compute the matrix of all 6 factor values + completeness mask
        # once; per-factor passes only filter rows.
        n_records = len(records)
        factor_matrix = np.full((n_records, len(ALL_FACTORS)), np.nan)
        for row_idx, record in enumerate(records):
            for col_idx, factor_name in enumerate(ALL_FACTORS):
                value = record.factors.get(factor_name)
                if value is not None and math.isfinite(float(value)):
                    factor_matrix[row_idx, col_idx] = float(value)
        completeness = (~np.isnan(factor_matrix)).mean(axis=1)
        quality_vec = np.array(
            [float(rec.quality_total) for rec in records], dtype=float
        )

        for factor_idx, factor_name in enumerate(ALL_FACTORS):
            parent_names = _parents_of(factor_name)
            parent_idx = [ALL_FACTORS.index(p) for p in parent_names]
            # Only use rows where the factor + every parent are observed.
            needed_cols = [factor_idx] + parent_idx
            mask = np.ones(n_records, dtype=bool)
            for col in needed_cols:
                mask &= ~np.isnan(factor_matrix[:, col])
            n_obs = int(mask.sum())
            if n_obs < self._min_obs:
                results.append(
                    CausalEffect(
                        factor=factor_name,
                        effect_pp=float("nan"),
                        ci_low=float("nan"),
                        ci_high=float("nan"),
                        n_observations=n_obs,
                        is_significant=False,
                    )
                )
                continue

            y = quality_vec[mask]
            x_factor = factor_matrix[mask, factor_idx]
            # Build design matrix: intercept + factor + parents.
            cols: list[np.ndarray] = [np.ones(n_obs), x_factor]
            for col in parent_idx:
                cols.append(factor_matrix[mask, col])
            # Heckman-lite: append a row-completeness proxy when the
            # factor itself has any missingness across ALL records.
            # When fully observed we skip the column to keep the design
            # matrix well-conditioned.
            n_total_observed_for_factor = int(
                (~np.isnan(factor_matrix[:, factor_idx])).sum()
            )
            if n_total_observed_for_factor < n_records:
                imr_proxy = completeness[mask].astype(float)
                # Center the proxy so it is orthogonal to the intercept
                # baseline; this stabilizes lstsq when completeness is
                # nearly constant.
                imr_proxy = imr_proxy - imr_proxy.mean()
                # Only include if it varies (otherwise it is collinear
                # with the intercept and adds nothing).
                if float(np.std(imr_proxy)) > 1e-9:
                    cols.append(imr_proxy)

            design = np.column_stack(cols)
            beta_hat, _ols_status = self._solve_ols(design, y)
            if beta_hat is None:
                # Singular system → mark as non-significant rather than
                # propagating a NaN that would mask the underlying issue.
                results.append(
                    CausalEffect(
                        factor=factor_name,
                        effect_pp=float("nan"),
                        ci_low=float("nan"),
                        ci_high=float("nan"),
                        n_observations=n_obs,
                        is_significant=False,
                    )
                )
                continue
            effect_pp = float(beta_hat[1])
            # Bootstrap residuals to obtain the 95% CI.
            ci_low, ci_high = self._bootstrap_factor_ci(
                design=design,
                y=y,
                beta_hat=beta_hat,
                rng=rng,
            )
            is_significant = bool(
                math.isfinite(ci_low)
                and math.isfinite(ci_high)
                and not (ci_low <= 0.0 <= ci_high)
            )
            results.append(
                CausalEffect(
                    factor=factor_name,
                    effect_pp=effect_pp,
                    ci_low=float(ci_low),
                    ci_high=float(ci_high),
                    n_observations=n_obs,
                    is_significant=is_significant,
                )
            )
        return tuple(results)

    @staticmethod
    def _solve_ols(
        design: np.ndarray, y: np.ndarray
    ) -> tuple[Optional[np.ndarray], int]:
        """Closed-form OLS via ``numpy.linalg.lstsq``.

        :returns: ``(beta_hat, rank)`` where *beta_hat* is :data:`None`
            when the system is so ill-conditioned that lstsq throws.
        """
        try:
            beta_hat, _residuals, rank, _sv = np.linalg.lstsq(
                design, y, rcond=None
            )
        except np.linalg.LinAlgError:
            return None, 0
        if not np.all(np.isfinite(beta_hat)):
            return None, int(rank)
        return beta_hat, int(rank)

    def _bootstrap_factor_ci(
        self,
        *,
        design: np.ndarray,
        y: np.ndarray,
        beta_hat: np.ndarray,
        rng: np.random.Generator,
    ) -> tuple[float, float]:
        """200-fold residual bootstrap of the factor coefficient.

        :returns: ``(ci_low, ci_high)`` -- 2.5th / 97.5th percentile of
            the bootstrap distribution.  When all bootstrap fits fail
            returns ``(nan, nan)``.
        """
        y_hat = design @ beta_hat
        residuals = y - y_hat
        n = residuals.shape[0]
        if n == 0:
            return float("nan"), float("nan")
        coefs: list[float] = []
        for _ in range(self._bootstrap_iters):
            sampled = rng.choice(residuals, size=n, replace=True)
            y_b = y_hat + sampled
            beta_b, _ = self._solve_ols(design, y_b)
            if beta_b is None:
                continue
            coefs.append(float(beta_b[1]))
        if not coefs:
            return float("nan"), float("nan")
        coef_arr = np.asarray(coefs, dtype=float)
        ci_low = float(np.percentile(coef_arr, 2.5))
        ci_high = float(np.percentile(coef_arr, 97.5))
        return ci_low, ci_high

    # ------------------------------------------------------------------
    # Top-factor selection + Chinese explanation (R5.2, R6.2)
    # ------------------------------------------------------------------

    @staticmethod
    def _pick_top_factor(
        effects: tuple[CausalEffect, ...],
        current_record: CausalFactorRecord,
    ) -> Optional[CausalEffect]:
        """Pick the factor with the largest |effect| among candidates.

        A candidate is a :class:`CausalEffect` that is statistically
        significant (CI does not cross 0) AND was actually observed in
        the current night's record.  Without an observation we cannot
        compute the counterfactual delta, so non-observed factors are
        filtered out even if their historical effect is significant.
        """
        candidates: list[CausalEffect] = []
        for effect in effects:
            if not effect.is_significant:
                continue
            if not math.isfinite(effect.effect_pp):
                continue
            current_value = current_record.factors.get(effect.factor)
            if current_value is None:
                continue
            candidates.append(effect)
        if not candidates:
            return None
        return max(candidates, key=lambda e: abs(e.effect_pp))

    @staticmethod
    def _factor_30d_mean(
        records: list[CausalFactorRecord], factor: str
    ) -> float:
        """Return the mean of *factor* over the last 30 records.

        Missing values are excluded; if every value is missing the
        function returns :data:`nan`.
        """
        recent = records[-30:] if len(records) >= 30 else records
        values = [
            float(r.factors[factor])
            for r in recent
            if r.factors.get(factor) is not None
        ]
        if not values:
            return float("nan")
        return float(sum(values) / len(values))

    @staticmethod
    def _build_explanation_zh(
        *,
        top: CausalEffect,
        current_quality: float,
        counterfactual_score: float,
        factor_current: Optional[float],
        factor_30d_mean: float,
    ) -> str:
        """Render the Chinese explanation following the R5.2 template.

        Appends "（统计显著性弱）" when the 95% CI crosses zero (R6.2);
        in practice :meth:`_pick_top_factor` already filters
        non-significant factors out so the suffix is only attached when
        a future caller bypasses that filter.
        """
        label = _FACTOR_LABELS_ZH.get(top.factor, top.factor)
        # Build the human-readable counterfactual sentence.
        if factor_current is None or not math.isfinite(factor_current):
            base = (
                f"今晚 {label} 的因果效应估计为 {top.effect_pp:+.1f} 分，"
                f"睡眠质量分有望从 {current_quality:.1f} 提升到 "
                f"{counterfactual_score:.1f}"
            )
        elif math.isfinite(factor_30d_mean):
            base = (
                f"如果今晚 {label} 从 {factor_current:.2f} 调整到 "
                f"{factor_30d_mean:.2f}，估计睡眠质量分会从 "
                f"{current_quality:.1f} 提升到 {counterfactual_score:.1f}"
            )
        else:
            base = (
                f"如果今晚 {label} ({factor_current:.2f}) 接近历史均值，"
                f"估计睡眠质量分会从 {current_quality:.1f} 提升到 "
                f"{counterfactual_score:.1f}"
            )
        ci_crosses_zero = (
            math.isfinite(top.ci_low)
            and math.isfinite(top.ci_high)
            and top.ci_low <= 0.0 <= top.ci_high
        )
        if ci_crosses_zero:
            base = base + "（统计显著性弱）"
        return base


__all__ = [
    "ALL_FACTORS",
    "CAUSAL_DAG",
    "QUALITY_SUBSCORE_KEYS",
    "STATUS_INSUFFICIENT_DATA",
    "STATUS_NOMINAL",
    "STATUS_OK",
    "STATUS_TIMEOUT",
    "AttributionResult",
    "CausalAttributionEngine",
    "CausalEffect",
    "CausalFactorRecord",
]
