"""探索测试 — Bug 1.8: 缺 Ingress IP 白名单 middleware。

本测试验证 ``sleep_classifier/web_ui.py`` 的 ``make_app()`` 是否注册了
IP 过滤中间件 (``ingress_ip_guard``)。当前代码未实现该 middleware，因此
任意 IP 均可访问 Web UI，违反 HA Add-on 安全规范。

**预期行为**：测试在未修复代码上 FAIL，证明 bug 真实存在。
修复后（Task 11 加入 middleware）测试应翻绿。

Validates: Requirements 1.8
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


_WEB_UI_PATH = Path(__file__).resolve().parent.parent / "sleep_classifier" / "web_ui.py"

# Ensure the sleep_classifier directory is importable
_ADDON_ROOT = Path(__file__).resolve().parents[1] / "sleep_classifier"
if str(_ADDON_ROOT) not in sys.path:
    sys.path.insert(0, str(_ADDON_ROOT))

import web_ui  # noqa: E402


def test_non_supervisor_ip_gets_200_without_guard() -> None:
    """Assert that make_app() registers an IP-filtering middleware.

    We read the source of web_ui.py and check for the presence of
    ``ingress_ip_guard`` middleware registration. The absence of such
    middleware means any IP can reach the Web UI — which is the bug.

    This test FAILS on unfixed code (no middleware present) and PASSES
    once the fix (Task 11) is applied.
    """
    source = _WEB_UI_PATH.read_text(encoding="utf-8")

    # Check 1: The source must define an ingress_ip_guard middleware function
    assert re.search(r"def\s+ingress_ip_guard", source), (
        "web_ui.py does not define an 'ingress_ip_guard' middleware function. "
        "Bug 1.8: any IP can access the Web UI without restriction."
    )

    # Check 2: make_app() must register the middleware
    assert re.search(r"middlewares\s*=\s*\[.*ingress_ip_guard", source, re.DOTALL), (
        "web_ui.py make_app() does not register 'ingress_ip_guard' in middlewares. "
        "Bug 1.8: the middleware exists but is not wired into the application."
    )

    # Check 3: The middleware must check request.remote
    assert re.search(r"request\.remote", source), (
        "web_ui.py does not inspect 'request.remote' for IP filtering. "
        "Bug 1.8: no source-IP validation is performed on incoming requests."
    )


# ---------------------------------------------------------------------------
# Functional tests for ingress_ip_guard middleware
# ---------------------------------------------------------------------------


def _make_request(remote_ip: str) -> MagicMock:
    """Create a mock aiohttp request with a given remote IP."""
    request = MagicMock(spec=web.Request)
    request.remote = remote_ip
    return request


async def _ok_handler(request: web.Request) -> web.Response:
    """Simple handler that returns 200 OK."""
    return web.Response(status=200, text="OK")


class TestIngressIpGuardFunctional:
    """Functional tests for the ingress_ip_guard middleware."""

    async def test_supervisor_ip_passes_through(self, monkeypatch) -> None:
        """(a) 172.30.32.2 passes through (200)."""
        monkeypatch.setattr(web_ui, "_ALLOWED_IPS", {"172.30.32.2"})
        monkeypatch.setattr(web_ui, "_DISABLE_GUARD", False)

        request = _make_request("172.30.32.2")
        resp = await web_ui.ingress_ip_guard(request, _ok_handler)
        assert resp.status == 200

    async def test_ipv6_mapped_supervisor_ip_passes_through(self, monkeypatch) -> None:
        """(b) ::ffff:172.30.32.2 passes through (200)."""
        monkeypatch.setattr(web_ui, "_ALLOWED_IPS", {"172.30.32.2"})
        monkeypatch.setattr(web_ui, "_DISABLE_GUARD", False)

        request = _make_request("::ffff:172.30.32.2")
        resp = await web_ui.ingress_ip_guard(request, _ok_handler)
        assert resp.status == 200

    async def test_non_supervisor_ip_gets_403(self, monkeypatch) -> None:
        """(c) 10.0.0.1 → 403."""
        monkeypatch.setattr(web_ui, "_ALLOWED_IPS", {"172.30.32.2"})
        monkeypatch.setattr(web_ui, "_DISABLE_GUARD", False)

        request = _make_request("10.0.0.1")
        resp = await web_ui.ingress_ip_guard(request, _ok_handler)
        assert resp.status == 403

    async def test_localhost_gets_403(self, monkeypatch) -> None:
        """(d) 127.0.0.1 → 403."""
        monkeypatch.setattr(web_ui, "_ALLOWED_IPS", {"172.30.32.2"})
        monkeypatch.setattr(web_ui, "_DISABLE_GUARD", False)

        request = _make_request("127.0.0.1")
        resp = await web_ui.ingress_ip_guard(request, _ok_handler)
        assert resp.status == 403

    async def test_disable_guard_allows_any_ip(self, monkeypatch) -> None:
        """(e) WEB_UI_DISABLE_INGRESS_GUARD=1 → any IP passes."""
        monkeypatch.setattr(web_ui, "_ALLOWED_IPS", {"172.30.32.2"})
        monkeypatch.setattr(web_ui, "_DISABLE_GUARD", True)

        request = _make_request("10.0.0.1")
        resp = await web_ui.ingress_ip_guard(request, _ok_handler)
        assert resp.status == 200

    async def test_custom_whitelist_overrides_default(self, monkeypatch) -> None:
        """(f) SUPERVISOR_IP_WHITELIST="fd00::2,127.0.0.1" overrides default."""
        custom_ips = web_ui._parse_allowed_ips("fd00::2,127.0.0.1")
        monkeypatch.setattr(web_ui, "_ALLOWED_IPS", custom_ips)
        monkeypatch.setattr(web_ui, "_DISABLE_GUARD", False)

        # fd00::2 should pass
        request = _make_request("fd00::2")
        resp = await web_ui.ingress_ip_guard(request, _ok_handler)
        assert resp.status == 200

        # 127.0.0.1 should pass (it's in the custom whitelist)
        request = _make_request("127.0.0.1")
        resp = await web_ui.ingress_ip_guard(request, _ok_handler)
        assert resp.status == 200

        # 172.30.32.2 should be rejected (not in custom whitelist)
        request = _make_request("172.30.32.2")
        resp = await web_ui.ingress_ip_guard(request, _ok_handler)
        assert resp.status == 403
