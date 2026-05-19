"""Unit tests for :mod:`src.preference_learner`."""
from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import List

import pytest

from src.data_structures import SleepStage
from src.preference_learner import (
    EnvironmentParams,
    PreferenceConfig,
    PreferenceLearner,
    SleepSession,
    compute_quality_score,
    stage_counts_from_sequence,
)


# ---------------------------------------------------------------------------
# Quality scoring
# ---------------------------------------------------------------------------


class TestQualityScore:
    def test_empty_distribution_returns_zero(self):
        assert compute_quality_score({}) == 0.0
        assert compute_quality_score({"AWAKE": 0, "LIGHT": 0}) == 0.0

    def test_good_night_scores_higher_than_bad(self):
        good = {"AWAKE": 5, "LIGHT": 50, "DEEP": 15, "REM": 30}    # near ideal
        bad = {"AWAKE": 50, "LIGHT": 40, "DEEP": 5, "REM": 5}     # fragmented
        assert compute_quality_score(good) > compute_quality_score(bad)
        assert compute_quality_score(good) >= 50.0
        assert compute_quality_score(bad) <= 50.0

    def test_clamped_to_unit_interval(self):
        # All AWAKE — heavily penalised.
        s = compute_quality_score({"AWAKE": 100, "LIGHT": 0, "DEEP": 0, "REM": 0})
        assert 0.0 <= s <= 100.0
        # All DEEP — exceeds 100% before clamp but should not leak past.
        s = compute_quality_score({"AWAKE": 0, "LIGHT": 0, "DEEP": 100, "REM": 0})
        assert 0.0 <= s <= 100.0


class TestStageCountsFromSequence:
    def test_counts_match_input(self):
        seq = [SleepStage.AWAKE, SleepStage.LIGHT, SleepStage.LIGHT,
               SleepStage.DEEP, SleepStage.REM]
        counts = stage_counts_from_sequence(seq)
        assert counts == {"AWAKE": 1, "LIGHT": 2, "DEEP": 1, "REM": 1}

    def test_empty_sequence(self):
        assert stage_counts_from_sequence([]) == {
            "AWAKE": 0, "LIGHT": 0, "DEEP": 0, "REM": 0,
        }


# ---------------------------------------------------------------------------
# Helper to construct sessions
# ---------------------------------------------------------------------------


