"""Population prior loaded from MESA + SHHS PSG datasets.

Why this module exists
----------------------
A brand-new add-on install has zero session history.  v2.x had to
``dry_run`` for ~7 nights before the weighted-median learner produced
useful setpoints.  v3.0.0 ships a hierarchical Bayesian prior trained
offline on the public NSRR datasets (MESA v0.6.0 + SHHS v8, ≈ 8000
subject-nights) so the very first night already has a sensible
``(temperature, humidity, brightness)`` mean for the user's
``(age_band, sex, chronotype, season)`` bucket.

Wire format
-----------
The pickle on disk is intentionally a **plain** ``dict`` of frozen
dataclasses, no lambdas or live class references beyond the dataclass
constructors themselves::

    {
        "buckets":  dict[BucketKey, PriorBucket],
        "metadata": PriorMetadata,
    }

* This is the **forward-compat wire format** for the v3.1.0 federated
  aggregator.  Any future Rust / Go FedAvg implementation must be able
  to parse the same bytes; therefore no closures, no instance methods,
  no third-party types are allowed inside the pickle.
* ``metadata.sha256`` is the SHA-256 of ``pickle.dumps(buckets,
  protocol=HIGHEST_PROTOCOL)`` *alone* — i.e. of the buckets dict
  before it is wrapped in the outer dict.  This avoids a circular
  hashing dependency (the metadata cannot include its own digest in
  its own pickle bytes) while still letting the runtime verify that
  the bucket payload has not been tampered with after training.

Runtime invariants
------------------
* Stdlib only: ``pickle``, ``hashlib``, ``pathlib``, ``logging``,
  ``dataclasses``, ``typing``.  No ``numpy`` / ``scipy`` —
  :mod:`bayesian_optimizer` is the only module that may pull those in
  (R12 import-coverage scan).
* :meth:`PopulationPriorRepository.load` returns ``None`` on **any**
  failure (file missing, > 8 MB, SHA-256 mismatch, malformed wire
  format).  The orchestrator then publishes
  ``sensor.sleep_classifier_prior_status = unavailable`` and BAO
  proceeds without a prior — degraded but never crashing (R8.1, R11.3).
* :meth:`PopulationPriorRepository.lookup` always returns a bucket
  (R8.6 last-resort).  Empty buckets dict raises ``ValueError`` at
  construction time so ``lookup`` itself can never see an empty table.

NSRR DUA summary (R14.1)
------------------------
We log the data-use-agreement summary exactly **once** per process
lifetime via the module-level :data:`_DUA_LOG_EMITTED` flag.  The log
line includes dataset names + DOI placeholders + the single sentence
"research data; not for individual diagnosis." in keeping with the
NSRR data-use agreement.  Full provenance lives in
``docs/POPULATION_PRIOR.md``.
"""
from __future__ import annotations

import hashlib
import logging
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Type aliases — also used by ``scripts/train_population_prior.py``
# ---------------------------------------------------------------------------

AgeBand = Literal["18-25", "26-35", "36-50", "51-65", "65+"]
Sex = Literal["M", "F", "unspecified"]
Chronotype = Literal["morning", "evening", "neutral"]
Season = Literal["spring", "summer", "autumn", "winter"]
BucketKey = Tuple[AgeBand, Sex, Chronotype, Season]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Hard cap on the on-disk pickle size (R7.3).  Build-time guard uses
#: :meth:`PopulationPriorRepository.expected_size_bytes` to compare
#: against this value.
MAX_PICKLE_SIZE_BYTES: int = 8 * 1024 * 1024  # 8 MB

#: Minimum number of subject-nights required to consider a bucket
#: large enough for direct lookup (R8.6).  Below this threshold the
#: lookup walks one rung up the fallback ladder.
MIN_BUCKET_N_SAMPLES: int = 50

#: Pickle protocol used by both the trainer and the runtime.  Pinned so
#: that re-pickling the buckets dict during load is byte-identical to
#: the trainer's output (and therefore SHA-256 verification works).
_PICKLE_PROTOCOL: int = 5

#: One-shot flag for the NSRR DUA INFO log (R14.1).  Module-level so
#: that subsequent ``PopulationPriorRepository.load`` calls inside the
#: same process do not re-emit the summary.
_DUA_LOG_EMITTED: bool = False


