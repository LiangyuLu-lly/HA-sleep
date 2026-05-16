# sleep_classifier/lovelace_template.py
"""预制 Lovelace dashboard 模板（v2.1.0 商业化补完 R8）。

为什么这个模块存在
-----------------
v2.0.3 用户想要 4-view dashboard 必须复制 ``examples/lovelace_dashboard.yaml``
到 HA 的 Raw configuration editor，对不熟悉 YAML 的新用户非常劝退（详见
requirements.md R8 Bug Condition）。v2.1.0 通过 ``Web UI → Import Lovelace
Dashboard`` 一键导入，调用 HA WebSocket ``lovelace/config/save`` 写入此模块
返回的纯字典。

设计约束（来自 design.md §3.8 / §4.6）
-------------------------------------
* **纯函数**：``build_dashboard_config()`` 不读任何外部文件、不做 I/O，常量
  全部内嵌在本模块。这样 ``Web UI`` 单元测试与 dashboard importer 的 mock
  路径都不需要 monkeypatch 文件系统。
* **不引入 yaml 运行时依赖**：HA WebSocket ``lovelace/config/save`` 接受
  JSON 字典 body，本模块返回 ``dict[str, Any]``，由 ``HAAPIClient`` 序列化
  为 JSON 即可，无需 ``import yaml``。
* **实体引用闭包**：``REFERENCED_ENTITIES`` 必须是 ``SleepStatePublisher``
  与 ``LearningPanelPublisher`` 在 v2.0.3 状态下声明的 20 个
  ``sensor.sleep_classifier_*`` 实体的子集（P8.1 静态守护，详见
  ``tests/test_lovelace_template.py``）。
* **4-view 结构**：与 ``examples/lovelace_dashboard.yaml`` 等价，分为
  Tonight / Stage / Learning / Diagnostics 四个视图，分别承担「实时一览 /
  阶段+趋势 / 学习面板 / 健康+质量细分」职责。
"""
from __future__ import annotations

from typing import Any, Final


# --------------------------------------------------------------------- #
# Public constants — Web UI / HA WebSocket payload 元数据                #
# --------------------------------------------------------------------- #

DASHBOARD_TITLE: Final[str] = "Sleep Classifier"
"""HA 侧边栏显示的标题。"""

DASHBOARD_URL_PATH: Final[str] = "sleep-classifier"
"""dashboard 的 url_path；与 ``Web UI`` 覆盖检测路径一致（R8.3 / R8.3a）。"""

DASHBOARD_ICON: Final[str] = "mdi:bed-clock"
"""HA 侧边栏图标。"""


# --------------------------------------------------------------------- #
# Entity ID 常量 — 与 src/sleep_state_publisher.py 声明严格一致            #
# --------------------------------------------------------------------- #
#
# 注意：此处显式列出而不是 ``from src.sleep_state_publisher import ENTITY_*``，
# 原因是 ``sleep_classifier/`` 目录在 Add-on 镜像中作为顶层包（不依赖
# ``src/`` 包路径），引入跨包 import 会破坏 Web UI 的最小依赖闭包。两端
# 一致性由 ``tests/test_lovelace_template.py`` 静态断言守护（P8.1）。

# Tonight / Stage 视图核心
_E_STAGE: Final[str] = "sensor.sleep_classifier_stage"
_E_CONFIDENCE: Final[str] = "sensor.sleep_classifier_confidence"
_E_QUALITY_SCORE: Final[str] = "sensor.sleep_classifier_quality_score"
_E_SESSION_DURATION: Final[str] = "sensor.sleep_classifier_session_duration"
_E_LAST_ACTION: Final[str] = "sensor.sleep_classifier_last_action"