def _session(
    sid: str,
    quality: float,
    *,
    temp: float = 20.0,
    hum: float = 55.0,
    bright: float = 0.0,
    n: int = 100,
) -> SleepSession:
    return SleepSession(
        session_id=sid,
        started_at=0.0,
        ended_at=0.0,
        env_params=EnvironmentParams(
            temperature_c=temp, humidity_pct=hum, brightness_pct=bright,
        ),
        stage_counts={"AWAKE": 5, "LIGHT": 80, "DEEP": 10, "REM": 5},
        quality_score=quality,
        n_samples=n,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_records_persist_to_disk(self, tmp_path: Path):
        cfg = PreferenceConfig(history_path=str(tmp_path / "history.json"))
        learner = PreferenceLearner(cfg)
        learner.record_session(_session("a", 75.0))
        learner.record_session(_session("b", 60.0))

        assert Path(cfg.history_path).exists()
        with open(cfg.history_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        assert payload["version"] == 1
        assert len(payload["sessions"]) == 2

    def test_reload_from_disk(self, tmp_path: Path):
        cfg = PreferenceConfig(history_path=str(tmp_path / "history.json"))
        learner = PreferenceLearner(cfg)
        learner.record_session(_session("a", 80.0))

        # Second learner instance reads the same file.
        learner2 = PreferenceLearner(cfg)
        assert learner2.n_sessions() == 1
        assert learner2.status().startswith("1 session(s)")

    def test_history_caps_at_max_sessions_kept(self, tmp_path: Path):
        cfg = PreferenceConfig(
            history_path=str(tmp_path / "h.json"),
            max_sessions_kept=5,
        )
        learner = PreferenceLearner(cfg)
        for i in range(10):
            learner.record_session(_session(f"s{i}", 50.0 + i))
        assert learner.n_sessions() == 5

    def test_corrupt_file_falls_back_to_empty(self, tmp_path: Path):
        path = tmp_path / "broken.json"
        path.write_text("not valid json", encoding="utf-8")
        cfg = PreferenceConfig(history_path=str(path))
        learner = PreferenceLearner(cfg)
        assert learner.n_sessions() == 0   # should not raise


# ---------------------------------------------------------------------------
# Recommendation
# ---------------------------------------------------------------------------


class TestRecommendation:
    def test_too_few_sessions_returns_defaults(self, tmp_path: Path):
        cfg = PreferenceConfig(
            history_path=str(tmp_path / "h.json"),
            min_sessions_for_personalisation=3,
        )
        learner = PreferenceLearner(cfg)
        learner.record_session(_session("a", 80.0, temp=18.0))

        defaults = EnvironmentParams(temperature_c=21.0, humidity_pct=55.0,
                                     brightness_pct=10.0)
        rec = learner.recommend(defaults)
        # Only 1 session in history → defaults must be returned verbatim.
        assert rec == defaults

    def test_picks_median_of_top_quantile(self, tmp_path: Path):
        cfg = PreferenceConfig(
            history_path=str(tmp_path / "h.json"),
            min_sessions_for_personalisation=3,
            quality_quantile=0.7,
        )
        learner = PreferenceLearner(cfg)
        # 5 sessions; top 30% = top 2 nights, both with temp=18°C
        learner.record_session(_session("worst", 30.0, temp=24.0))
        learner.record_session(_session("bad",   40.0, temp=23.0))
        learner.record_session(_session("ok",    60.0, temp=22.0))
        learner.record_session(_session("good",  85.0, temp=18.0))
        learner.record_session(_session("best",  95.0, temp=18.0))

        rec = learner.recommend(EnvironmentParams(temperature_c=21.0))
        assert rec.temperature_c == pytest.approx(18.0)

    def test_defaults_fill_missing_fields(self, tmp_path: Path):
        cfg = PreferenceConfig(
            history_path=str(tmp_path / "h.json"),
            min_sessions_for_personalisation=2,
            quality_quantile=0.0,
        )
        learner = PreferenceLearner(cfg)
        # All sessions only have temperature recorded; brightness etc.
        # should fall back to defaults.
        for i in range(3):
            learner.record_session(SleepSession(
                session_id=f"s{i}",
                started_at=0.0,
                ended_at=0.0,
                env_params=EnvironmentParams(temperature_c=19.0),
                stage_counts={"AWAKE": 5, "LIGHT": 70, "DEEP": 15, "REM": 10},
                quality_score=80.0,
                n_samples=100,
            ))
        defaults = EnvironmentParams(
            temperature_c=21.0, humidity_pct=55.0, brightness_pct=8.0,
        )
        rec = learner.recommend(defaults)
        assert rec.temperature_c == pytest.approx(19.0)
        assert rec.humidity_pct == 55.0
        assert rec.brightness_pct == 8.0

    def test_exploration_adds_noise(self, tmp_path: Path):
        cfg = PreferenceConfig(
            history_path=str(tmp_path / "h.json"),
            min_sessions_for_personalisation=2,
            quality_quantile=0.0,
            exploration_rate=0.5,
        )
        learner = PreferenceLearner(cfg, rng=random.Random(0))
        for i in range(3):
            learner.record_session(_session(f"s{i}", 80.0, temp=19.0, hum=55.0))

        defaults = EnvironmentParams(temperature_c=19.0, humidity_pct=55.0)
        rec = learner.recommend(defaults, explore=True)
        # With non-zero noise the recommendation should differ from the
        # historical median (the deterministic random.Random seed makes this
        # reproducible).
        assert rec.temperature_c != pytest.approx(19.0)


# ---------------------------------------------------------------------------
# v1.5.0 — Per-stage env deltas
# ---------------------------------------------------------------------------


def _per_stage_session(
    sid: str,
    *,
    quality: float = 80.0,
    light_temp: float = 21.0,
    deep_temp: float = 19.0,
    rem_temp: float = 19.5,
    awake_temp: float = 23.0,
    light_bright: float = 0.0,
    deep_bright: float = 0.0,
    recorded_at: float = 0.0,
) -> SleepSession:
    """Helper that fabricates a session with a full env_by_stage trace."""
    return SleepSession(
        session_id=sid,
        started_at=0.0,
        ended_at=0.0,
        env_params=EnvironmentParams(temperature_c=light_temp, humidity_pct=55.0),
        stage_counts={"AWAKE": 5, "LIGHT": 80, "DEEP": 10, "REM": 5},
        quality_score=quality,
        n_samples=100,
        recorded_at=recorded_at or time.time(),
        env_by_stage={
            "AWAKE": EnvironmentParams(
                temperature_c=awake_temp, brightness_pct=40.0,
            ),
            "LIGHT": EnvironmentParams(
                temperature_c=light_temp, brightness_pct=light_bright,
            ),
            "DEEP": EnvironmentParams(
                temperature_c=deep_temp, brightness_pct=deep_bright,
            ),
            "REM": EnvironmentParams(
                temperature_c=rem_temp, brightness_pct=0.0,
            ),
        },
    )


class TestPerStageDeltas:
    """``recommend_per_stage_deltas`` is the v1.5.0 marquee feature."""

    def test_empty_learner_returns_none_per_field(self, tmp_path: Path):
        cfg = PreferenceConfig(history_path=str(tmp_path / "h.json"))
        learner = PreferenceLearner(cfg)
        result = learner.recommend_per_stage_deltas()
        # All four stages present, all non-baseline fields None.
        assert set(result) == {"AWAKE", "LIGHT", "DEEP", "REM"}
        for stage in ("AWAKE", "DEEP", "REM"):
            assert result[stage]["temperature_c"] is None
            assert result[stage]["ess"] == 0.0
        # LIGHT is the baseline: zeros, not Nones.
        assert result["LIGHT"]["temperature_c"] == 0.0

    def test_round_trip_preserves_env_by_stage(self, tmp_path: Path):
        """JSON persistence keeps env_by_stage intact."""
        cfg = PreferenceConfig(history_path=str(tmp_path / "h.json"))
        learner = PreferenceLearner(cfg)
        learner.record_session(_per_stage_session("s1"))
        # Force a reload.
        learner2 = PreferenceLearner(cfg)
        sessions = learner2.sessions()
        assert len(sessions) == 1
        assert "DEEP" in sessions[0].env_by_stage
        assert sessions[0].env_by_stage["DEEP"].temperature_c == 19.0

    def test_pre_v15_session_loads_with_empty_env_by_stage(
        self, tmp_path: Path,
    ):
        """Legacy sessions on disk must keep working — no crash, no garbage."""
        history_path = tmp_path / "h.json"
        # Simulate a v1.3 / v1.4 file that never knew about env_by_stage.
        history_path.write_text(json.dumps([{
            "session_id": "legacy",
            "started_at": 0.0,
            "ended_at": 0.0,
            "env_params": {"temperature_c": 20.0, "humidity_pct": 50.0},
            "stage_counts": {"LIGHT": 100},
            "quality_score": 75.0,
            "n_samples": 100,
            # no env_by_stage field!
        }]))
        learner = PreferenceLearner(PreferenceConfig(history_path=str(history_path)))
        sessions = learner.sessions()
        assert len(sessions) == 1
        assert sessions[0].env_by_stage == {}
        # And the learner gracefully returns nothing learnable.
        result = learner.recommend_per_stage_deltas()
        assert result["DEEP"]["temperature_c"] is None

    def test_ess_guard_blocks_under_threshold(self, tmp_path: Path):
        """With only 3 sessions ESS≈3 < 4 → no learned delta yet."""
        cfg = PreferenceConfig(history_path=str(tmp_path / "h.json"))
        learner = PreferenceLearner(cfg)
        for i in range(3):
            learner.record_session(_per_stage_session(f"s{i}"))
        result = learner.recommend_per_stage_deltas()
        # ESS = 3 ↛ above threshold, so DEEP temperature_c stays None.
        assert result["DEEP"]["ess"] < 4.0
        assert result["DEEP"]["temperature_c"] is None

    def test_personalised_delta_after_enough_sessions(self, tmp_path: Path):
        """5 consistent sessions cross ESS=4 → DEEP delta materialises."""
        cfg = PreferenceConfig(history_path=str(tmp_path / "h.json"))
        learner = PreferenceLearner(cfg)
        # Heavy-duvet user: same temp across all stages.  Expected DEEP
        # delta vs LIGHT = 0 °C — completely different from the -2 °C
        # clinical default.
        for i in range(5):
            learner.record_session(_per_stage_session(
                f"s{i}",
                light_temp=19.0,
                deep_temp=19.0,
                rem_temp=19.0,
                awake_temp=21.0,
            ))
        result = learner.recommend_per_stage_deltas()
        assert result["DEEP"]["ess"] >= 4.0
        assert result["DEEP"]["temperature_c"] == pytest.approx(0.0)
        # AWAKE delta = +2 °C, matches the clinical default but is now
        # *learned* rather than assumed.
        assert result["AWAKE"]["temperature_c"] == pytest.approx(2.0)

    def test_weighted_median_robust_to_one_outlier(self, tmp_path: Path):
        """One anomalous night doesn't blow up the learned DEEP delta."""
        cfg = PreferenceConfig(history_path=str(tmp_path / "h.json"))
        learner = PreferenceLearner(cfg)
        # 4 nights at delta = -2, 1 anomalous night at delta = +5.
        for i in range(4):
            learner.record_session(_per_stage_session(
                f"s{i}", light_temp=21.0, deep_temp=19.0,
            ))
        learner.record_session(_per_stage_session(
            "anomaly", light_temp=21.0, deep_temp=26.0,
        ))
        result = learner.recommend_per_stage_deltas()
        # Median of [-2, -2, -2, -2, +5] = -2, NOT the mean (-0.4).
        assert result["DEEP"]["temperature_c"] == pytest.approx(-2.0)

    def test_per_field_independence(self, tmp_path: Path):
        """Brightness can be learned even when humidity is missing."""
        cfg = PreferenceConfig(history_path=str(tmp_path / "h.json"))
        learner = PreferenceLearner(cfg)
        # 5 sessions with temperature + brightness but no humidity.
        for i in range(5):
            learner.record_session(_per_stage_session(
                f"s{i}",
                light_temp=21.0,
                deep_temp=19.0,
                light_bright=10.0,
                deep_bright=0.0,
            ))
        result = learner.recommend_per_stage_deltas()
        # Temperature delta learned.
        assert result["DEEP"]["temperature_c"] == pytest.approx(-2.0)
        # Brightness delta learned (-10 %).
        assert result["DEEP"]["brightness_pct"] == pytest.approx(-10.0)
        # Humidity unknown — never recorded → None even with ESS ≥ 4.
        assert result["DEEP"]["humidity_pct"] is None

    def test_effective_sample_size_kish_formula(self, tmp_path: Path):
        """ESS = n when all weights are equal."""
        cfg = PreferenceConfig(history_path=str(tmp_path / "h.json"))
        learner = PreferenceLearner(cfg)
        # 6 fresh sessions → all weights ~1, ESS ≈ 6.
        for i in range(6):
            learner.record_session(_per_stage_session(f"s{i}"))
        result = learner.recommend_per_stage_deltas(now=time.time())
        assert result["DEEP"]["n_sessions"] == 6
        # Allow some slack for decay over the millisecond between record
        # and read.
        assert result["DEEP"]["ess"] > 5.9


# ---------------------------------------------------------------------------
# v1.9.0 — Timezone robustness
# ---------------------------------------------------------------------------


class TestTimezoneRobustness:
    """Verify recommend_bedtime handles DST transitions gracefully."""

    def test_recommend_bedtime_handles_dst_transition(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """recommend_bedtime must not crash when now_local() lands on a
        DST spring-forward boundary (e.g. 2024-03-31 02:30 in CET,
        which doesn't exist in wall-clock time but can appear if the
        system clock is slightly off or the TZ database is stale).

        We monkeypatch now_local to return a naive datetime on the
        transition day and verify the method returns a sane result.
        """
        from datetime import datetime
        from src import _time_utils

        # Simulate a DST spring-forward day: 2024-03-31 02:30 (CET→CEST).
        # In reality 02:30 doesn't exist (clocks jump 02:00→03:00), but
        # naive local datetimes can still hold this value.
        dst_moment = datetime(2024, 3, 31, 2, 30, 0)
        monkeypatch.setattr(_time_utils, "now_local", lambda: dst_moment)

        cfg = PreferenceConfig(
            history_path=str(tmp_path / "h.json"),
            min_sessions_for_personalisation=3,
        )
        learner = PreferenceLearner(cfg)

        # Seed enough sessions so the bedtime recommendation fires.
        # Place bedtimes around 23:00 on weekdays.
        base_ts = datetime(2024, 3, 25, 23, 0, 0).timestamp()
        for i in range(5):
            learner.record_session(SleepSession(
                session_id=f"dst_{i}",
                started_at=base_ts + i * 86400,
                ended_at=base_ts + i * 86400 + 7 * 3600,
                env_params=EnvironmentParams(temperature_c=20.0, humidity_pct=50.0),
                stage_counts={"AWAKE": 5, "LIGHT": 70, "DEEP": 15, "REM": 10},
                quality_score=75.0,
                n_samples=100,
            ))

        # Must not raise — and should return a non-None weekday bedtime.
        result = learner.recommend_bedtime(now=dst_moment)
        assert result is not None
        assert result["weekday_bedtime"] is not None
        # Sanity: the recommended bedtime should be in the evening range.
        hh = int(result["weekday_bedtime"].split(":")[0])
        assert hh >= 20 or hh <= 2, f"Unexpected bedtime hour: {hh}"


# ---------------------------------------------------------------------------
# v3.0.0 — Session listener hook (Task 4.2)
# ---------------------------------------------------------------------------
#
# 这些测试覆盖 Requirements 4.2 / 11.3 关键不变量：
#   1. listener 在 record_session 持久化成功后被 fire-and-forget 调用
#   2. listener 抛异常仅 log + 计数，不传播；不影响 record_session 返回
#   3. 派发的 task 进入 _v3_tasks，可由 pending_listener_tasks() 拿到
#      用于 PR5 优雅退出
#   4. 没有运行中事件循环时 record_session 仍可同步调用（向后兼容 v2.x）


import asyncio


class TestSessionListenerHook:
    async def test_listener_invoked_after_record_session(self, tmp_path: Path):
        """注册的 async listener 在 record_session 后被调用。"""
        cfg = PreferenceConfig(history_path=str(tmp_path / "h.json"))
        learner = PreferenceLearner(cfg)

        seen: List[SleepSession] = []

        async def listener(session: SleepSession) -> None:
            seen.append(session)

        learner.add_session_listener(listener)
        s = _session("a", 80.0)
        learner.record_session(s)

        # fire-and-forget — drain 所有 pending listener task。
        await asyncio.gather(*learner.pending_listener_tasks())

        assert len(seen) == 1
        assert seen[0].session_id == "a"

    async def test_record_session_signature_unchanged(self, tmp_path: Path):
        """PR2 不变量：record_session 仍返回 None，仍是同步可调用。"""
        cfg = PreferenceConfig(history_path=str(tmp_path / "h.json"))
        learner = PreferenceLearner(cfg)
        # 没有 listener 注册的情况下，record_session 行为完全等价 v2.x。
        result = learner.record_session(_session("a", 80.0))
        assert result is None
        assert learner.pending_listener_tasks() == ()

    async def test_listener_exception_does_not_propagate(self, tmp_path: Path):
        """Listener 抛异常只 log + 计数，不污染调用栈。"""
        cfg = PreferenceConfig(history_path=str(tmp_path / "h.json"))
        learner = PreferenceLearner(cfg)

        async def boom(session: SleepSession) -> None:
            raise RuntimeError("listener exploded")

        learner.add_session_listener(boom)
        # record_session 必须正常返回，不冒泡 RuntimeError。
        learner.record_session(_session("a", 80.0))

        # 等所有 task 跑完，让 _invoke_listener 捕获异常并计数。
        await asyncio.gather(
            *learner.pending_listener_tasks(), return_exceptions=True,
        )

        assert learner.listener_error_count == 1

    async def test_multiple_listeners_isolated(self, tmp_path: Path):
        """多个 listener 互相隔离：一个失败不阻止其它执行。"""
        cfg = PreferenceConfig(history_path=str(tmp_path / "h.json"))
        learner = PreferenceLearner(cfg)

        good_calls: List[str] = []

        async def good(session: SleepSession) -> None:
            good_calls.append(session.session_id)

        async def bad(session: SleepSession) -> None:
            raise ValueError("bad listener")

        # 顺序：bad → good，验证 good 仍然被调用。
        learner.add_session_listener(bad)
        learner.add_session_listener(good)

        learner.record_session(_session("a", 80.0))
        await asyncio.gather(
            *learner.pending_listener_tasks(), return_exceptions=True,
        )

        assert good_calls == ["a"]
        assert learner.listener_error_count == 1

    async def test_pending_tasks_registered_for_graceful_shutdown(
        self, tmp_path: Path,
    ):
        """派发的 task 进入 _v3_tasks，可被 PR5 SIGTERM 流程 await。"""
        cfg = PreferenceConfig(history_path=str(tmp_path / "h.json"))
        learner = PreferenceLearner(cfg)

        gate = asyncio.Event()

        async def slow(session: SleepSession) -> None:
            await gate.wait()  # 故意挂住，模拟慢 listener

        learner.add_session_listener(slow)
        learner.record_session(_session("a", 80.0))

        pending = learner.pending_listener_tasks()
        assert len(pending) == 1
        assert not pending[0].done()
        # 返回值应为 tuple，调用方不能从外部 mutate 内部清单。
        assert isinstance(pending, tuple)

        # 释放 listener 让 task 完结，避免污染后续测试的 event loop。
        gate.set()
        await asyncio.gather(*pending)
        # done task 在下一次访问时被自动剔除。
        assert learner.pending_listener_tasks() == ()

    def test_record_session_works_without_running_loop(self, tmp_path: Path):
        """record_session 从同步上下文调用（无 loop）时不抛异常。"""
        cfg = PreferenceConfig(history_path=str(tmp_path / "h.json"))
        learner = PreferenceLearner(cfg)

        called = []

        async def listener(session: SleepSession) -> None:
            called.append(session.session_id)

        learner.add_session_listener(listener)
        # 不在 async 测试函数里 → 没有 running loop。
        # 期望：log + 跳过 listener 派发，不抛 RuntimeError。
        learner.record_session(_session("sync", 70.0))

        # listener 没有被调度（没有 loop 接住 task）。
        assert called == []
        assert learner.pending_listener_tasks() == ()
