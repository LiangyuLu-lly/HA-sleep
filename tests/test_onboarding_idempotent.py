"""Tests for onboarding wizard completion idempotency (Property 7).

Task 6.3 — Validates: Requirements 7.8
**Property 7: Onboarding wizard 完成幂等**

Parametrize user completing wizard N∈[1,10] times with different
sleep_stage_source each time; assert final web_ui_overrides.json
matches only the last submission.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from aiohttp.test_utils import TestClient, TestServer

_ADDON_ROOT = Path(__file__).resolve().parents[1] / "sleep_classifier"
sys.path.insert(0, str(_ADDON_ROOT))

import web_ui  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(web_ui, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(web_ui, "_OVERRIDES_PATH", tmp_path / "web_ui_overrides.json")
    monkeypatch.setattr(web_ui, "_OPTIONS_PATH", tmp_path / "options.json")
    monkeypatch.setattr(web_ui, "_LAST_UPGRADE_CHECK_PATH", tmp_path / "last_upgrade_check.json")
    monkeypatch.setattr(web_ui, "_candidates_cache", {"data": None, "ts": 0.0})
    return tmp_path


@pytest.fixture
def app(monkeypatch):
    async def _fake_fetch():
        return []
    monkeypatch.setattr(web_ui, "_fetch_states", _fake_fetch)
    monkeypatch.setattr(web_ui, "_DISABLE_GUARD", True)
    return web_ui.make_app()


@pytest.fixture
async def client(app):
    async with TestClient(TestServer(app)) as c:
        yield c


# ---------------------------------------------------------------------------
# Property 7: Idempotent wizard completion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n_completions", [1, 2, 3, 5, 7, 10])
async def test_wizard_completion_idempotent(
    client, isolate_data_dir, n_completions: int
):
    """Completing wizard N times: final overrides == last submission only.

    **Validates: Requirements 7.8**
    """
    sources = [f"sensor.sleep_stage_{i}" for i in range(n_completions)]

    for source in sources:
        resp = await client.post(
            "/api/onboarding/save",
            json={"sleep_stage_source": source},
        )
        assert resp.status == 200

    # Read persisted file
    overrides_path = isolate_data_dir / "web_ui_overrides.json"
    assert overrides_path.exists()
    data = json.loads(overrides_path.read_text(encoding="utf-8"))

    # Final value matches ONLY the last submission
    assert data["sleep_stage_source"] == sources[-1]
    assert data["onboarding_skipped"] is True


@pytest.mark.parametrize("n_completions", [2, 5, 10])
async def test_wizard_different_slots_each_time(
    client, isolate_data_dir, n_completions: int
):
    """Different slot combinations each time; only last one persists."""
    last_payload = None
    for i in range(n_completions):
        payload = {
            "sleep_stage_source": f"sensor.stage_{i}",
            "temperature_source": f"sensor.temp_{i}",
        }
        resp = await client.post("/api/onboarding/save", json=payload)
        assert resp.status == 200
        last_payload = payload

    data = json.loads(
        (isolate_data_dir / "web_ui_overrides.json").read_text(encoding="utf-8")
    )
    assert data["sleep_stage_source"] == last_payload["sleep_stage_source"]
    assert data["temperature_source"] == last_payload["temperature_source"]
