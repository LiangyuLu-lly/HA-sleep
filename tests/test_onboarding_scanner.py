"""候选实体扫描完备性 + 评分单调性测试（commercial-readiness-v2.1.0 / Property 2）.

本测试套件验证 :mod:`src.onboarding_scanner` 的两个公开 API：

* :func:`score_candidate` —— 单实体评分。
* :func:`filter_candidates` —— 批量过滤 + 排序。

使用 ``pytest.mark.parametrize`` 构造宽网格合成 HA 状态，覆盖：

1. 所有命中关键字的实体出现在结果中（recall / 无漏召）。
2. 所有未命中关键字的实体不出现在结果中（precision / 无误召）。
3. 结果按 ``(-score, entity_id)`` 严格排序。
4. 空输入 → 空列表。
5. domain 不在白名单 → 即使 friendly_name 命中也排除。
6. ``friendly_name=None`` 时 fallback 到 ``entity_id``。

**Property 2: 候选实体扫描完备性 + 评分单调性**

**Validates: Requirements 7.3**
"""
from __future__ import annotations

import itertools
from typing import Any

import pytest

from src.onboarding_scanner import (
    SLEEP_STAGE_KEYWORD_PATTERN,
    filter_candidates,
    score_candidate,
)
from src.data_structures import CandidateEntity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(
    entity_id: str,
    friendly_name: str | None = None,
) -> dict[str, Any]:
    """构造一个最小 HA state dict。"""
    attrs: dict[str, Any] = {}
    if friendly_name is not None:
        attrs["friendly_name"] = friendly_name
    return {"entity_id": entity_id, "attributes": attrs}


def _has_keyword(text: str | None) -> bool:
    """判断文本是否命中关键字 pattern。"""
    if not text:
        return False
    return SLEEP_STAGE_KEYWORD_PATTERN.search(text) is not None


# ---------------------------------------------------------------------------
# score_candidate 单元测试
# ---------------------------------------------------------------------------

class TestScoreCandidate:
    """score_candidate 评分逻辑验证。"""

    @pytest.mark.parametrize(
        "entity_id, friendly_name, expected_score",
        [
            # 两个分量都命中 → 100
            ("sensor.bedroom_sleep_stage", "Bedroom Sleep Stage", 100),
            # 仅 entity_id 命中 → 60
            ("sensor.bedroom_sleep_stage", "Bedroom Sensor", 60),
            # 仅 friendly_name 命中 → 40
            ("sensor.bedroom_temperature", "Sleep Monitor", 40),
            # 都不命中 → 0
            ("sensor.living_room_temperature", "Living Room Temp", 0),
            # 中文关键字 entity_id 命中，friendly_name 不命中
            ("sensor.睡眠分期", "卧室传感器", 60),
            # 中文 friendly_name 命中
            ("sensor.bedroom_monitor", "睡眠阶段", 40),
            # 大小写不敏感
            ("sensor.SLEEP_STAGE", "BEDROOM SLEEP", 100),
            ("sensor.Sleep_Stage", None, 100),
        ],
    )
    def test_score_known_examples(
        self,
        entity_id: str,
        friendly_name: str | None,
        expected_score: int,
    ) -> None:
        assert score_candidate(entity_id, friendly_name) == expected_score

    def test_friendly_name_none_fallback_to_entity_id(self) -> None:
        """friendly_name=None 时 fallback 到 entity_id 自身。"""
        # entity_id 含关键字 → fallback 后 friendly_name 也命中 → 100
        assert score_candidate("sensor.sleep_stage", None) == 100
        # entity_id 不含关键字 → fallback 后仍不命中 → 0
        assert score_candidate("sensor.temperature", None) == 0

    def test_friendly_name_empty_string_fallback(self) -> None:
        """friendly_name="" 等价于 None，触发 fallback。"""
        assert score_candidate("sensor.sleep_monitor", "") == 100

    def test_score_range_always_0_to_100(self) -> None:
        """评分永远在 [0, 100] 范围内。"""
        # 构造极端 entity_id 含多次关键字
        score = score_candidate(
            "sensor.sleep_sleep_stage_stage_睡眠_分期",
            "Sleep Stage 睡眠分期 sleep stage",
        )
        assert 0 <= score <= 100


