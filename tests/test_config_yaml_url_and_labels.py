"""Exploration test for Bug 1.12 — config.yaml 缺 url 字段.

验证 sleep_classifier/config.yaml 包含 ``url`` 字段并指向项目 GitHub 仓库。
HA Add-on Store 使用该字段在详情页渲染可点击的 "Project homepage" 链接；
缺失时用户无法从 UI 跳转到源码仓库。

本测试在修复前 **预期失败**，失败即证明 bug 真实存在。
"""

from pathlib import Path

import yaml


_CONFIG_PATH = Path(__file__).resolve().parent.parent / "sleep_classifier" / "config.yaml"


def test_config_yaml_has_url():
    """config.yaml 必须包含 url 字段且指向项目 GitHub 仓库."""
    text = _CONFIG_PATH.read_text(encoding="utf-8")
    config = yaml.safe_load(text)

    assert "url" in config, "config.yaml 缺少 'url' 字段"
    assert config["url"] == "https://github.com/LiangyuLu-lly/HA-sleep", (
        f"url 值不正确: {config.get('url')!r}"
    )