# ---------------------------------------------------------------------------
# Frozen / slots dataclasses (forward-compat: only stdlib primitives)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class PriorBucket:
    """One leaf in the hierarchical Bayesian prior tree.

    :ivar temperature_mean_c: Posterior mean of bedroom temperature
        (°C).
    :ivar temperature_var_c2: Posterior variance (°C²).  Always
        non-negative.
    :ivar humidity_mean_pct: Posterior mean of relative humidity (%).
    :ivar humidity_var_pct2: Posterior variance (%²).  Always
        non-negative.
    :ivar brightness_mean_pct: Posterior mean of bedroom illuminance
        (%, 0..100 normalised by sensor max).
    :ivar brightness_var_pct2: Posterior variance (%²).  Always
        non-negative.
    :ivar n_samples: Number of subject-nights aggregated into this leaf.
    """

    temperature_mean_c: float
    temperature_var_c2: float
    humidity_mean_pct: float
    humidity_var_pct2: float
    brightness_mean_pct: float
    brightness_var_pct2: float
    n_samples: int


@dataclass(frozen=True, slots=True)
class PriorMetadata:
    """Provenance + integrity metadata embedded in the pickle (R7.4).

    :ivar schema_version: Wire-format version.  ``1`` for v3.0.0; the
        v3.1.0 federated aggregator may bump to ``2``.
    :ivar sources: Dataset citations including DOI strings, e.g.
        ``("MESA v0.6.0 (10.5061/dryad.placeholder1)",
        "SHHS v8 (10.5061/dryad.placeholder2)")``.  The runtime echoes
        these verbatim into the one-shot DUA INFO log.
    :ivar trained_at: ISO-8601 UTC timestamp of when the trainer ran.
    :ivar git_commit: Short SHA of the training repo state.
    :ivar n_subject_nights: Total subject-night count across all
        buckets (provenance only — :class:`PriorBucket.n_samples` is
        the source of truth at lookup time).
    :ivar sha256: Hex digest of ``pickle.dumps(buckets,
        protocol=_PICKLE_PROTOCOL)``.  See module docstring for why we
        digest the buckets dict alone.
    """

    schema_version: int
    sources: tuple[str, ...]
    trained_at: str
    git_commit: str
    n_subject_nights: int
    sha256: str


@dataclass(frozen=True, slots=True)
class PopulationPrior:
    """Top-level prior loaded from
    ``training_config/population_prior.pickle``.

    The wire format is intentionally a plain
    ``dict[BucketKey, PriorBucket]`` plus a :class:`PriorMetadata` so
    that the v3.1.0 federated aggregator can parse without depending
    on any v3.0.0 add-on code (forward-compat).
    """

    buckets: Mapping[BucketKey, PriorBucket]
    metadata: PriorMetadata


# ---------------------------------------------------------------------------
# Repository — load + lookup; never writes the pickle at runtime (R14.2)
# ---------------------------------------------------------------------------

