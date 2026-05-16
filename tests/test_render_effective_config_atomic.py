"""Bug 1.7 探索测试 — 原子写缺失。

render_effective_config.py 当前使用 Path.write_text() 直接写入
/data/effective_config.json。这是非原子操作（open-truncate-write-close），
中途 SIGKILL 或 ENOSPC 会留下半截 JSON，下次启动 jq parse error。

正确做法是 atomic write：写 tmp 文件 → fsync → os.replace() 覆盖主文件。
本测试断言当前代码 **未** 使用原子写模式，从而证明 bug 存在。

修复前：FAIL（代码直接用 write_text，未走 atomic pattern）
修复后（Task 10.2）：PASS（改用 atomic_write_text）

Validates: Requirements 1.7 / Design: §3.7
"""
from __future__ import annotations

import ast
import re
from pathlib import Path


_SRC_PATH = Path("sleep_classifier/render_effective_config.py")


def test_mid_write_sigkill_preserves_main_file():
    """断言 render_effective_config.py 使用原子写模式（tmp + os.replace）。

    原子写的最低要求：
    1. 不直接对目标文件调用 write_text / open(..., 'w').write(...)
    2. 使用 tempfile 或 .tmp 后缀写入临时文件
    3. 使用 os.replace / os.rename 原子替换

    当前代码直接调用 _OUT_PATH.write_text(...)，不满足上述任何一条，
    因此本测试在修复前 **应当 FAIL**。
    """
    source = _SRC_PATH.read_text(encoding="utf-8")

    # --- 检查是否存在原子写模式的标志 ---
    has_atomic_pattern = _has_atomic_write_pattern(source)

    # 断言：代码应该使用原子写模式
    # 修复前此断言 FAIL（证明 bug 存在）
    assert has_atomic_pattern, (
        "render_effective_config.py 未使用原子写模式。\n"
        "当前代码直接调用 _OUT_PATH.write_text(...)，中途 SIGKILL 会截断主文件。\n"
        "应改为：写 tmp 文件 → fsync → os.replace() 覆盖主文件。"
    )


def _has_atomic_write_pattern(source: str) -> bool:
    """检测源码是否包含原子写模式的关键特征。

    原子写模式至少需要以下之一：
    - 调用 os.replace / os.rename 进行原子替换
    - 使用 tempfile.mkstemp / tempfile.NamedTemporaryFile
    - 导入并调用 atomic_write_text / atomic_write_json 辅助函数
    """
    # 特征 1：使用 os.replace 或 os.rename（原子替换的核心）
    if re.search(r"\bos\.replace\b", source):
        return True
    if re.search(r"\bos\.rename\b", source):
        return True

    # 特征 2：使用 tempfile 模块（写临时文件）
    if re.search(r"\btempfile\.(mkstemp|NamedTemporaryFile)\b", source):
        return True

    # 特征 3：使用项目内的 atomic_write 辅助函数
    if re.search(r"\batomic_write_(text|json)\b", source):
        return True

    # 没有找到任何原子写特征
    return False
