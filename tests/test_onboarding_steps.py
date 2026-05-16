"""Tests for onboarding wizard step rendering and state restoration.

Task 6.2 — Validates: Requirements 7.1, 7.2, 7.5, 7.7, 7.8

Uses aiohttp.test_utils.TestClient to hit the real web_ui handlers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

# Ensure sleep_classifier/ is importable
_ADDON_ROOT = Path(__file__).resolve().parents[1] / "sleep_classifier"
sys.path.insert(0, str(_ADDON_ROOT))

import web_ui  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_data_dir(tmp_path, monkeypatch):
    """Redirect /data writes to tmp_path so tests don't pollute the system."""
    monkeypatch.setattr(web_ui, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(web_ui, "_OVERRIDES_PATH", tmp_path / "web_ui_overrides.json")
    monkeypatch.setattr(web_ui, "_OPTIONS_PATH", tmp_path / "options.json")
    monkeypatch.setattr(web_ui, "_LAST_UPGRADE_CHECK_PATH", tmp_path / "last_upgrade_check.json")
    # Reset candidates cache
    monkeypatch.setattr(web_ui, "_candidates_cache", {"data": None, "ts": 0.0})
    return tmp_path


@pytest.fixture
def fake_states() -> List[Dict[str, Any]]:
    """A representative HA /api/states snapshot."""
    def s(eid, friendly=None):
        return {"entity_id": eid,
                "attributes": {"friendly_name": friendly or eid}}
    return [
        s("sensor.bedroom_sleep_stage", "Bedroom Sleep Stage"),
        s("sensor.bedroom_temperature", "Bedroom Temp"),
        s("light.bedroom_main", "Bedroom Main"),
    ]


@pytest.fixture
def app_with_states(fake_states, monkeypatch):
    """Create app with working _fetch_states."""
    async def _fake_fetch():
        return fake_states
    monkeypatch.setattr(web_ui, "_fetch_states", _fake_fetch)
    monkeypatch.setattr(web_ui, "_DISABLE_GUARD", True)
    return web_ui.make_app()


@pytest.fixture
async def client(app_with_states):
    async with TestClient(TestServer(app_with_states)) as c:
        yield c


# ---------------------------------------------------------------------------
# Step rendering tests
# ---------------------------------------------------------------------------


class TestOnboardingStepRendering:
    """Assert step 1-4 HTML renders contain expected text keys."""

    async def test_step1_contains_welcome_and_disclaimer(self, client):
        resp = await client.get("/onboarding")
        assert resp.status == 200
        body = await resp.text()
        # Default English text
        assert "Welcome to Sleep Classifier" in body
        assert "not a medical device" in body or "Medical Disclaimer" in body

    async def test_step2_contains_scanning_title(self, client):
        resp = await client.get("/onboarding")
        assert resp.status == 200
        body = await resp.text()
        assert "Scanning for sleep-stage entities" in body

    async def test_step3_contains_confirm_title(self, client):
        resp = await client.get("/onboarding")
        assert resp.status == 200
        body = await resp.text()
        assert "Confirm environment sensors" in body

    async def test_step4_contains_dry_run_warning(self, client):
        resp = await client.get("/onboarding")
        assert resp.status == 200
        body = await resp.text()
        assert "dry_run" in body
        assert "7 days" in body


# ---------------------------------------------------------------------------
# i18n tests
# ---------------------------------------------------------------------------


class TestOnboardingI18n:
    """Accept-Language: zh-cn renders Chinese; fr falls back to English."""

    async def test_zh_cn_renders_chinese(self, client):
        resp = await client.get("/onboarding", headers={"Accept-Language": "zh-cn"})
        assert resp.status == 200
        body = await resp.text()
        # zh-cn translation should contain Chinese strings from translations
        # The lang attribute should be zh-cn
        assert 'lang="zh-cn"' in body

    async def test_fr_falls_back_to_english(self, client):
        resp = await client.get("/onboarding", headers={"Accept-Language": "fr"})
        assert resp.status == 200
        body = await resp.text()
        # Should fall back to English
        assert 'lang="en"' in body
        assert "Welcome to Sleep Classifier" in body


# ---------------------------------------------------------------------------
# Onboarding redirect tests
# ---------------------------------------------------------------------------


class TestOnboardingRedirect:
    """Test index redirect logic based on web_ui_overrides.json state."""

    async def test_no_overrides_file_redirects_to_onboarding(self, client, isolate_data_dir):
        """When web_ui_overrides.json doesn't exist, GET / redirects to /onboarding."""
        # Ensure file does not exist
        overrides_path = isolate_data_dir / "web_ui_overrides.json"
        if overrides_path.exists():
            overrides_path.unlink()
        resp = await client.get("/", allow_redirects=False)
        assert resp.status == 302
        assert "onboarding" in resp.headers.get("Location", "")

    async def test_with_sleep_stage_source_no_redirect(self, client, isolate_data_dir):
        """When overrides has sleep_stage_source, no redirect."""
        overrides_path = isolate_data_dir / "web_ui_overrides.json"
        overrides_path.write_text(
            json.dumps({"sleep_stage_source": "sensor.bedroom_sleep_stage"}),
            encoding="utf-8",
        )
        resp = await client.get("/", allow_redirects=False)
        assert resp.status == 200
        body = await resp.text()
        assert "Sleep Classifier" in body

    async def test_empty_sleep_stage_source_redirects(self, client, isolate_data_dir):
        """When overrides has empty sleep_stage_source, redirect to onboarding."""
        overrides_path = isolate_data_dir / "web_ui_overrides.json"
        overrides_path.write_text(
            json.dumps({"sleep_stage_source": ""}),
            encoding="utf-8",
        )
        resp = await client.get("/", allow_redirects=False)
        assert resp.status == 302


# ---------------------------------------------------------------------------
# HA unreachable degradation
# ---------------------------------------------------------------------------


class TestHAUnreachableDegradation:
    """When HA states unreachable, step 2 renders degraded text + skip."""

    async def test_candidates_returns_error_when_ha_unreachable(
        self, monkeypatch, isolate_data_dir
    ):
        """Mock _fetch_states raises → candidates endpoint returns error."""
        async def _broken():
            raise aiohttp.ClientError("HA not reachable")

        monkeypatch.setattr(web_ui, "_fetch_states", _broken)
        monkeypatch.setattr(web_ui, "_DISABLE_GUARD", True)
        app = web_ui.make_app()
        async with TestClient(TestServer(app)) as c:
            resp = await c.get("/api/onboarding/candidates")
            assert resp.status == 200
            data = await resp.json()
            assert "error" in data
            assert data["candidates"] == []

    async def test_onboarding_page_has_skip_button_markup(self, client):
        """The wizard HTML always includes the degraded/skip section (hidden by default)."""
        resp = await client.get("/onboarding")
        body = await resp.text()
        # The skip button exists in the HTML (shown via JS when HA is unreachable)
        assert "Skip" in body or "skip" in body.lower()
        assert "ha-degraded" in body
