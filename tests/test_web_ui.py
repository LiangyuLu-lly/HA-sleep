"""Tests for the add-on's embedded entity-picker web UI.

These tests run the aiohttp app under ``aiohttp.test_utils`` so we can
hit the real handlers without a supervisor.  ``_fetch_states`` is
monkeypatched to a fixed in-memory snapshot so we don't need a live HA.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import List, Dict, Any

import pytest

# The module lives outside the normal src/ tree (it's bundled into the
# add-on image), so we add its directory to sys.path for the import.
_ADDON_ROOT = Path(__file__).resolve().parents[1] / "sleep_classifier"
sys.path.insert(0, str(_ADDON_ROOT))

import web_ui    # noqa: E402  (import-after-sys-path is the point)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_data_dir(tmp_path, monkeypatch):
    """Redirect /data writes to a tmp_path so tests don't pollute the system."""
    monkeypatch.setattr(web_ui, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(web_ui, "_OVERRIDES_PATH", tmp_path / "web_ui_overrides.json")
    monkeypatch.setattr(web_ui, "_OPTIONS_PATH", tmp_path / "options.json")
    return tmp_path


@pytest.fixture
def fake_states() -> List[Dict[str, Any]]:
    """A representative HA /api/states snapshot covering all the domains."""
    def s(eid, friendly=None):
        return {"entity_id": eid,
                "attributes": {"friendly_name": friendly or eid}}
    return [
        # v1.3.0: a single sleep-stage entity replaces the legacy
        # heart-rate / movement / breathing slots.  We still expose the
        # old fake entities so existing assertions about domain filtering
        # keep working.
        s("sensor.bedroom_sleep_stage",   "Bedroom Sleep Stage"),
        s("input_select.bedroom_phase",  "Bedroom Phase"),
        s("sensor.bedroom_heart_rate",    "Bedroom Heart Rate"),
        s("sensor.bedroom_movement",      "Bedroom Movement"),
        s("sensor.bedroom_breathing",     "Bedroom Breathing"),
        s("sensor.bedroom_temperature",   "Bedroom Temp"),
        s("sensor.bedroom_humidity",      "Bedroom Humidity"),
        s("sensor.bedroom_illuminance",   "Bedroom Lux"),
        s("binary_sensor.bedroom_motion", "Bedroom Motion"),
        s("light.bedroom_main",           "Bedroom Main"),
        s("light.bedside_lamp",           "Bedside Lamp"),
        s("switch.bedroom_socket",        "Bedroom Socket"),
        s("climate.bedroom_ac"),
        s("fan.bedroom_fan"),
        s("humidifier.bedroom_humidifier"),
        s("media_player.bedroom_speaker"),
        s("input_number.sleep_rating"),
        s("weather.home"),                # should be filtered out
        s("sun.sun"),                     # should be filtered out
    ]


@pytest.fixture
def app(fake_states, monkeypatch):
    async def _fake_fetch():
        return fake_states
    monkeypatch.setattr(web_ui, "_fetch_states", _fake_fetch)
    # Disable the ingress IP guard for handler-level tests — these tests
    # exercise route logic, not the middleware.  The middleware has its own
    # dedicated test file (test_web_ui_ip_guard.py).
    monkeypatch.setattr(web_ui, "_DISABLE_GUARD", True)
    return web_ui.make_app()


@pytest.fixture
async def client(app):
    """Hand-rolled aiohttp TestClient — avoids the pytest-aiohttp dep."""
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(app)) as c:
        yield c


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------


class TestIndex:
    async def test_returns_html_page(self, client, isolate_data_dir) -> None:
        # v2.1.0: index now redirects to /onboarding when overrides missing.
        # Write a minimal overrides to get the picker page.
        (isolate_data_dir / "web_ui_overrides.json").write_text(
            json.dumps({"sleep_stage_source": "sensor.bedroom_sleep_stage"}),
            encoding="utf-8",
        )
        resp = await client.get("/")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/html")
        body = await resp.text()
        assert "Sleep Classifier" in body
        assert "api/entities" in body   # JS fetches this endpoint


# ---------------------------------------------------------------------------
# GET /api/entities
# ---------------------------------------------------------------------------