# Learning / Stage 视图：偏好学习面板
_E_LEARNED_BEDTIME_WORKDAY: Final[str] = (
    "sensor.sleep_classifier_learned_bedtime_workday"
)
_E_LEARNED_BEDTIME_WEEKEND: Final[str] = (
    "sensor.sleep_classifier_learned_bedtime_weekend"
)
_E_LEARNED_ENVIRONMENT: Final[str] = (
    "sensor.sleep_classifier_learned_environment"
)
_E_RECOMMENDATION_EXPLAIN: Final[str] = (
    "sensor.sleep_classifier_recommendation_explain"
)
_E_PER_STAGE_DELTAS: Final[str] = "sensor.sleep_classifier_per_stage_deltas"

# Learning 视图：自然睡眠 / 唤醒
_E_DEBT_HOURS: Final[str] = "sensor.sleep_classifier_debt_hours"
_E_RECOMMENDED_BEDTIME: Final[str] = (
    "sensor.sleep_classifier_recommended_bedtime"
)
_E_WAKE_DECISION: Final[str] = "sensor.sleep_classifier_wake_decision"
_E_SOUNDSCAPE: Final[str] = "sensor.sleep_classifier_soundscape"

# Diagnostics 视图：健康 + 质量子分 + 呼吸暂停趋势
_E_HEALTH: Final[str] = "sensor.sleep_classifier_health"
_E_APNEA_INDEX: Final[str] = "sensor.sleep_classifier_apnea_index"
_E_QUALITY_ARCHITECTURE: Final[str] = (
    "sensor.sleep_classifier_quality_architecture"
)
_E_QUALITY_EFFICIENCY: Final[str] = (
    "sensor.sleep_classifier_quality_efficiency"
)
_E_QUALITY_FRAGMENTATION: Final[str] = (
    "sensor.sleep_classifier_quality_fragmentation"
)
_E_QUALITY_ONSET: Final[str] = "sensor.sleep_classifier_quality_onset"


REFERENCED_ENTITIES: Final[frozenset[str]] = frozenset({
    _E_STAGE,
    _E_CONFIDENCE,
    _E_QUALITY_SCORE,
    _E_SESSION_DURATION,
    _E_LAST_ACTION,
    _E_LEARNED_BEDTIME_WORKDAY,
    _E_LEARNED_BEDTIME_WEEKEND,
    _E_LEARNED_ENVIRONMENT,
    _E_RECOMMENDATION_EXPLAIN,
    _E_PER_STAGE_DELTAS,
    _E_DEBT_HOURS,
    _E_RECOMMENDED_BEDTIME,
    _E_WAKE_DECISION,
    _E_SOUNDSCAPE,
    _E_HEALTH,
    _E_APNEA_INDEX,
    _E_QUALITY_ARCHITECTURE,
    _E_QUALITY_EFFICIENCY,
    _E_QUALITY_FRAGMENTATION,
    _E_QUALITY_ONSET,
})
"""dashboard 引用的全部 ``sensor.sleep_classifier_*`` 实体。

P8.1 守护：此集合必须是 ``SleepStatePublisher`` /
``LearningPanelPublisher`` 已声明的 20 个实体的子集。
"""


# --------------------------------------------------------------------- #
# View builders — 每个视图独立构造，便于审阅与未来增删                    #
# --------------------------------------------------------------------- #

def _view_tonight() -> dict[str, Any]:
    """View 1 — 实时一览：现在睡到什么阶段、质量分、最近一次操作。"""
    return {
        "title": "Tonight",
        "path": "tonight",
        "icon": "mdi:bed",
        "cards": [
            {
                "type": "vertical-stack",
                "cards": [
                    {
                        "type": "glance",
                        "title": "Live status",
                        "columns": 4,
                        "entities": [
                            {"entity": _E_STAGE, "name": "Stage"},
                            {"entity": _E_CONFIDENCE, "name": "Confidence"},
                            {"entity": _E_QUALITY_SCORE, "name": "Quality"},
                            {"entity": _E_SESSION_DURATION, "name": "Duration"},
                        ],
                    },
                    {
                        "type": "gauge",
                        "entity": _E_QUALITY_SCORE,
                        "name": "Tonight quality",
                        "min": 0,
                        "max": 100,
                        "severity": {"green": 75, "yellow": 50, "red": 0},
                    },
                    {
                        "type": "entities",
                        "title": "Last action",
                        "entities": [
                            {"entity": _E_LAST_ACTION, "name": "Last action"},
                            {
                                "type": "attribute",
                                "entity": _E_LAST_ACTION,
                                "attribute": "executed",
                                "name": "Executed?",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_LAST_ACTION,
                                "attribute": "skipped_by_capability",
                                "name": "Skipped (capability)",
                            },
                        ],
                    },
                ],
            },
            {
                "type": "history-graph",
                "title": "Stage trace (12h)",
                "hours_to_show": 12,
                "entities": [{"entity": _E_STAGE}],
            },
        ],
    }


