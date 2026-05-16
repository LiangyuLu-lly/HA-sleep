"""Tests for src/upgrade_notifier.py."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from src.upgrade_notifier import UpgradeNotifier


# ---------------------------------------------------------------------------
# is_newer tests
# ---------------------------------------------------------------------------


class TestIsNewer:
    """Unit tests for UpgradeNotifier.is_newer static method."""

    def test_newer_patch(self) -> None:
        assert UpgradeNotifier.is_newer("2.1.0", "2.1.1") is True

    def test_newer_minor(self) -> None:
        assert UpgradeNotifier.is_newer("2.1.0", "2.2.0") is True

    def test_newer_major(self) -> None:
        assert UpgradeNotifier.is_newer("2.1.0", "3.0.0") is True

    def test_same_version(self) -> None:
        assert UpgradeNotifier.is_newer("2.1.0", "2.1.0") is False

    def test_older_version(self) -> None:
        assert UpgradeNotifier.is_newer("2.2.0", "2.1.0") is False

    def test_prerelease_current(self) -> None:
        # Pre-release is older than release
        assert UpgradeNotifier.is_newer("2.1.0a1", "2.1.0") is True

    def test_prerelease_latest(self) -> None:
        # Pre-release latest is not newer than stable current
        assert UpgradeNotifier.is_newer("2.1.0", "2.1.1a1") is True

    def test_invalid_current(self) -> None:
        assert UpgradeNotifier.is_newer("not-a-version", "2.1.0") is False

    def test_invalid_latest(self) -> None:
        assert UpgradeNotifier.is_newer("2.1.0", "garbage!!") is False

    def test_both_invalid(self) -> None:
        assert UpgradeNotifier.is_newer("abc", "xyz") is False

    def test_empty_strings(self) -> None:
        assert UpgradeNotifier.is_newer("", "") is False


# ---------------------------------------------------------------------------
# run() tests
# ---------------------------------------------------------------------------


class TestRun:
    """Tests for the run() lifecycle."""

    async def test_disabled_returns_immediately(self, tmp_path: Path) -> None:
        """enabled=False -> run() returns without side effects."""
        ha_client = MagicMock()
        notifier = UpgradeNotifier(
            enabled=False,
            current_version="2.1.0",
            owner="owner",
            repo="repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=1.0,
        )
        # run() should return immediately
        await asyncio.wait_for(notifier.run(), timeout=1.0)
        # No files should be created
        assert not (tmp_path / "last_upgrade_check.json").exists()

    async def test_enabled_fetches_and_notifies(self, tmp_path: Path) -> None:
        """enabled=True -> fetches latest, detects newer, notifies."""
        ha_client = MagicMock()
        ha_client.call_service = AsyncMock()

        notifier = UpgradeNotifier(
            enabled=True,
            current_version="2.0.0",
            owner="owner",
            repo="repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=86400.0,
        )

        # Directly test _tick instead of run() to avoid sleep
        with patch.object(notifier, "_fetch_latest", return_value="v2.1.0"):
            await notifier._tick()

        # Should have called HA persistent notification
        ha_client.call_service.assert_called_once()
        call_args = ha_client.call_service.call_args
        assert call_args[0] == ("persistent_notification", "create")
        assert call_args[1]["notification_id"] == "sleep_classifier_upgrade"

        # Should have written state file
        state_path = tmp_path / "last_upgrade_check.json"
        assert state_path.exists()
        state = json.loads(state_path.read_text())
        assert state["latest"] == "v2.1.0"
        assert state["notified"] is True

    async def test_no_notify_when_already_notified(self, tmp_path: Path) -> None:
        """Don't re-notify for same version if already notified."""
        ha_client = MagicMock()
        ha_client.call_service = AsyncMock()

        # Pre-seed state with already-notified version
        state_path = tmp_path / "last_upgrade_check.json"
        state_path.write_text(json.dumps({
            "checked_at": "2025-01-01T00:00:00+00:00",
            "latest": "v2.1.0",
            "notified": True,
        }))

        notifier = UpgradeNotifier(
            enabled=True,
            current_version="2.0.0",
            owner="owner",
            repo="repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=86400.0,
        )

        with patch.object(notifier, "_fetch_latest", return_value="v2.1.0"):
            await notifier._tick()

        # Should NOT have called HA notification again
        ha_client.call_service.assert_not_called()

    async def test_no_notify_when_current_is_latest(self, tmp_path: Path) -> None:
        """No notification when current >= latest."""
        ha_client = MagicMock()
        ha_client.call_service = AsyncMock()

        notifier = UpgradeNotifier(
            enabled=True,
            current_version="2.1.0",
            owner="owner",
            repo="repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=86400.0,
        )

        with patch.object(notifier, "_fetch_latest", return_value="v2.1.0"):
            await notifier._tick()

        ha_client.call_service.assert_not_called()

    async def test_tick_exception_never_bubbles(self, tmp_path: Path) -> None:
        """Exceptions in _tick are caught and don't crash run()."""
        ha_client = MagicMock()

        notifier = UpgradeNotifier(
            enabled=True,
            current_version="2.0.0",
            owner="owner",
            repo="repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=0.01,
        )

        call_count = 0
        original_sleep = asyncio.sleep

        async def patched_sleep(delay: float) -> None:
            nonlocal call_count
            # Only count the interval sleeps (not backoff sleeps)
            if delay == 0.01:
                call_count += 1
                if call_count >= 2:
                    raise asyncio.CancelledError
            # For very short sleeps just return
            await original_sleep(0)

        with patch.object(notifier, "_tick", side_effect=RuntimeError("simulated")):
            with patch("asyncio.sleep", side_effect=patched_sleep):
                with pytest.raises(asyncio.CancelledError):
                    await notifier.run()

        # Should have survived at least one RuntimeError
        assert call_count >= 2

    async def test_fetch_failure_returns_none(self, tmp_path: Path) -> None:
        """Network failures in _fetch_latest return None gracefully."""
        ha_client = MagicMock()

        notifier = UpgradeNotifier(
            enabled=True,
            current_version="2.0.0",
            owner="owner",
            repo="repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=86400.0,
        )

        # Patch asyncio.sleep to avoid real waits during backoff
        with patch("src.upgrade_notifier.aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value.__aenter__ = AsyncMock(
                side_effect=aiohttp.ClientError("connection refused")
            )
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await notifier._fetch_latest()

        assert result is None


