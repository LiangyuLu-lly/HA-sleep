"""探索性测试 — Bug 1.9: WS 单次 401 即 stop。

验证 `_task_ws_listener` 对 `HAAuthError` 的处理逻辑。

当前行为（bug）：单次 `HAAuthError` 立即调用 `stop_event.set()`，
导致 HA Core 重启期间短暂的 401 就让整个 smart service 退出。

期望行为：应有计数器/阈值机制（如 `MAX_AUTH_FAILURES`），只有连续
多次 auth 失败才判定 token 真正失效并 stop。

**Validates: Requirements 1.9**
"""
import ast
import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SERVICE_SCRIPT = _PROJECT_ROOT / "scripts" / "run_ha_smart_service.py"


def test_single_auth_error_sets_stop_event():
    """Assert that _task_ws_listener has a counter/threshold mechanism for
    auth errors before calling stop_event.set().

    Currently it does NOT — a single HAAuthError immediately triggers
    stop_event.set(). This test FAILS on unfixed code, proving the bug
    exists.
    """
    source = _SERVICE_SCRIPT.read_text(encoding="utf-8")

    # --- Strategy 1: AST-based analysis ---
    # Parse the source and find the _task_ws_listener method.
    tree = ast.parse(source)

    ws_listener_node = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_task_ws_listener":
            ws_listener_node = node
            break

    assert ws_listener_node is not None, (
        "_task_ws_listener method not found in run_ha_smart_service.py"
    )

    # Extract the source lines of _task_ws_listener for regex analysis.
    start_line = ws_listener_node.lineno
    end_line = ws_listener_node.end_lineno
    lines = source.splitlines()
    method_source = "\n".join(lines[start_line - 1 : end_line])

    # --- Strategy 2: Check for counter/threshold mechanism ---
    # The fixed code should have something like:
    #   - A variable tracking auth failure count (e.g., auth_fail_count, _auth_failures)
    #   - A constant like MAX_AUTH_FAILURES or AUTH_FAILURE_THRESHOLD
    #   - A conditional check before calling stop_event.set()
    #     (e.g., `if auth_fail_count >= MAX_AUTH_FAILURES:`)

    has_counter_pattern = bool(re.search(
        r"(auth_fail|auth_error|auth_failure|consecutive_auth|auth_count)",
        method_source,
        re.IGNORECASE,
    ))

    has_threshold_constant = bool(re.search(
        r"(MAX_AUTH|AUTH_THRESHOLD|AUTH_FAILURE_LIMIT|max_auth_failures)",
        method_source,
        re.IGNORECASE,
    ))

    # Also check module-level for the threshold constant
    has_module_threshold = bool(re.search(
        r"(MAX_AUTH|AUTH_THRESHOLD|AUTH_FAILURE_LIMIT|max_auth_failures)\s*=\s*\d+",
        source,
        re.IGNORECASE,
    ))

    # --- Strategy 3: Verify the bug pattern exists ---
    # The bug: HAAuthError is caught and immediately calls stop_event.set()
    # without any conditional counter check.
    # Look for the pattern: except (HAAuthError, ...) followed by stop_event.set()
    # WITHOUT an intervening counter check.
    bug_pattern = re.search(
        r"except\s*\(HAAuthError.*?\).*?:"
        r".*?stop_event\.set\(\)",
        method_source,
        re.DOTALL,
    )

    # The fix should have a counter mechanism — at least ONE of these should be true:
    has_retry_mechanism = has_counter_pattern or has_threshold_constant or has_module_threshold

    # ASSERTION: The code should have a counter/threshold mechanism.
    # On unfixed code this FAILS — proving the bug exists.
    assert has_retry_mechanism, (
        "Bug 1.9 confirmed: _task_ws_listener has NO counter/threshold "
        "mechanism for HAAuthError. A single auth error immediately calls "
        "stop_event.set(), killing the service on any transient HA Core "
        "restart. Expected a pattern like 'if auth_fail_count >= MAX_AUTH_FAILURES' "
        "before stop_event.set()."
    )

    # If we somehow get here (code was fixed), also verify the bug pattern is gone
    if has_retry_mechanism:
        # The unconditional stop_event.set() in the HAAuthError handler should
        # be guarded by a counter check
        assert not bug_pattern or has_counter_pattern, (
            "stop_event.set() is called in HAAuthError handler but no "
            "counter variable is visible in the method body."
        )


# ---------------------------------------------------------------------------
# Functional tests — exercise _task_ws_listener with mocked HA client
# ---------------------------------------------------------------------------

import argparse
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

try:
    import aiohttp
except ImportError:
    aiohttp = None


