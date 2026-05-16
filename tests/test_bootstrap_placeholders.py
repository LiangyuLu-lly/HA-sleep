"""Bug 1.1 探索测试 + 单元测试 — 占位 sensor bootstrap 脚本。

修复前：sleep_classifier/bootstrap_placeholders.py 不存在 → 测试 FAIL
（确认 bug）。修复后（Task 2.1 落地脚本后）：测试 PASS。

单元测试覆盖：
(a) SUPERVISOR_TOKEN 缺失 → 静默跳过返回 0
(b) POST 全部成功 → 5 条占位写入
(c) POST 部分失败 → stderr 记录 + 其它继续
(d) entity_id / friendly_name 含中文正确编码
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


async def test_no_placeholder_sensors_without_bootstrap_script() -> None:
    """断言 bootstrap_placeholders.py 存在且可导入。

    bug-condition C(X)：未绑 stage + bootstrap 脚本不存在 ⇒ 0 占位 sensor。
    脚本不存在时 run.sh 永远不会发占位实体，Lovelace 全部 Entity not available。
    """
    bootstrap = Path(__file__).resolve().parent.parent / "sleep_classifier" / "bootstrap_placeholders.py"
    assert bootstrap.is_file(), (
        f"bootstrap_placeholders.py 不存在于 {bootstrap}; "
        "未绑 stage 时 0 占位 sensor 的 bug 条件成立"
    )


# ---------------------------------------------------------------------------
# Helper: import the bootstrap module from sleep_classifier/ (not a package)
# ---------------------------------------------------------------------------

def _import_bootstrap():
    """Import bootstrap_placeholders.py as a module."""
    module_path = Path(__file__).resolve().parent.parent / "sleep_classifier" / "bootstrap_placeholders.py"
    spec = importlib.util.spec_from_file_location("bootstrap_placeholders", module_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# (a) SUPERVISOR_TOKEN missing → silent skip, return 0
# ---------------------------------------------------------------------------

async def test_missing_supervisor_token_returns_zero(monkeypatch) -> None:
    """When SUPERVISOR_TOKEN is not set, main() returns 0 without any POST."""
    mod = _import_bootstrap()
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    result = await mod.main()
    assert result == 0


# ---------------------------------------------------------------------------
# (b) POST all succeed → 5 placeholders written
# ---------------------------------------------------------------------------

async def test_all_posts_succeed(monkeypatch) -> None:
    """When all 5 POSTs return 201, main() completes and returns 0."""
    mod = _import_bootstrap()
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-token")
    monkeypatch.setenv("SUPERVISOR_HA_BASE", "http://fake-supervisor/core")

    posted_entities: list[str] = []

    class FakeResponse:
        status = 201

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def post(self, url, **kwargs):
            # Extract entity_id from URL
            entity_id = url.split("/api/states/")[-1]
            posted_entities.append(entity_id)
            return FakeResponse()

    with patch("aiohttp.ClientSession", return_value=FakeSession()):
        result = await mod.main()

    assert result == 0
    assert len(posted_entities) == 5
    expected_entities = {
        "sensor.sleep_classifier_stage",
        "sensor.sleep_classifier_confidence",
        "sensor.sleep_classifier_health",
        "sensor.sleep_classifier_last_action",
        "sensor.sleep_classifier_session_duration",
    }
    assert set(posted_entities) == expected_entities


# ---------------------------------------------------------------------------
# (c) POST partial failure → stderr logged + others continue
# ---------------------------------------------------------------------------

async def test_partial_failure_logs_stderr_and_continues(monkeypatch, capsys) -> None:
    """When some POSTs fail, errors are logged to stderr but all 5 are attempted."""
    mod = _import_bootstrap()
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-token")
    monkeypatch.setenv("SUPERVISOR_HA_BASE", "http://fake-supervisor/core")

    call_count = 0

    class FakeResponse:
        def __init__(self, status):
            self.status = status

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def post(self, url, **kwargs):
            nonlocal call_count
            call_count += 1
            # First 2 calls fail, rest succeed
            if call_count <= 2:
                raise ConnectionError("simulated network failure")
            return FakeResponse(201)

    with patch("aiohttp.ClientSession", return_value=FakeSession()):
        result = await mod.main()

    assert result == 0
    # All 5 entities were attempted (gather runs all concurrently)
    assert call_count == 5
    # Failures logged to stderr
    captured = capsys.readouterr()
    assert "ConnectionError" in captured.err


# ---------------------------------------------------------------------------
# (d) entity_id / friendly_name with Chinese characters encoded correctly
# ---------------------------------------------------------------------------

async def test_chinese_characters_encoded_correctly(monkeypatch) -> None:
    """Verify that Chinese characters in friendly_name are sent as valid JSON (UTF-8)."""
    mod = _import_bootstrap()
    monkeypatch.setenv("SUPERVISOR_TOKEN", "fake-token")
    monkeypatch.setenv("SUPERVISOR_HA_BASE", "http://fake-supervisor/core")

    import json as json_mod
    captured_bodies: list[dict] = []

    class FakeResponse:
        status = 201

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def post(self, url, **kwargs):
            # Capture the JSON body to verify encoding
            body = kwargs.get("json", {})
            captured_bodies.append(body)
            # Verify the body can be serialized to JSON with non-ASCII chars
            serialized = json_mod.dumps(body, ensure_ascii=False)
            # Verify it can be deserialized back
            deserialized = json_mod.loads(serialized)
            assert deserialized == body
            return FakeResponse()

    # Temporarily patch PLACEHOLDERS to include Chinese characters
    original_placeholders = mod.PLACEHOLDERS
    mod.PLACEHOLDERS = [
        ("sensor.sleep_classifier_stage", "configuring", {"friendly_name": "睡眠阶段"}),
        ("sensor.sleep_classifier_confidence", "0", {"friendly_name": "睡眠分类器置信度", "unit_of_measurement": "%"}),
        ("sensor.sleep_classifier_health", "configuring", {"friendly_name": "睡眠分类器健康"}),
        ("sensor.sleep_classifier_last_action", "—", {"friendly_name": "最后一次睡眠自动化动作"}),
        ("sensor.sleep_classifier_session_duration", "0", {"friendly_name": "睡眠会话时长", "unit_of_measurement": "s"}),
    ]

    try:
        with patch("aiohttp.ClientSession", return_value=FakeSession()):
            result = await mod.main()
    finally:
        mod.PLACEHOLDERS = original_placeholders

    assert result == 0
    assert len(captured_bodies) == 5
    # Verify Chinese characters are present in the captured bodies
    all_friendly_names = [b["attributes"]["friendly_name"] for b in captured_bodies]
    assert any("睡眠" in name for name in all_friendly_names)