class PopulationPriorRepository:
    """Read-only access to the population prior pickle.

    Construct via :meth:`load`; the public ``__init__`` is reserved for
    tests that build an in-memory prior without going through disk.
    """

    __slots__ = ("_prior", "_size_bytes", "_error_count")

    def __init__(self, prior: PopulationPrior, *, size_bytes: int) -> None:
        if not prior.buckets:
            # An empty prior is unusable — bail early so that
            # :meth:`lookup` can assume at least one bucket exists and
            # therefore always return a valid result (R8.6).
            raise ValueError("PopulationPrior has no buckets")
        self._prior = prior
        self._size_bytes = size_bytes
        self._error_count: int = 0

    # -- health convention (R11.3) ----------------------------------------

    @property
    def error_count(self) -> int:
        """Runtime error counter for the v3.0.0 auto-degradation state machine."""
        return self._error_count

    @property
    def should_disable(self) -> bool:
        """Return ``True`` when ``error_count >= 3`` (R11.3 threshold)."""
        return self._error_count >= 3

    # -- load -------------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "PopulationPriorRepository | None":
        """Atomically load + verify SHA-256.

        :param path: Filesystem path to ``population_prior.pickle``.
        :returns: ``None`` if the file is missing, larger than
            :data:`MAX_PICKLE_SIZE_BYTES`, malformed, or fails SHA-256
            verification.  The orchestrator publishes
            ``sensor.sleep_classifier_prior_status = unavailable`` on
            ``None`` (R8.1).

        On the first **successful** load within a process the NSRR DUA
        summary is emitted to the module logger at INFO level (R14.1).
        Subsequent loads within the same process do not re-emit.
        """
        path = Path(path)

        # 1. File existence ------------------------------------------------
        if not path.exists():
            logger.warning(
                "Population prior pickle missing at %s; "
                "BAO will start without a prior.",
                path,
            )
            return None

        # 2. Size guard (R7.3) --------------------------------------------
        try:
            size_bytes = path.stat().st_size
        except OSError as exc:
            logger.warning(
                "Population prior pickle stat failed at %s: %s", path, exc,
            )
            return None
        if size_bytes > MAX_PICKLE_SIZE_BYTES:
            logger.warning(
                "Population prior pickle %s is %d bytes, exceeds %d-byte cap.",
                path,
                size_bytes,
                MAX_PICKLE_SIZE_BYTES,
            )
            return None

        # 3. Read + unpickle ----------------------------------------------
        try:
            raw = path.read_bytes()
        except OSError as exc:
            logger.warning("Population prior pickle unreadable at %s: %s", path, exc)
            return None
        try:
            wire = pickle.loads(raw)
        except (pickle.UnpicklingError, EOFError, AttributeError, ImportError, ValueError) as exc:
            logger.warning("Population prior pickle unparseable at %s: %s", path, exc)
            return None

        # 4. Schema check -------------------------------------------------
        if not isinstance(wire, dict) or "buckets" not in wire or "metadata" not in wire:
            logger.warning(
                "Population prior pickle at %s has invalid wire layout.", path,
            )
            return None
        buckets = wire["buckets"]
        metadata = wire["metadata"]
        if not isinstance(metadata, PriorMetadata):
            logger.warning(
                "Population prior metadata at %s is not a PriorMetadata.", path,
            )
            return None
        if not isinstance(buckets, dict) or not buckets:
            logger.warning(
                "Population prior buckets at %s are missing or empty.", path,
            )
            return None

        # 5. SHA-256 verification (digest of the buckets dict alone) -----
        try:
            actual_sha = hashlib.sha256(
                pickle.dumps(buckets, protocol=_PICKLE_PROTOCOL)
            ).hexdigest()
        except (pickle.PicklingError, TypeError) as exc:
            logger.warning(
                "Population prior buckets at %s could not be re-pickled: %s",
                path,
                exc,
            )
            return None
        if actual_sha != metadata.sha256:
            logger.warning(
                "Population prior SHA-256 mismatch at %s: "
                "expected %s, got %s.",
                path,
                metadata.sha256,
                actual_sha,
            )
            return None

        # 6. Build repo + emit one-shot DUA log ---------------------------
        prior = PopulationPrior(buckets=buckets, metadata=metadata)
        try:
            repo = cls(prior, size_bytes=size_bytes)
        except ValueError as exc:
            logger.warning("Population prior at %s rejected: %s", path, exc)
            return None
        _emit_dua_log_once(metadata)
        return repo

    # -- lookup -----------------------------------------------------------

    def lookup(
        self,
        *,
        age_band: AgeBand,
        sex: Sex,
        chronotype: Chronotype,
        season: Season,
    ) -> tuple[PriorBucket, int]:
        """Return ``(bucket, fallback_level)`` for the requested cell.

        Fallback ladder (R8.6):

        * ``0`` — exact ``(age_band, sex, chronotype, season)`` match
          **and** the bucket has at least
          :data:`MIN_BUCKET_N_SAMPLES` samples.
        * ``1`` — ``sex`` relaxed to ``"unspecified"`` (age, chronotype,
          season unchanged).
        * ``2`` — ``chronotype`` also relaxed to ``"neutral"``.
        * ``3`` — ``age_band`` also relaxed: pick the
          ``(*, "unspecified", "neutral", season)`` bucket with the
          largest ``n_samples``; fall back further to any
          ``(*, "unspecified", "neutral", *)`` bucket if no
          season-matching root exists; finally fall back to **any**
          bucket with the largest ``n_samples``.

        Lookup always returns a bucket (R8.6 last-resort).  When even
        the root fallback has ``n_samples < MIN_BUCKET_N_SAMPLES`` the
        bucket is still returned with ``fallback_level == 3`` so that
        the caller can decide whether to dampen the prior weight.
        """
        buckets = self._prior.buckets

        # Level 0 — exact match with sufficient samples.
        exact = buckets.get((age_band, sex, chronotype, season))
        if exact is not None and exact.n_samples >= MIN_BUCKET_N_SAMPLES:
            return exact, 0

        # Level 1 — sex relaxed.
        l1 = buckets.get((age_band, "unspecified", chronotype, season))
        if l1 is not None and l1.n_samples >= MIN_BUCKET_N_SAMPLES:
            return l1, 1

        # Level 2 — chronotype additionally relaxed.
        l2 = buckets.get((age_band, "unspecified", "neutral", season))
        if l2 is not None and l2.n_samples >= MIN_BUCKET_N_SAMPLES:
            return l2, 2

        # Level 3 — age_band additionally relaxed.  Prefer same-season
        # roots; among those pick the largest bucket.
        season_roots = [
            b for k, b in buckets.items()
            if k[1] == "unspecified" and k[2] == "neutral" and k[3] == season
        ]
        if season_roots:
            best = max(season_roots, key=lambda b: b.n_samples)
            return best, 3

        # No same-season root — try any (unspecified, neutral, *).
        any_roots = [
            b for k, b in buckets.items()
            if k[1] == "unspecified" and k[2] == "neutral"
        ]
        if any_roots:
            best = max(any_roots, key=lambda b: b.n_samples)
            return best, 3

        # Truly degenerate prior (no root buckets at all): return the
        # largest available bucket so the caller still gets a usable
        # mean.  This covers tiny test fixtures that only populate
        # specific cells.
        if exact is not None:
            return exact, 3
        if l1 is not None:
            return l1, 3
        if l2 is not None:
            return l2, 3
        best = max(buckets.values(), key=lambda b: b.n_samples)
        return best, 3

    # -- accessors --------------------------------------------------------

    def expected_size_bytes(self) -> int:
        """Return the on-disk byte size of the loaded pickle.

        Used by the build-time guard (R7.3) to compare against
        :data:`MAX_PICKLE_SIZE_BYTES`.
        """
        return self._size_bytes

    @property
    def metadata(self) -> PriorMetadata:
        """Read-only access to the embedded :class:`PriorMetadata`."""
        return self._prior.metadata

    @property
    def buckets(self) -> Mapping[BucketKey, PriorBucket]:
        """Read-only view of the underlying buckets dict."""
        return self._prior.buckets


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _emit_dua_log_once(metadata: PriorMetadata) -> None:
    """Emit the NSRR DUA summary at most once per process (R14.1).

    The summary line lists the dataset sources (which already include
    DOI placeholders per :class:`PriorMetadata`) and the standard NSRR
    disclaimer "research data; not for individual diagnosis."  Full
    provenance lives in ``docs/POPULATION_PRIOR.md``.
    """
    global _DUA_LOG_EMITTED
    if _DUA_LOG_EMITTED:
        return
    _DUA_LOG_EMITTED = True
    sources_str = ", ".join(metadata.sources) if metadata.sources else "(unspecified)"
    logger.info(
        "Population prior loaded — sources: %s; trained_at=%s; "
        "n_subject_nights=%d; NSRR DUA: research data; not for individual "
        "diagnosis. See docs/POPULATION_PRIOR.md for full provenance.",
        sources_str,
        metadata.trained_at,
        metadata.n_subject_nights,
    )


def reset_dua_log_for_tests() -> None:
    """Reset the one-shot DUA flag.  **Test-only helper.**

    The DUA summary is intentionally emitted once per process.  Tests
    that need to assert the log is printed exactly once across multiple
    :meth:`PopulationPriorRepository.load` invocations call this to
    bring the flag back to its pristine state.
    """
    global _DUA_LOG_EMITTED
    _DUA_LOG_EMITTED = False