def _view_stage() -> dict[str, Any]:
    """View 2 — 阶段诊断：per-stage 学习偏移 + 呼吸暂停趋势 + 短期 trace。"""
    return {
        "title": "Stage",
        "path": "stage",
        "icon": "mdi:sleep",
        "cards": [
            {
                "type": "vertical-stack",
                "cards": [
                    {
                        "type": "glance",
                        "title": "Stage signals",
                        "columns": 3,
                        "entities": [
                            {"entity": _E_STAGE, "name": "Stage"},
                            {"entity": _E_CONFIDENCE, "name": "Confidence"},
                            {"entity": _E_SESSION_DURATION, "name": "Duration"},
                        ],
                    },
                    {
                        "type": "entities",
                        "title": "Learned per-stage deltas (vs LIGHT)",
                        "entities": [
                            {
                                "entity": _E_PER_STAGE_DELTAS,
                                "name": "State",
                                "secondary_info": "last-changed",
                            },
                            {"type": "section", "label": "Temperature delta"},
                            {
                                "type": "attribute",
                                "entity": _E_PER_STAGE_DELTAS,
                                "attribute": "awake_temperature_c_delta",
                                "name": "AWAKE Δ",
                                "suffix": " °C",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_PER_STAGE_DELTAS,
                                "attribute": "deep_temperature_c_delta",
                                "name": "DEEP Δ",
                                "suffix": " °C",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_PER_STAGE_DELTAS,
                                "attribute": "rem_temperature_c_delta",
                                "name": "REM Δ",
                                "suffix": " °C",
                            },
                            {"type": "section", "label": "Effective sample size"},
                            {
                                "type": "attribute",
                                "entity": _E_PER_STAGE_DELTAS,
                                "attribute": "ess_threshold",
                                "name": "Threshold",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_PER_STAGE_DELTAS,
                                "attribute": "awake_ess",
                                "name": "AWAKE ESS",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_PER_STAGE_DELTAS,
                                "attribute": "deep_ess",
                                "name": "DEEP ESS",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_PER_STAGE_DELTAS,
                                "attribute": "rem_ess",
                                "name": "REM ESS",
                            },
                        ],
                    },
                    {
                        "type": "entities",
                        "title": "Apnea / hypopnea trend",
                        "entities": [
                            {"entity": _E_APNEA_INDEX, "name": "Trend"},
                            {
                                "type": "attribute",
                                "entity": _E_APNEA_INDEX,
                                "attribute": "disclaimer",
                                "name": "Disclaimer",
                            },
                        ],
                    },
                ],
            },
            {
                "type": "history-graph",
                "title": "Stage + confidence (24h)",
                "hours_to_show": 24,
                "entities": [
                    {"entity": _E_STAGE},
                    {"entity": _E_CONFIDENCE},
                ],
            },
        ],
    }


