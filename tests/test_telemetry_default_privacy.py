"""Property 4 (telemetry part): 默认与禁用状态下零外部出站请求.

Patch ``aiohttp.ClientSession`` to count outbound requests.  With
``enabled=False``, assert ``TelemetryReporter.run()`` returns immediately,
HTTP count is 0, and ``install_id.uuid`` does not exist in ``tmp_path``.

**Validates: Requirements 6.4**
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.telemetry_reporter import TelemetryReporter


async def test_disabled_reporter_no_http_no_install_id(tmp_path: Path) -> None:
    """enabled=False → run() returns immediately, zero HTTP, no install_id file."""
    http_count = 0

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            nonlocal http_count
            http_count += 1
            return AsyncMock(status=200)

    reporter = TelemetryReporter(
        enabled=False,
        endpoint="https://telemetry.example.com/v1/report",
        version="2.1.0",
        ha_version="2024.6.1",
        arch="aarch64",
        locale="zh-cn",
        data_dir=tmp_path,
    )

    with patch("aiohttp.ClientSession", return_value=FakeSession()):
        await reporter.run()

    assert http_count == 0, "No HTTP requests should be made when disabled"
    install_id_file = tmp_path / "install_id.uuid"
    assert not install_id_file.exists(), "install_id.uuid must not be created when disabled"


async def test_disabled_reporter_returns_immediately(tmp_path: Path) -> None:
    """run() must return (not block) when enabled=False."""
    reporter = TelemetryReporter(
        enabled=False,
        endpoint="https://telemetry.example.com/v1/report",
        version="2.1.0",
        ha_version="2024.6.1",
        arch="aarch64",
        locale="en",
        data_dir=tmp_path,
    )

    # If run() blocks, this test will timeout (pytest-timeout at 60s)
    await reporter.run()
    # If we reach here, run() returned immediately — success


async def test_disabled_reporter_does_not_touch_existing_install_id(tmp_path: Path) -> None:
    """Even if install_id.uuid already exists, disabled reporter must not read or modify it."""
    install_id_file = tmp_path / "install_id.uuid"
    install_id_file.write_text("pre-existing-uuid", encoding="utf-8")

    reporter = TelemetryReporter(
        enabled=False,
        endpoint="https://telemetry.example.com/v1/report",
        version="2.1.0",
        ha_version="2024.6.1",
        arch="amd64",
        locale="en",
        data_dir=tmp_path,
    )

    await reporter.run()

    # File should still exist untouched (disabled reporter doesn't delete it)
    assert install_id_file.exists()
    assert install_id_file.read_text(encoding="utf-8") == "pre-existing-uuid"
