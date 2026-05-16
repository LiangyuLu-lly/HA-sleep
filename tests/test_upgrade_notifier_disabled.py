"""Property 4 (upgrade part): disabled state -> zero external outbound requests.

Mocks aiohttp outbound and asserts that when enabled=False:
- run() returns immediately
- HTTP request count is 0
- /data/last_upgrade_check.json is not written

**Validates: Requirements 9.3**
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.upgrade_notifier import UpgradeNotifier


class TestUpgradeNotifierDisabled:
    """Verify zero side effects when upgrade notifications are disabled."""

    async def test_disabled_run_returns_immediately(self, tmp_path: Path) -> None:
        """enabled=False -> run() returns without blocking."""
        ha_client = MagicMock()
        ha_client.call_service = AsyncMock()

        notifier = UpgradeNotifier(
            enabled=False,
            current_version="2.1.0",
            owner="owner",
            repo="repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=1.0,
        )

        # Should complete within a very short time (immediate return)
        await asyncio.wait_for(notifier.run(), timeout=1.0)

    async def test_disabled_no_http_requests(self, tmp_path: Path) -> None:
        """enabled=False -> no aiohttp.ClientSession created."""
        ha_client = MagicMock()
        ha_client.call_service = AsyncMock()

        notifier = UpgradeNotifier(
            enabled=False,
            current_version="2.1.0",
            owner="owner",
            repo="repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=1.0,
        )

        with patch("src.upgrade_notifier.aiohttp.ClientSession") as mock_session_cls:
            await asyncio.wait_for(notifier.run(), timeout=1.0)
            # ClientSession should never have been instantiated
            mock_session_cls.assert_not_called()

    async def test_disabled_no_state_file_written(self, tmp_path: Path) -> None:
        """enabled=False -> last_upgrade_check.json is not created."""
        ha_client = MagicMock()
        ha_client.call_service = AsyncMock()

        notifier = UpgradeNotifier(
            enabled=False,
            current_version="2.1.0",
            owner="owner",
            repo="repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=1.0,
        )

        await asyncio.wait_for(notifier.run(), timeout=1.0)

        state_file = tmp_path / "last_upgrade_check.json"
        assert not state_file.exists(), "State file should not be written when disabled"

    async def test_disabled_ha_client_not_called(self, tmp_path: Path) -> None:
        """enabled=False -> ha_client.call_service is never invoked."""
        ha_client = MagicMock()
        ha_client.call_service = AsyncMock()

        notifier = UpgradeNotifier(
            enabled=False,
            current_version="2.1.0",
            owner="owner",
            repo="repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=1.0,
        )

        await asyncio.wait_for(notifier.run(), timeout=1.0)
        ha_client.call_service.assert_not_called()

    async def test_disabled_no_atomic_write(self, tmp_path: Path) -> None:
        """enabled=False -> atomic_write_json is never called."""
        ha_client = MagicMock()
        ha_client.call_service = AsyncMock()

        notifier = UpgradeNotifier(
            enabled=False,
            current_version="2.1.0",
            owner="owner",
            repo="repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=1.0,
        )

        with patch("src.upgrade_notifier.atomic_write_json") as mock_write:
            await asyncio.wait_for(notifier.run(), timeout=1.0)
            mock_write.assert_not_called()
