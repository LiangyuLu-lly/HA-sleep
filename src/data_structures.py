"""跨模块共享的核心数据类型。

v1.3.0 移除本地 CNN-BiLSTM 模型之后，本模块里原先大部分的
``HeartRateData`` / ``MovementData`` / ``EDFHeader`` / ``Dataset`` /
``TrainingSet`` / ``ModelWeights`` / ``PerformanceMetrics`` /
``MQTTMessage`` 等 dataclass 都成了死代码——整个 ``src/`` 和
``tests/`` 对它们零引用，它们只会把 numpy 拉进运行时依赖、
让新人读 data_structures.py 时误以为项目仍然跑深度模型。

因此本模块现在只导出 :class:`SleepStage` 枚举——这是唯一被
子模块（orchestrator、preference_learner、smart_environment_controller、
smart_wake、external_stage_subscriber、whitenoise_matcher、
sleep_state_publisher、sleep_quality_score）共用的类型。

保留这个枚举是因为：

* 子模块通过 :attr:`SleepStage.name` 做持久化（``SleepSession.stage_counts``
  以字符串键存盘），枚举是唯一的命名权威；
* 整数值 ``0..3`` 与 HA 行业里常见的 0-based 睡眠阶段编码一致
  （见 :mod:`src.external_stage_subscriber` 的 ``_NUMERIC_0_BASED``
  表），同时也覆盖了 Mi Band / Withings 的 1-based 变体映射。
"""
from __future__ import annotations

from enum import Enum


class SleepStage(Enum):
    """睡眠阶段枚举——跨模块的单一定义来源。

    值约定（0-based）与 :mod:`src.external_stage_subscriber` 里的
    数字解析保持一致；新增阶段（比如细分 N1/N2）必须沿用连续整数，
    避免破坏 HA 实体里已经落库的状态值。
    """

    AWAKE = 0   # 清醒
    LIGHT = 1   # 浅睡（N1 + N2）
    DEEP = 2    # 深睡（N3 / SWS）
    REM = 3     # 快速眼动


__all__ = ["SleepStage"]
