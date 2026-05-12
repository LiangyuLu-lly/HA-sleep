""":mod:`src.data_structures` 的最小回归测试。

v1.3.0 后本模块只剩 :class:`SleepStage` 一个枚举（其它 dataclass
全是 CNN-BiLSTM 时代遗留，整个项目零引用），所以这里只测枚举
的值约定——子模块用 ``SleepStage.name`` 做字符串持久化、用整数
值做 HA 数字态解析，这两个不变量值得锁死。
"""
from __future__ import annotations

from src.data_structures import SleepStage


class TestSleepStageEnum:
    def test_values_are_zero_based_and_contiguous(self) -> None:
        # 子模块（比如 external_stage_subscriber 的 _NUMERIC_0_BASED
        # 表）直接依赖 0..3 这个整数约定——改动必须破坏测试。
        assert SleepStage.AWAKE.value == 0
        assert SleepStage.LIGHT.value == 1
        assert SleepStage.DEEP.value == 2
        assert SleepStage.REM.value == 3

    def test_names_match_persistence_keys(self) -> None:
        # SleepSession.stage_counts 以 name 为键存盘；改名等于破坏
        # 所有用户的 user_preferences.json。
        expected = {"AWAKE", "LIGHT", "DEEP", "REM"}
        assert {s.name for s in SleepStage} == expected

    def test_no_extra_members_leaked(self) -> None:
        # 确保没有从旧版残留进来的额外成员（比如 N1/N2）。
        assert len(SleepStage) == 4
