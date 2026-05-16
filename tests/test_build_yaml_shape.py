"""Bug 1.4 探索测试 — build.yaml 必须用 docker.io 完整路径

v2.0.1 把 build_from 切到 ``python:3.11-alpine`` 解决 ghcr.io 国内不可达问题。
v2.1.0 进一步切到完整 ``docker.io/library/python:3.11-alpine``：新版 Supervisor
（>=2024.10）+ buildx 模式不再隐式 fall back 到 ``docker.io/library/`` 命名
空间，bare image name 会触发 "image not found" 装不上错误。

测试守护两件事：
1. build_from 不能含 ghcr.io（v2.0.1 决策）
2. build_from 必须含完整 registry 域名（v2.1.0 决策）
"""

from pathlib import Path

import yaml


_EXPECTED_IMAGE = "docker.io/library/python:3.11-alpine"


def test_build_from_not_ghcr():
    """build_from 应指向 docker.io/library/python:3.11-alpine。"""
    build_yaml_path = Path(__file__).resolve().parent.parent / "sleep_classifier" / "build.yaml"
    data = yaml.safe_load(build_yaml_path.read_text(encoding="utf-8"))

    build_from = data["build_from"]

    # aarch64 应为完整 docker.io/library/ 路径
    assert build_from["aarch64"] == _EXPECTED_IMAGE, (
        f"Expected '{_EXPECTED_IMAGE}', got '{build_from['aarch64']}'"
    )

    # amd64 同样
    assert build_from["amd64"] == _EXPECTED_IMAGE, (
        f"Expected '{_EXPECTED_IMAGE}', got '{build_from['amd64']}'"
    )

    # 两者都不应包含 ghcr.io
    assert "ghcr.io" not in build_from["aarch64"], (
        f"aarch64 value should not contain 'ghcr.io': {build_from['aarch64']}"
    )
    assert "ghcr.io" not in build_from["amd64"], (
        f"amd64 value should not contain 'ghcr.io': {build_from['amd64']}"
    )

    # v2.1.0 守护：必须含完整 registry 域名
    assert "docker.io" in build_from["aarch64"], (
        f"aarch64 must use full registry path 'docker.io/library/...': "
        f"{build_from['aarch64']}"
    )
    assert "docker.io" in build_from["amd64"], (
        f"amd64 must use full registry path 'docker.io/library/...': "
        f"{build_from['amd64']}"
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
