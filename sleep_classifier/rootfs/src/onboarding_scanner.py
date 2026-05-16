"""Onboarding wizard 的候选睡眠分期实体扫描器（纯函数）。

模块职责
--------

为 ``sleep_classifier/web_ui.py`` 的「首次安装向导」第 2 步提供后端
逻辑：从 HA ``/api/states`` 响应里挑出「看起来像睡眠分期来源」的实
体，按相关性排序后交给前端展示。

设计纪律
--------

* **纯函数**：本模块不读任何文件、不发任何 HTTP、不持有任何状态。
  HA 状态由调用方（``web_ui.py``）通过共享的 ``HAAPIClient`` 拉
  取后注入，便于把 wizard 的展示逻辑做单测，无需 mock aiohttp。
* **零运行时新依赖**：仅使用 stdlib 的 ``re`` / ``dataclasses`` /
  ``typing``，与 ``tech.md``「运行时仅 aiohttp」的硬约束一致。
* **确定性排序**：相同分数的候选按 ``entity_id`` 升序兜底，避免
  Python ``sort`` 对相等键的实现细节泄漏到前端 UI。

评分规则
--------

关键字集合 ``sleep | 睡眠 | stage | 分期``（``re.IGNORECASE``）：

* ``entity_id`` 命中关键字 → +60；
* ``friendly_name`` 命中关键字 → +40；
* 两个分量各自封顶（重复命中不再叠加），合计封顶 100。

举例：

* ``entity_id = "sensor.bedroom_sleep_stage"``、``friendly_name =
  "Bedroom Sleep Stage"`` → 60 + 40 = 100；
* ``entity_id = "sensor.bedroom_sleep_stage"``、``friendly_name =
  None`` → fallback 把 ``friendly_name`` 视作 ``entity_id`` 自身，
  仍命中两个分量，最终 100；
* ``entity_id = "sensor.living_room_temperature"`` 且 ``friendly_name``
  也无关键字 → 0，不会出现在 :func:`filter_candidates` 结果里。
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .data_structures import CandidateEntity

#: 睡眠分期关键字正则。中英混合，``re.IGNORECASE`` 让 ``Sleep`` /
#: ``SLEEP`` 等变体一并命中；中文字面量在大小写不敏感模式下不变。
#: 暴露为模块级常量是为了让上层（web_ui / 测试）可以复用同一份
#: pattern，避免「评分用一份、过滤用另一份」造成的语义漂移。
SLEEP_STAGE_KEYWORD_PATTERN: re.Pattern[str] = re.compile(
    r"sleep|睡眠|stage|分期", re.IGNORECASE
)

#: ``entity_id`` 命中关键字的权重（命中即加，重复命中不叠加）。
_ENTITY_ID_WEIGHT: int = 60

#: ``friendly_name`` 命中关键字的权重（命中即加，重复命中不叠加）。
_FRIENDLY_NAME_WEIGHT: int = 40

#: 最大评分上限。
_MAX_SCORE: int = 100

#: ``filter_candidates`` 默认接受的 HA domain 白名单。睡眠分期实体
#: 在主流硬件接入下基本只落在这三个 domain：
#:
#: * ``sensor`` —— 大多数手环 / 雷达通过模板传感器暴露文本阶段；
#: * ``binary_sensor`` —— 简化的 awake / asleep 二元开关；
#: * ``input_select`` —— 用户手动模拟阶段（开发 / 调试场景）。
_DEFAULT_DOMAINS: frozenset[str] = frozenset(
    {"sensor", "binary_sensor", "input_select"}
)


def _hits(text: str | None, pattern: re.Pattern[str]) -> bool:
    """判断 *text* 是否被 *pattern* 命中。

    :param text: 待扫描字符串；``None`` / 空串等价于未命中。
    :param pattern: 已编译好的关键字正则。
    :return: ``True`` 当且仅当 ``pattern`` 在 ``text`` 中至少匹配一次。
    """
    if not text:
        return False
    return pattern.search(text) is not None


def _score(
    entity_id: str,
    friendly_name: str | None,
    pattern: re.Pattern[str],
) -> int:
    """评分内部实现，pattern 可注入以便复用与测试。

    每个分量独立判断「是否命中」并按权重一次性计入：

    * ``entity_id`` 命中 → +60；
    * ``friendly_name`` 命中 → +40。

    重复命中（同一字符串里关键字出现多次）不再叠加，避免一个
    ``sensor.sleep_sleep_sleep_stage`` 单凭 entity_id 就把分数刷到
    封顶。两个分量都命中时合计 100，正好等于 :data:`_MAX_SCORE`，
    所以理论上不会越界；保险起见仍做一次 ``min`` clamp。

    :param entity_id: HA 实体 ID。
    :param friendly_name: HA ``attributes.friendly_name``，可空。
    :param pattern: 关键字正则；调用方负责保证大小写不敏感等语义。
    :return: 0–100 的相关性评分（已 clamp）。
    """
    raw = 0
    if _hits(entity_id, pattern):
        raw += _ENTITY_ID_WEIGHT
    if _hits(friendly_name, pattern):
        raw += _FRIENDLY_NAME_WEIGHT
    return min(raw, _MAX_SCORE)


def score_candidate(entity_id: str, friendly_name: str | None) -> int:
    """根据关键字命中度返回 0–100 的相关性评分。

    :param entity_id: HA 实体 ID，例如 ``sensor.bedroom_sleep_stage``。
    :param friendly_name: HA ``attributes.friendly_name``；调用方未拿
        到时可传 ``None`` 或空串，函数内部会把它降级成 ``entity_id``
        自身——等价于 :func:`filter_candidates` 在缺省 friendly_name
        时的 fallback 行为，保证两个 API 的语义一致。
    :return: 0–100 的整数评分。``entity_id`` 命中关键字 +60，
        ``friendly_name`` 命中关键字 +40，两个分量独立计算后求和并
        clamp 到 100。
    :Example:

        >>> score_candidate("sensor.bedroom_sleep_stage", "Bedroom Sleep")
        100
        >>> score_candidate("sensor.bedroom_sleep_stage", None)
        100
        >>> score_candidate("sensor.living_room_temperature", "Living Room")
        0
    """
    effective_friendly = friendly_name if friendly_name else entity_id
    return _score(entity_id, effective_friendly, SLEEP_STAGE_KEYWORD_PATTERN)


def filter_candidates(
    states: Iterable[Mapping[str, Any]],
    *,
    domains: frozenset[str] = _DEFAULT_DOMAINS,
    keyword_pattern: re.Pattern[str] = SLEEP_STAGE_KEYWORD_PATTERN,
) -> list[CandidateEntity]:
    """过滤 + 评分 + 排序 HA states，输出候选睡眠分期实体列表。

    :param states: HA ``/api/states`` 风格的状态字典 Iterable。每个元
        素至少应含 ``entity_id`` 与 ``attributes`` 两个 key；其它字段
        被忽略，以便测试用最小 fixture 覆盖。
    :param domains: 接受的 HA domain 集合，默认白名单为 ``sensor`` /
        ``binary_sensor`` / ``input_select``。不在白名单内的实体直接
        丢弃，即使 ``friendly_name`` 命中关键字也不会出现在结果里。
    :param keyword_pattern: 关键字正则；默认值
        :data:`SLEEP_STAGE_KEYWORD_PATTERN`。允许测试 / 未来 i18n
        注入额外关键字（例如新增 ``sommeil`` 等）。
    :return: 评分 > 0 的候选列表，按 ``score`` 严格降序；同分时按
        ``entity_id`` 升序兜底，保证幂等可复现。

    设计要点：

    * 评分为 0 的实体被显式过滤掉（无关键字命中即不展示），
      避免向用户抛出整屋几百个无关实体；
    * 排序键为 ``(-score, entity_id)``，对应 design §3.7.1 中
      Property 2 的「按 ``score`` 严格降序」要求；
    * ``friendly_name`` 缺省时退化成 ``entity_id`` 自身，与
      :class:`~src.data_structures.CandidateEntity` 的字段语义一致。
    """
    candidates: list[CandidateEntity] = []
    for state in states:
        entity_id = state.get("entity_id")
        if not isinstance(entity_id, str) or "." not in entity_id:
            continue
        domain = entity_id.split(".", 1)[0]
        if domain not in domains:
            continue

        attrs = state.get("attributes") or {}
        friendly_raw = (
            attrs.get("friendly_name") if isinstance(attrs, Mapping) else None
        )
        friendly_name = (
            friendly_raw
            if isinstance(friendly_raw, str) and friendly_raw
            else entity_id
        )

        score = _score(entity_id, friendly_name, keyword_pattern)
        if score <= 0:
            continue

        candidates.append(
            CandidateEntity(
                entity_id=entity_id,
                friendly_name=friendly_name,
                score=score,
            )
        )

    candidates.sort(key=lambda c: (-c.score, c.entity_id))
    return candidates


__all__ = [
    "SLEEP_STAGE_KEYWORD_PATTERN",
    "score_candidate",
    "filter_candidates",
]
