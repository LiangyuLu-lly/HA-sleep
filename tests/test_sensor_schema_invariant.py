"""PR2 sensor schema 不变量测试 —— 锁定 v2.1.0 的 20 个 sensor 契约。

为什么需要这份测试
==================

PR2 是 ``algorithmic-moat-v3.0.0`` spec 中最强的「向后兼容」契约：

> 现有 20 个 ``sensor.sleep_classifier_*`` SHALL 保留 entity_id +
> attribute schema，新 sensor 仅追加（绝不修改 / 删除现有 entity_id 与
> 既有 attribute key）。

下游 Lovelace 仪表板、HA 自动化、脚本 / Node-RED 流程都把这 20 个
entity_id 当作稳定 ABI 来引用；任何 rename / delete / 删 attribute 的
变更都会**静默**让用户的自动化失效。本测试通过两条约束守住该契约：

1. ``BASELINE_V2_1_0`` fixture 把 v2.1.0 的 ``(entity_id,
   frozenset(attribute_keys))`` 对**逐字嵌入测试**；
2. 跑「一晚合成数据」（覆盖所有 ``publish_*`` 入口）后断言：
   - 实际发布的 entity_id 集合 == baseline；
   - 每个 baseline entity 的 attribute key 集合 ⊇ baseline（允许追加，
     例如 ``confidence_pct`` / ``temperature_c`` 等运行期写入字段）；
   - 当 4 个 v3.0.0 算法模块均未注入（``v3_modules_loaded=False``）时，
     14 个 v3 sensor 一个都不能露面 —— 这是 R11.4 / PR2 字节级等价回退到
     v2.1.0 行为的硬保证（design §6.3）。

Validates: Requirements 11.6
"""
from __future__ import annotations

from typing import Any, Dict, Set, Tuple
from unittest.mock import AsyncMock

import pytest

from src.data_structures import SleepStage
from src.sleep_state_publisher import SleepStatePublisher


# --------------------------------------------------------------------- #
# Baseline fixture —— v2.1.0 schema snapshot.                           #
# --------------------------------------------------------------------- #
# 列表项 = (entity_id, frozenset(<attribute keys ALWAYS published>))。
# 这些 key 全部来自 ``src.sleep_state_publisher`` 中的 ``_STATIC_ATTRS_*``
# 字典；任何 ``publish_*`` 调用都会先 ``dict(_STATIC_ATTRS_*)`` 复制一份，
# 再按需追加运行期字段。因此 baseline 是该 entity 在任意发布路径下都
# 一定写入的「最小集合」，运行期可追加，但不能删除。