class TestApiEntities:
    async def test_filters_per_slot_domain(self, client) -> None:
        resp = await client.get("/api/entities")
        assert resp.status == 200
        data = await resp.json()
        slots = data["slots"]

        # Sleep-stage slot accepts sensor + binary_sensor + input_select.
        # weather + sun are excluded because they're not in our domain set.
        stage = slots["sleep_stage_source"]
        eids = {c["entity_id"] for c in stage["candidates"]}
        assert "sensor.bedroom_sleep_stage" in eids
        assert "input_select.bedroom_phase" in eids
        assert "binary_sensor.bedroom_motion" in eids
        assert "weather.home" not in eids
        assert "sun.sun" not in eids
        assert stage["multi"] is False

        # Light targets are multi-select and only see light.*
        lt = slots["light_targets"]
        assert lt["multi"] is True
        assert {c["entity_id"] for c in lt["candidates"]} == {
            "light.bedroom_main", "light.bedside_lamp",
        }

    async def test_temperature_slot_excludes_binary_sensors(
        self, client,
    ) -> None:
        resp = await client.get("/api/entities")
        data = await resp.json()
        ts = data["slots"]["temperature_source"]
        # temperature_source has domains={"sensor"} → no binary_sensor
        for c in ts["candidates"]:
            assert c["entity_id"].startswith("sensor.")

    async def test_returns_existing_overrides_in_current(
        self, client, isolate_data_dir,
    ) -> None:
        (isolate_data_dir / "web_ui_overrides.json").write_text(
            json.dumps({"sleep_stage_source": "sensor.bedroom_sleep_stage"}),
            encoding="utf-8",
        )
        resp = await client.get("/api/entities")
        data = await resp.json()
        assert data["current"]["sleep_stage_source"] == "sensor.bedroom_sleep_stage"

    async def test_502_when_ha_unreachable(self, monkeypatch) -> None:
        import aiohttp
        from aiohttp.test_utils import TestClient, TestServer
        async def _broken():
            raise aiohttp.ClientConnectionError("no route")
        monkeypatch.setattr(web_ui, "_fetch_states", _broken)
        monkeypatch.setattr(web_ui, "_DISABLE_GUARD", True)
        app = web_ui.make_app()
        async with TestClient(TestServer(app)) as c:
            resp = await c.get("/api/entities")
            assert resp.status == 502
            body = await resp.json()
            assert "error" in body


# ---------------------------------------------------------------------------
# POST /api/options — the main "save" path
# ---------------------------------------------------------------------------


class TestApiSave:
    async def test_save_writes_overrides_atomically(
        self, client, isolate_data_dir,
    ) -> None:
        body = {
            "sleep_stage_source": "sensor.bedroom_sleep_stage",
            "light_targets":      ["light.bedroom_main", "light.bedside_lamp"],
        }
        resp = await client.post("/api/options", json=body)
        assert resp.status == 200
        data = await resp.json()
        assert data["rejected"] == []
        assert data["saved"]["sleep_stage_source"] == "sensor.bedroom_sleep_stage"
        assert data["saved"]["light_targets"] == [
            "light.bedroom_main", "light.bedside_lamp",
        ]

        # File on disk matches.
        on_disk = json.loads(
            (isolate_data_dir / "web_ui_overrides.json").read_text(encoding="utf-8"),
        )
        assert on_disk == data["saved"]

    async def test_unknown_entity_is_rejected_not_saved(
        self, client, isolate_data_dir,
    ) -> None:
        body = {"sleep_stage_source": "sensor.nonexistent"}
        resp = await client.post("/api/options", json=body)
        assert resp.status == 200
        data = await resp.json()
        # The bad pick is logged in 'rejected' and NOT written through.
        assert any("nonexistent" in r for r in data["rejected"])
        assert "sleep_stage_source" not in data["saved"]

    async def test_literal_double_quote_normalised_to_empty(
        self, client, isolate_data_dir,
    ) -> None:
        # User clicks "blank" option whose value renders as ``""``;
        # we should treat this as empty-string, not as an entity_id.
        body = {"sleep_stage_source": '""'}
        resp = await client.post("/api/options", json=body)
        data = await resp.json()
        assert data["saved"]["sleep_stage_source"] == ""
        assert data["rejected"] == []

    async def test_multi_select_drops_unknowns_keeps_valid(
        self, client, isolate_data_dir,
    ) -> None:
        body = {"light_targets": [
            "light.bedroom_main",   # valid
            "light.does_not_exist", # invalid
            "",                      # blank
        ]}
        resp = await client.post("/api/options", json=body)
        data = await resp.json()
        assert data["saved"]["light_targets"] == ["light.bedroom_main"]
        assert any("does_not_exist" in r for r in data["rejected"])

    async def test_invalid_json_returns_400(self, client) -> None:
        resp = await client.post("/api/options", data="not-json")
        assert resp.status == 400


# ---------------------------------------------------------------------------
# _normalise unit tests
# ---------------------------------------------------------------------------


class TestNormalise:
    def test_strips_literal_double_quote(self) -> None:
        assert web_ui._normalise('""') == ""
        assert web_ui._normalise("''") == ""

    def test_strips_whitespace(self) -> None:
        assert web_ui._normalise("  sensor.foo  ") == "sensor.foo"

    def test_lists_dropped_blanks(self) -> None:
        assert web_ui._normalise(['""', "light.x", "  ", "light.y"]) == [
            "light.x", "light.y",
        ]

    def test_passthrough_for_non_strings(self) -> None:
        assert web_ui._normalise(42) == 42
        assert web_ui._normalise(None) is None
