"""3-D Gaussian Process posterior + Thompson Sampling decision layer (BAO).

This is the runtime core of the v3.0.0 algorithmic moat described in
``.kiro/specs/algorithmic-moat-v3.0.0/design.md`` §3.2.  Every night
the orchestrator hands the optimiser one
:class:`GPObservation` ``(temperature_c, humidity_pct, brightness_pct,
quality_score, ...)`` tuple and the optimiser maintains a 3-D RBF GP
posterior over ``f: (T, H, L) -> quality``.  At every stage decision
the orchestrator asks for a :class:`GPRecommendation`; the optimiser
either samples Thompson values from the posterior (exploit) or returns
the most uncertain candidate point on a chosen dimension (explore).

Why we bound the cholesky to numpy + scipy.linalg
-------------------------------------------------

The hard rule in ``.kiro/steering/tech.md`` is that no third-party GP
library (``GPy`` / ``GPyTorch`` / ``scikit-learn``) is allowed in the
add-on image — they would push the wheel set well above the 96 MB CI
ceiling.  Vectorised numpy + ``scipy.linalg.cho_factor`` /
``cho_solve`` is sufficient for a 60-observation problem in ≤ 200 ms on
a Pi 4B (R1.3).

Cholesky failure semantics (R1.4)
---------------------------------

Numerical instability surfaces as a :class:`GPNumericalError` raised by
:meth:`BayesianOptimizer.observe` (and indirectly by
:meth:`BayesianOptimizer.recommend` if the new observation cannot be
incorporated).  The orchestrator catches that exception, falls back to
the v2.x ``PreferenceLearner.recommend()`` path, and bumps
``self._error_count``; three consecutive failures auto-disable the BAO
pillar (see :mod:`scripts.run_ha_smart_service`).

Repeatability (R2.6)
--------------------

The Thompson Sampling RNG seed is derived from
``sha256(install_id + ISO-date)`` so every decision is reproducible in
post-mortem analysis.  Python's built-in :func:`hash` is intentionally
*not* used because it is salted per-process, which would break
day-over-day repeatability across add-on restarts.

Forward-compatibility (v3.1.0 federated learning)
-------------------------------------------------

* :class:`BAOPersistedState` is a stdlib-only dataclass tree
  (``str / int / float / tuple / dataclass``); no closures, no live
  class references beyond the dataclass constructors themselves.  This
  is the same wire-format strategy used by
  :class:`src.population_prior.PopulationPrior`.
* :meth:`BayesianOptimizer.export_hyperparams_json` returns a plain
  dict of primitives so a future Rust / Go FedAvg implementation can
  parse the same JSON.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import pickle
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import numpy as np
import scipy.linalg

from src import _io_utils
from src.data_structures import SleepStage

if TYPE_CHECKING:  # pragma: no cover - typing only
    from src.population_prior import PopulationPriorRepository

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: Default RBF length scale on the temperature axis (°C).  Picked so
#: that a 1.5 °C bedroom temperature delta produces a noticeable kernel
#: response — most users perceive a 2 °C swing as "different night".
_DEFAULT_LENGTH_TEMP: Final[float] = 1.5

#: Default RBF length scale on the humidity axis (% RH).
_DEFAULT_LENGTH_HUMIDITY: Final[float] = 8.0

#: Default RBF length scale on the brightness axis (% of sensor max).
_DEFAULT_LENGTH_BRIGHTNESS: Final[float] = 15.0

#: Default signal variance σ_f² in (quality_score)² units.  100 ⇒
#: σ_f = 10 quality points, in the same order of magnitude as a single
#: night's noise floor.
_DEFAULT_SIGNAL_VARIANCE: Final[float] = 100.0

#: Default observation noise variance σ_n².  25 ⇒ σ_n = 5 quality
#: points per night, consistent with the v2.x weighted-median
#: empirical jitter.
_DEFAULT_NOISE_VARIANCE: Final[float] = 25.0

#: Inert fallback bucket used when no :class:`PopulationPrior` is
#: loaded.  These match the v2.1.0 ``_DEFAULT_TARGETS`` LIGHT-stage
#: setpoint so a brand-new install behaves identically to v2.x until
#: the user fills in their profile.
_FALLBACK_PRIOR: Final[dict[str, float]] = {
    "temperature_c": 21.0,
    "humidity_pct": 50.0,
    "brightness_pct": 5.0,
    "temperature_var_c2": 4.0,
    "humidity_var_pct2": 100.0,
    "brightness_var_pct2": 25.0,
}

#: Quality presumed at the prior bucket centre (in score units, 0..100).
#: This is what the prior mean function peaks at.
_PRIOR_PEAK_QUALITY: Final[float] = 80.0

#: Quality presumed in regions with no observations and no prior pull.
#: Observations are centred on this value before fitting so the GP's
#: zero-mean prior corresponds to ``quality == 50``.
_PRIOR_BASELINE_QUALITY: Final[float] = 50.0

#: Decision modes exported via ``sensor.sleep_classifier_decision_mode``
#: (R2.4).  Order matters for the ``rng.integers(0, 3)`` explore-dim
#: lookup.
_EXPLORE_MODES: Final[tuple[str, str, str]] = (
    "explore-temp", "explore-humidity", "explore-brightness",
)

#: Field names mirrored from :class:`GPRecommendation` for
#: ``locked_dimensions`` matching (R2.5).
_DIM_FIELDS: Final[tuple[str, str, str]] = (
    "temperature_c", "humidity_pct", "brightness_pct",
)

#: Threshold below which BAO refuses to use the GP path and falls back
#: to prior-only (R1.2).
_MIN_OBS_FOR_GP: Final[int] = 5

#: Population-prior bucket dimensions accepted by
#: :meth:`PopulationPriorRepository.lookup`.  Anything outside these
#: sets is coerced to a neutral default before lookup so that an
#: unsanitised user profile does not raise.
_VALID_AGE_BANDS: Final[frozenset[str]] = frozenset(
    {"18-25", "26-35", "36-50", "51-65", "65+"}
)
_VALID_SEXES: Final[frozenset[str]] = frozenset({"M", "F", "unspecified"})
_VALID_CHRONOTYPES: Final[frozenset[str]] = frozenset(
    {"morning", "evening", "neutral"}
)
_VALID_SEASONS: Final[frozenset[str]] = frozenset(
    {"spring", "summer", "autumn", "winter"}
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class GPNumericalError(Exception):
    """Raised when the GP cholesky decomposition fails (R1.4).

    The orchestrator catches this exception, falls back to the v2.x
    ``PreferenceLearner.recommend()`` path, and increments
    :attr:`BayesianOptimizer.error_count`.  Three consecutive failures
    auto-disable the BAO pillar.
    """


# ---------------------------------------------------------------------------
# Frozen / slots dataclasses (forward-compat: only stdlib primitives)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class GPHyperparams:
    """Hyperparameters of the 3-D RBF GP.

    :ivar length_scale_temp_c: RBF length scale on the temperature axis
        (°C).
    :ivar length_scale_humidity_pct: RBF length scale on the humidity
        axis (% RH).
    :ivar length_scale_brightness_pct: RBF length scale on the
        brightness axis (% of sensor max).
    :ivar signal_variance: σ_f² in (quality_score)² units.
    :ivar noise_variance: σ_n², additive observation noise variance.
    :ivar schema_version: Wire-format version.  ``1`` for v3.0.0; the
        v3.1.0 federated aggregator may bump to ``2``.
    """

    length_scale_temp_c: float = _DEFAULT_LENGTH_TEMP
    length_scale_humidity_pct: float = _DEFAULT_LENGTH_HUMIDITY
    length_scale_brightness_pct: float = _DEFAULT_LENGTH_BRIGHTNESS
    signal_variance: float = _DEFAULT_SIGNAL_VARIANCE
    noise_variance: float = _DEFAULT_NOISE_VARIANCE
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class GPObservation:
    """One night's environment + quality measurement (R1.3).

    :ivar temperature_c: Average bedroom temperature (°C).
    :ivar humidity_pct: Average relative humidity (%).
    :ivar brightness_pct: Average brightness (% of sensor max).
    :ivar quality_score: 0..100 sleep quality score for the night.
    :ivar timestamp: Unix seconds of session end.
    :ivar install_id: Opaque add-on install identifier; only its
        ``sha256`` is persisted (R14.2).
    """

    temperature_c: float
    humidity_pct: float
    brightness_pct: float
    quality_score: float
    timestamp: float
    install_id: str


@dataclass(frozen=True, slots=True)
class GPRecommendation:
    """Result of a single :meth:`BayesianOptimizer.recommend` call.

    :ivar temperature_c: Recommended bedroom temperature (°C).
    :ivar humidity_pct: Recommended relative humidity (%).
    :ivar brightness_pct: Recommended brightness (%).
    :ivar mode: Decision mode exposed by
        ``sensor.sleep_classifier_decision_mode`` (R2.4).  One of
        ``"exploit"``, ``"explore-temp"``, ``"explore-humidity"``,
        ``"explore-brightness"``, or ``"prior-only"``.
    :ivar posterior_std: ``(σ_T, σ_H, σ_L)`` predictive std at the
        recommended point (R1.7).
    :ivar prior_weight: 0..1; the weight α of the prior in the
        posterior mean (R8.4 / R8.5).
    """

    temperature_c: float
    humidity_pct: float
    brightness_pct: float
    mode: Literal[
        "exploit",
        "explore-temp",
        "explore-humidity",
        "explore-brightness",
        "prior-only",
    ]
    posterior_std: tuple[float, float, float]
    prior_weight: float


@dataclass(frozen=True, slots=True)
class UserProfile:
    """User profile passed to :meth:`BayesianOptimizer.recommend`.

    Distinct from :class:`src.user_profile.UserProfile` — the v2.x
    profile tracks the Bayesian posterior on personal sleep need; this
    one is the inert bucket key for the v3.0.0 population prior plus a
    per-user override on the prior weight (R8.5).  Keeping the two
    classes physically separate prevents the v3 prior bucket lookup
    from accidentally rewiring the v2.x sleep-need update.

    :ivar age_band: One of ``"18-25"``, ``"26-35"``, ``"36-50"``,
        ``"51-65"``, ``"65+"``.  Empty string ⇒ unspecified.
    :ivar sex: ``"M"`` / ``"F"`` / ``"unspecified"``.
    :ivar chronotype: ``"morning"`` / ``"evening"`` / ``"neutral"``.
    :ivar season: ``"spring"`` / ``"summer"`` / ``"autumn"`` /
        ``"winter"``.
    :ivar prior_weight_lock: When non-``None`` the value is used
        verbatim as the prior weight, bypassing the exponential decay
        formula (R8.5 user override).  ``0.0`` lets the user fully
        disable the population prior even before they have 14 nights of
        data; ``1.0`` pins the prior at full strength.
    """

    age_band: str
    sex: str
    chronotype: str
    season: str
    prior_weight_lock: float | None = None


@dataclass(frozen=True, slots=True)
class BAOPersistedState:
    """Snapshot pickled to ``/data/bao_model.pickle`` (R1.6, PR3).

    The wire format is intentionally a plain dataclass tree of stdlib
    primitives so that the v3.1.0 federated aggregator can parse it
    without depending on the v3.0.0 add-on code (forward-compat).

    :ivar install_id_hash: ``sha256(install_id)``; the raw install_id
        is **never** persisted (R14.2).
    :ivar hyperparams: :class:`GPHyperparams` snapshot at persist time.
    :ivar observations: FIFO buffer of ≤ 60 :class:`GPObservation` (R1.6).
    :ivar last_persist_at: ISO-8601 UTC timestamp of the persist call.
    :ivar error_count: Cumulative numerical-error count (R1.4).
    :ivar schema_version: Wire-format version.  ``1`` for v3.0.0.
    """

    install_id_hash: str
    hyperparams: GPHyperparams
    observations: tuple[GPObservation, ...]
    last_persist_at: str
    error_count: int
    schema_version: int = 1


# ---------------------------------------------------------------------------
# BayesianOptimizer
# ---------------------------------------------------------------------------

class BayesianOptimizer:
    """3-D RBF Gaussian Process posterior + Thompson Sampling.

    See :mod:`src.bayesian_optimizer` for the design rationale and
    forward-compat contract.

    :param prior: Optional :class:`PopulationPriorRepository` loaded
        at startup.  ``None`` ⇒ use :data:`_FALLBACK_PRIOR` so that
        BAO degrades gracefully when the prior pickle is missing
        (R8.1).
    :param hyperparams: :class:`GPHyperparams` — defaults are tuned
        for a typical bedroom (temperature 16-28 °C, humidity 30-70 %,
        brightness 0-50 %).
    :param state_path: ``/data/bao_model.pickle`` (PR3 atomic write).
    :param max_observations: Rolling FIFO cap (R1.6); default 60.
    :param exploration_rate: ``[0.0, 0.5]`` per R2.2; default ``0.1``.

    :raises ValueError: When ``max_observations <= 0`` or
        ``exploration_rate`` is outside ``[0.0, 0.5]``.
    """

    __slots__ = (
        "_prior",
        "_hp",
        "_state_path",
        "_max_observations",
        "_exploration_rate",
        "_observations",
        "_X_train",
        "_y_train_centered",
        "_cho_factor",
        "_alpha",
        "_install_id_hash",
        "_error_count",
        "_v3_tasks",
    )

    def __init__(
        self,
        *,
        prior: "PopulationPriorRepository | None",
        hyperparams: GPHyperparams,
        state_path: Path,
        max_observations: int = 60,
        exploration_rate: float = 0.1,
    ) -> None:
        if max_observations <= 0:
            raise ValueError(
                f"max_observations must be > 0, got {max_observations!r}"
            )
        if not (0.0 <= exploration_rate <= 0.5):
            raise ValueError(
                "exploration_rate must be in [0.0, 0.5], got "
                f"{exploration_rate!r}"
            )
        self._prior = prior
        self._hp = hyperparams
        self._state_path = Path(state_path)
        self._max_observations = max_observations
        self._exploration_rate = exploration_rate
        self._observations: list[GPObservation] = []
        # Cached training matrices + cholesky factor; recomputed by
        # ``_refit`` whenever the observation buffer changes.
        self._X_train: np.ndarray | None = None
        self._y_train_centered: np.ndarray | None = None
        self._cho_factor: np.ndarray | None = None
        self._alpha: np.ndarray | None = None
        self._install_id_hash: str = ""
        self._error_count: int = 0
        # ------------------------------------------------------------ #
        # v3.0.0 — fire-and-forget persist task registry (Task 3.3,    #
        # Requirements 1.6 / 11.3).  每次 ``observe`` 后通过            #
        # ``asyncio.create_task`` 派发一次 ``persist()``；派发出来的     #
        # task 进入此清单，主入口 SIGTERM 时通过                       #
        # ``pending_persist_tasks()`` 拿到列表，await asyncio.gather    #
        # 实现优雅退出（PR5）。同步上下文（如测试 / CLI 工具）调用     #
        # ``observe`` 时事件循环不存在，跳过派发，列表保持为空。       #
        # ------------------------------------------------------------ #
        self._v3_tasks: list[asyncio.Task[Any]] = []

    # ------------------------------------------------------------------ #
    # Construction helpers
    # ------------------------------------------------------------------ #

    @classmethod
    def load_or_init(
        cls,
        *,
        state_path: Path,
        prior: "PopulationPriorRepository | None",
        hyperparams: GPHyperparams,
        max_observations: int = 60,
        exploration_rate: float = 0.1,
    ) -> "BayesianOptimizer":
        """Best-effort load; on corruption ⇒ init empty + log WARN.

        :param state_path: Path to ``/data/bao_model.pickle``.
        :returns: A :class:`BayesianOptimizer` with the persisted
            observations replayed (and the cholesky factor rebuilt).
            On any read / unpickle / cholesky failure we log a warning
            and return a fresh empty optimiser so the orchestrator can
            continue (R11.3).
        """
        instance = cls(
            prior=prior,
            hyperparams=hyperparams,
            state_path=state_path,
            max_observations=max_observations,
            exploration_rate=exploration_rate,
        )
        path = Path(state_path)
        if not path.exists():
            return instance
        try:
            raw = path.read_bytes()
        except OSError as exc:
            logger.warning(
                "BAO state at %s unreadable: %s; starting fresh.", path, exc,
            )
            return instance
        try:
            persisted = pickle.loads(raw)
        except (
            pickle.UnpicklingError,
            EOFError,
            AttributeError,
            ImportError,
            ValueError,
            TypeError,
        ) as exc:
            logger.warning(
                "BAO state at %s unparseable: %s; starting fresh.", path, exc,
            )
            return instance
        if not isinstance(persisted, BAOPersistedState):
            logger.warning(
                "BAO state at %s has unexpected type %s; starting fresh.",
                path, type(persisted).__name__,
            )
            return instance
        # Replay observations through the FIFO buffer.
        for obs in persisted.observations:
            if isinstance(obs, GPObservation):
                instance._observations.append(obs)
        if len(instance._observations) > instance._max_observations:
            instance._observations = (
                instance._observations[-instance._max_observations:]
            )
        instance._install_id_hash = persisted.install_id_hash
        instance._error_count = max(0, int(persisted.error_count))
        try:
            instance._refit()
        except GPNumericalError as exc:
            logger.warning(
                "Cholesky failed during BAO state load (%s); "
                "clearing observations to recover.",
                exc,
            )
            instance._observations.clear()
            instance._X_train = None
            instance._y_train_centered = None
            instance._cho_factor = None
            instance._alpha = None
        return instance

    # ------------------------------------------------------------------ #
    # Public read-only accessors
    # ------------------------------------------------------------------ #

    @property
    def n_observations(self) -> int:
        """Current size of the FIFO observation buffer (≤ ``max_observations``)."""
        return len(self._observations)

    @property
    def error_count(self) -> int:
        """Cumulative cholesky / numerical-error count (R1.4)."""
        return self._error_count

    @property
    def should_disable(self) -> bool:
        """Return ``True`` when ``error_count >= 3`` (R11.3 threshold)."""
        return self._error_count >= 3

    # ------------------------------------------------------------------ #
    # observe
    # ------------------------------------------------------------------ #

    def observe(self, obs: GPObservation) -> None:
        """Update the GP posterior with one new observation (R1.3).

        :param obs: New :class:`GPObservation` (one night).
        :raises GPNumericalError: When cholesky decomposition fails;
            the caller catches and falls back to the v2.x recommend
            path (R1.4).  ``error_count`` is bumped before the
            exception propagates.

        Implementation details
        ----------------------

        * Append-then-truncate keeps the buffer bounded at
          ``max_observations`` (FIFO, R1.6).
        * The full ``K + σ_n² I`` kernel matrix is rebuilt from
          scratch on every call.  With ``N ≤ 60`` this is < 200 ms on
          a Pi 4B (R1.3); a rank-1 cholesky update would shave a few
          ms but adds non-trivial complexity not warranted at this
          scale.
        * On a successful refit we fire-and-forget a :meth:`persist`
          task so the on-disk pickle never lags more than one night
          behind the in-memory state (Task 3.3, R1.6).  Cholesky
          failures intentionally **do not** trigger persistence —
          letting a numerically broken state hit ``/data`` would mask
          the underlying issue at the next add-on restart.
        """
        self._observations.append(obs)
        if len(self._observations) > self._max_observations:
            self._observations = (
                self._observations[-self._max_observations:]
            )
        if obs.install_id:
            self._install_id_hash = hashlib.sha256(
                obs.install_id.encode("utf-8")
            ).hexdigest()
        try:
            self._refit()
        except GPNumericalError:
            self._error_count += 1
            raise
        # Refit succeeded — schedule a fire-and-forget persist task so
        # ``/data/bao_model.pickle`` stays in sync with the in-memory
        # FIFO buffer (Task 3.3 / R1.6 / PR5).
        self._schedule_persist()

    def _schedule_persist(self) -> None:
        """Fire-and-forget :meth:`persist` on the running event loop.

        没有运行中的事件循环时（同步测试 / CLI 工具直接调用
        ``observe``）静默跳过，保持 ``observe`` 的同步调用兼容性
        （PR2）。派发出来的 task 进入 ``_v3_tasks``，主入口 SIGTERM
        时通过 :meth:`pending_persist_tasks` 拿到列表后 ``await
        asyncio.gather`` 实现优雅退出（PR5）。

        派发前顺手清理已经 ``done`` 的 task，避免清单无界增长。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # observe 也可能从同步上下文（pytest 同步用例、CLI 工具）
            # 调用——此时事件循环不存在，跳过派发即可。下一个有事件
            # 循环的 observe 会重新尝试派发，最坏情况只是磁盘上的
            # pickle 落后一个 observation，但 in-memory 状态保持正确。
            return
        # 顺手做一次 done-task 清理，避免 ``_v3_tasks`` 无界增长。
        self._v3_tasks = [t for t in self._v3_tasks if not t.done()]
        task = loop.create_task(self.persist())
        self._v3_tasks.append(task)

    def pending_persist_tasks(self) -> tuple["asyncio.Task[Any]", ...]:
        """Return a snapshot of currently in-flight persist tasks.

        主入口在 SIGTERM 时拿这份列表 ``await asyncio.gather(...)`` 实现
        优雅退出（PR5）。返回 tuple 而非 list，调用方不能从外部 mutate
        我们的内部清单。
        """
        # 顺手清理已经 done 的 task，避免 ``_v3_tasks`` 无界增长。
        self._v3_tasks = [t for t in self._v3_tasks if not t.done()]
        return tuple(self._v3_tasks)

    # ------------------------------------------------------------------ #
    # recommend
    # ------------------------------------------------------------------ #

    def recommend(
        self,
        *,
        user_profile: UserProfile,
        current_stage: SleepStage,
        in_wind_down: bool,
        locked_dimensions: frozenset[str] = frozenset(),
        install_id: str = "default",
    ) -> GPRecommendation:
        """Return a Thompson-Sampled or explore setpoint (R2).

        :param user_profile: Bucket key + ``prior_weight_lock`` override.
        :param current_stage: Current :class:`SleepStage` (informational
            for log lines; the GP itself is stage-agnostic in v3.0.0).
        :param in_wind_down: When ``True`` we force ``mode == "exploit"``
            so the user does not see exploratory swings while falling
            asleep (R2.3).
        :param locked_dimensions: Subset of
            ``{"temperature_c", "humidity_pct", "brightness_pct"}`` the
            user has temporarily pinned (R2.5).  Locked dimensions
            always take their value from the posterior-mean argmax;
            the call also forces ``mode == "exploit"`` so that the
            exploration policy never picks a locked dimension.
        :param install_id: Forwarded into the per-decision RNG seed
            (R2.6); empty string ``"default"`` is acceptable for tests.

        :returns: :class:`GPRecommendation` with the chosen setpoint,
            decision mode, and per-dim posterior std.

        Decision tree
        -------------

        ``N < 5``
            Mode ``"prior-only"``; setpoint = prior bucket mean
            (R1.2).

        ``in_wind_down OR locked_dimensions``
            Mode ``"exploit"``; setpoint = Thompson sample with locked
            dims overridden by the posterior-mean argmax (R2.3, R2.5,
            P13).

        Otherwise
            Bernoulli ``exploration_rate`` flip:

            * ``True``  ⇒ ``mode == "explore-{dim}"``; setpoint takes
              the posterior-mean argmax on every dim *except* the
              chosen explore dim, which takes the posterior-σ argmax
              instead (R2.2).
            * ``False`` ⇒ ``mode == "exploit"``; setpoint = Thompson
              sample of ``μ + z · σ`` (R2.1).
        """
        n_obs = len(self._observations)
        bucket = self._lookup_prior_bucket(user_profile)
        prior_pt = np.array(
            [
                bucket["temperature_c"],
                bucket["humidity_pct"],
                bucket["brightness_pct"],
            ],
            dtype=np.float64,
        )
        prior_dim_std = (
            float(math.sqrt(max(bucket["temperature_var_c2"], 0.0))),
            float(math.sqrt(max(bucket["humidity_var_pct2"], 0.0))),
            float(math.sqrt(max(bucket["brightness_var_pct2"], 0.0))),
        )
        prior_weight = self._compute_prior_weight(
            n_obs=n_obs, lock=user_profile.prior_weight_lock,
        )

        # Per-decision repeatable RNG (R2.6).
        today = datetime.now(timezone.utc).date()
        rng = np.random.default_rng(
            self._seed_for_decision(install_id, today)
        )

        # Path 1 — N < 5 ⇒ prior-only.
        if n_obs < _MIN_OBS_FOR_GP:
            return GPRecommendation(
                temperature_c=float(prior_pt[0]),
                humidity_pct=float(prior_pt[1]),
                brightness_pct=float(prior_pt[2]),
                mode="prior-only",
                posterior_std=prior_dim_std,
                prior_weight=prior_weight,
            )

        # Path 2 — N ≥ 5 ⇒ build candidate grid + posterior + sample.
        candidates = self._build_candidate_grid(prior_pt)
        mu_obs, sigma_obs = self._gp_predict(candidates)

        # Blend the prior with observations using ``prior_weight`` α
        # (design.md §3.2.3).  The prior mean function is a Gaussian
        # bump centred at ``prior_pt`` with peak height
        # (PEAK - BASELINE).  The convex combination keeps the
        # posterior calibrated when N is small.
        prior_kernel = self._rbf_kernel(
            candidates, prior_pt[None, :],
        ).flatten()  # shape (M,)
        # Normalise so the bump peaks at exactly 1.0 at ``prior_pt``.
        prior_bump = prior_kernel / max(self._hp.signal_variance, 1e-12)
        prior_residual_mean = (
            (_PRIOR_PEAK_QUALITY - _PRIOR_BASELINE_QUALITY) * prior_bump
        )
        # The prior variance is the average of the bucket's per-dim
        # variances — a rough scalar but enough to dampen σ when the
        # user has lots of cohort data backing them.
        prior_var_scalar = float(np.mean([
            bucket["temperature_var_c2"],
            bucket["humidity_var_pct2"],
            bucket["brightness_var_pct2"],
        ]))
        prior_var_scalar = max(prior_var_scalar, 1e-12)

        mu_combined = (
            prior_weight * prior_residual_mean
            + (1.0 - prior_weight) * mu_obs
        )
        sigma_combined = np.sqrt(
            (prior_weight ** 2) * prior_var_scalar
            + ((1.0 - prior_weight) ** 2) * (sigma_obs ** 2)
        )

        # Decision mode.
        forced_exploit = bool(in_wind_down) or bool(locked_dimensions)
        if forced_exploit:
            mode = "exploit"
        else:
            # rng.random() draws first, rng.integers() draws second —
            # this ordering is intentional so changing the explore
            # rate does not perturb the integers stream when the
            # Bernoulli flip lands on exploit.
            if float(rng.random()) < self._exploration_rate:
                mode = _EXPLORE_MODES[int(rng.integers(0, 3))]
            else:
                mode = "exploit"

        exploit_argmax_idx = int(np.argmax(mu_combined))
        exploit_pt = candidates[exploit_argmax_idx].copy()

        if mode == "exploit":
            z = rng.standard_normal(len(candidates))
            ts_values = mu_combined + z * sigma_combined
            ts_argmax_idx = int(np.argmax(ts_values))
            setpoint = candidates[ts_argmax_idx].copy()
            # Locked dims override with the posterior-mean argmax (P13).
            for d, name in enumerate(_DIM_FIELDS):
                if name in locked_dimensions:
                    setpoint[d] = exploit_pt[d]
        else:  # explore-temp / -humidity / -brightness
            explore_dim_idx = _EXPLORE_MODES.index(mode)
            sigma_argmax_idx = int(np.argmax(sigma_combined))
            setpoint = exploit_pt.copy()
            setpoint[explore_dim_idx] = (
                candidates[sigma_argmax_idx][explore_dim_idx]
            )

        post_std = self.posterior_uncertainty(
            at=(float(setpoint[0]), float(setpoint[1]), float(setpoint[2])),
        )

        return GPRecommendation(
            temperature_c=float(setpoint[0]),
            humidity_pct=float(setpoint[1]),
            brightness_pct=float(setpoint[2]),
            mode=mode,  # type: ignore[arg-type]
            posterior_std=post_std,
            prior_weight=prior_weight,
        )

    # ------------------------------------------------------------------ #
    # posterior_uncertainty
    # ------------------------------------------------------------------ #

    def posterior_uncertainty(
        self, *, at: tuple[float, float, float],
    ) -> tuple[float, float, float]:
        """Return ``(σ_T, σ_H, σ_L)`` predictive std at a 3-D point.

        :param at: ``(temperature_c, humidity_pct, brightness_pct)``
            query point.

        Since the GP posterior over a scalar function has scalar
        variance, all three returned values are numerically identical;
        the triple is used by ``sensor.sleep_classifier_optimizer_uncertainty``
        so the Lovelace card can label the std with the appropriate
        unit (°C / % / lux) per dim (R1.7).
        """
        X_q = np.array(
            [[float(at[0]), float(at[1]), float(at[2])]], dtype=np.float64,
        )
        _mu, sigma = self._gp_predict(X_q)
        s = float(sigma[0])
        return (s, s, s)

    # ------------------------------------------------------------------ #
    # persist
    # ------------------------------------------------------------------ #

    async def persist(self) -> None:
        """Atomic-write the pickle to ``/data/bao_model.pickle`` (PR3).

        Pickling + atomic write happens in
        :func:`asyncio.to_thread` so the asyncio main loop is never
        blocked on disk I/O (tech.md hard rule).
        """
        state = BAOPersistedState(
            install_id_hash=self._install_id_hash,
            hyperparams=self._hp,
            observations=tuple(self._observations),
            last_persist_at=datetime.now(timezone.utc).isoformat(),
            error_count=self._error_count,
            schema_version=1,
        )
        data = pickle.dumps(state, protocol=pickle.HIGHEST_PROTOCOL)
        await asyncio.to_thread(
            _io_utils.atomic_write_bytes, self._state_path, data,
        )

    # ------------------------------------------------------------------ #
    # export_hyperparams_json
    # ------------------------------------------------------------------ #

    def export_hyperparams_json(self) -> dict[str, Any]:
        """Return a plain dict of primitives for v3.1.0 FedAvg.

        :returns: ``dict[str, int | float]`` containing only stdlib
            primitives — no numpy scalars, no dataclasses.  This is the
            forward-compat hyperparameter exchange format described in
            design.md §3.2 (forward-compat hooks).
        """
        return {
            "length_scale_temp_c": float(self._hp.length_scale_temp_c),
            "length_scale_humidity_pct": float(
                self._hp.length_scale_humidity_pct
            ),
            "length_scale_brightness_pct": float(
                self._hp.length_scale_brightness_pct
            ),
            "signal_variance": float(self._hp.signal_variance),
            "noise_variance": float(self._hp.noise_variance),
            "schema_version": int(self._hp.schema_version),
        }

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _refit(self) -> None:
        """Recompute ``K``, cholesky factor, and ``α`` from observations.

        :raises GPNumericalError: When :func:`scipy.linalg.cho_factor`
            or :func:`scipy.linalg.cho_solve` fails (singular kernel
            matrix, NaN inputs, ...).
        """
        if not self._observations:
            self._X_train = None
            self._y_train_centered = None
            self._cho_factor = None
            self._alpha = None
            return
        X = np.array(
            [
                [o.temperature_c, o.humidity_pct, o.brightness_pct]
                for o in self._observations
            ],
            dtype=np.float64,
        )
        y = np.array(
            [o.quality_score for o in self._observations], dtype=np.float64,
        )
        # Centre the observations on the baseline so the GP's
        # zero-mean prior corresponds to ``quality == 50``.
        y_centered = y - _PRIOR_BASELINE_QUALITY

        K = self._rbf_kernel(X, X) + self._hp.noise_variance * np.eye(
            X.shape[0]
        )
        try:
            factor, _lower = scipy.linalg.cho_factor(
                K, lower=True, check_finite=True,
            )
        except (np.linalg.LinAlgError, ValueError) as exc:
            raise GPNumericalError(
                f"Cholesky decomposition failed: {exc}"
            ) from exc
        try:
            alpha = scipy.linalg.cho_solve(
                (factor, True), y_centered, check_finite=True,
            )
        except (np.linalg.LinAlgError, ValueError) as exc:
            raise GPNumericalError(
                f"cho_solve failed: {exc}"
            ) from exc
        self._X_train = X
        self._y_train_centered = y_centered
        self._cho_factor = factor
        self._alpha = alpha

    def _rbf_kernel(self, X1: np.ndarray, X2: np.ndarray) -> np.ndarray:
        """Vectorised 3-D RBF kernel.

        ``k(x, x') = σ_f² · exp(-0.5 · Σ_d ((x_d - x'_d) / l_d)²)``.

        :param X1: ``(N, 3)``.
        :param X2: ``(M, 3)``.
        :returns: ``(N, M)`` kernel matrix.
        """
        l = np.array(
            [
                self._hp.length_scale_temp_c,
                self._hp.length_scale_humidity_pct,
                self._hp.length_scale_brightness_pct,
            ],
            dtype=np.float64,
        )
        X1s = X1 / l
        X2s = X2 / l
        # Pairwise squared distance, ``max(0)`` to absorb floating
        # point round-off before exp().
        d2 = (
            np.sum(X1s ** 2, axis=1)[:, None]
            + np.sum(X2s ** 2, axis=1)[None, :]
            - 2.0 * X1s @ X2s.T
        )
        d2 = np.maximum(d2, 0.0)
        return self._hp.signal_variance * np.exp(-0.5 * d2)

    def _gp_predict(
        self, X_query: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Posterior mean (residual) + std at ``X_query``.

        :param X_query: ``(M, 3)`` query points.
        :returns: ``(mu, sigma)``, each shape ``(M,)``.  ``mu`` is in
            *residual* units (centred on :data:`_PRIOR_BASELINE_QUALITY`),
            ``sigma`` is the predictive standard deviation.

        When the optimiser has no observations (empty buffer) the
        residual mean is zero everywhere and ``sigma`` equals the
        signal std (the kernel's prior std at any point).
        """
        if (
            self._X_train is None
            or self._cho_factor is None
            or self._alpha is None
        ):
            return (
                np.zeros(len(X_query), dtype=np.float64),
                np.full(
                    len(X_query),
                    math.sqrt(self._hp.signal_variance),
                    dtype=np.float64,
                ),
            )
        K_qt = self._rbf_kernel(X_query, self._X_train)  # (M, N)
        mu = K_qt @ self._alpha  # (M,)
        # var = k(x*, x*) - k(x*, X) K^{-1} k(X, x*) (diagonal only).
        v = scipy.linalg.cho_solve(
            (self._cho_factor, True), K_qt.T, check_finite=False,
        )  # shape (N, M)
        # Diagonal of K_qt @ v == elementwise sum across rows.
        var = self._hp.signal_variance - np.einsum("ij,ji->i", K_qt, v)
        # Floor at a small positive value to absorb round-off; the
        # true posterior variance is non-negative analytically.
        var = np.maximum(var, 1e-12)
        return mu, np.sqrt(var)

    def _build_candidate_grid(self, center: np.ndarray) -> np.ndarray:
        """Return a 5×5×5 = 125-point Cartesian grid around ``center``.

        Each dim is sampled at offsets ``[-1, -0.5, 0, +0.5, +1]`` of
        the corresponding length scale, giving a ±1 length-scale
        coverage in each direction.  125 points × 60 observations is a
        ~30 µs einsum; the bottleneck is the 60×60 cholesky in
        :meth:`_refit`.
        """
        ls = np.array(
            [
                self._hp.length_scale_temp_c,
                self._hp.length_scale_humidity_pct,
                self._hp.length_scale_brightness_pct,
            ],
            dtype=np.float64,
        )
        offsets = np.array([-1.0, -0.5, 0.0, 0.5, 1.0], dtype=np.float64)
        # itertools.product would build 125 tuples — vectorising via
        # meshgrid is slightly leaner.
        ot, oh, ob = np.meshgrid(offsets, offsets, offsets, indexing="ij")
        delta = np.stack(
            [ot.ravel() * ls[0], oh.ravel() * ls[1], ob.ravel() * ls[2]],
            axis=1,
        )
        return center[None, :] + delta

    def _lookup_prior_bucket(self, user_profile: UserProfile) -> dict[str, float]:
        """Look up the prior bucket; coerce invalid profile fields.

        :param user_profile: Caller-supplied profile.  Empty / unknown
            field values are coerced to neutral defaults (``"26-35"``,
            ``"unspecified"``, ``"neutral"``, ``"spring"``) so that
            :meth:`PopulationPriorRepository.lookup` always sees valid
            categorical inputs.
        :returns: A plain dict with the same six float fields as
            :data:`_FALLBACK_PRIOR`.
        """
        if self._prior is None:
            return dict(_FALLBACK_PRIOR)
        age_band = (
            user_profile.age_band
            if user_profile.age_band in _VALID_AGE_BANDS
            else "26-35"
        )
        sex = (
            user_profile.sex
            if user_profile.sex in _VALID_SEXES
            else "unspecified"
        )
        chronotype = (
            user_profile.chronotype
            if user_profile.chronotype in _VALID_CHRONOTYPES
            else "neutral"
        )
        season = (
            user_profile.season
            if user_profile.season in _VALID_SEASONS
            else "spring"
        )
        try:
            bucket, _level = self._prior.lookup(
                age_band=age_band,  # type: ignore[arg-type]
                sex=sex,  # type: ignore[arg-type]
                chronotype=chronotype,  # type: ignore[arg-type]
                season=season,  # type: ignore[arg-type]
            )
        except Exception as exc:  # noqa: BLE001 — defensive at boundary
            logger.warning(
                "Prior lookup failed (%s); falling back to defaults.", exc,
            )
            return dict(_FALLBACK_PRIOR)
        return {
            "temperature_c": float(bucket.temperature_mean_c),
            "humidity_pct": float(bucket.humidity_mean_pct),
            "brightness_pct": float(bucket.brightness_mean_pct),
            "temperature_var_c2": float(bucket.temperature_var_c2),
            "humidity_var_pct2": float(bucket.humidity_var_pct2),
            "brightness_var_pct2": float(bucket.brightness_var_pct2),
        }

    @staticmethod
    def _compute_prior_weight(*, n_obs: int, lock: float | None) -> float:
        """Return α ∈ [0, 1] per R8.4 / R8.5.

        :param n_obs: Number of observations currently in the buffer.
        :param lock: When non-``None``, used verbatim (clipped to
            ``[0.0, 1.0]`` for safety) as the prior weight (R8.5).

        Decay formula
        -------------

        ``α(N) = max(0.1, exp(-N / 14))`` for ``N ≥ 1``, with a
        special-case ``α(0) == 1.0`` (no observations ⇒ pure prior).
        The floor at 0.1 ensures the prior never fully disappears, so
        a user with hundreds of nights of data still gets a small
        regularising pull toward their cohort's mean.
        """
        if lock is not None:
            return max(0.0, min(1.0, float(lock)))
        if n_obs <= 0:
            return 1.0
        return max(0.1, math.exp(-n_obs / 14.0))

    @staticmethod
    def _seed_for_decision(install_id: str, today: date) -> int:
        """Repeatable per-decision RNG seed (R2.6).

        :param install_id: Opaque add-on install identifier.
        :param today: ISO date of the decision (UTC).
        :returns: 64-bit unsigned integer suitable for
            :func:`numpy.random.default_rng`.

        Python's built-in :func:`hash` is salted per process, which
        would break day-over-day repeatability across add-on restarts;
        we therefore use ``sha256`` and slice the first 8 bytes.
        """
        seed_input = f"{install_id}|{today.isoformat()}".encode("utf-8")
        digest = hashlib.sha256(seed_input).digest()
        return int.from_bytes(digest[:8], "big")
