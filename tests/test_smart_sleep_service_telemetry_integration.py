"""Integration tests for telemetry_reporter + upgrade_notifier task registration.

Validates: Requirements 6.8, 9.1, PR5.2

Tests:
- Two new tasks are registered by name ("telemetry_reporter", "upgrade_notifier")
- SIGTERM (simulated via task.cancel()) causes main process to exit within ≤10s
  and new tasks are cancelled
- enabled=false state: two tasks return immediately, no HTTP outbound
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _write_config(tmp_path: Path, overrides: dict | None = None) -> Path:
    """Write a minimal config.json for dry-run testing."""
    from training_config.config_loader import get_default_config

    cfg = get_default_config()
    ha = cfg.setdefault("home_assistant", {})
    ha["api"] = {
        "base_url": "http://localhost:8123",
        "access_token": "test-token",
        "verify_ssl": False,
        "sleep_stage_source": "sensor.test_sleep_stage",
    }
    ha["preference_learner"] = {
        "enabled": False,
        "history_path": str(tmp_path / "user_preferences.json"),
    }
    ha["smart_control"] = {"enabled": True, "dry_run": True}
    ha["natural_sleep"] = {
        "profile_path": str(tmp_path / "user_profile.json"),
    }
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def _write_overrides(tmp_path: Path, telemetry_enabled: bool = False,
                     upgrade_enabled: bool = False) -> None:
    """Write web_ui_overrides.json with specified flags."""
    overrides = {
        "sleep_stage_source": "sensor.test_sleep_stage",
        "telemetry_enabled": telemetry_enabled,
        "upgrade_notifications_enabled": upgrade_enabled,
    }
    overrides_path = tmp_path / "web_ui_overrides.json"
    overrides_path.write_text(json.dumps(overrides), encoding="utf-8")


class TestTelemetryUpgradeTaskRegistration:
    """Test that telemetry_reporter and upgrade_notifier tasks are registered."""

    async def test_tasks_registered_by_name(self, tmp_path: Path):
        """Two new tasks 'telemetry_reporter' and 'upgrade_notifier' are created."""
        _write_config(tmp_path)
        _write_overrides(tmp_path, telemetry_enabled=True, upgrade_enabled=True)

        created_task_names: list[str] = []
        original_create_task = asyncio.create_task

        def _tracking_create_task(coro, *, name=None, **kwargs):
            task = original_create_task(coro, name=name, **kwargs)
            if name:
                created_task_names.append(name)
            return task

        # Mock HA client to avoid real connections
        mock_ha = AsyncMock()
        mock_ha.ping = AsyncMock(return_value=True)
        mock_ha.get_states = AsyncMock(return_value=[])
        mock_ha.connect_websocket = AsyncMock()
        mock_ha.subscribe_state_changes = AsyncMock()
        mock_ha.iter_state_changes = AsyncMock(return_value=iter([]))
        mock_ha.get_ha_version = AsyncMock(return_value="2024.10.0")
        mock_ha.__aenter__ = AsyncMock(return_value=mock_ha)
        mock_ha.__aexit__ = AsyncMock(return_value=False)

        from scripts.run_ha_smart_service import SmartSleepService, _BUFFER_DIR
        import argparse

        args = argparse.Namespace(
            config=str(tmp_path / "cfg.json"),
            model="models/best_model.h5",
            base_url=None, token="test-token", area=None,
            infer_interval=30.0,
            session_interval=1800.0,
            duration=2.0,  # run for 2 seconds
            dry_run=True,
            verbose=False,
        )

        service = SmartSleepService(args)

        with patch.object(service, '_BUFFER_DIR', tmp_path, create=True):
            pass

        # Patch _BUFFER_DIR at module level
        import scripts.run_ha_smart_service as svc_mod
        original_buffer_dir = svc_mod._BUFFER_DIR

        try:
            svc_mod._BUFFER_DIR = tmp_path
            # Re-create service to pick up new buffer dir
            service = SmartSleepService(args)

            with patch("asyncio.create_task", side_effect=_tracking_create_task):
                with patch(
                    "scripts.run_ha_smart_service.HomeAssistantClient",
                    return_value=mock_ha,
                ):
                    # Patch publisher to avoid errors
                    with patch.object(
                        service, 'publisher', None
                    ):
                        result = await asyncio.wait_for(service.run(), timeout=10.0)

            assert "telemetry_reporter" in created_task_names
            assert "upgrade_notifier" in created_task_names
        finally:
            svc_mod._BUFFER_DIR = original_buffer_dir


class TestTaskShutdownOnCancel:
    """Test that SIGTERM (task.cancel()) causes exit within ≤10s."""

    async def test_cancel_causes_timely_exit(self, tmp_path: Path):
        """Simulate SIGTERM via stop_event.set(); tasks are cancelled promptly."""
        from src.telemetry_reporter import TelemetryReporter
        from src.upgrade_notifier import UpgradeNotifier

        # Create a TelemetryReporter and UpgradeNotifier with enabled=True
        reporter = TelemetryReporter(
            enabled=True,
            endpoint="http://localhost:9999/v1/report",
            version="2.1.0",
            ha_version="2024.10.0",
            arch="amd64",
            locale="en",
            data_dir=tmp_path,
            interval_seconds=86400.0,
        )

        mock_ha_client = AsyncMock()
        notifier = UpgradeNotifier(
            enabled=True,
            current_version="2.1.0",
            owner="test-owner",
            repo="test-repo",
            ha_client=mock_ha_client,
            data_dir=tmp_path,
            interval_seconds=86400.0,
        )

        # Start the tasks
        telemetry_task = asyncio.create_task(
            reporter.run(), name="telemetry_reporter"
        )
        upgrade_task = asyncio.create_task(
            notifier.run(), name="upgrade_notifier"
        )

        # Give them a moment to start
        await asyncio.sleep(0.1)

        # Simulate SIGTERM by cancelling tasks
        start = time.monotonic()
        telemetry_task.cancel()
        upgrade_task.cancel()

        # Await with timeout ≤ 10 seconds
        done, pending = await asyncio.wait(
            [telemetry_task, upgrade_task],
            timeout=10.0,
        )

        elapsed = time.monotonic() - start
        assert len(pending) == 0, "Tasks did not complete within 10 seconds"
        assert elapsed <= 10.0, f"Shutdown took {elapsed:.1f}s, exceeding 10s limit"

        # Verify tasks are done/cancelled
        assert telemetry_task.done()
        assert upgrade_task.done()


class TestDisabledTasksNoOutbound:
    """Test that enabled=false tasks return immediately with no HTTP outbound."""

    async def test_telemetry_disabled_returns_immediately(self, tmp_path: Path):
        """TelemetryReporter with enabled=False returns immediately, no files."""
        from src.telemetry_reporter import TelemetryReporter

        reporter = TelemetryReporter(
            enabled=False,
            endpoint="http://should-not-be-called.invalid/v1/report",
            version="2.1.0",
            ha_version="2024.10.0",
            arch="amd64",
            locale="en",
            data_dir=tmp_path,
            interval_seconds=86400.0,
        )

        # Should return immediately
        task = asyncio.create_task(reporter.run(), name="telemetry_reporter")
        await asyncio.wait_for(task, timeout=2.0)

        # No install_id file should be created
        assert not (tmp_path / "install_id.uuid").exists()

    async def test_upgrade_disabled_returns_immediately(self, tmp_path: Path):
        """UpgradeNotifier with enabled=False returns immediately, no files."""
        from src.upgrade_notifier import UpgradeNotifier

        mock_ha_client = AsyncMock()
        notifier = UpgradeNotifier(
            enabled=False,
            current_version="2.1.0",
            owner="test-owner",
            repo="test-repo",
            ha_client=mock_ha_client,
            data_dir=tmp_path,
            interval_seconds=86400.0,
        )

        # Should return immediately
        task = asyncio.create_task(notifier.run(), name="upgrade_notifier")
        await asyncio.wait_for(task, timeout=2.0)

        # No state file should be created
        assert not (tmp_path / "last_upgrade_check.json").exists()
        # No HA calls should be made
        mock_ha_client.call_service.assert_not_called()

    async def test_disabled_tasks_no_http_outbound(self, tmp_path: Path):
        """With both disabled, zero HTTP requests are made (Property 4 cross-check)."""
        import aiohttp
        from src.telemetry_reporter import TelemetryReporter
        from src.upgrade_notifier import UpgradeNotifier

        http_call_count = 0
        original_init = aiohttp.ClientSession.__init__

        class TrackingSession(aiohttp.ClientSession):
            async def _request(self, method, url, **kwargs):
                nonlocal http_call_count
                http_call_count += 1
                raise AssertionError(
                    f"Unexpected HTTP request: {method} {url}"
                )

        reporter = TelemetryReporter(
            enabled=False,
            endpoint="http://should-not-be-called.invalid/v1/report",
            version="2.1.0",
            ha_version="2024.10.0",
            arch="amd64",
            locale="en",
            data_dir=tmp_path,
            interval_seconds=86400.0,
        )

        mock_ha_client = AsyncMock()
        notifier = UpgradeNotifier(
            enabled=False,
            current_version="2.1.0",
            owner="test-owner",
            repo="test-repo",
            ha_client=mock_ha_client,
            data_dir=tmp_path,
            interval_seconds=86400.0,
        )

        with patch("aiohttp.ClientSession", TrackingSession):
            t1 = asyncio.create_task(reporter.run(), name="telemetry_reporter")
            t2 = asyncio.create_task(notifier.run(), name="upgrade_notifier")

            await asyncio.wait_for(
                asyncio.gather(t1, t2), timeout=2.0
            )

        assert http_call_count == 0, (
            f"Expected zero HTTP requests when disabled; got {http_call_count}"
        )