# ---------------------------------------------------------------------------
# filter_candidates 参数化网格测试
# ---------------------------------------------------------------------------

#: 合成实体 ID 集合 —— 覆盖命中/未命中 × 各 domain
_ENTITY_IDS_WITH_KEYWORD: list[str] = [
    "sensor.bedroom_sleep_stage",
    "sensor.sleep_monitor",
    "binary_sensor.bed_sleep_detected",
    "input_select.sleep_stage_override",
    "sensor.睡眠分期",
    "sensor.mmwave_stage_detector",
]

_ENTITY_IDS_WITHOUT_KEYWORD: list[str] = [
    "sensor.living_room_temperature",
    "sensor.bedroom_humidity",
    "binary_sensor.motion_detected",
    "input_select.hvac_mode",
    "sensor.power_consumption",
]

_ENTITY_IDS_WRONG_DOMAIN: list[str] = [
    "climate.bedroom_ac",
    "light.bedroom_main",
    "switch.fan_power",
    "automation.bedtime_routine",
    "number.target_temperature",
]

_FRIENDLY_NAMES_WITH_KEYWORD: list[str | None] = [
    "Bedroom Sleep Stage",
    "Sleep Monitor",
    "睡眠阶段传感器",
    "分期检测",
]

_FRIENDLY_NAMES_WITHOUT_KEYWORD: list[str | None] = [
    "Living Room Temperature",
    "Bedroom Humidity",
    "Motion Detector",
    None,
    "",
]


