"""持久化字段缺失安全测试（commercial-readiness-v2.1.0 / Property 11）.

This test suite exercises :func:`src._overrides_schema.apply_v2_1_0_defaults`
under every possible combination of *missing* v2.1.0 fields, asserting that
the function:

1. **Never raises** —— ``KeyError`` / ``ValidationError`` 等异常都不许冒泡。
2. 总是返回**最隐私友好默认值**（``onboarding_skipped=False``、
   ``telemetry_enabled=False``、``upgrade_notifications_enabled=True``）
   填补缺失字段。
3. **不破坏** v2.0.3 既有字段（``sleep_stage_source`` 等槽位绑定）。
4. **保留**任何外来键（例如来自 ``/data/last_upgrade_check.json`` 的
   ``checked_at`` / ``latest`` / ``notified``，理论上不会出现在
   ``web_ui_overrides.json`` 中，但函数对额外键必须保持鲁棒）。

The combinatorial sweep substitutes for hypothesis-style PBT: with
``itertools.combinations`` we enumerate **every** subset of the v2.1.0
keys, giving exhaustive (not random-sampled) coverage of the field-
absent state space.

**Property 11: 持久化字段缺失安全**

**Validates: Requirements 6.6, 7.8, 9.3**
"""
from __future__ import annotations

import itertools
from typing import Any

import pytest

from src._overrides_schema import (
    DEFAULT_ONBOARDING_SKIPPED,
    DEFAULT_TELEMETRY_ENABLED,
    DEFAULT_UPGRADE_NOTIFICATIONS_ENABLED,
    V2_1_0_DEFAULTS,
    apply_v2_1_0_defaults,
)

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

#: v2.1.0 字段集合；与 V2_1_0_DEFAULTS 同步（验证用，不直接复用以独立断言）。
V2_1_0_KEYS: tuple[str, ...] = (
    "onboarding_skipped",
    "telemetry_enabled",
    "upgrade_notifications_enabled",
)

#: ``/data/last_upgrade_check.json`` 字段（与 v2.1.0 task 1.2 描述一致）。
#: 这些字段不属于 ``web_ui_overrides.json`` 但 task 描述要求函数对额外键鲁棒。
LAST_UPGRADE_CHECK_KEYS: tuple[str, ...] = ("checked_at", "latest", "notified")

#: 「最隐私友好」默认值 —— 与 design §4.2 / Property 11 描述对齐。
PRIVACY_SAFE_DEFAULTS: dict[str, Any] = {
    "onboarding_skipped": False,
    "telemetry_enabled": False,
    "upgrade_notifications_enabled": True,
}


def _powerset(items: tuple[str, ...]) -> list[tuple[str, ...]]:
    """所有子集（含空集与全集），用于参数化「缺失字段组合」。"""
    return [
        subset
        for r in range(len(items) + 1)
        for subset in itertools.combinations(items, r)
    ]


def _full_v2_1_0_input() -> dict[str, Any]:
    """构造一份「v2.1.0 字段全部就位」的合法 overrides dict（含 v2.0.3 槽位）。

    使用与默认值**相反**的取值，便于断言「已存在的值不会被覆盖回默认值」。
    """
    return {
        # v2.0.3 槽位绑定 —— 不能被新逻辑动到。
        "sleep_stage_source": "sensor.mmwave_sleep_stage",
        "light_targets": ["light.bedroom_main"],
        "climate_targets": ["climate.bedroom_ac"],
        # v2.1.0 字段，故意取与 PRIVACY_SAFE_DEFAULTS 相反的值。
        "onboarding_skipped": True,
        "telemetry_enabled": True,
        "upgrade_notifications_enabled": False,
    }


# ---------------------------------------------------------------------------
# Property 11 —— 缺失字段子集 × 是否携带额外键，参数化穷举
# ---------------------------------------------------------------------------