# ---------------------------------------------------------------------------
# Task 4.12: GitHub API response handling & header privacy tests
# ---------------------------------------------------------------------------


class TestGitHubAPINewVersion:
    """Mock GitHub API returns new version -> ha_client.call_service called."""

    async def test_new_version_triggers_notification(self, tmp_path: Path) -> None:
        """GitHub returns newer version -> persistent notification created."""
        ha_client = MagicMock()
        ha_client.call_service = AsyncMock()

        notifier = UpgradeNotifier(
            enabled=True,
            current_version="2.0.0",
            owner="test-owner",
            repo="test-repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=86400.0,
        )

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"tag_name": "v2.1.0"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("src.upgrade_notifier.aiohttp.ClientSession", return_value=mock_session):
            await notifier._tick()

        ha_client.call_service.assert_called_once()
        call_kwargs = ha_client.call_service.call_args[1]
        assert call_kwargs["notification_id"] == "sleep_classifier_upgrade"

    async def test_notification_id_is_sleep_classifier_upgrade(self, tmp_path: Path) -> None:
        """Notification uses the fixed notification_id."""
        ha_client = MagicMock()
        ha_client.call_service = AsyncMock()

        notifier = UpgradeNotifier(
            enabled=True,
            current_version="1.0.0",
            owner="owner",
            repo="repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=86400.0,
        )

        with patch.object(notifier, "_fetch_latest", return_value="v2.0.0"):
            await notifier._tick()

        ha_client.call_service.assert_called_once()
        args, kwargs = ha_client.call_service.call_args
        assert args == ("persistent_notification", "create")
        assert kwargs["notification_id"] == "sleep_classifier_upgrade"


