"""Tests for src/telemetry_reporter.py.

Covers Requirements 6.1, 6.2, 6.3, 6.4, 6.6, 6.7, 6.8, 6.9.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.telemetry_reporter import TelemetryReporter, _ENTITY_ID_PATTERN


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Provide a temporary data directory."""
    return tmp_path


def _make_reporter(
    data_dir: Path,
    *,
    enabled: bool = True,
    endpoint: str = "https://telemetry.example.com/v1/report",
    version: str = "2.1.0",
    ha_version: str = "2024.10.4",
    arch: str = "aarch64",
    locale: str = "zh-cn",
    clock=None,
    interval_seconds: float = 0.01,
) -> TelemetryReporter:
    return TelemetryReporter(
        enabled=enabled,
        endpoint=endpoint,
        version=version,
        ha_version=ha_version,
        arch=arch,
        locale=locale,
        data_dir=data_dir,
        clock=clock or (lambda: 1_700_000_000.0),
        interval_seconds=interval_seconds,
    )


# ---------------------------------------------------------------------------
# Tests: build_payload (Requirement 6.3)
# ---------------------------------------------------------------------------


class TestBuildPayload:
    """Test TelemetryReporter.build_payload static method."""

    def test_basic_payload_structure(self) -> None:
        payload = TelemetryReporter.build_payload(
            install_id="550e8400-e29b-41d4-a716-446655440000",
            version="2.1.0",
            ha_version="2024.10.4",
            arch="aarch64",
            locale="zh-cn",
            days_since_install=42,
            active_last_24h=True,
        )
        assert payload["install_id"] == "550e8400-e29b-41d4-a716-446655440000"
        assert payload["version"] == "2.1.0"
        assert payload["ha_version"] == "2024.10.4"
        assert payload["arch"] == "aarch64"
        assert payload["locale"] == "zh-cn"
        assert payload["days_since_install"] == 42
        assert payload["active_last_24h"] is True

    def test_payload_does_not_leak_entity_id(self) -> None:
        payload = TelemetryReporter.build_payload(
            install_id="test-uuid",
            version="2.1.0",
            ha_version="2024.10.4",
            arch="amd64",
            locale="en",
            days_since_install=0,
            active_last_24h=False,
        )
        serialized = json.dumps(payload, sort_keys=True)
        assert not _ENTITY_ID_PATTERN.search(serialized)

    def test_payload_raises_on_entity_id_pattern_match(self) -> None:
        """The self-check raises if _ENTITY_ID_PATTERN matches the serialized
        payload.  We test the mechanism by patching the pattern to always match.
        """
        import src.telemetry_reporter as mod

        original = mod._ENTITY_ID_PATTERN
        try:
            # Use a pattern that matches everything
            mod._ENTITY_ID_PATTERN = re.compile(r"install_id")
            with pytest.raises(RuntimeError, match="leaked entity_id"):
                TelemetryReporter.build_payload(
                    install_id="test-uuid",
                    version="2.1.0",
                    ha_version="2024.10.4",
                    arch="amd64",
                    locale="en",
                    days_since_install=0,
                    active_last_24h=False,
                )
        finally:
            mod._ENTITY_ID_PATTERN = original


# ---------------------------------------------------------------------------
# Tests: disabled mode (Requirement 6.4)
# ---------------------------------------------------------------------------


class TestDisabledMode:
    """When telemetry is disabled, no side effects should occur."""

    async def test_run_returns_immediately_when_disabled(
        self, data_dir: Path
    ) -> None:
        reporter = _make_reporter(data_dir, enabled=False)
        # run() should return immediately without blocking
        await asyncio.wait_for(reporter.run(), timeout=1.0)

    async def test_no_install_id_created_when_disabled(
        self, data_dir: Path
    ) -> None:
        reporter = _make_reporter(data_dir, enabled=False)
        await reporter.run()
        assert not (data_dir / "install_id.uuid").exists()


# ---------------------------------------------------------------------------
# Tests: install_id lifecycle (Requirement 6.1, 6.2)
# ---------------------------------------------------------------------------


class TestInstallId:
    """install_id generation and persistence."""

    def test_creates_install_id_on_first_run(self, data_dir: Path) -> None:
        reporter = _make_reporter(data_dir, enabled=True)
        install_id = reporter._get_or_create_install_id()
        assert (data_dir / "install_id.uuid").exists()
        assert install_id == (data_dir / "install_id.uuid").read_text().strip()

    def test_reuses_existing_install_id(self, data_dir: Path) -> None:
        existing_id = "existing-uuid-value"
        (data_dir / "install_id.uuid").write_text(existing_id)
        reporter = _make_reporter(data_dir, enabled=True)
        install_id = reporter._get_or_create_install_id()
        assert install_id == existing_id


# ---------------------------------------------------------------------------
# Tests: disable() idempotency (Requirement 6.6)
# ---------------------------------------------------------------------------


