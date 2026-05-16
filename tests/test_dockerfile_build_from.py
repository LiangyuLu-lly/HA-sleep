"""Bug 1.11 探索测试 — Dockerfile 未用 ARG BUILD_FROM。

当前 Dockerfile 硬编码 ``FROM python:3.11-alpine``，未通过
``ARG BUILD_FROM`` 消费 ``build.yaml`` 注入的基础镜像变量。
对标 hassio-addons/app-example，HA 官方 add-on 都保留
``ARG BUILD_FROM`` + ``FROM ${BUILD_FROM}``，新老 builder 兼容。

本测试在修复前应 FAIL（证明 bug 存在），修复后翻绿。

**Validates: Requirements 1.11**
"""

import re
from pathlib import Path


DOCKERFILE_PATH = Path(__file__).resolve().parent.parent / "sleep_classifier" / "Dockerfile"


def test_dockerfile_uses_arg_build_from():
    """Dockerfile 必须声明 ARG BUILD_FROM 并在 FROM 中引用，不应硬编码基础镜像。"""
    content = DOCKERFILE_PATH.read_text(encoding="utf-8")

    # 1. 必须包含 ARG BUILD_FROM 声明
    assert re.search(
        r"^\s*ARG\s+BUILD_FROM", content, re.MULTILINE
    ), "Dockerfile 缺少 'ARG BUILD_FROM' 声明"

    # 2. 必须包含 FROM ${BUILD_FROM} 或 FROM $BUILD_FROM（使用 ARG 变量）
    assert re.search(
        r"^\s*FROM\s+\$\{?BUILD_FROM\}?", content, re.MULTILINE
    ), "Dockerfile 缺少 'FROM ${BUILD_FROM}' 或 'FROM $BUILD_FROM'"

    # 3. 不应存在未被 ARG 替换的硬编码 FROM python:3.11-alpine
    #    如果 ARG BUILD_FROM 在 FROM 之前声明，则不应再有独立的硬编码 FROM
    hardcoded_from = re.findall(
        r"^\s*FROM\s+python:3\.11-alpine", content, re.MULTILINE
    )
    assert len(hardcoded_from) == 0, (
        "Dockerfile 仍包含硬编码的 'FROM python:3.11-alpine'，"
        "应改为 'FROM ${BUILD_FROM}' 并通过 ARG 声明默认值"
    )
