"""Bug 1.5 探索测试 — Dockerfile LABEL 数量不足 15

这是一个 exploration test（探索性测试）。在修复前，此测试 **预期失败**，
因为 sleep_classifier/Dockerfile 当前没有（或仅有极少量）LABEL 指令，
远未达到 hassio-addons/app-example 的 15 条工业级元数据基线
（5 条 io.hass.* + 10 条 org.opencontainers.image.*）。

测试失败即证明 Bug 1.5 存在：Dockerfile 缺少 HA Supervisor 识别所需的
元数据标签，导致 UI 版本比对 / 依赖验证显示 "unknown"，且对 HACS /
镜像扫描工具不友好。
"""

import re
from pathlib import Path


def test_dockerfile_has_15_labels():
    """Dockerfile 应包含至少 15 个唯一 LABEL key。"""
    dockerfile_path = (
        Path(__file__).resolve().parent.parent / "sleep_classifier" / "Dockerfile"
    )
    content = dockerfile_path.read_text(encoding="utf-8")

    # 匹配 LABEL 指令中的 key=value 对。
    # 支持两种形式：
    #   LABEL key=value
    #   LABEL key="value"
    #   LABEL key=value \
    #         key2=value2
    # 以及多行续行中的 key=value 或 key="value" 模式。
    #
    # 策略：找到所有 key=... 模式（key 由字母、数字、点、下划线、连字符组成）
    # 出现在 LABEL 指令块中的。
    label_keys: set[str] = set()

    # 先把续行合并（反斜杠 + 换行 → 空格）
    merged = content.replace("\\\n", " ")

    for line in merged.splitlines():
        stripped = line.strip()
        if not stripped.startswith("LABEL"):
            continue
        # 去掉 LABEL 关键字
        rest = stripped[len("LABEL"):].strip()
        # 提取所有 key=... 对
        # key 格式：字母/数字/点/下划线/连字符
        keys_in_line = re.findall(
            r'([a-zA-Z0-9._-]+)\s*=\s*(?:"[^"]*"|\'[^\']*\'|\S+)',
            rest,
        )
        label_keys.update(keys_in_line)

    # 断言总数 >= 15
    assert len(label_keys) >= 15, (
        f"Expected at least 15 unique LABEL keys, found {len(label_keys)}: "
        f"{sorted(label_keys)}"
    )

    # 断言 5 条 io.hass.* 必须存在
    required_io_hass = {
        "io.hass.name",
        "io.hass.description",
        "io.hass.arch",
        "io.hass.type",
        "io.hass.version",
    }
    missing_io_hass = required_io_hass - label_keys
    assert not missing_io_hass, (
        f"Missing required io.hass.* labels: {sorted(missing_io_hass)}"
    )

    # 断言至少 10 条 org.opencontainers.image.* key
    oci_keys = {k for k in label_keys if k.startswith("org.opencontainers.image.")}
    assert len(oci_keys) >= 10, (
        f"Expected at least 10 org.opencontainers.image.* labels, "
        f"found {len(oci_keys)}: {sorted(oci_keys)}"
    )
