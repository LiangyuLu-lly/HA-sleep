"""BayesianOptimizer 补充覆盖测试（覆盖率 ≥95%）。

这个文件聚焦在 ``tests/test_bayesian_optimizer.py`` 既有套件未触达的
分支：

* ``load_or_init`` 文件不存在 / 含非 GPObservation 元素 / 持久化
  observations 超出 ``max_observations`` 的截断；
* ``should_disable`` 阈值属性；
* ``observe`` 收到空 ``install_id`` 时跳过哈希更新；
* ``recommend`` ``N < _MIN_OBS_FOR_GP=5`` 的 ``"prior-only"`` 短路；
* ``_refit`` 在空 buffer 下的 noop reset；
* ``cho_solve`` 失败时抛 :class:`GPNumericalError`；
* ``_gp_predict`` 在无训练数据时返回先验 std；
* ``posterior_uncertainty`` 在空 buffer 下的边界；
* ``_compute_prior_weight`` 的 ``lock`` / ``n_obs <= 0`` 分支。

风格沿用 ``test_bayesian_optimizer.py``：``prior=None`` 走
``_FALLBACK_PRIOR``，把测试范围限定在 GP / TS / 持久化层，避免被
:mod:`src.population_prior` pickle 加载链路污染。
"""
from __future__ import annotations

import math
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import scipy.linalg

from src.bayesian_optimizer import (
    BAOPersistedState,
    BayesianOptimizer,
    GPHyperparams,
    GPNumericalError,
    GPObservation,
    UserProfile,
    _FALLBACK_PRIOR,
)
from src.data_structures import SleepStage


# ---------------------------------------------------------------------------
# 共用 fixtures / helpers（与 test_bayesian_optimizer.py 同形）
# ---------------------------------------------------------------------------


def _make_optimizer(
    tmp_path: Path, *, max_observations: int = 60,
) -> BayesianOptimizer:
    """构造一个无 prior、默认超参的 BAO（最小测试夹具）。"""
    return BayesianOptimizer(
        prior=None,
        hyperparams=GPHyperparams(),
        state_path=tmp_path / "bao_model.pickle",
        max_observations=max_observations,
    )


def _make_obs(i: int, *, install_id: str = "test-install") -> GPObservation:
    """生成第 ``i`` 个合成 observation（与既有测试形态保持一致）。"""
    return GPObservation(
        temperature_c=21.0 + 0.1 * (i % 11 - 5),
        humidity_pct=50.0 + 0.5 * (i % 7 - 3),
        brightness_pct=5.0 + 0.5 * (i % 5 - 2),
        quality_score=70.0 + (i % 13 - 6),
        timestamp=1_700_000_000.0 + 86_400.0 * i,
        install_id=install_id,
    )


def _default_profile() -> UserProfile:
    return UserProfile(
        age_band="26-35",
        sex="unspecified",
        chronotype="neutral",
        season="spring",
        prior_weight_lock=None,
    )


# ---------------------------------------------------------------------------
# load_or_init —— 缺失分支
# ---------------------------------------------------------------------------


class TestLoadOrInitMissingFile:
    """``state_path`` 不存在时 ``load_or_init`` 直接返回新实例（line 425）。"""

    def test_load_or_init_returns_fresh_when_state_absent(
        self, tmp_path: Path,
    ) -> None:
        # 故意指向不存在的子路径——load_or_init 必须早返回一个空实例，
        # 不抛异常、observations 计数为 0、状态文件也不会被偷偷创建。
        absent = tmp_path / "definitely_does_not_exist.pickle"
        assert not absent.exists()

        opt = BayesianOptimizer.load_or_init(
            state_path=absent,
            prior=None,
            hyperparams=GPHyperparams(),
        )

        assert opt.n_observations == 0
        assert opt.error_count == 0
        # 确认仅仅是「文件不存在 → 早返回」，没有副作用写盘。
        assert not absent.exists()