class TestDisable:
    """disable() must be idempotent and clean up state."""

    async def test_disable_removes_install_id_file(
        self, data_dir: Path
    ) -> None:
        reporter = _make_reporter(data_dir, enabled=True)
        reporter._get_or_create_install_id()
        assert (data_dir / "install_id.uuid").exists()

        await reporter.disable()
        assert not (data_dir / "install_id.uuid").exists()

    async def test_disable_idempotent(self, data_dir: Path) -> None:
        reporter = _make_reporter(data_dir, enabled=True)
        reporter._get_or_create_install_id()

        # Call disable multiple times
        await reporter.disable()
        await reporter.disable()
        await reporter.disable()
        # Should not raise
        assert not (data_dir / "install_id.uuid").exists()

    async def test_disable_cancels_running_task(
        self, data_dir: Path
    ) -> None:
        reporter = _make_reporter(data_dir, enabled=True)

        # Simulate a running task
        with patch.object(reporter, "_tick", new_callable=AsyncMock):
            reporter.enable()
            assert reporter._task is not None
            assert not reporter._task.done()

            await reporter.disable()
            assert reporter._task is None


# ---------------------------------------------------------------------------
# Tests: enable() state machine
# ---------------------------------------------------------------------------


class TestEnable:
    """enable() / run() task management."""

    async def test_enable_starts_task(self, data_dir: Path) -> None:
        reporter = _make_reporter(data_dir, enabled=False)

        with patch.object(reporter, "run", new_callable=AsyncMock):
            reporter._enabled = True
            reporter.enable()
            assert reporter._task is not None

            # Cleanup
            reporter._task.cancel()
            try:
                await reporter._task
            except asyncio.CancelledError:
                pass

    async def test_enable_is_noop_when_already_running(
        self, data_dir: Path
    ) -> None:
        reporter = _make_reporter(data_dir, enabled=True)

        with patch.object(reporter, "run", new_callable=AsyncMock):
            reporter.enable()
            first_task = reporter._task
            reporter.enable()
            assert reporter._task is first_task

            # Cleanup
            if reporter._task and not reporter._task.done():
                reporter._task.cancel()
                try:
                    await reporter._task
                except asyncio.CancelledError:
                    pass


# ---------------------------------------------------------------------------
# Tests: network failure backoff (Requirement 6.7)
# ---------------------------------------------------------------------------


class TestBackoff:
    """Exponential backoff on network failures."""

    async def test_backoff_doubles_on_failure(self, data_dir: Path) -> None:
        reporter = _make_reporter(data_dir, enabled=True)
        reporter._get_or_create_install_id()

        # Initial backoff should be 60s
        assert reporter._backoff == 60.0

        # Simulate a failed tick
        with patch.object(
            reporter,
            "_tick",
            side_effect=[Exception("network error"), asyncio.CancelledError],
        ):
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                with pytest.raises(asyncio.CancelledError):
                    await reporter.run()

                # First call to sleep should be the backoff value (60s)
                assert mock_sleep.call_args_list[0][0][0] == 60.0

        # Backoff should have doubled
        assert reporter._backoff == 120.0

    async def test_backoff_caps_at_max(self, data_dir: Path) -> None:
        reporter = _make_reporter(data_dir, enabled=True)
        reporter._backoff = 50_000.0  # Already high

        # After one failure, should cap at 86400
        with patch.object(
            reporter,
            "_tick",
            side_effect=[Exception("fail"), asyncio.CancelledError],
        ):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(asyncio.CancelledError):
                    await reporter.run()

        assert reporter._backoff == 86_400.0

    async def test_backoff_resets_on_success(self, data_dir: Path) -> None:
        reporter = _make_reporter(data_dir, enabled=True)
        reporter._backoff = 500.0  # Elevated from previous failures

        call_count = 0

        async def tick_side_effect(install_id: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise asyncio.CancelledError

        with patch.object(reporter, "_tick", side_effect=tick_side_effect):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                with pytest.raises(asyncio.CancelledError):
                    await reporter.run()

        # After success, backoff should reset to initial
        assert reporter._backoff == 60.0


# ---------------------------------------------------------------------------
# Tests: HTTP reporting (Requirement 6.8)
# ---------------------------------------------------------------------------


class TestHTTPReporting:
    """Telemetry reports via aiohttp."""

    async def test_successful_report(self, data_dir: Path) -> None:
        reporter = _make_reporter(data_dir, enabled=True)
        install_id = reporter._get_or_create_install_id()

        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.request_info = AsyncMock()
        mock_resp.history = ()

        mock_post_cm = AsyncMock()
        mock_post_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_post_cm.__aexit__ = AsyncMock(return_value=False)

        mock_session = AsyncMock()
        mock_session.post = lambda *a, **kw: mock_post_cm
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.telemetry_reporter.aiohttp.ClientSession",
            return_value=mock_session,
        ):
            await reporter._tick(install_id)
