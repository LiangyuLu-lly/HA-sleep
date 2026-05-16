"""Bug 1.6 探索测试 — startup 语义错。

这是一个探索性测试，用于验证 ``sleep_classifier/config.yaml`` 中
``startup`` 字段是否为正确的 ``application`` 值。

Home Assistant Add-on manifest 中 ``startup: services`` 表示该 Add-on
在 HA Core 启动之前运行（用于基础设施服务如 DNS、数据库），而
``startup: application`` 表示在 HA Core 就绪之后启动——Sleep Classifier
依赖 HA REST/WS API，必须等 Core 就绪才能工作，因此正确值应为
``application``。

**预期行为**：在未修复的代码上此测试应当 FAIL（当前值为 ``services``），
测试失败即证明 Bug 存在。请勿修改 config.yaml 来使测试通过。
"""

from pathlib import Path

import yaml


def test_startup_is_application():
    """config.yaml 的 startup 字段应为 'application'。"""
    config_path = Path(__file__).resolve().parent.parent / "sleep_classifier" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["startup"] == "application", (
        f"Expected startup == 'application', got '{config['startup']}'"
    )
