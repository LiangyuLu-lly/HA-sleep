"""Bug 1.4 探索测试 — build.yaml 仍指向 ghcr.io

这是一个 exploration test（探索性测试）。在修复前，此测试 **预期失败**，
因为 sleep_classifier/build.yaml 的 build_from.aarch64 仍然指向
ghcr.io/home-assistant/aarch64-base:3.19（v2.0.0 遗留），而 Dockerfile
已经硬编码 FROM python:3.11-alpine（v2.0.1 国内网络决策）。

测试失败即证明 Bug 1.4 存在：build.yaml 与 Dockerfile 的基础镜像配置不一致。
"""

from pathlib import Path

import yaml


def test_build_from_not_ghcr():
    """build_from 应指向 python:3.11-alpine，不含 ghcr.io。"""
    build_yaml_path = Path(__file__).resolve().parent.parent / "sleep_classifier" / "build.yaml"
    data = yaml.safe_load(build_yaml_path.read_text(encoding="utf-8"))

    build_from = data["build_from"]

    # aarch64 应为 python:3.11-alpine
    assert build_from["aarch64"] == "python:3.11-alpine", (
        f"Expected 'python:3.11-alpine', got '{build_from['aarch64']}'"
    )

    # amd64 应为 python:3.11-alpine
    assert build_from["amd64"] == "python:3.11-alpine", (
        f"Expected 'python:3.11-alpine', got '{build_from['amd64']}'"
    )

    # 两者都不应包含 ghcr.io
    assert "ghcr.io" not in build_from["aarch64"], (
        f"aarch64 value should not contain 'ghcr.io': {build_from['aarch64']}"
    )
    assert "ghcr.io" not in build_from["amd64"], (
        f"amd64 value should not contain 'ghcr.io': {build_from['amd64']}"
    )


def test_labels_block_has_min_4_entries():
    """labels 区块应至少包含 4 条 OCI 标签。"""
    build_yaml_path = Path(__file__).resolve().parent.parent / "sleep_classifier" / "build.yaml"
    data = yaml.safe_load(build_yaml_path.read_text(encoding="utf-8"))

    assert "labels" in data, "build.yaml is missing a 'labels' block"
    labels = data["labels"]
    assert isinstance(labels, dict), f"labels should be a dict, got {type(labels)}"
    assert len(labels) >= 4, (
        f"Expected at least 4 OCI labels, got {len(labels)}: {list(labels.keys())}"
    )
