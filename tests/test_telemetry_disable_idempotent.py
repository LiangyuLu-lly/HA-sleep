"""Property 6: Telemetry 撤回幂等.

Parametrize N in [1..10] ``enable()`` / ``disable()`` sequences.  After
final ``disable()``, assert ``_task is None`` and install_id file gone.
Equivalent to single disable().

**Validates: Requirements 6.6**
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from src.telemetry_reporter import TelemetryReporter


@pytest.fixture()
def reporter(tmp_path: Path) -> TelemetryReporter:
    """Create a TelemetryReporter with tmp_path as data_dir."""
    return TelemetryReporter(
        enabled=False,
        endpoint="https://telemetry.example.com/v1/report",
        version="2.1.0",
        ha_version="2024.6.1",
        arch="aarch64",
        locale="zh-cn",
        data_dir=tmp_path,
    )


@pytest.mark.parametrize("n", range(1, 11))
async def test_enable_disable_sequence_idempotent(
    reporter: TelemetryReporter, tmp_path: Path, n: int
) -> None:
    """After N enable()/disable() cycles, state equals single disable()."""
    for _ in range(n):
        reporter.enable()
        # Give the task a moment to start
        await asyncio.sleep(0.01)
        await reporter.disable()

    # Final state assertions
    assert reporter._task is None, f"_task must be None after {n} disable() calls"
    install_id_file = tmp_path / "install_id.uuid"
    assert not install_id_file.exists(), (
        f"install_id.uuid must not exist after {n} disable() calls"
    )


async def test_single_disable_from_never_enabled(
    reporter: TelemetryReporter, tmp_path: Path
) -> None:
    """disable() on never-enabled reporter is a no-op (idempotent base case)."""
    await reporter.disable()

    assert reporter._task is None
    install_id_file = tmp_path / "install_id.uuid"
    assert not install_id_file.exists()


async def test_multiple_disables_without_enable(
    reporter: TelemetryReporter, tmp_path: Path
) -> None:
    """Multiple disable() calls without any enable() do not raise."""
    for _ in range(5):
        await reporter.disable()

    assert reporter._task is None
    install_id_file = tmp_path / "install_id.uuid"
    assert not install_id_file.exists()


async def test_enable_creates_install_id_then_disable_removes_it(
    reporter: TelemetryReporter, tmp_path: Path
) -> None:
    """enable() creates install_id.uuid; disable() removes it."""
    reporter.enable()
    await asyncio.sleep(0.05)  # Let the task start and create install_id

    install_id_file = tmp_path / "install_id.uuid"
    assert install_id_file.exists(), "enable() should create install_id.uuid"

    await reporter.disable()

    assert not install_id_file.exists(), "disable() should remove install_id.uuid"
    assert reporter._task is None
