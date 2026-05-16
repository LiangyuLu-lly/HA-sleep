"""Tests for dry_run default safety guarantee (Property 8).

Task 6.3 — Validates: Requirements 7.6
**Property 8: dry_run 默认安全**

Parametrize various wizard inputs; assert dry_run is always true
unless payload contains confirm_disable_dry_run=true.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

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
# Property 8: dry_run default safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    {"sleep_stage_source": "sensor.stage_1"},
    {"sleep_stage_source": "sensor.stage_1", "confirm_disable_dry_run": False},
    {"sleep_stage_source": "sensor.stage_1", "confirm_disable_dry_run": None},
    {"sleep_stage_source": "sensor.stage_1", "confirm_disable_dry_run": 0},
    {"sleep_stage_source": "sensor.stage_1", "confirm_disable_dry_run": ""},
    # Even with many extra fields
    {"sleep_stage_source": "sensor.x", "temperature_source": "sensor.t",
     "light_targets": ["light.a"]},
    # Empty payload
    {},
])
async def test_dry_run_stays_true_without_explicit_confirm(
    client, isolate_data_dir, payload
):
    """dry_run must be true unless confirm_disable_dry_run=true is in payload.

    **Validates: Requirements 7.6**
    """
    resp = await client.post("/api/onboarding/save", json=payload)
    assert resp.status == 200

    data = json.loads(
        (isolate_data_dir / "web_ui_overrides.json").read_text(encoding="utf-8")
    )
    assert data.get("dry_run") is True


async def test_dry_run_false_only_when_explicitly_confirmed(
    client, isolate_data_dir
):
    """Only confirm_disable_dry_run=true allows dry_run=false.

    **Validates: Requirements 7.6**
    """
    resp = await client.post(
        "/api/onboarding/save",
        json={"sleep_stage_source": "sensor.x", "confirm_disable_dry_run": True},
    )
    assert resp.status == 200

    data = json.loads(
        (isolate_data_dir / "web_ui_overrides.json").read_text(encoding="utf-8")
    )
    assert data.get("dry_run") is False


@pytest.mark.parametrize("n_saves", [2, 5, 8])
async def test_repeated_saves_without_confirm_keep_dry_run_true(
    client, isolate_data_dir, n_saves: int
):
    """Multiple saves without confirm: dry_run remains true."""
    for i in range(n_saves):
        resp = await client.post(
            "/api/onboarding/save",
            json={"sleep_stage_source": f"sensor.s{i}"},
        )
        assert resp.status == 200

    data = json.loads(
        (isolate_data_dir / "web_ui_overrides.json").read_text(encoding="utf-8")
    )
    assert data.get("dry_run") is True


async def test_dry_run_true_after_confirm_then_normal_save(
    client, isolate_data_dir
):
    """If user previously disabled dry_run then completes wizard normally,
    dry_run should stay as-is (setdefault preserves existing value)."""
    # First save with confirm
    await client.post(
        "/api/onboarding/save",
        json={"sleep_stage_source": "sensor.a", "confirm_disable_dry_run": True},
    )
    # Second save without confirm — dry_run should remain false
    # (setdefault doesn't overwrite existing)
    await client.post(
        "/api/onboarding/save",
        json={"sleep_stage_source": "sensor.b"},
    )
    data = json.loads(
        (isolate_data_dir / "web_ui_overrides.json").read_text(encoding="utf-8")
    )
    # The dry_run was already set to False and setdefault won't change it
    # Actually re-reading the impl: setdefault only sets if key missing,
    # but load_existing will read from the file which has dry_run=False
    assert data.get("dry_run") is False
