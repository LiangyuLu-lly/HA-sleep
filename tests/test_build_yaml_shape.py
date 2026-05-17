"""build.yaml 已废弃 — v2.1.0 守护：必须不存在。

历史背景：
- v1.0–v2.0：用 build.yaml 提供 build_from + labels。
- v2.1.0 第一轮修复：把 ``python:3.11-alpine`` 改成
  ``docker.io/library/python:3.11-alpine``。
- v2.1.0 第二轮修复：发现新版 Supervisor 把 build.yaml 标 deprecated，
  且字段解析失败时**不报错**，而是 silently fall back 到 HA 自带 base
  image。HA base 没装 pip → 构建挂在 ``pip: not found``。
- v2.1.0 第三轮（当前）：删除 build.yaml，所有 build 元数据挪进
  Dockerfile 的 ARG / LABEL 块。Supervisor 用 Dockerfile ARG 默认值
  ``library/python:3.11-alpine``，docker daemon 自动展开为
  ``docker.io/library/python:3.11-alpine``。

测试守护两件事：
1. ``sleep_classifier/build.yaml`` 必须不存在（如果误恢复就 fail）。
2. ``sleep_classifier/Dockerfile`` 的 ARG BUILD_FROM 默认值必须是
   ``library/python:3.11-alpine`` 这种「正好两级」格式（与 Supervisor
   build_from 校验正则兼容，作为本地 ``docker build`` 时的兜底值）。

如果未来需要加回 build.yaml（比如要 per-arch 不同的 base image），
请同时更新这两条断言。
"""

from __future__ import annotations

import re
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
_ADDON_DIR = _REPO_ROOT / "sleep_classifier"
_BUILD_YAML = _ADDON_DIR / "build.yaml"
_DOCKERFILE = _ADDON_DIR / "Dockerfile"

# Supervisor 实际使用的 build_from 校验正则（来自 supervisor/addons/validate.py）。
# ``library/python:3.11-alpine`` 必须命中此正则；否则 Supervisor 会 silently
# fall back 到 HA 自带 base image。
_SUPERVISOR_BUILD_FROM_PATTERN = re.compile(
    r"^([a-zA-Z\-\.:\d{}]+/)*?([\-\w{}]+)/([\-\w{}]+)(:[\.\-\w{}]+)?$"
)


def test_build_yaml_does_not_exist():
    """build.yaml 在 v2.1.0 后被废弃，必须不存在。

    新版 HA Supervisor 标记 build.yaml 为 deprecated，对其字段的解析
    在 schema 失败时不报硬错而是 silently fall back 到 HA 自带 base image，
    那个 image 没装 pip，会让构建挂在 ``pip: not found``。
    """
    assert not _BUILD_YAML.exists(), (
        f"{_BUILD_YAML} should not exist in v2.1.0+. "
        "Move build_from / labels into the Dockerfile (ARG + LABEL blocks)."
    )


def test_dockerfile_arg_build_from_uses_two_segment_format():
    """Dockerfile 的 ``ARG BUILD_FROM=...`` 必须用两段路径格式。

    格式：``owner/repo[:tag]``，如 ``library/python:3.11-alpine``。
    Supervisor 解析 build_from（来自 build.yaml 或 Dockerfile ARG 默认）
    时用上述正则校验，三段路径如 ``docker.io/library/python:3.11-alpine``
    在某些 Supervisor 版本上不能稳定通过；裸名 ``python:3.11-alpine``
    缺少斜杠也会被拒。
    """
    text = _DOCKERFILE.read_text(encoding="utf-8")

    # 找出所有 ``ARG BUILD_FROM=<value>`` 的 value
    matches = re.findall(r"^ARG\s+BUILD_FROM=(\S+)$", text, re.MULTILINE)
    assert matches, "Dockerfile must declare ARG BUILD_FROM=<image>"

    # 接受 ``library/python:<tag>`` 或 ``library/alpine:<tag>``。具体 tag
    # 是实施细节，会被用来强制 buildkit cache miss（v2.1.0 修复 musl ABI
    # 残留 layer + docker hub multi-arch manifest 解析问题时换过几次）。
    accepted_pattern = re.compile(
        r"^library/(python:3\.\d+(\.\d+)?-alpine(\d+\.\d+)?|alpine:\d+\.\d+)$"
    )
    for value in matches:
        assert accepted_pattern.match(value), (
            f"Dockerfile ARG BUILD_FROM default = {value!r}; "
            "expected ``library/python:3.<minor>[.<patch>]-alpine[<ver>]`` "
            "or ``library/alpine:<ver>`` (two-segment)."
        )

        assert _SUPERVISOR_BUILD_FROM_PATTERN.match(value), (
            f"ARG BUILD_FROM={value!r} fails the Supervisor build_from "
            "regex. Use the owner/repo:tag form."
        )


def test_dockerfile_no_pip3_command():
    """Dockerfile 不能用 bare ``pip3`` 命令。

    HA 新版 base image (``home-assistant/base:latest``) 没有 ``pip3``
    符号链接，只暴露 ``python3 -m pip``。bare ``pip3`` 会导致构建挂在
    ``pip3: not found``。本测试保护我们的 Dockerfile 始终用 ``python3
    -m pip`` 形式。
    """
    text = _DOCKERFILE.read_text(encoding="utf-8")

    # 排除注释里出现的 pip3
    code_lines = [
        line for line in text.splitlines()
        if not line.lstrip().startswith("#")
    ]
    code = "\n".join(code_lines)

    assert "pip3 install" not in code, (
        "Dockerfile uses bare 'pip3 install' which fails on HA base "
        "image (no pip3 symlink). Use 'python3 -m pip install' instead."
    )
    assert "pip3 " not in code or "python3 -m pip" in code, (
        "Dockerfile must use 'python3 -m pip' form, not bare 'pip3'."
    )