BASELINE_V2_1_0: Tuple[Tuple[str, frozenset], ...] = (
    (
        "sensor.sleep_classifier_stage",
        frozenset({"friendly_name", "icon", "device_class", "options"}),
    ),
    (
        "sensor.sleep_classifier_confidence",
        frozenset({"friendly_name", "icon", "unit_of_measurement", "state_class"}),
    ),
    (
        "sensor.sleep_classifier_quality_score",
        frozenset({"friendly_name", "icon", "unit_of_measurement", "state_class"}),
    ),
    (
        "sensor.sleep_classifier_session_duration",
        frozenset({
            "friendly_name", "icon", "unit_of_measurement",
            "device_class", "state_class",
        }),
    ),
    (
        "sensor.sleep_classifier_last_action",
        frozenset({"friendly_name", "icon"}),
    ),
    (
        "sensor.sleep_classifier_debt_hours",
        frozenset({"friendly_name", "icon", "unit_of_measurement", "state_class"}),
    ),
    (
        "sensor.sleep_classifier_recommended_bedtime",
        frozenset({"friendly_name", "icon", "device_class"}),
    ),
    (
        "sensor.sleep_classifier_wake_decision",
        frozenset({"friendly_name", "icon", "device_class", "options"}),
    ),
    (
        "sensor.sleep_classifier_soundscape",
        frozenset({"friendly_name", "icon", "device_class", "options"}),
    ),
    (
        "sensor.sleep_classifier_learned_bedtime_workday",
        frozenset({"friendly_name", "icon"}),
    ),
    (
        "sensor.sleep_classifier_learned_bedtime_weekend",
        frozenset({"friendly_name", "icon"}),
    ),
    (
        "sensor.sleep_classifier_learned_environment",
        frozenset({"friendly_name", "icon"}),
    ),
    (
        "sensor.sleep_classifier_recommendation_explain",
        frozenset({"friendly_name", "icon", "device_class", "options"}),
    ),
    (
        "sensor.sleep_classifier_per_stage_deltas",
        frozenset({"friendly_name", "icon", "device_class", "options"}),
    ),
    (
        "sensor.sleep_classifier_apnea_index",
        frozenset({
            "friendly_name", "icon", "device_class", "options", "disclaimer",
        }),
    ),
    (
        "sensor.sleep_classifier_health",
        frozenset({"friendly_name", "icon", "device_class", "options"}),
    ),
    (
        "sensor.sleep_classifier_quality_architecture",
        frozenset({"friendly_name", "icon", "unit_of_measurement", "state_class"}),
    ),
    (
        "sensor.sleep_classifier_quality_efficiency",
        frozenset({"friendly_name", "icon", "unit_of_measurement", "state_class"}),
    ),
    (
        "sensor.sleep_classifier_quality_fragmentation",
        frozenset({"friendly_name", "icon", "unit_of_measurement", "state_class"}),
    ),
    (
        "sensor.sleep_classifier_quality_onset",
        frozenset({"friendly_name", "icon", "unit_of_measurement", "state_class"}),
    ),
)

V2_1_0_ENTITY_IDS: frozenset = frozenset(eid for eid, _ in BASELINE_V2_1_0)


# v3.0.0 新增的 14 个 sensor —— 当 ``v3_modules_loaded=False`` 时一个都不能
# 出现在 publisher 发出的 ``update_state`` 调用里（R11.4 字节级等价回退）。
V3_NEW_ENTITY_IDS: frozenset = frozenset({
    "sensor.sleep_classifier_optimizer_health",
    "sensor.sleep_classifier_optimizer_status",
    "sensor.sleep_classifier_optimizer_uncertainty",
    "sensor.sleep_classifier_decision_mode",
    "sensor.sleep_classifier_locked_dimensions",
    "sensor.sleep_classifier_quality_trend_14d",
    "sensor.sleep_classifier_attribution",
    "sensor.sleep_classifier_attribution_full",
    "sensor.sleep_classifier_prior_status",
    "sensor.sleep_classifier_prior_weight",
    "sensor.sleep_classifier_predictor_health",
    "sensor.sleep_classifier_predictor_status",
    "sensor.sleep_classifier_predictor_hit_rate_7d",
    "sensor.sleep_classifier_v3_health_summary",
})


# --------------------------------------------------------------------- #
# Fixtures                                                               #
# --------------------------------------------------------------------- #


@pytest.fixture
def ha_client() -> AsyncMock:
    """伪 HA 客户端：仅暴露 ``update_state`` async mock。"""
    client = AsyncMock()
    client.update_state = AsyncMock(return_value=None)
    return client


@pytest.fixture
def publisher(ha_client: AsyncMock) -> SleepStatePublisher:
    """全新 publisher，**不**调用 ``set_v3_modules`` —— 模拟 4 个 v3 flag
    全关的 v2.1.0 等价模式（design §6.3 / R11.4）。"""
    return SleepStatePublisher(ha_client, confidence_deadband=0.05)


# --------------------------------------------------------------------- #
# Helpers                                                                #
# --------------------------------------------------------------------- #


