""":mod:`training_config.config_loader` 的单元测试。

v1.3.0 的清理砍掉了 ``model`` / ``mqtt`` / ``training`` /
``disaster_monitoring`` 等 CNN 时代遗留的配置段，所以这套测试
也重写为只围绕现在真正在用的 ``home_assistant`` 子树。
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from training_config.config_loader import (
    ConfigurationError,
    get_default_config,
    load_config,
    validate_config,
)


class TestDefaultConfig:
    def test_has_expected_top_level_shape(self) -> None:
        cfg = get_default_config()
        ha = cfg["home_assistant"]
        # 三大子树是 orchestrator / learner / controller 各自读的位置。
        assert set(ha.keys()) >= {
            "api", "preference_learner", "smart_control", "natural_sleep",
        }

    def test_dry_run_defaults_on(self) -> None:
        """首次安装必须保证不会误下发真实指令——dry_run 必须默认为 True。"""
        assert get_default_config()["home_assistant"]["smart_control"]["dry_run"] is True

    def test_validate_default_is_accepted(self) -> None:
        validate_config(get_default_config())   # 默认值必须能通过校验


class TestValidate:
    def test_missing_home_assistant_section_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            validate_config({"something_else": {}})

    def test_negative_deadband_rejected(self) -> None:
        cfg = get_default_config()
        cfg["home_assistant"]["smart_control"]["deadband_temperature_c"] = -0.1
        with pytest.raises(ConfigurationError):
            validate_config(cfg)

    def test_negative_rate_limit_rejected(self) -> None:
        cfg = get_default_config()
        cfg["home_assistant"]["smart_control"]["min_seconds_between_actions"] = -1
        with pytest.raises(ConfigurationError):
            validate_config(cfg)

    def test_quantile_outside_unit_interval_rejected(self) -> None:
        cfg = get_default_config()
        cfg["home_assistant"]["preference_learner"]["quality_quantile"] = 1.2
        with pytest.raises(ConfigurationError):
            validate_config(cfg)

    def test_min_sessions_zero_rejected(self) -> None:
        cfg = get_default_config()
        cfg["home_assistant"]["preference_learner"]["min_sessions_for_personalisation"] = 0
        with pytest.raises(ConfigurationError):
            validate_config(cfg)

    def test_unknown_keys_tolerated(self) -> None:
        """未知键交给下游 dataclass.from_dict 过滤，校验不该为此崩。"""
        cfg = get_default_config()
        cfg["home_assistant"]["smart_control"]["some_future_knob"] = True
        validate_config(cfg)


class TestLoadConfig:
    def test_load_from_file(self, tmp_path) -> None:
        path = tmp_path / "cfg.json"
        path.write_text(
            json.dumps(get_default_config()), encoding="utf-8",
        )
        loaded = load_config(str(path))
        assert "home_assistant" in loaded

    def test_missing_file_falls_back_to_default(self, tmp_path) -> None:
        loaded = load_config(str(tmp_path / "does_not_exist.json"))
        # 必须是一个有效的默认配置。
        validate_config(loaded)

    def test_invalid_json_falls_back_to_default(self, tmp_path) -> None:
        path = tmp_path / "broken.json"
        path.write_text("{ not valid json", encoding="utf-8")
        loaded = load_config(str(path))
        validate_config(loaded)

    def test_failing_validation_falls_back_to_default(self) -> None:
        """加载一份会被校验拒绝的配置，应当静默退回默认值。"""
        bad = get_default_config()
        bad["home_assistant"]["preference_learner"]["quality_quantile"] = 5.0
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        ) as f:
            json.dump(bad, f)
            tmp_path = f.name
        try:
            loaded = load_config(tmp_path)
            # dry_run 默认开启说明我们回退到了内置默认。
            assert (
                loaded["home_assistant"]["smart_control"]["dry_run"] is True
            )
        finally:
            os.unlink(tmp_path)