class TestFilterCandidates:
    """filter_candidates 批量过滤 + 排序验证。"""

    def test_empty_input_returns_empty_list(self) -> None:
        """0 候选时返回空列表（与 wizard CTA 联动验证）。"""
        assert filter_candidates([]) == []

    def test_empty_input_various_forms(self) -> None:
        """各种空输入形式都返回空列表。"""
        assert filter_candidates(iter([])) == []
        assert filter_candidates(()) == []

    def test_no_matching_entities_returns_empty(self) -> None:
        """所有实体都不命中关键字 → 空列表。"""
        states = [
            _make_state(eid, "No Keywords Here")
            for eid in _ENTITY_IDS_WITHOUT_KEYWORD
        ]
        assert filter_candidates(states) == []

    @pytest.mark.parametrize("entity_id", _ENTITY_IDS_WITH_KEYWORD)
    def test_recall_keyword_in_entity_id(self, entity_id: str) -> None:
        """entity_id 含关键字的实体必须出现在结果中（无漏召）。"""
        states = [_make_state(entity_id, "Some Friendly Name")]
        result = filter_candidates(states)
        result_ids = {c.entity_id for c in result}
        assert entity_id in result_ids

    @pytest.mark.parametrize("friendly_name", _FRIENDLY_NAMES_WITH_KEYWORD)
    def test_recall_keyword_in_friendly_name(self, friendly_name: str | None) -> None:
        """friendly_name 含关键字的实体必须出现在结果中（无漏召）。"""
        entity_id = "sensor.generic_device"
        states = [_make_state(entity_id, friendly_name)]
        result = filter_candidates(states)
        result_ids = {c.entity_id for c in result}
        assert entity_id in result_ids

    @pytest.mark.parametrize("entity_id", _ENTITY_IDS_WITHOUT_KEYWORD)
    def test_precision_no_keyword_excluded(self, entity_id: str) -> None:
        """entity_id 和 friendly_name 都不含关键字 → 不出现（无误召）。"""
        states = [_make_state(entity_id, "No Keywords Here")]
        result = filter_candidates(states)
        result_ids = {c.entity_id for c in result}
        assert entity_id not in result_ids

    @pytest.mark.parametrize("entity_id", _ENTITY_IDS_WRONG_DOMAIN)
    def test_wrong_domain_excluded_even_if_friendly_name_matches(
        self, entity_id: str
    ) -> None:
        """domain 不在白名单 → 即使 friendly_name 命中也排除。"""
        states = [_make_state(entity_id, "Sleep Stage Monitor 睡眠分期")]
        result = filter_candidates(states)
        result_ids = {c.entity_id for c in result}
        assert entity_id not in result_ids

    def test_sorting_by_score_desc_then_entity_id_asc(self) -> None:
        """结果按 (-score, entity_id) 排序。"""
        states = [
            # score=100 (entity_id + friendly_name 都命中)
            _make_state("sensor.zzz_sleep_stage", "Sleep Stage"),
            # score=100 (entity_id + friendly_name 都命中)
            _make_state("sensor.aaa_sleep_stage", "Sleep Monitor"),
            # score=60 (仅 entity_id 命中)
            _make_state("sensor.bbb_sleep_monitor", "Generic Device"),
            # score=40 (仅 friendly_name 命中)
            _make_state("sensor.ccc_generic", "Sleep Tracker"),
            # score=0 (不命中)
            _make_state("sensor.ddd_temperature", "Room Temp"),
        ]
        result = filter_candidates(states)

        # 验证排序：score 降序，同分 entity_id 升序
        assert len(result) == 4  # ddd_temperature 被排除
        assert result[0].entity_id == "sensor.aaa_sleep_stage"  # 100, aaa
        assert result[1].entity_id == "sensor.zzz_sleep_stage"  # 100, zzz
        assert result[2].entity_id == "sensor.bbb_sleep_monitor"  # 60
        assert result[3].entity_id == "sensor.ccc_generic"  # 40

        # 验证 score 严格降序（同分允许）
        for i in range(len(result) - 1):
            assert result[i].score >= result[i + 1].score
            if result[i].score == result[i + 1].score:
                assert result[i].entity_id < result[i + 1].entity_id

    def test_friendly_name_none_fallback_in_filter(self) -> None:
        """friendly_name=None 时 fallback 到 entity_id。"""
        states = [
            _make_state("sensor.sleep_stage_monitor", None),
            _make_state("sensor.temperature", None),
        ]
        result = filter_candidates(states)
        result_ids = {c.entity_id for c in result}

        # sleep_stage_monitor 命中 → 出现
        assert "sensor.sleep_stage_monitor" in result_ids
        # temperature 不命中 → 不出现
        assert "sensor.temperature" not in result_ids

    def test_friendly_name_stored_as_entity_id_when_none(self) -> None:
        """当 friendly_name 缺失时，CandidateEntity.friendly_name 退化为 entity_id。"""
        states = [_make_state("sensor.sleep_stage", None)]
        result = filter_candidates(states)
        assert len(result) == 1
        assert result[0].friendly_name == "sensor.sleep_stage"


# ---------------------------------------------------------------------------
# Property 2 —— 参数化宽网格：完备性 + 评分单调性
# ---------------------------------------------------------------------------

# 构造混合状态集合的参数化网格
_MIXED_STATES_GRID: list[tuple[str, list[dict[str, Any]]]] = []

# 生成各种组合：有命中的 + 无命中的 + 错误 domain 的
for n_hit in range(0, 4):
    for n_miss in range(0, 4):
        for n_wrong_domain in range(0, 3):
            hit_entities = _ENTITY_IDS_WITH_KEYWORD[:n_hit]
            miss_entities = _ENTITY_IDS_WITHOUT_KEYWORD[:n_miss]
            wrong_entities = _ENTITY_IDS_WRONG_DOMAIN[:n_wrong_domain]

            states: list[dict[str, Any]] = []
            for eid in hit_entities:
                states.append(_make_state(eid, "Some Name"))
            for eid in miss_entities:
                states.append(_make_state(eid, "No Match"))
            for eid in wrong_entities:
                states.append(_make_state(eid, "Sleep Stage 睡眠"))

            label = f"hit={n_hit}_miss={n_miss}_wrong={n_wrong_domain}"
            _MIXED_STATES_GRID.append((label, states))


