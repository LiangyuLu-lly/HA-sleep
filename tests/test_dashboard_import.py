"""Tests for Lovelace dashboard importer — 4 combinations + rollback.

Task 6.5 — Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, P8.2

Mock HAAPIClient, parametrize (existing, confirm_overwrite) ∈ {True, False}²:
- (False, *) → 201 + lovelace_create_dashboard called
- (True, False) → 409 + save_config NOT called
- (True, True) → 201 + save_config called
- Half-success (save_config raises) → 502 + delete called for rollback
- Success response link is relative path (not starting with /lovelace)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

_ADDON_ROOT = Path(__file__).resolve().parents[1] / "sleep_classifier"
sys.path.insert(0, str(_ADDON_ROOT))

import web_ui  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(web_ui, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(web_ui, "_OVERRIDES_PATH", tmp_path / "web_ui_overrides.json")
    monkeypatch.setattr(web_ui, "_OPTIONS_PATH", tmp_path / "options.json")
    monkeypatch.setattr(web_ui, "_LAST_UPGRADE_CHECK_PATH", tmp_path / "last_upgrade_check.json")
    monkeypatch.setattr(web_ui, "_candidates_cache", {"data": None, "ts": 0.0})
    # Write overrides so index doesn't redirect to onboarding
    (tmp_path / "web_ui_overrides.json").write_text(
        json.dumps({"sleep_stage_source": "sensor.test"}), encoding="utf-8"
    )
    return tmp_path


class MockHAClient:
    """Mock HAAPIClient with controllable lovelace methods."""

    def __init__(self, *, existing_dashboards: List[Dict[str, Any]] = None,
                 save_config_error: Exception | None = None):
        self._existing = existing_dashboards or []
        self._save_config_error = save_config_error
        self.lovelace_create_dashboard = AsyncMock(return_value={"url_path": "sleep-classifier"})
        self.lovelace_save_config = AsyncMock(side_effect=save_config_error)
        self._ws_request = AsyncMock()  # for delete rollback

    async def lovelace_dashboards(self) -> List[Dict[str, Any]]:
        return self._existing


def _make_app_with_client(mock_client, monkeypatch):
    monkeypatch.setattr(web_ui, "_DISABLE_GUARD", True)

    async def _fake_fetch():
        return []
    monkeypatch.setattr(web_ui, "_fetch_states", _fake_fetch)
    return web_ui.make_app(ha_client=mock_client)


# ---------------------------------------------------------------------------
# Test: (False, *) → 201 + lovelace_create_dashboard called
# ---------------------------------------------------------------------------


class TestDashboardNotExisting:
    """When dashboard doesn't exist, import creates it."""

    @pytest.mark.parametrize("confirm_overwrite", [True, False])
    async def test_creates_dashboard_when_not_existing(
        self, monkeypatch, isolate_data_dir, confirm_overwrite
    ):
        mock_client = MockHAClient(existing_dashboards=[])
        app = _make_app_with_client(mock_client, monkeypatch)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/api/dashboard/import",
                json={"confirm_overwrite": confirm_overwrite},
            )
            assert resp.status == 201
            data = await resp.json()
            assert data["created"] is True
            mock_client.lovelace_create_dashboard.assert_called_once()
            mock_client.lovelace_save_config.assert_called_once()


# ---------------------------------------------------------------------------
# Test: (True, False) → 409 + save_config NOT called
# ---------------------------------------------------------------------------


class TestDashboardExistingNoOverwrite:
    """When existing and not confirmed, return 409."""

    async def test_409_when_existing_not_confirmed(
        self, monkeypatch, isolate_data_dir
    ):
        existing = [{"url_path": "sleep-classifier", "title": "Old"}]
        mock_client = MockHAClient(existing_dashboards=existing)
        app = _make_app_with_client(mock_client, monkeypatch)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/api/dashboard/import",
                json={"confirm_overwrite": False},
            )
            assert resp.status == 409
            data = await resp.json()
            assert data["existing"] is True
            # save_config must NOT be called (P8.2)
            mock_client.lovelace_save_config.assert_not_called()


# ---------------------------------------------------------------------------
# Test: (True, True) → 201 + save_config called
# ---------------------------------------------------------------------------


class TestDashboardExistingWithOverwrite:
    """When existing and confirmed, overwrite succeeds."""

    async def test_201_when_existing_and_confirmed(
        self, monkeypatch, isolate_data_dir
    ):
        existing = [{"url_path": "sleep-classifier", "title": "Old"}]
        mock_client = MockHAClient(existing_dashboards=existing)
        app = _make_app_with_client(mock_client, monkeypatch)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/api/dashboard/import",
                json={"confirm_overwrite": True},
            )
            assert resp.status == 201
            data = await resp.json()
            assert data["created"] is True
            # save_config should be called
            mock_client.lovelace_save_config.assert_called_once()


# ---------------------------------------------------------------------------
# Test: Half-success (save_config raises) → 502 + delete called for rollback
# ---------------------------------------------------------------------------


class TestDashboardRollback:
    """When save_config fails after create, rollback by deleting."""

    async def test_502_and_rollback_on_save_config_failure(
        self, monkeypatch, isolate_data_dir
    ):
        mock_client = MockHAClient(
            existing_dashboards=[],
            save_config_error=RuntimeError("HA rejected config"),
        )
        app = _make_app_with_client(mock_client, monkeypatch)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/api/dashboard/import",
                json={"confirm_overwrite": False},
            )
            assert resp.status == 502
            data = await resp.json()
            assert "error" in data
            assert "failed" in data["error"].lower() or "rejected" in data["error"].lower()
            # Delete should have been called for rollback
            mock_client._ws_request.assert_called_once()
            call_args = mock_client._ws_request.call_args[0][0]
            assert call_args["type"] == "lovelace/dashboards/delete"


# ---------------------------------------------------------------------------
# Test: Success response link is relative path
# ---------------------------------------------------------------------------


class TestDashboardResponseLink:
    """Success response link is relative path (not starting with /lovelace)."""

    async def test_success_link_is_relative(
        self, monkeypatch, isolate_data_dir
    ):
        mock_client = MockHAClient(existing_dashboards=[])
        app = _make_app_with_client(mock_client, monkeypatch)
        async with TestClient(TestServer(app)) as c:
            resp = await c.post(
                "/api/dashboard/import",
                json={"confirm_overwrite": False},
            )
            assert resp.status == 201
            data = await resp.json()
            link = data.get("link", "")
            # Link must NOT start with /lovelace (ingress contract)
            assert not link.startswith("/lovelace")
            # Link should be a relative path
            assert not link.startswith("/") or link.startswith("lovelace-")