def _view_learning() -> dict[str, Any]:
    """View 3 — 偏好学习面板：bedtime / 环境 / 推荐理由 / 唤醒 / 音景。"""
    return {
        "title": "Learning",
        "path": "learning",
        "icon": "mdi:brain",
        "cards": [
            {
                "type": "vertical-stack",
                "cards": [
                    {
                        "type": "glance",
                        "title": "Bedtime predictions",
                        "columns": 3,
                        "entities": [
                            {
                                "entity": _E_RECOMMENDED_BEDTIME,
                                "name": "Tonight",
                            },
                            {
                                "entity": _E_LEARNED_BEDTIME_WORKDAY,
                                "name": "Workday",
                            },
                            {
                                "entity": _E_LEARNED_BEDTIME_WEEKEND,
                                "name": "Weekend",
                            },
                        ],
                    },
                    {
                        "type": "entities",
                        "title": "Best-fit environment (k-NN)",
                        "entities": [
                            {
                                "entity": _E_LEARNED_ENVIRONMENT,
                                "name": "Tonight",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_LEARNED_ENVIRONMENT,
                                "attribute": "temperature_c",
                                "name": "Temperature",
                                "suffix": " °C",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_LEARNED_ENVIRONMENT,
                                "attribute": "humidity_pct",
                                "name": "Humidity",
                                "suffix": " %",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_LEARNED_ENVIRONMENT,
                                "attribute": "brightness_pct",
                                "name": "Brightness",
                                "suffix": " %",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_LEARNED_ENVIRONMENT,
                                "attribute": "confidence",
                                "name": "Confidence",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_LEARNED_ENVIRONMENT,
                                "attribute": "n_used",
                                "name": "Sessions used",
                            },
                        ],
                    },
                    {
                        "type": "entities",
                        "title": "Why this recommendation?",
                        "entities": [
                            {
                                "entity": _E_RECOMMENDATION_EXPLAIN,
                                "name": "State",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_RECOMMENDATION_EXPLAIN,
                                "attribute": "method",
                                "name": "Algorithm",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_RECOMMENDATION_EXPLAIN,
                                "attribute": "n_total",
                                "name": "Total sessions",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_RECOMMENDATION_EXPLAIN,
                                "attribute": "avg_age_days",
                                "name": "Avg age (days)",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_RECOMMENDATION_EXPLAIN,
                                "attribute": "effective_sample_size",
                                "name": "ESS",
                            },
                        ],
                    },
                    {
                        "type": "gauge",
                        "entity": _E_DEBT_HOURS,
                        "name": "Sleep debt",
                        "min": 0,
                        "max": 12,
                        "severity": {"green": 0, "yellow": 4, "red": 8},
                        "unit": " h",
                    },
                    {
                        "type": "entities",
                        "title": "Smart wake",
                        "entities": [
                            {"entity": _E_WAKE_DECISION, "name": "Decision"},
                            {
                                "type": "attribute",
                                "entity": _E_WAKE_DECISION,
                                "attribute": "alarm_time",
                                "name": "Alarm time",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_WAKE_DECISION,
                                "attribute": "light_ramp_start",
                                "name": "Light ramp start",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_WAKE_DECISION,
                                "attribute": "matched_stage",
                                "name": "Matched stage",
                            },
                        ],
                    },
                    {
                        "type": "entities",
                        "title": "Soundscape",
                        "entities": [
                            {"entity": _E_SOUNDSCAPE, "name": "Current"},
                            {
                                "type": "attribute",
                                "entity": _E_SOUNDSCAPE,
                                "attribute": "volume_pct",
                                "name": "Volume",
                                "suffix": " %",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_SOUNDSCAPE,
                                "attribute": "reason",
                                "name": "Reason",
                            },
                        ],
                    },
                ],
            },
        ],
    }


