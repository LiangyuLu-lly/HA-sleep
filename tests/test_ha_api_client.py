"""Unit tests for :mod:`src.ha_api_client`.

We don't spin up a real Home Assistant instance; instead we drive the client
with a stub ``aiohttp.ClientSession`` that returns canned JSON responses.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.ha_api_client import (
    HAAPIError,
    HAAuthError,
    HAEntity,
    HomeAssistantClient,
    StateChangeEvent,
)


# ---------------------------------------------------------------------------
# Fake aiohttp session + response builder
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(
        self,
        status: int = 200,
        body: Any = None,
        text: str = "",
    ) -> None:
        self.status = status
        self._body = body
        self._text = text
        self.content_length = len(text) or (1 if body is not None else 0)

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    async def json(self) -> Any:
        return self._body

    async def text(self) -> str:
        return self._text


class _FakeSession:
    def __init__(self) -> None:
        self.closed = False
        self._responses: List[_FakeResponse] = []
        self.requests: List[Dict[str, Any]] = []

    def queue(self, *responses: _FakeResponse) -> None:
        self._responses.extend(responses)

    def request(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        if not self._responses:
            raise AssertionError(f"No fake response queued for {method} {url}")
        return self._responses.pop(0)

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Entity dataclass
# ---------------------------------------------------------------------------


class TestHAEntity:
    def test_from_dict_basic(self):
        raw = {
            "entity_id": "sensor.bedroom_temp",
            "state": "22.5",
            "attributes": {
                "friendly_name": "Bedroom Temp",
                "unit_of_measurement": "°C",
                "device_class": "temperature",
            },
            "last_changed": "2026-05-11T12:00:00Z",
        }
        e = HAEntity.from_dict(raw)
        assert e.entity_id == "sensor.bedroom_temp"
        assert e.domain == "sensor"
        assert e.object_id == "bedroom_temp"
        assert e.friendly_name == "Bedroom Temp"
        assert e.device_class == "temperature"
        assert e.unit_of_measurement == "°C"
        assert e.numeric_state() == 22.5

    def test_numeric_state_returns_none_for_non_numeric(self):
        e = HAEntity(entity_id="x.y", state="unavailable", attributes={})
        assert e.numeric_state() is None

    def test_friendly_name_falls_back_to_entity_id(self):
        e = HAEntity(entity_id="x.y", state="0", attributes={})
        assert e.friendly_name == "x.y"


class TestStateChangeEvent:
    def test_from_event(self):
        ev = {
            "data": {
                "entity_id": "sensor.hr",
                "new_state": {"entity_id": "sensor.hr", "state": "72",
                              "attributes": {}},
                "old_state": {"entity_id": "sensor.hr", "state": "70",
                              "attributes": {}},
            },
            "time_fired": "2026-05-11T12:00:00Z",
        }
        sce = StateChangeEvent.from_event(ev)
        assert sce.entity_id == "sensor.hr"
        assert sce.new_state.state == "72"
        assert sce.old_state.state == "70"


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


class TestClientConstruction:
    def test_empty_token_raises(self):
        with pytest.raises(HAAuthError):
            HomeAssistantClient("http://h.local:8123", "")

    def test_trailing_slash_stripped(self):
        cli = HomeAssistantClient(
            "http://h.local:8123/", "token",
            session=_FakeSession(),  # type: ignore[arg-type]
        )
        assert cli.base_url == "http://h.local:8123"

    def test_ws_url_for_http(self):
        cli = HomeAssistantClient(
            "http://h.local:8123", "tok",
            session=_FakeSession(),  # type: ignore[arg-type]
        )
        assert cli._ws_url == "ws://h.local:8123/api/websocket"

    def test_ws_url_for_https(self):
        cli = HomeAssistantClient(
            "https://h.local:8123", "tok",
            session=_FakeSession(),  # type: ignore[arg-type]
        )
        assert cli._ws_url == "wss://h.local:8123/api/websocket"


# ---------------------------------------------------------------------------
# REST helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def session_and_client():
    session = _FakeSession()
    client = HomeAssistantClient(
        "http://h.local:8123", "tok",
        session=session,  # type: ignore[arg-type]
    )
    return session, client


@pytest.mark.asyncio
class TestREST:
    async def test_ping_returns_true_on_message_payload(self, session_and_client):
        session, client = session_and_client
        session.queue(_FakeResponse(status=200, body={"message": "API running."}))
        assert await client.ping() is True

    async def test_ping_returns_false_on_auth_error(self, session_and_client):
        session, client = session_and_client
        session.queue(_FakeResponse(status=401, text="bad token"))
        # ping wraps HAAuthError and returns False
        assert await client.ping() is False

    async def test_get_states_parses_list(self, session_and_client):
        session, client = session_and_client
        raw = [
            {"entity_id": "light.a", "state": "on", "attributes": {}},
            {"entity_id": "sensor.x", "state": "1", "attributes": {}},
        ]
        session.queue(_FakeResponse(status=200, body=raw))
        states = await client.get_states()
        assert len(states) == 2
        assert isinstance(states[0], HAEntity)
        assert states[0].entity_id == "light.a"

    async def test_get_states_rejects_non_list(self, session_and_client):
        session, client = session_and_client
        session.queue(_FakeResponse(status=200, body={"oops": True}))
        with pytest.raises(HAAPIError):
            await client.get_states()

    async def test_get_state_returns_none_on_404(self, session_and_client):
        session, client = session_and_client
        session.queue(_FakeResponse(status=404, text="Not found"))
        result = await client.get_state("sensor.missing")
        assert result is None

    async def test_call_service_includes_entity_id(self, session_and_client):
        session, client = session_and_client
        session.queue(_FakeResponse(status=200, body=[]))
        await client.call_service(
            "light", "turn_on", entity_id="light.bedroom",
            brightness_pct=50, kelvin=2700,
        )
        sent = session.requests[-1]
        assert sent["method"] == "POST"
        assert "/api/services/light/turn_on" in sent["url"]
        body = sent["json"]
        assert body["entity_id"] == "light.bedroom"
        assert body["brightness_pct"] == 50
        assert body["kelvin"] == 2700

    async def test_auth_header_present(self, session_and_client):
        session, client = session_and_client
        session.queue(_FakeResponse(status=200, body={"message": "API running."}))
        await client.ping()
        sent = session.requests[-1]
        assert sent["headers"]["Authorization"] == "Bearer tok"

    async def test_auth_error_on_401(self, session_and_client):
        session, client = session_and_client
        session.queue(_FakeResponse(status=401, text="reject"))
        with pytest.raises(HAAuthError):
            await client.get_states()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestLifecycle:
    async def test_close_closes_session_when_owned(self):
        client = HomeAssistantClient("http://h.local", "tok")
        # No session injected → client created one on first use; closing should
        # not crash even without ever using it.
        await client.close()

    async def test_close_does_not_close_external_session(self, session_and_client):
        session, client = session_and_client
        await client.close()
        assert session.closed is False  # we own this session, client must not close it