# 64 = 2³ (v2.1.0 missing subsets) × 2³ (last_upgrade_check 额外键子集) — 但
# 我们用 nested parametrize 让 pytest 报告里两个轴清晰可见。
@pytest.mark.parametrize("missing_keys", _powerset(V2_1_0_KEYS))
@pytest.mark.parametrize("extraneous_keys", _powerset(LAST_UPGRADE_CHECK_KEYS))
def test_apply_v2_1_0_defaults_safe_when_keys_missing(
    missing_keys: tuple[str, ...],
    extraneous_keys: tuple[str, ...],
) -> None:
    """对任意「缺失子集 × 额外键子集」组合：
    - 不抛异常。
    - 缺失的 v2.1.0 字段被填为「最隐私友好」默认值。
    - 已存在的 v2.1.0 字段保持原值（不被覆盖回默认值）。
    - v2.0.3 既有字段全部保留。
    - 来自 last_upgrade_check.json 的额外键不会让函数崩溃，且原样保留。
    """
    base = _full_v2_1_0_input()

    # 模拟「v2.1.0 字段被部分删除」的实际盘面。
    for k in missing_keys:
        base.pop(k, None)

    # 注入 last_upgrade_check.json 的额外字段，验证函数对额外键鲁棒。
    extraneous_values: dict[str, Any] = {
        "checked_at": 1700000000.0,
        "latest": "2.1.0",
        "notified": True,
    }
    for k in extraneous_keys:
        base[k] = extraneous_values[k]

    # 调用前对入参做 snapshot，验证 PR3.1「不就地修改」契约。
    snapshot = dict(base)

    # ── (1) 永不抛异常 ──────────────────────────────────────────────
    result = apply_v2_1_0_defaults(base)

    # 入参未被就地改写。
    assert base == snapshot, "apply_v2_1_0_defaults 必须保持入参不变（PR3.1）"

    # ── (2) 三个 v2.1.0 字段总是存在于输出里 ───────────────────────
    for key in V2_1_0_KEYS:
        assert key in result, f"v2.1.0 字段 {key} 缺失于结果"

    # ── (3) 缺失的字段 → 最隐私友好默认值 ─────────────────────────
    for key in missing_keys:
        assert result[key] == PRIVACY_SAFE_DEFAULTS[key], (
            f"缺失字段 {key} 未填为最隐私友好默认值: "
            f"got={result[key]!r}, expected={PRIVACY_SAFE_DEFAULTS[key]!r}"
        )

    # ── (4) 未缺失的 v2.1.0 字段保持原值 ──────────────────────────
    for key in V2_1_0_KEYS:
        if key not in missing_keys:
            assert result[key] == snapshot[key], (
                f"已存在的字段 {key} 被错误覆盖: "
                f"original={snapshot[key]!r}, got={result[key]!r}"
            )

    # ── (5) v2.0.3 既有字段全部保留 ────────────────────────────────
    assert result["sleep_stage_source"] == "sensor.mmwave_sleep_stage"
    assert result["light_targets"] == ["light.bedroom_main"]
    assert result["climate_targets"] == ["climate.bedroom_ac"]

    # ── (6) 额外键原样保留（不丢失、不修改） ──────────────────────
    for key in extraneous_keys:
        assert result[key] == extraneous_values[key], (
            f"额外键 {key} 未被原样保留: got={result.get(key)!r}"
        )


# ---------------------------------------------------------------------------
# 边界用例：None 入参、空 dict、纯额外键
# ---------------------------------------------------------------------------


def test_apply_v2_1_0_defaults_with_none_input() -> None:
    """``data=None`` —— 文件未创建场景；返回纯默认值 dict，且不抛异常。"""
    result = apply_v2_1_0_defaults(None)

    assert result == {
        "onboarding_skipped": DEFAULT_ONBOARDING_SKIPPED,
        "telemetry_enabled": DEFAULT_TELEMETRY_ENABLED,
        "upgrade_notifications_enabled": DEFAULT_UPGRADE_NOTIFICATIONS_ENABLED,
    }
    # 与 PRIVACY_SAFE_DEFAULTS 对齐 —— double check that defaults are
    # truly the "privacy-safest" values per Property 11.
    assert result == PRIVACY_SAFE_DEFAULTS


def test_apply_v2_1_0_defaults_with_empty_dict() -> None:
    """空 dict —— 等价于 None 但走显式分支。"""
    result = apply_v2_1_0_defaults({})

    assert result == PRIVACY_SAFE_DEFAULTS


def test_apply_v2_1_0_defaults_only_extraneous_keys() -> None:
    """仅含 last_upgrade_check.json 字段的 dict —— 不应抛异常，
    额外键保留，v2.1.0 字段全部填默认值。
    """
    raw = {
        "checked_at": 1700000000.0,
        "latest": "2.1.0",
        "notified": False,
    }

    result = apply_v2_1_0_defaults(raw)

    # 额外键保留。
    assert result["checked_at"] == 1700000000.0
    assert result["latest"] == "2.1.0"
    assert result["notified"] is False

    # v2.1.0 字段全部为最隐私友好默认值。
    for key, expected in PRIVACY_SAFE_DEFAULTS.items():
        assert result[key] == expected


# ---------------------------------------------------------------------------
# 默认值常量自身的不变量 —— 防止后续重构悄悄改坏「最隐私友好」语义
# ---------------------------------------------------------------------------


def test_default_constants_are_privacy_safest() -> None:
    """``DEFAULT_*`` 与 ``V2_1_0_DEFAULTS`` 必须等于 Property 11 定义的
    「最隐私友好」语义：onboarding 重新弹（False）、telemetry off
    （False）、upgrade notifications on（True）。
    """
    assert DEFAULT_ONBOARDING_SKIPPED is False
    assert DEFAULT_TELEMETRY_ENABLED is False
    assert DEFAULT_UPGRADE_NOTIFICATIONS_ENABLED is True

    assert V2_1_0_DEFAULTS == {
        "onboarding_skipped": False,
        "telemetry_enabled": False,
        "upgrade_notifications_enabled": True,
    }
    # 集合相等 —— V2_1_0_KEYS 与 V2_1_0_DEFAULTS 永不漂移。
    assert set(V2_1_0_DEFAULTS.keys()) == set(V2_1_0_KEYS)