def _view_diagnostics() -> dict[str, Any]:
    """View 4 — 健康 + 质量子分 + 长期趋势。"""
    return {
        "title": "Diagnostics",
        "path": "diagnostics",
        "icon": "mdi:gauge",
        "cards": [
            {
                "type": "vertical-stack",
                "cards": [
                    {
                        "type": "entities",
                        "title": "System health",
                        "entities": [
                            {"entity": _E_HEALTH, "name": "Aggregate"},
                            {
                                "type": "attribute",
                                "entity": _E_HEALTH,
                                "attribute": "stage_source_stale",
                                "name": "Stage source stale",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_HEALTH,
                                "attribute": "env_stale_fields",
                                "name": "Env stale fields",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_HEALTH,
                                "attribute": "publisher_failures",
                                "name": "Publisher failures",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_HEALTH,
                                "attribute": "learner_sessions",
                                "name": "Learner sessions",
                            },
                        ],
                    },
                    {
                        "type": "glance",
                        "title": "Quality sub-scores (0-100)",
                        "columns": 4,
                        "entities": [
                            {
                                "entity": _E_QUALITY_ARCHITECTURE,
                                "name": "Architecture",
                            },
                            {
                                "entity": _E_QUALITY_EFFICIENCY,
                                "name": "Efficiency",
                            },
                            {
                                "entity": _E_QUALITY_FRAGMENTATION,
                                "name": "Fragmentation",
                            },
                            {"entity": _E_QUALITY_ONSET, "name": "Onset"},
                        ],
                    },
                    {
                        "type": "gauge",
                        "entity": _E_QUALITY_EFFICIENCY,
                        "name": "Sleep efficiency",
                        "min": 0,
                        "max": 100,
                        "severity": {"green": 85, "yellow": 70, "red": 0},
                    },
                    {
                        "type": "entities",
                        "title": "Last action drill-down",
                        "entities": [
                            {"entity": _E_LAST_ACTION, "name": "Last action"},
                            {
                                "type": "attribute",
                                "entity": _E_LAST_ACTION,
                                "attribute": "skipped_unavailable",
                                "name": "Skipped (unavailable)",
                            },
                            {
                                "type": "attribute",
                                "entity": _E_LAST_ACTION,
                                "attribute": "skipped_user_override",
                                "name": "Skipped (user override)",
                            },
                        ],
                    },
                ],
            },
            {
                "type": "history-graph",
                "title": "Quality trend (30 days)",
                "hours_to_show": 720,
                "entities": [
                    {"entity": _E_QUALITY_SCORE},
                    {"entity": _E_QUALITY_ARCHITECTURE},
                    {"entity": _E_QUALITY_EFFICIENCY},
                    {"entity": _E_QUALITY_FRAGMENTATION},
                    {"entity": _E_QUALITY_ONSET},
                ],
            },
            {
                "type": "history-graph",
                "title": "Sleep debt (30 days)",
                "hours_to_show": 720,
                "entities": [{"entity": _E_DEBT_HOURS}],
            },
            {
                "type": "statistics-graph",
                "title": "Confidence (7 days)",
                "chart_type": "line",
                "period": "hour",
                "days_to_show": 7,
                "stat_types": ["mean", "min", "max"],
                "entities": [_E_CONFIDENCE],
            },
        ],
    }


# --------------------------------------------------------------------- #
# Public entry point                                                    #
# --------------------------------------------------------------------- #

def build_dashboard_config() -> dict[str, Any]:
    """构造 4-view dashboard 配置 dict。

    返回结构与 ``examples/lovelace_dashboard.yaml`` 等价（覆盖同样 20 个
    ``sensor.sleep_classifier_*`` 实体），可直接作为 HA WebSocket
    ``lovelace/config/save`` 的 body 使用。

    Pure function — 不读外部文件、不做 I/O，每次返回的都是新构造的
    ``dict``（views 列表与卡片字典都是全新对象，调用方可安全
    mutate）。
    """
    return {
        "title": DASHBOARD_TITLE,
        "views": [
            _view_tonight(),
            _view_stage(),
            _view_learning(),
            _view_diagnostics(),
        ],
    }


__all__ = [
    "DASHBOARD_TITLE",
    "DASHBOARD_URL_PATH",
    "DASHBOARD_ICON",
    "REFERENCED_ENTITIES",
    "build_dashboard_config",
]