class TestProperty2CompletenessAndMonotonicity:
    """Property 2: 候选实体扫描完备性 + 评分单调性。

    对参数化生成的宽网格合成 HA 状态，验证三个不变量：
    (a) 所有命中关键字的实体出现在结果中（无漏召）
    (b) 所有未命中的不出现（无误召）
    (c) 结果按 score 严格降序（同分按 entity_id 升序）
    """

    @pytest.mark.parametrize("label,states", _MIXED_STATES_GRID)
    def test_recall_precision_and_sorting(
        self, label: str, states: list[dict[str, Any]]
    ) -> None:
        """对每个合成状态集合验证完备性 + 精确性 + 排序。"""
        result = filter_candidates(states)
        result_ids = {c.entity_id for c in result}

        for state in states:
            entity_id = state["entity_id"]
            domain = entity_id.split(".", 1)[0]
            attrs = state.get("attributes") or {}
            friendly_name = attrs.get("friendly_name")

            # 计算该实体是否应该出现在结果中
            effective_friendly = friendly_name if friendly_name else entity_id
            eid_hits = _has_keyword(entity_id)
            fname_hits = _has_keyword(effective_friendly)
            in_whitelist = domain in {"sensor", "binary_sensor", "input_select"}
            should_appear = in_whitelist and (eid_hits or fname_hits)

            if should_appear:
                # (a) 无漏召
                assert entity_id in result_ids, (
                    f"[{label}] 实体 {entity_id} 命中关键字但未出现在结果中"
                )
            else:
                # (b) 无误召
                assert entity_id not in result_ids, (
                    f"[{label}] 实体 {entity_id} 不应出现在结果中"
                )

        # (c) 排序验证：(-score, entity_id)
        for i in range(len(result) - 1):
            curr = result[i]
            nxt = result[i + 1]
            assert curr.score >= nxt.score, (
                f"[{label}] 排序违反：{curr.entity_id}(score={curr.score}) "
                f"在 {nxt.entity_id}(score={nxt.score}) 之前但分数更低"
            )
            if curr.score == nxt.score:
                assert curr.entity_id < nxt.entity_id, (
                    f"[{label}] 同分排序违反：{curr.entity_id} 应在 "
                    f"{nxt.entity_id} 之前（按 entity_id 升序）"
                )

    @pytest.mark.parametrize(
        "entity_id,friendly_name",
        list(
            itertools.product(
                _ENTITY_IDS_WITH_KEYWORD + _ENTITY_IDS_WITHOUT_KEYWORD,
                _FRIENDLY_NAMES_WITH_KEYWORD + _FRIENDLY_NAMES_WITHOUT_KEYWORD,
            )
        ),
    )
    def test_individual_entity_recall_precision(
        self, entity_id: str, friendly_name: str | None
    ) -> None:
        """对 entity_id × friendly_name 的笛卡尔积验证单实体的召回/精确。"""
        states = [_make_state(entity_id, friendly_name)]
        result = filter_candidates(states)
        result_ids = {c.entity_id for c in result}

        domain = entity_id.split(".", 1)[0]
        in_whitelist = domain in {"sensor", "binary_sensor", "input_select"}

        # 计算 effective friendly_name（与模块内部逻辑一致）
        effective_friendly = (
            friendly_name
            if isinstance(friendly_name, str) and friendly_name
            else entity_id
        )
        eid_hits = _has_keyword(entity_id)
        fname_hits = _has_keyword(effective_friendly)
        should_appear = in_whitelist and (eid_hits or fname_hits)

        if should_appear:
            assert entity_id in result_ids, (
                f"实体 {entity_id} (friendly={friendly_name!r}) "
                f"命中关键字但未出现在结果中"
            )
        else:
            assert entity_id not in result_ids, (
                f"实体 {entity_id} (friendly={friendly_name!r}) "
                f"不应出现在结果中"
            )

    def test_score_monotonicity_entity_id_only_vs_both(self) -> None:
        """entity_id 命中 + friendly_name 命中的分数 ≥ 仅 entity_id 命中。"""
        # 仅 entity_id 命中
        score_eid_only = score_candidate("sensor.sleep_stage", "Generic")
        # 两个都命中
        score_both = score_candidate("sensor.sleep_stage", "Sleep Monitor")
        assert score_both >= score_eid_only

    def test_score_monotonicity_friendly_only_vs_both(self) -> None:
        """entity_id 命中 + friendly_name 命中的分数 ≥ 仅 friendly_name 命中。"""
        # 仅 friendly_name 命中
        score_fname_only = score_candidate("sensor.generic", "Sleep Stage")
        # 两个都命中
        score_both = score_candidate("sensor.sleep_monitor", "Sleep Stage")
        assert score_both >= score_fname_only