class TestLoadOrInitFiltersBadObservations:
    """``persisted.observations`` 含非 :class:`GPObservation` 元素时跳过（branch 455->454）。"""

    def test_load_or_init_skips_non_gpobservation_entries(
        self, tmp_path: Path,
    ) -> None:
        # 构造一个 BAOPersistedState，但 observations tuple 内混进
        # 一个普通字典——既有版本的 isinstance 守卫必须把它过滤掉，
        # 同时保留合法的 GPObservation 条目。
        good_obs = _make_obs(0)
        # ``BAOPersistedState`` 是 frozen dataclass，但元素本身不会被
        # 校验类型——这正是我们要的：注入异构数据测过滤分支。
        # 这里使用 ``object.__setattr__`` 绕过 frozen 限制构造 tuple
        # 是不必要的——dataclass 是 frozen 但 tuple 字段本身可装任意
        # 对象。
        mixed: tuple[Any, ...] = (good_obs, {"not": "an obs"}, good_obs)
        state = BAOPersistedState(
            install_id_hash="deadbeef",
            hyperparams=GPHyperparams(),
            observations=mixed,  # type: ignore[arg-type]
            last_persist_at="2024-01-01T00:00:00Z",
            error_count=0,
            schema_version=1,
        )
        path = tmp_path / "bao_model.pickle"
        path.write_bytes(pickle.dumps(state))

        opt = BayesianOptimizer.load_or_init(
            state_path=path,
            prior=None,
            hyperparams=GPHyperparams(),
        )

        # 两个合法的 GPObservation 被保留，非法的字典被跳过。
        assert opt.n_observations == 2
        assert opt.error_count == 0
        assert opt._install_id_hash == "deadbeef"


class TestLoadOrInitTruncatesOverflow:
    """持久化 observations 超过 ``max_observations`` 时尾部截断（line 458）。"""

    def test_load_or_init_truncates_persisted_overflow(
        self, tmp_path: Path,
    ) -> None:
        # 持久化 8 条 obs，但 load 时设 max_observations=5——加载后
        # 只保留尾部 5 条（FIFO 语义）。
        full = tuple(_make_obs(i) for i in range(8))
        state = BAOPersistedState(
            install_id_hash="abc",
            hyperparams=GPHyperparams(),
            observations=full,
            last_persist_at="2024-01-01T00:00:00Z",
            error_count=2,
            schema_version=1,
        )
        path = tmp_path / "bao_model.pickle"
        path.write_bytes(pickle.dumps(state))

        opt = BayesianOptimizer.load_or_init(
            state_path=path,
            prior=None,
            hyperparams=GPHyperparams(),
            max_observations=5,
        )

        assert opt.n_observations == 5
        # 尾部 5 条对应 i=3..7；timestamp 单调递增可以稳定校验。
        kept_ts = [o.timestamp for o in opt._observations]
        expected_ts = [full[i].timestamp for i in range(3, 8)]
        assert kept_ts == expected_ts
        assert opt.error_count == 2


# ---------------------------------------------------------------------------
# should_disable 阈值（line 495）
# ---------------------------------------------------------------------------


