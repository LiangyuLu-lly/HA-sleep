"""Async Home Assistant client (REST + WebSocket).

This client is the foundation for *deep* HA integration:

* it pulls the **full entity registry** (so we know every light, climate,
  fan, humidifier, ... the user has) via ``GET /api/states``;
* it streams every ``state_changed`` event over the WebSocket API so the
  service reacts to live sensor updates with sub-second latency;
* it issues ``call_service`` requests through REST (simpler than WS for
  fire-and-forget control).

Authentication uses a **Long-Lived Access Token** generated from the user's
HA profile page (``http://homeassistant.local:8123/profile``).  The token is
never logged in clear-text.

References
----------
- HA REST API: https://developers.home-assistant.io/docs/api/rest/
- HA WebSocket API: https://developers.home-assistant.io/docs/api/websocket/
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

try:
    import aiohttp  # type: ignore[import]
except ImportError:  # pragma: no cover - aiohttp is in requirements.txt
    aiohttp = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class HAEntity:
    """A snapshot of a single Home Assistant entity."""

    entity_id: str
    state: str
    attributes: Dict[str, Any] = field(default_factory=dict)
    last_changed: Optional[str] = None
    last_updated: Optional[str] = None

    @property
    def domain(self) -> str:
        """Entity domain, e.g. ``"light"`` for ``light.bedroom``."""
        return self.entity_id.split(".", 1)[0]

    @property
    def object_id(self) -> str:
        """Local part of the entity id (``"bedroom"`` for ``light.bedroom``)."""
        return self.entity_id.split(".", 1)[1] if "." in self.entity_id else self.entity_id

    @property
    def friendly_name(self) -> str:
        return str(self.attributes.get("friendly_name", self.entity_id))

    @property
    def device_class(self) -> Optional[str]:
        return self.attributes.get("device_class")

    @property
    def unit_of_measurement(self) -> Optional[str]:
        return self.attributes.get("unit_of_measurement")

    @property
    def area(self) -> Optional[str]:
        # HA returns ``area_id`` in attributes when the area is exposed.
        return self.attributes.get("area_id") or self.attributes.get("area")

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "HAEntity":
        return cls(
            entity_id=str(raw["entity_id"]),
            state=str(raw.get("state", "")),
            attributes=dict(raw.get("attributes", {})),
            last_changed=raw.get("last_changed"),
            last_updated=raw.get("last_updated"),
        )

    def numeric_state(self) -> Optional[float]:
        """Return ``state`` cast to float, or None if not numeric."""
        try:
            return float(self.state)
        except (TypeError, ValueError):
            return None


@dataclass
class StateChangeEvent:
    """A single ``state_changed`` event carried over the WebSocket."""

    entity_id: str
    new_state: Optional[HAEntity]
    old_state: Optional[HAEntity]
    timestamp: Optional[str] = None

    @classmethod
    def from_event(cls, event: Dict[str, Any]) -> "StateChangeEvent":
        data = event.get("data", {})
        new_raw = data.get("new_state")
        old_raw = data.get("old_state")
        return cls(
            entity_id=str(data.get("entity_id", "")),
            new_state=HAEntity.from_dict(new_raw) if new_raw else None,
            old_state=HAEntity.from_dict(old_raw) if old_raw else None,
            timestamp=event.get("time_fired"),
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class HAAPIError(RuntimeError):
    """Raised when a REST or WebSocket call fails."""


class HAAuthError(HAAPIError):
    """Raised when the access token is missing or rejected by HA."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class HomeAssistantClient:
    """Async REST + WebSocket client for a single Home Assistant instance.

    Designed for use inside an ``asyncio`` event loop::

        async with HomeAssistantClient(base_url, token) as ha:
            entities = await ha.get_states()
            await ha.call_service("light", "turn_on", entity_id="light.bedroom")
            async for ev in ha.iter_state_changes():
                print(ev.entity_id, ev.new_state.state)

    All HTTP calls timeout at ``request_timeout`` seconds (10s by default).
    The WebSocket keeps a ping/pong loop to detect broken broker links.
    """

    REST_PREFIX = "/api"

    def __init__(
        self,
        base_url: str,
        access_token: str,
        *,
        verify_ssl: bool = True,
        request_timeout: float = 10.0,
        session: "Optional[aiohttp.ClientSession]" = None,
    ) -> None:
        if aiohttp is None:
            raise RuntimeError(
                "aiohttp is not installed — `pip install aiohttp>=3.9.0`"
            )
        if not access_token:
            raise HAAuthError("Empty HA access token")

        self.base_url = base_url.rstrip("/")
        self._token = access_token
        self._verify_ssl = verify_ssl
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._session = session
        self._owns_session = session is None
        # WebSocket bookkeeping
        self._ws: "Optional[aiohttp.ClientWebSocketResponse]" = None
        self._ws_msg_id: int = 0
        self._ws_lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def __aenter__(self) -> "HomeAssistantClient":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._owns_session = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the WebSocket (if open) and the underlying HTTP session."""
        if self._ws is not None and not self._ws.closed:
            try:
                await self._ws.close()
            except Exception as exc:  # pragma: no cover
                logger.warning("HA WS close failed: %s", exc)
            finally:
                self._ws = None
        if self._owns_session and self._session is not None and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ #
    # REST helpers                                                       #
    # ------------------------------------------------------------------ #

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _ensure_session(self) -> "aiohttp.ClientSession":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
            self._owns_session = True
        return self._session

    async def _rest(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Issue a REST request and return the parsed JSON body."""
        url = f"{self.base_url}{path}"
        session = await self._ensure_session()
        try:
            async with session.request(
                method,
                url,
                headers=self._auth_headers(),
                json=json_body,
                ssl=self._verify_ssl,
            ) as resp:
                if resp.status == 401:
                    raise HAAuthError("HA rejected the access token (HTTP 401)")
                if resp.status >= 400:
                    body = await resp.text()
                    raise HAAPIError(
                        f"HA REST {method} {path} failed: {resp.status} {body[:200]}"
                    )
                if resp.status == 200 and resp.content_length:
                    return await resp.json()
                return None
        except aiohttp.ClientError as exc:
            raise HAAPIError(f"HA REST request to {url} failed: {exc}") from exc

    async def ping(self) -> bool:
        """Verify the token / connectivity.  Returns True when HA replies 200."""
        try:
            body = await self._rest("GET", f"{self.REST_PREFIX}/")
            return isinstance(body, dict) and "message" in body
        except HAAPIError as exc:
            logger.error("HA ping failed: %s", exc)
            return False

    async def get_states(self) -> List[HAEntity]:
        """List every entity HA exposes."""
        raw = await self._rest("GET", f"{self.REST_PREFIX}/states")
        if not isinstance(raw, list):
            raise HAAPIError("Unexpected /api/states payload (not a list)")
        return [HAEntity.from_dict(item) for item in raw]

    async def get_state(self, entity_id: str) -> Optional[HAEntity]:
        """Return one entity's current state, or None if HA says 404."""
        try:
            raw = await self._rest("GET", f"{self.REST_PREFIX}/states/{entity_id}")
        except HAAPIError as exc:
            if "404" in str(exc):
                return None
            raise
        if not raw:
            return None
        return HAEntity.from_dict(raw)

    async def call_service(
        self,
        domain: str,
        service: str,
        *,
        entity_id: Optional[str] = None,
        **service_data: Any,
    ) -> Any:
        """Call a Home Assistant service.

        Examples::

            await ha.call_service("light", "turn_on",
                                  entity_id="light.bedroom",
                                  brightness_pct=10, kelvin=2200)
            await ha.call_service("climate", "set_temperature",
                                  entity_id="climate.bedroom_ac",
                                  temperature=19, hvac_mode="cool")
        """
        body: Dict[str, Any] = dict(service_data)
        if entity_id is not None:
            body["entity_id"] = entity_id
        path = f"{self.REST_PREFIX}/services/{domain}/{service}"
        return await self._rest("POST", path, json_body=body)

    async def update_state(
        self,
        entity_id: str,
        state: Any,
        *,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> HAEntity:
        """Publish or update an entity's state directly via the REST API.

        Unlike :meth:`call_service`, this does not invoke a domain service —
        it writes a "virtual" entity that lives only in HA's state machine.
        The add-on uses this to expose its own diagnostics (sleep stage,
        confidence, session duration, …) so they appear on Lovelace
        dashboards without the user having to set up MQTT discovery.

        HA recognises the following payload schema for ``POST /api/states/<id>``:

            {"state": "DEEP", "attributes": {"friendly_name": "Sleep stage", ...}}

        Returns the HAEntity HA echoes back, which now includes the
        canonical ``last_changed`` / ``last_updated`` timestamps.
        """
        body: Dict[str, Any] = {"state": str(state)}
        if attributes:
            body["attributes"] = dict(attributes)
        path = f"{self.REST_PREFIX}/states/{entity_id}"
        raw = await self._rest("POST", path, json_body=body)
        if not isinstance(raw, dict):
            raise HAAPIError(
                f"Unexpected /api/states/{entity_id} payload (not a dict)"
            )
        return HAEntity.from_dict(raw)

    # ------------------------------------------------------------------ #
    # WebSocket                                                          #
    # ------------------------------------------------------------------ #

    @property
    def _ws_url(self) -> str:
        url = self.base_url
        if url.startswith("http://"):
            return "ws://" + url[len("http://"):] + "/api/websocket"
        if url.startswith("https://"):
            return "wss://" + url[len("https://"):] + "/api/websocket"
        return url + "/api/websocket"

    async def _next_msg_id(self) -> int:
        async with self._ws_lock:
            self._ws_msg_id += 1
            return self._ws_msg_id

    async def connect_websocket(self) -> None:
        """Open the WS, perform auth, and leave the socket ready for events."""
        if self._ws is not None and not self._ws.closed:
            return
        session = await self._ensure_session()
        try:
            self._ws = await session.ws_connect(
                self._ws_url, ssl=self._verify_ssl, heartbeat=30,
            )
        except aiohttp.ClientError as exc:
            raise HAAPIError(f"HA WS connect failed: {exc}") from exc

        # HA protocol: server sends auth_required, we reply auth, expect auth_ok.
        hello = await self._ws.receive_json()
        if hello.get("type") != "auth_required":
            raise HAAPIError(f"Unexpected first WS message: {hello!r}")

        await self._ws.send_json({"type": "auth", "access_token": self._token})
        auth_reply = await self._ws.receive_json()
        if auth_reply.get("type") != "auth_ok":
            await self._ws.close()
            self._ws = None
            raise HAAuthError(
                f"HA WS auth failed: {auth_reply.get('message', auth_reply)}"
            )
        # Reset the per-connection id counter (HA expects monotonically
        # increasing ids *per* WS connection).
        self._ws_msg_id = 0
        logger.info("Connected to HA WebSocket at %s", self._ws_url)

    async def subscribe_state_changes(self) -> int:
        """Tell HA to push every ``state_changed`` event to us.

        Returns the subscription id (useful for ``unsubscribe_events``).
        """
        if self._ws is None or self._ws.closed:
            await self.connect_websocket()
        assert self._ws is not None
        sub_id = await self._next_msg_id()
        await self._ws.send_json(
            {"id": sub_id, "type": "subscribe_events", "event_type": "state_changed"}
        )
        # HA replies with {"id": sub_id, "type": "result", "success": true}.
        result = await self._ws.receive_json()
        if not result.get("success", False):
            raise HAAPIError(f"subscribe_events failed: {result}")
        return sub_id

    async def _ws_request(self, message: Dict[str, Any]) -> Any:
        """Send a JSON command on the WS and await the matching ``result``.

        Helper for **command-style** WS calls (anything that has request /
        response semantics rather than a long-lived event stream — e.g.
        ``lovelace/dashboards/list``).  Re-uses the existing auth handshake
        from :meth:`connect_websocket` and the per-connection id counter
        from :meth:`_next_msg_id`, so callers don't need to know anything
        about HA's framing.

        Behaviour:

        * Auto-(re)connects when ``self._ws`` is ``None`` or closed.
        * Allocates a fresh msg id via ``_next_msg_id`` (under
          ``_ws_lock``) and merges it into the outgoing payload.
        * Reads frames in a loop, skipping anything whose ``id`` does
          not match ours (HA may interleave ``event`` frames from a
          previous ``subscribe_events`` subscription).
        * On ``{"success": false, ...}`` raises :class:`HAAPIError` with
          the HA-reported error code and message.

        :raises HAAuthError: if the (re)connect handshake is rejected.
        :raises HAAPIError: for transport errors and HA-side failures.

        .. note::
           This helper is **not** safe to call while
           :meth:`iter_state_changes` is the active WS consumer — that
           coroutine owns the receive loop and will swallow our result
           frame.  Lovelace command methods are therefore intended to be
           called from web-UI request handlers that do not share the
           same WS consumer with the main event loop, or before the
           ``state_changed`` subscription is started.
        """
        if aiohttp is None:  # pragma: no cover - aiohttp is in requirements.txt
            raise RuntimeError("aiohttp is not installed")
        if self._ws is None or self._ws.closed:
            await self.connect_websocket()
        assert self._ws is not None
        msg_id = await self._next_msg_id()
        payload = {**message, "id": msg_id}
        try:
            await self._ws.send_json(payload)
            while True:
                reply = await self._ws.receive_json()
                if reply.get("id") != msg_id:
                    # Frame for a different request / subscription.  HA
                    # is allowed to interleave; just keep reading.
                    continue
                if reply.get("type") != "result":
                    continue
                if not reply.get("success", False):
                    err = reply.get("error") or {}
                    raise HAAPIError(
                        f"HA WS command {message.get('type')!r} failed: "
                        f"{err.get('code', 'unknown')} "
                        f"{err.get('message', reply)}"
                    )
                return reply.get("result")
        except aiohttp.ClientError as exc:
            raise HAAPIError(
                f"HA WS command {message.get('type')!r} transport error: {exc}"
            ) from exc

    # ------------------------------------------------------------------ #
    # Lovelace WebSocket commands (v2.1.0 onboarding dashboard importer) #
    # ------------------------------------------------------------------ #

    async def lovelace_dashboards(self) -> List[Dict[str, Any]]:
        """List every storage-mode Lovelace dashboard known to HA.

        Sends ``lovelace/dashboards/list`` and returns HA's ``result``
        array.  Each entry is a dict containing at least ``url_path``,
        ``title``, ``icon``, ``mode``, ``require_admin`` and
        ``show_in_sidebar``.  YAML-mode dashboards are *not* returned by
        HA — only user-managed (Storage) ones.

        Used by the v2.1.0 dashboard importer (``web_ui.py``) to detect
        the existing-but-overwrite case before calling
        :meth:`lovelace_create_dashboard`.

        :raises HAAuthError: if the access token is rejected on
                             (re)connect.
        :raises HAAPIError: for transport / protocol errors.
        """
        result = await self._ws_request(
            {"type": "lovelace/dashboards/list"}
        )
        if not isinstance(result, list):
            raise HAAPIError(
                "lovelace/dashboards/list returned a non-list payload: "
                f"{type(result).__name__}"
            )
        return [dict(item) for item in result]

    async def lovelace_create_dashboard(
        self,
        *,
        url_path: str,
        title: str,
        icon: str,
        require_admin: bool = False,
        show_in_sidebar: bool = True,
    ) -> Dict[str, Any]:
        """Create a new storage-mode Lovelace dashboard.

        Sends ``lovelace/dashboards/create`` with ``mode="storage"`` so
        that a subsequent :meth:`lovelace_save_config` call can write
        the actual view layout produced by
        :func:`sleep_classifier.lovelace_template.build_dashboard_config`.

        :param url_path: dashboard slug (appears in the HA URL —
                         ``/lovelace-<url_path>``).
        :param title: human-readable title shown in the sidebar.
        :param icon: ``mdi:`` icon to render in the sidebar.
        :param require_admin: when true, only HA admins can view it.
        :param show_in_sidebar: when true, HA renders a sidebar entry.
        :returns: the dashboard dict echoed back by HA (same shape as
                  :meth:`lovelace_dashboards` entries).
        :raises HAAuthError: if the access token is rejected on
                             (re)connect.
        :raises HAAPIError: when HA reports the create failed (e.g. a
                            duplicate ``url_path``) or returns an
                            unexpected payload — in which case the
                            caller is responsible for any rollback.
        """
        result = await self._ws_request(
            {
                "type": "lovelace/dashboards/create",
                "url_path": url_path,
                "title": title,
                "icon": icon,
                "require_admin": require_admin,
                "show_in_sidebar": show_in_sidebar,
                "mode": "storage",
            }
        )
        if not isinstance(result, dict):
            raise HAAPIError(
                "lovelace/dashboards/create returned a non-dict payload: "
                f"{type(result).__name__}"
            )
        return dict(result)

    async def lovelace_save_config(
        self,
        *,
        url_path: str,
        config: Dict[str, Any],
    ) -> None:
        """Persist the full view config for a storage-mode dashboard.

        Sends ``lovelace/config/save`` with the dashboard body produced
        by :func:`sleep_classifier.lovelace_template.build_dashboard_config`.
        HA returns no payload on success; on failure (invalid
        ``url_path``, schema rejection, transport error)
        :class:`HAAPIError` is raised so the caller can compensate —
        the v2.1.0 importer reacts by calling HA's
        ``lovelace/dashboards/delete`` to roll back the half-created
        dashboard before surfacing 502 to the user.

        :param url_path: dashboard slug previously created via
                         :meth:`lovelace_create_dashboard`.
        :param config: dashboard body (``{"views": [...], "title": ...,
                       ...}``).
        :raises HAAuthError: if the access token is rejected on
                             (re)connect.
        :raises HAAPIError: when HA rejects the save or the transport
                            errors out.
        """
        await self._ws_request(
            {
                "type": "lovelace/config/save",
                "url_path": url_path,
                "config": config,
            }
        )

    async def iter_state_changes(
        self, sub_id: Optional[int] = None,
    ) -> AsyncIterator[StateChangeEvent]:
        """Yield :class:`StateChangeEvent` instances forever.

        If ``sub_id`` is None, automatically subscribes first.  Cancels cleanly
        on ``CancelledError`` and silently swallows ping/pong messages.
        """
        if sub_id is None:
            sub_id = await self.subscribe_state_changes()
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    payload = json.loads(msg.data)
                    if (
                        payload.get("type") == "event"
                        and payload.get("id") == sub_id
                        and payload.get("event", {}).get("event_type") == "state_changed"
                    ):
                        yield StateChangeEvent.from_event(payload["event"])
                elif msg.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    logger.info("HA WebSocket closed (type=%s)", msg.type)
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    raise HAAPIError(f"HA WS error: {self._ws.exception()}")
        except asyncio.CancelledError:
            raise


__all__ = [
    "HAEntity",
    "StateChangeEvent",
    "HAAPIError",
    "HAAuthError",
    "HomeAssistantClient",
]
