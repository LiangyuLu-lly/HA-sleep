""":mod:`training_config.config_loader` 的 v3.0.0 配置兼容性测试。

PR6 的核心契约：v2.1.0 老用户升级到 v3.0.0 时，``options.json``
里**没有**任何 ``home_assistant.v3.*`` 字段，加载流程也必须：

1. 不抛异常；
2. 自动应用全套 8 个 v3 默认值；
3. 缺失字段只打**一行** INFO 日志（不是 WARN，避免老用户刷屏）。

同时校验非法 ``user_profile_*`` 取值会回退默认 + 打 INFO 日志。

对应 spec：``algorithmic-moat-v3.0.0`` task 6.5 / Requirements 11.1, 11.2。
"""
from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any, Dict

import pytest

from training_config.config_loader import (
    _v3_section_defaults,
    get_default_config,
    load_config,
)


# ---------------------------------------------------------------------------
# 辅助：构造一份「v2.1.0 形态」的 config。
# ---------------------------------------------------------------------------

def _v2_1_0_config() -> Dict[str, Any]:
    """返回一份符合 v2.1.0 schema 的配置（不含任何 ``home_assistant.v3``）。

    直接拿 :func:`get_default_config` 当起点，再把 v3.0.0 才出现的
    ``v3`` 子树整体删除——这样既保证其它字段与真实 v2.1.0 用户的
    options.json 形态一致，又能模拟「老 config 没有任何 v3 字段」的
    升级路径。
    """
    cfg = deepcopy(get_default_config())
    cfg["home_assistant"].pop("v3", None)
    assert "v3" not in cfg["home_assistant"]
    return cfg


# ---------------------------------------------------------------------------
# 测试 1：v2.1.0 老 config → 自动补齐全套 v3 默认值。
# ---------------------------------------------------------------------------

class TestV2OldConfigCompatibility:
    """老用户从 v2.1.0 升级，options.json 里完全没有 v3 字段的场景。"""

    def test_load_config_v2_old_config_applies_v3_defaults(self) -> None:
        """v2.1.0 老 config 不含 v3 段时，``load_config`` 不抛异常并补齐 8 个默认值。"""
        old = _v2_1_0_config()

        # 不应抛任何异常。
        result = load_config(old)

        v3 = result["home_assistant"]["v3"]
        expected = _v3_section_defaults()

        # 8 个 key 一个不少。
        assert set(v3.keys()) == set(expected.keys())
        assert len(v3) == 8

        # 每个默认值都和 _v3_section_defaults() 一致。
        for key, default in expected.items():
            assert v3[key] == default, (
                f"v3.{key} 期望默认值 {default!r}，实际 {v3[key]!r}"
            )

        # 显式检查 4 个算法 flag 全部为 True（合理默认：开箱即享算法收益）。
        assert v3["bayesian_optimizer_enabled"] is True
        assert v3["causal_attribution_enabled"] is True
        assert v3["population_prior_enabled"] is True
        assert v3["stage_predictor_enabled"] is True

        # explain_all 默认 False（保守披露）。
        assert v3["causal_attribution_explain_all"] is False

        # 三个用户画像字段默认空字符串（unspecified / neutral）。
        assert v3["user_profile_age_band"] == ""
        assert v3["user_profile_sex"] == ""
        assert v3["user_profile_chronotype"] == ""


# ---------------------------------------------------------------------------
# 测试 2：缺失 v3 字段 → 一条 INFO 日志列出缺失 key。
# ---------------------------------------------------------------------------