# ---------------------------------------------------------------------------
# 边界用例与鲁棒性
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """边界用例与鲁棒性验证。"""

    def test_malformed_entity_id_no_dot(self) -> None:
        """entity_id 不含 '.' → 被跳过。"""
        states = [_make_state("invalid_entity_id", "Sleep Stage")]
        result = filter_candidates(states)
        assert result == []

    def test_entity_id_not_string(self) -> None:
        """entity_id 非字符串 → 被跳过。"""
        states = [{"entity_id": 123, "attributes": {"friendly_name": "Sleep"}}]
        result = filter_candidates(states)
        assert result == []

    def test_missing_entity_id_key(self) -> None:
        """缺少 entity_id key → 被跳过。"""
        states = [{"attributes": {"friendly_name": "Sleep Stage"}}]
        result = filter_candidates(states)
        assert result == []

    def test_attributes_none(self) -> None:
        """attributes 为 None → 不崩溃。"""
        states = [{"entity_id": "sensor.sleep_stage", "attributes": None}]
        result = filter_candidates(states)
        # entity_id 命中 + friendly_name fallback 到 entity_id → 出现
        assert len(result) == 1
        assert result[0].entity_id == "sensor.sleep_stage"

    def test_attributes_missing(self) -> None:
        """缺少 attributes key → 不崩溃。"""
        states = [{"entity_id": "sensor.sleep_stage"}]
        result = filter_candidates(states)
        assert len(result) == 1

    def test_custom_domains_parameter(self) -> None:
        """自定义 domains 参数覆盖默认白名单。"""
        states = [
            _make_state("climate.sleep_ac", "Sleep AC"),
            _make_state("sensor.sleep_stage", "Sleep Stage"),
        ]
        # 仅接受 climate domain
        result = filter_candidates(states, domains=frozenset({"climate"}))
        result_ids = {c.entity_id for c in result}
        assert "climate.sleep_ac" in result_ids
        assert "sensor.sleep_stage" not in result_ids

    def test_candidate_entity_fields(self) -> None:
        """验证返回的 CandidateEntity 字段正确。"""
        states = [_make_state("sensor.sleep_stage", "My Sleep Stage")]
        result = filter_candidates(states)
        assert len(result) == 1
        candidate = result[0]
        assert isinstance(candidate, CandidateEntity)
        assert candidate.entity_id == "sensor.sleep_stage"
        assert candidate.friendly_name == "My Sleep Stage"
        assert candidate.score == 100

    def test_large_state_list_performance(self) -> None:
        """大量实体（500+）不崩溃且结果正确。"""
        states: list[dict[str, Any]] = []
        # 10 个命中的
        for i in range(10):
            states.append(_make_state(f"sensor.sleep_device_{i:03d}", f"Sleep {i}"))
        # 500 个不命中的
        for i in range(500):
            states.append(_make_state(f"sensor.device_{i:03d}", f"Device {i}"))

        result = filter_candidates(states)
        assert len(result) == 10

        # 验证排序
        for i in range(len(result) - 1):
            assert result[i].score >= result[i + 1].score
            if result[i].score == result[i + 1].score:
                assert result[i].entity_id < result[i + 1].entity_id