def _write_config(tmp_path: Path) -> Path:
    """Write a minimal config.json for SmartSleepService instantiation."""
    from training_config.config_loader import get_default_config

    cfg = get_default_config()
    ha = cfg.setdefault("home_assistant", {})
    ha["api"] = {
        "base_url": "http://localhost:8123",
        "access_token": "test-token",
        "verify_ssl": False,
    }
    ha["preference_learner"] = {
        "enabled": False,
        "history_path": str(tmp_path / "user_preferences.json"),
    }
    ha["smart_control"] = {"enabled": True, "dry_run": True}
    ha["natural_sleep"] = {"profile_path": str(tmp_path / "user_profile.json")}
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def _args(config_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        config=str(config_path),
        model="models/best_model.h5",
        base_url=None,
        token=None,
        area=None,
        infer_interval=30.0,
        session_interval=1800.0,
        duration=None,
        dry_run=True,
        verbose=False,
    )


def _make_service(tmp_path: Path):
    """Create a SmartSleepService instance for testing."""
    from scripts.run_ha_smart_service import SmartSleepService

    cfg_path = _write_config(tmp_path)
    return SmartSleepService(_args(cfg_path))


def _mock_ha_client():
    """Create a mock HA client with async methods."""
    ha = AsyncMock()
    ha.connect_websocket = AsyncMock()
    ha.subscribe_state_changes = AsyncMock()
    return ha


def _mock_discovery():
    """Create a minimal mock DiscoveryResult."""
    discovery = MagicMock()
    discovery.devices.lights = []
    discovery.devices.climates = []
    discovery.devices.fans = []
    discovery.devices.humidifiers = []
    discovery.devices.switches = []
    discovery.devices.media_players = []
    discovery.sensors.temperature = []
    discovery.sensors.humidity = []
    discovery.sensors.illuminance = []
    return discovery


def _mock_engine():
    """Create a minimal mock ExternalStageSubscriber."""
    return MagicMock()


# ---------------------------------------------------------------------------
# (a) Single HAAuthError → stop_event NOT set
# ---------------------------------------------------------------------------