class TestShouldDisableThreshold:
    """``error_count`` ≥ 3 ⇒ ``should_disable == True``（R11.3）。"""

    def test_should_disable_false_below_threshold(
        self, tmp_path: Path,
    ) -> None:
        opt = _make_optimizer(tmp_path)
        assert opt.error_count == 0
        assert opt.should_disable is False

    def test_should_disable_true_at_threshold(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        opt = _make_optimizer(tmp_path)
        # 灌一条让 _refit 进 cholesky 路径。
        opt.observe(_make_obs(0))
        # 连续三次 monkeypatch cholesky 失败，让 error_count 累积到 3。
        def _fail(*_a: Any, **_kw: Any) -> Any:
            raise np.linalg.LinAlgError("simulated singular")

        monkeypatch.setattr(scipy.linalg, "cho_factor", _fail)
        for i in range(3):
            with pytest.raises(GPNumericalError):
                opt.observe(_make_obs(i + 1))
        assert opt.error_count == 3
        assert opt.should_disable is True


# ---------------------------------------------------------------------------
# observe —— install_id 为空时不更新哈希（branch 532->536）
# ---------------------------------------------------------------------------


class TestObserveEmptyInstallId:
    """空 ``install_id`` 不会重置已经写入的 ``_install_id_hash``。"""

    def test_empty_install_id_skips_hash_update(
        self, tmp_path: Path,
    ) -> None:
        opt = _make_optimizer(tmp_path)
        # 第一次给非空 install_id：哈希被写入。
        opt.observe(_make_obs(0, install_id="real-install"))
        first_hash = opt._install_id_hash
        assert first_hash != ""

        # 第二次空 install_id：哈希必须保持不变（命中 532->536 短路分支）。
        opt.observe(_make_obs(1, install_id=""))
        assert opt._install_id_hash == first_hash
        assert opt.n_observations == 2


# ---------------------------------------------------------------------------
# recommend —— prior-only 短路（line 663）
# ---------------------------------------------------------------------------


class TestRecommendPriorOnly:
    """``N < _MIN_OBS_FOR_GP=5`` ⇒ ``mode == "prior-only"``（R1.2）。"""

    def test_prior_only_returns_fallback_setpoint(
        self, tmp_path: Path,
    ) -> None:
        opt = _make_optimizer(tmp_path)
        # 0 个 observation，直接走 prior-only 分支。
        rec = opt.recommend(
            user_profile=_default_profile(),
            current_stage=SleepStage.LIGHT,
            in_wind_down=False,
        )
        assert rec.mode == "prior-only"
        # 没有 prior repository 时，prior_pt 来自 _FALLBACK_PRIOR。
        assert rec.temperature_c == pytest.approx(_FALLBACK_PRIOR["temperature_c"])
        assert rec.humidity_pct == pytest.approx(_FALLBACK_PRIOR["humidity_pct"])
        assert rec.brightness_pct == pytest.approx(_FALLBACK_PRIOR["brightness_pct"])
        # n_obs == 0 ⇒ prior_weight == 1.0（_compute_prior_weight 早返回）。
        assert rec.prior_weight == pytest.approx(1.0)
        # posterior_std 来自 prior_dim_std = sqrt(var)。
        assert rec.posterior_std[0] == pytest.approx(
            math.sqrt(_FALLBACK_PRIOR["temperature_var_c2"])
        )
        assert rec.posterior_std[1] == pytest.approx(
            math.sqrt(_FALLBACK_PRIOR["humidity_var_pct2"])
        )
        assert rec.posterior_std[2] == pytest.approx(
            math.sqrt(_FALLBACK_PRIOR["brightness_var_pct2"])
        )

    def test_prior_only_below_min_obs_threshold(
        self, tmp_path: Path,
    ) -> None:
        # 4 条 obs（< _MIN_OBS_FOR_GP=5）仍然走 prior-only。
        opt = _make_optimizer(tmp_path)
        for i in range(4):
            opt.observe(_make_obs(i))
        rec = opt.recommend(
            user_profile=_default_profile(),
            current_stage=SleepStage.DEEP,
            in_wind_down=False,
        )
        assert rec.mode == "prior-only"


# ---------------------------------------------------------------------------
# _refit / _gp_predict —— 空 buffer 路径（lines 841-845, 932）
# ---------------------------------------------------------------------------


class TestRefitEmptyBuffer:
    """空 ``_observations`` 时 ``_refit`` 把缓存全部置 None 然后早返回。"""

    def test_refit_clears_cache_when_empty(self, tmp_path: Path) -> None:
        opt = _make_optimizer(tmp_path)
        # 先灌一个让缓存有值。
        opt.observe(_make_obs(0))
        assert opt._X_train is not None
        assert opt._cho_factor is not None

        # 手动清空 observations 后 _refit：触发 line 841-845 的全置 None
        # 早返回路径。
        opt._observations.clear()
        opt._refit()
        assert opt._X_train is None
        assert opt._y_train_centered is None
        assert opt._cho_factor is None
        assert opt._alpha is None

    def test_gp_predict_empty_buffer_returns_signal_std(
        self, tmp_path: Path,
    ) -> None:
        # 空 buffer 时 _gp_predict 走「无训练数据 → 残差 0、std=sqrt(σ_f²)」
        # 早返回路径（line 932）。posterior_uncertainty 是最薄的对外
        # 入口，刚好覆盖这条分支。
        opt = _make_optimizer(tmp_path)
        sigma = opt.posterior_uncertainty(at=(21.0, 50.0, 5.0))
        expected = math.sqrt(GPHyperparams().signal_variance)
        assert sigma == pytest.approx((expected, expected, expected))


# ---------------------------------------------------------------------------
# _refit —— cho_solve 失败分支（lines 875-876）
# ---------------------------------------------------------------------------


class TestRefitChoSolveFailure:
    """``cho_factor`` 成功但 ``cho_solve`` 失败 ⇒ :class:`GPNumericalError`。"""

    def test_cho_solve_failure_raises_gp_numerical_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 真实 cho_factor 通过，但我们 monkeypatch cho_solve 抛异常，
        # 走第二段 try/except 的 ``raise GPNumericalError("cho_solve
        # failed: ...")`` 路径。
        opt = _make_optimizer(tmp_path)
        opt.observe(_make_obs(0))  # 先有一条 obs，下次会真的进入 cho_solve
        baseline = opt.error_count

        def _fail_solve(*_a: Any, **_kw: Any) -> Any:
            raise np.linalg.LinAlgError("simulated cho_solve failure")

        # 注意只 patch 模块属性 cho_solve；cho_factor 走真实实现。
        monkeypatch.setattr(scipy.linalg, "cho_solve", _fail_solve)

        with pytest.raises(GPNumericalError, match="cho_solve failed"):
            opt.observe(_make_obs(1))
        # 错误计数照例 +1（observe 在 raise 之前自增）。
        assert opt.error_count == baseline + 1


# ---------------------------------------------------------------------------
# _compute_prior_weight —— lock / n_obs <= 0 分支（lines 1052, 1054）
# ---------------------------------------------------------------------------


class TestComputePriorWeight:
    """``_compute_prior_weight`` 三条分支：lock 优先 / N=0 返回 1.0 / 衰减。"""

    def test_lock_overrides_decay(self) -> None:
        # lock 非 None ⇒ 直接 clip 后返回，不看 n_obs（line 1052）。
        assert BayesianOptimizer._compute_prior_weight(
            n_obs=100, lock=0.42,
        ) == pytest.approx(0.42)

    def test_lock_clipped_to_unit_interval(self) -> None:
        # 上界裁剪：lock > 1.0 ⇒ 1.0；下界裁剪：lock < 0.0 ⇒ 0.0。
        assert BayesianOptimizer._compute_prior_weight(
            n_obs=10, lock=2.5,
        ) == 1.0
        assert BayesianOptimizer._compute_prior_weight(
            n_obs=10, lock=-1.0,
        ) == 0.0

    def test_zero_observations_returns_one(self) -> None:
        # n_obs == 0 ⇒ 纯 prior（line 1054）。
        assert BayesianOptimizer._compute_prior_weight(
            n_obs=0, lock=None,
        ) == 1.0

    def test_negative_observations_returns_one(self) -> None:
        # n_obs < 0 也被同一分支吸收（防御性 ≤ 0 判断）。
        assert BayesianOptimizer._compute_prior_weight(
            n_obs=-3, lock=None,
        ) == 1.0

    def test_decay_floor_at_point_one(self) -> None:
        # 大 N 触发 max(0.1, exp(-N/14)) 的下界。
        assert BayesianOptimizer._compute_prior_weight(
            n_obs=1000, lock=None,
        ) == pytest.approx(0.1)

    def test_lock_at_zero_observations_takes_precedence(self) -> None:
        # 同时 n_obs == 0 + lock 给定，lock 仍优先（保证 R8.5 用户
        # 「prior_weight=0」可以彻底关掉先验，即便没有任何观测）。
        assert BayesianOptimizer._compute_prior_weight(
            n_obs=0, lock=0.0,
        ) == 0.0