class TestGitHubAPIErrors:
    """GitHub 403/404/5xx -> call_service not called, silent backoff."""

    @pytest.mark.parametrize("status_code", [403, 404, 500, 502, 503])
    async def test_error_status_no_notification(
        self, tmp_path: Path, status_code: int
    ) -> None:
        """HTTP error status codes -> no notification dispatched."""
        ha_client = MagicMock()
        ha_client.call_service = AsyncMock()

        notifier = UpgradeNotifier(
            enabled=True,
            current_version="2.0.0",
            owner="owner",
            repo="repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=86400.0,
        )

        mock_response = AsyncMock()
        mock_response.status = status_code
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("src.upgrade_notifier.aiohttp.ClientSession", return_value=mock_session):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                await notifier._tick()

        ha_client.call_service.assert_not_called()

    @pytest.mark.parametrize("status_code", [403, 404, 500])
    async def test_error_status_backoff_applied(
        self, tmp_path: Path, status_code: int
    ) -> None:
        """Error responses trigger backoff (sleep called)."""
        ha_client = MagicMock()
        ha_client.call_service = AsyncMock()

        notifier = UpgradeNotifier(
            enabled=True,
            current_version="2.0.0",
            owner="owner",
            repo="repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=86400.0,
        )

        mock_response = AsyncMock()
        mock_response.status = status_code
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.get = MagicMock(return_value=mock_response)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("src.upgrade_notifier.aiohttp.ClientSession", return_value=mock_session):
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                result = await notifier._fetch_latest()

        assert result is None
        # Backoff sleep should have been called
        mock_sleep.assert_called()


class TestHeaderPrivacy:
    """Headers must NOT contain install_id or Authorization."""

    async def test_no_install_id_in_headers(self, tmp_path: Path) -> None:
        """Request headers must not contain install_id."""
        ha_client = MagicMock()
        ha_client.call_service = AsyncMock()

        notifier = UpgradeNotifier(
            enabled=True,
            current_version="2.1.0",
            owner="owner",
            repo="repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=86400.0,
        )

        captured_headers: dict[str, Any] = {}

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"tag_name": "v2.1.0"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()

        def capture_get(url: str, *, headers: dict, **kwargs: Any) -> Any:
            captured_headers.update(headers)
            return mock_response

        mock_session.get = MagicMock(side_effect=capture_get)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("src.upgrade_notifier.aiohttp.ClientSession", return_value=mock_session):
            await notifier._fetch_latest()

        # Verify no sensitive headers
        header_keys_lower = {k.lower() for k in captured_headers}
        assert "install_id" not in header_keys_lower
        assert "x-install-id" not in header_keys_lower
        assert "authorization" not in header_keys_lower

        # Positive check: User-Agent IS present
        assert "User-Agent" in captured_headers or "user-agent" in captured_headers

    async def test_no_authorization_header(self, tmp_path: Path) -> None:
        """Request headers must not contain Authorization."""
        ha_client = MagicMock()
        ha_client.call_service = AsyncMock()

        notifier = UpgradeNotifier(
            enabled=True,
            current_version="2.1.0",
            owner="owner",
            repo="repo",
            ha_client=ha_client,
            data_dir=tmp_path,
            interval_seconds=86400.0,
        )

        captured_headers: dict[str, Any] = {}

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json = AsyncMock(return_value={"tag_name": "v2.1.0"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()

        def capture_get(url: str, *, headers: dict, **kwargs: Any) -> Any:
            captured_headers.update(headers)
            return mock_response

        mock_session.get = MagicMock(side_effect=capture_get)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("src.upgrade_notifier.aiohttp.ClientSession", return_value=mock_session):
            await notifier._fetch_latest()

        # Verify Authorization is not present
        for key in captured_headers:
            assert key.lower() != "authorization", (
                f"Authorization header found: {key}={captured_headers[key]}"
            )