async def test_single_auth_error_does_not_stop(tmp_path: Path):
    """A single HAAuthError should NOT trigger stop_event.set().

    **Validates: Requirements 1.9**
    """
    from src.ha_api_client import HAAuthError

    svc = _make_service(tmp_path)
    ha = _mock_ha_client()
    discovery = _mock_discovery()
    engine = _mock_engine()

    # iter_state_changes raises HAAuthError once, then we set stop_event
    # to break the loop on the second iteration.
    call_count = 0

    async def _fake_iter(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise HAAuthError("HA WS auth failed: 401")
        # Second call: set stop_event to end the loop
        svc.stop_event.set()
        return
        yield  # make it an async generator  # noqa: RET503

    ha.iter_state_changes = _fake_iter

    await asyncio.wait_for(
        svc._task_ws_listener(ha, discovery, engine),
        timeout=5.0,
    )

    # After a single auth error, stop_event should have been set by US
    # (to end the test), not by the auth failure threshold.
    # The key assertion: the service did NOT stop on the first auth error.
    # It only stopped because we set stop_event on the second iteration.
    assert call_count == 2


# ---------------------------------------------------------------------------
# (b) 10 consecutive HAAuthError → stop_event.set()
# ---------------------------------------------------------------------------


async def test_consecutive_auth_errors_trigger_stop(tmp_path: Path):
    """10 consecutive HAAuthErrors should trigger stop_event.set().

    **Validates: Requirements 1.9**
    """
    from scripts.run_ha_smart_service import MAX_AUTH_FAILURES
    from src.ha_api_client import HAAuthError

    svc = _make_service(tmp_path)
    ha = _mock_ha_client()
    discovery = _mock_discovery()
    engine = _mock_engine()

    # iter_state_changes always raises HAAuthError
    async def _fake_iter(*args, **kwargs):
        raise HAAuthError("HA WS auth failed: 401")
        yield  # noqa: RET503

    ha.iter_state_changes = _fake_iter

    # Patch random.uniform to 0 so effective timeout = backoff (still slow).
    # The key trick: replace stop_event with one whose wait() never resolves
    # but we make the timeout tiny by patching the backoff calculation.
    # We'll monkeypatch the module-level random.uniform to return a value
    # that makes timeout negative (clamped to 0 by asyncio).
    # Actually: timeout = backoff + uniform(-jitter, jitter).
    # If uniform returns -backoff, timeout = 0 → instant TimeoutError.
    # But uniform is called with (-jitter, jitter) where jitter = backoff*0.2
    # so the mock return value just needs to be within that range... no.
    # We're patching the function itself, so we can return anything.
    # Return -backoff to make timeout = backoff + (-backoff) = 0.
    # asyncio.wait_for with timeout=0 raises TimeoutError immediately if
    # the coroutine doesn't complete synchronously.

    # We need a dynamic return: -backoff changes each iteration.
    # Simpler: just return a large negative number like -1000.
    with patch("scripts.run_ha_smart_service.random.uniform", return_value=-1000.0):
        await asyncio.wait_for(
            svc._task_ws_listener(ha, discovery, engine),
            timeout=10.0,
        )

    assert svc.stop_event.is_set()


# ---------------------------------------------------------------------------
# (c) HAAPIError 500 → reconnect (stop_event NOT set)
# ---------------------------------------------------------------------------


async def test_api_error_does_not_stop(tmp_path: Path):
    """HAAPIError (non-auth, e.g. 500) should reconnect, not stop.

    **Validates: Requirements 1.9**
    """
    from src.ha_api_client import HAAPIError

    svc = _make_service(tmp_path)
    ha = _mock_ha_client()
    discovery = _mock_discovery()
    engine = _mock_engine()

    call_count = 0

    async def _fake_iter(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise HAAPIError("HA REST GET /api/states failed: 500")
        # Second call: stop to end the test
        svc.stop_event.set()
        return
        yield  # noqa: RET503

    ha.iter_state_changes = _fake_iter

    await asyncio.wait_for(
        svc._task_ws_listener(ha, discovery, engine),
        timeout=5.0,
    )

    # The service reconnected (call_count == 2) and only stopped because
    # we explicitly set stop_event, not because of the API error.
    assert call_count == 2


# ---------------------------------------------------------------------------
# (d) CancelledError → re-raise
# ---------------------------------------------------------------------------


async def test_cancelled_error_reraises(tmp_path: Path):
    """asyncio.CancelledError should be re-raised, not swallowed.

    **Validates: Requirements 1.9**
    """
    svc = _make_service(tmp_path)
    ha = _mock_ha_client()
    discovery = _mock_discovery()
    engine = _mock_engine()

    async def _fake_iter(*args, **kwargs):
        raise asyncio.CancelledError()
        yield  # noqa: RET503

    ha.iter_state_changes = _fake_iter

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            svc._task_ws_listener(ha, discovery, engine),
            timeout=5.0,
        )


# ---------------------------------------------------------------------------
# (e) aiohttp.ClientConnectorError → reconnect
# ---------------------------------------------------------------------------


@pytest.mark.skipif(aiohttp is None, reason="aiohttp not installed")
async def test_client_connector_error_reconnects(tmp_path: Path):
    """aiohttp.ClientConnectorError should trigger reconnect, not stop.

    **Validates: Requirements 1.9**
    """
    svc = _make_service(tmp_path)
    ha = _mock_ha_client()
    discovery = _mock_discovery()
    engine = _mock_engine()

    call_count = 0

    async def _fake_iter(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Simulate a connection error (falls into bare Exception handler)
            raise aiohttp.ClientConnectorError(
                connection_key=MagicMock(),
                os_error=OSError("Connection refused"),
            )
        svc.stop_event.set()
        return
        yield  # noqa: RET503

    ha.iter_state_changes = _fake_iter

    await asyncio.wait_for(
        svc._task_ws_listener(ha, discovery, engine),
        timeout=5.0,
    )

    assert call_count == 2
    # stop_event was set by us, not by the error handler
    assert svc.stop_event.is_set()


# ---------------------------------------------------------------------------
# (f) Auth success resets counter to 0
# ---------------------------------------------------------------------------


async def test_auth_success_resets_counter(tmp_path: Path):
    """A successful state event after auth errors should reset the counter.

    **Validates: Requirements 1.9**
    """
    from src.ha_api_client import HAAuthError, StateChangeEvent, HAEntity

    svc = _make_service(tmp_path)
    ha = _mock_ha_client()
    discovery = _mock_discovery()
    engine = _mock_engine()

    call_count = 0

    async def _fake_iter(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 5:
            # First 5 calls: auth errors (below threshold of 10)
            raise HAAuthError("HA WS auth failed: 401")
        if call_count == 6:
            # 6th call: yield a successful event, then return
            event = StateChangeEvent(
                entity_id="sensor.test",
                new_state=HAEntity(entity_id="sensor.test", state="20.5"),
                old_state=None,
            )
            yield event
            return
        if call_count <= 11:
            # After reset, another 5 auth errors (still below threshold)
            raise HAAuthError("HA WS auth failed: 401")
        # Final: stop
        svc.stop_event.set()
        return
        yield  # noqa: RET503

    ha.iter_state_changes = _fake_iter

    # Patch random.uniform to return a large negative so backoff timeout ≈ 0
    with patch("scripts.run_ha_smart_service.random.uniform", return_value=-1000.0):
        await asyncio.wait_for(
            svc._task_ws_listener(ha, discovery, engine),
            timeout=10.0,
        )

    # The service should NOT have stopped due to auth failures because
    # the successful event in call 6 reset the counter. Total auth errors
    # were 5 + 5 = 10, but never 10 *consecutive*.
    assert call_count == 12