async def _simulate_one_night(publisher: SleepStatePublisher) -> None:
    """覆盖所有 v2.1.0 ``publish_*`` 入口，模拟一晚合成数据流。

    顺序贴近真实 add-on 启动 + 推断循环：
    boot → stage transitions → quality + duration → debt / wake / sound
    → learning panel → apnea + last_action + health。
    """
    # 1) Boot：写一次 20 个 sensor 的占位值。
    await publisher.publish_initial_placeholders()

    # 2) stage 切换：4 个阶段各写一次。stage_changed 为 True 时强制触发
    #    confidence 同步发布（覆盖 ENTITY_STAGE / ENTITY_CONFIDENCE）。
    await publisher.publish_stage(
        SleepStage.AWAKE,
        0.42,
        env_temperature_c=22.5,
        env_humidity_pct=50.0,
        env_brightness_pct=10.0,
    )
    await publisher.publish_stage(SleepStage.LIGHT, 0.85)
    await publisher.publish_stage(SleepStage.DEEP, 0.91)
    await publisher.publish_stage(SleepStage.REM, 0.78)

    # 3) Quality / duration / 4 个子分。
    await publisher.publish_quality(82.5)
    await publisher.publish_duration(28800.0)  # 8 h
    await publisher.publish_quality_sub_scores({
        "architecture": 80.0,
        "efficiency": 88.0,
        "fragmentation": 75.0,
        "onset": 92.0,
    })

    # 4) Debt / bedtime / wake / soundscape。
    await publisher.publish_debt(
        1.5,
        severity="mild",
        target_hours=8.0,
        nights_to_full_recovery=2,
    )
    await publisher.publish_recommended_bedtime(
        None,
        tonight_target_hours=8.0,
        reason="weekday default",
    )
    await publisher.publish_wake_decision(
        "fire_now",
        reason="REM detected",
        matched_stage="REM",
    )
    await publisher.publish_soundscape("brown_noise", volume_pct=30.0)

    # 5) Learning panel：4 个 v1.3.0 学习 sensor + 1 个 v1.5.0 per-stage 表。
    await publisher.publish_learned_bedtime({
        "weekday_bedtime": "23:30",
        "weekend_bedtime": "00:00",
        "n_workday": 12,
        "n_weekend": 5,
        "confidence": 0.7,
        "tonight_bucket": "weekday",
    })
    await publisher.publish_learned_environment(
        {
            "temperature_c": 19.5,
            "humidity_pct": 50.0,
            "brightness_pct": 5.0,
            "fan_speed_pct": 0.0,
        },
        confidence=0.8,
        n_used=10,
    )
    await publisher.publish_recommendation_explain({
        "ready": True,
        "method": "knn",
        "n_total": 20,
        "avg_age_days": 7.0,
        "decay_half_life_days": 14.0,
        "effective_sample_size": 6.5,
        "recommendation": "warmer",
        "bedtime": "23:30",
        "confidence": 0.78,
        "reason": "neighbours match",
        "neighbors": [],
    })
    await publisher.publish_per_stage_deltas({
        "AWAKE": {"temperature_c": 0.5, "ess": 5.0, "n_sessions": 8},
        "LIGHT": {"temperature_c": 0.0, "ess": 4.5, "n_sessions": 7},
        "DEEP": {"temperature_c": -0.8, "ess": 6.0, "n_sessions": 9},
        "REM": {"temperature_c": -0.5, "ess": 4.2, "n_sessions": 5},
    })

    # 6) v1.7+ apnea trend、v1.6+ last_action、v1.8 health。
    await publisher.publish_apnea_index(
        "calibrating",
        status={
            "enabled": True,
            "consent": True,
            "calibration_nights_required": 7,
            "calibration_nights_completed": 3,
        },
    )
    await publisher.publish_last_action(
        "climate.bedroom → 19.5 °C", executed=True,
    )
    await publisher.publish_health(
        stage_source_stale=False,
        env_stale_fields=[],
        publisher_failures=0,
        learner_sessions=12,
        capability_skipped=0,
    )


def _collect_entity_attr_keys(
    ha_client: AsyncMock,
) -> Dict[str, Set[str]]:
    """聚合所有 ``update_state`` 调用 → ``{entity_id: union(attr keys)}``."""
    result: Dict[str, Set[str]] = {}
    for call in ha_client.update_state.call_args_list:
        # call.args[0] = entity_id；attributes 走 keyword（_safe_update 总是
        # 通过 ``attributes=attrs`` 传）。容错读 args 也兼容老代码路径。
        entity_id = call.args[0]
        attrs: Dict[str, Any] = call.kwargs.get("attributes") or {}
        result.setdefault(entity_id, set()).update(attrs.keys())
    return result