class TestMissingV3KeysLogging:
    """缺失字段路径必须打一行 INFO 日志，列出所有缺失 key。"""

    def test_load_config_missing_v3_keys_logs_info_once(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """部分 v3 字段缺失时，只打一行 INFO 日志，列出缺失的 key 名。"""
        cfg = _v2_1_0_config()
        # 只显式提供其中两个 flag，剩下 6 个 key 缺失。
        cfg["home_assistant"]["v3"] = {
            "bayesian_optimizer_enabled": False,
            "causal_attribution_enabled": True,
        }
        missing = sorted(set(_v3_section_defaults().keys()) - {
            "bayesian_optimizer_enabled",
            "causal_attribution_enabled",
        })
        assert len(missing) == 6

        with caplog.at_level(logging.INFO, logger="training_config.config_loader"):
            result = load_config(cfg)

        # 缺失字段日志：恰好 1 条，message 含「v3.0.0 字段缺失」+ 全部缺失 key。
        missing_logs = [
            r for r in caplog.records
            if r.name == "training_config.config_loader"
            and r.levelno == logging.INFO
            and "v3.0.0 字段缺失" in r.getMessage()
        ]
        assert len(missing_logs) == 1, (
            f"期望恰好 1 条 INFO 日志，实际 {len(missing_logs)} 条："
            f"{[r.getMessage() for r in missing_logs]}"
        )
        msg = missing_logs[0].getMessage()
        for key in missing:
            assert key in msg, f"INFO 日志未列出缺失字段 {key!r}: {msg!r}"

        # 缺失字段全部补齐为默认值。
        v3 = result["home_assistant"]["v3"]
        for key in missing:
            assert v3[key] == _v3_section_defaults()[key]

        # 已存在的两个 flag 保持原值不被覆盖。
        assert v3["bayesian_optimizer_enabled"] is False
        assert v3["causal_attribution_enabled"] is True


# ---------------------------------------------------------------------------
# 测试 3：非法 user_profile 取值 → 回退默认 + 一条 INFO 日志。
# ---------------------------------------------------------------------------

class TestInvalidUserProfileFallback:
    """``user_profile_*`` 非法取值必须静默回退到默认 + 打一行 INFO 日志。"""

    def test_load_config_invalid_user_profile_falls_back_with_info_log(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """``user_profile_age_band`` 非法值时回退默认 + 打 INFO 日志。"""
        cfg = _v2_1_0_config()
        # 显式提供完整 v3 段，避免触发「字段缺失」分支干扰本测试。
        cfg["home_assistant"]["v3"] = _v3_section_defaults()
        cfg["home_assistant"]["v3"]["user_profile_age_band"] = "invalid-value"

        with caplog.at_level(logging.INFO, logger="training_config.config_loader"):
            result = load_config(cfg)

        # 非法值已被回退到默认空字符串。
        v3 = result["home_assistant"]["v3"]
        assert v3["user_profile_age_band"] == ""

        # 其它 user_profile 字段未受影响（仍为合法默认空字符串）。
        assert v3["user_profile_sex"] == ""
        assert v3["user_profile_chronotype"] == ""

        # 恰好 1 条 INFO 日志，message 含「user_profile」+ 实际非法值。
        profile_logs = [
            r for r in caplog.records
            if r.name == "training_config.config_loader"
            and r.levelno == logging.INFO
            and "user_profile" in r.getMessage()
        ]
        assert len(profile_logs) == 1, (
            f"期望恰好 1 条 user_profile INFO 日志，实际 {len(profile_logs)} 条："
            f"{[r.getMessage() for r in profile_logs]}"
        )
        msg = profile_logs[0].getMessage()
        assert "user_profile_age_band" in msg
        assert "invalid-value" in msg

        # 因为 v3 段完整提供了 8 个 key，不应有「字段缺失」日志干扰。
        missing_logs = [
            r for r in caplog.records
            if r.name == "training_config.config_loader"
            and "v3.0.0 字段缺失" in r.getMessage()
        ]
        assert missing_logs == []

    def test_load_config_invalid_sex_and_chronotype_logged_together(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """多个 user_profile 字段同时非法时，只打一条 INFO 日志，列出所有非法字段。"""
        cfg = _v2_1_0_config()
        cfg["home_assistant"]["v3"] = _v3_section_defaults()
        cfg["home_assistant"]["v3"]["user_profile_sex"] = "X"
        cfg["home_assistant"]["v3"]["user_profile_chronotype"] = "robot"

        with caplog.at_level(logging.INFO, logger="training_config.config_loader"):
            result = load_config(cfg)

        v3 = result["home_assistant"]["v3"]
        assert v3["user_profile_sex"] == ""
        assert v3["user_profile_chronotype"] == ""

        profile_logs = [
            r for r in caplog.records
            if r.name == "training_config.config_loader"
            and r.levelno == logging.INFO
            and "user_profile" in r.getMessage()
        ]
        assert len(profile_logs) == 1
        msg = profile_logs[0].getMessage()
        assert "user_profile_sex" in msg
        assert "user_profile_chronotype" in msg