# --------------------------------------------------------------------- #
# Tests                                                                  #
# --------------------------------------------------------------------- #


class TestSensorSchemaInvariantV2_1_0:
    """PR2 不变量：v2.1.0 schema 不能被破坏。"""

    def test_baseline_size_is_exactly_20(self) -> None:
        """baseline 必须正好 20 个 sensor —— 防止本测试自身被悄悄缩减。"""
        assert len(BASELINE_V2_1_0) == 20
        assert len(V2_1_0_ENTITY_IDS) == 20

    def test_baseline_entity_ids_use_namespace_prefix(self) -> None:
        """所有 baseline entity_id 都用统一项目前缀。"""
        for entity_id, _ in BASELINE_V2_1_0:
            assert entity_id.startswith("sensor.sleep_classifier_"), (
                f"{entity_id} 未使用 sensor.sleep_classifier_* 前缀"
            )

    async def test_published_entity_ids_match_baseline_exactly(
        self,
        publisher: SleepStatePublisher,
        ha_client: AsyncMock,
    ) -> None:
        """跑一晚合成数据后 → 实际发布的 entity_id 集合与 baseline 完全一致。"""
        await _simulate_one_night(publisher)

        captured = set(_collect_entity_attr_keys(ha_client).keys())

        missing = V2_1_0_ENTITY_IDS - captured
        assert missing == set(), (
            f"v2.1.0 baseline 中的 entity_id 缺失（不允许被删除！）："
            f"{sorted(missing)}"
        )

        extras = captured - V2_1_0_ENTITY_IDS
        assert extras == set(), (
            "v3_modules_loaded=False 时 publisher 不应发布任何额外 sensor，"
            f"但发现：{sorted(extras)}"
        )

    async def test_baseline_attribute_keys_are_superset_preserved(
        self,
        publisher: SleepStatePublisher,
        ha_client: AsyncMock,
    ) -> None:
        """每个 baseline entity 的 attribute key 集合 ⊇ baseline。

        允许新增字段（运行期写入 ``confidence_pct`` / ``temperature_c`` /
        ``executed`` 等），但**不允许删除**任何 baseline key —— 否则下游
        Lovelace / 自动化对该字段的引用会静默失效。
        """
        await _simulate_one_night(publisher)

        captured = _collect_entity_attr_keys(ha_client)

        for entity_id, expected_keys in BASELINE_V2_1_0:
            assert entity_id in captured, (
                f"baseline entity 未被发布：{entity_id}"
            )
            actual_keys = captured[entity_id]
            missing_keys = expected_keys - actual_keys
            assert missing_keys == set(), (
                f"{entity_id} 缺失 baseline attribute key："
                f"{sorted(missing_keys)}（PR2 schema 不变量被破坏）"
            )

    async def test_v3_sensors_not_published_when_modules_not_loaded(
        self,
        publisher: SleepStatePublisher,
        ha_client: AsyncMock,
    ) -> None:
        """PR2 硬契约：4 个 v3 flag 全关时 14 个 v3 sensor 一个都不能露面。

        这是 R11.4「4 个 flag 全 false 时字节级等价回退到 v2.1.0」与 PR2
        「新 sensor 仅追加」共同要求的最强保证。``v3_modules_loaded`` 默认
        ``False``，编排层（task 8.1）只有在至少一个 flag 启用时才会调用
        ``set_v3_modules``。
        """
        # 显式断言初始状态 —— 防止未来重构悄悄改默认值。
        assert publisher.v3_modules_loaded is False

        await _simulate_one_night(publisher)

        captured = set(_collect_entity_attr_keys(ha_client).keys())

        leaked = captured & V3_NEW_ENTITY_IDS
        assert leaked == set(), (
            f"v3_modules_loaded=False 时仍有 v3 sensor 被发布："
            f"{sorted(leaked)}（PR2 / R11.4 字节级等价回退被破坏）"
        )
