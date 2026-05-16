"""Opt-in anonymous telemetry reporter.

Sends a minimal JSON payload (version, arch, locale, active status) to the
project telemetry endpoint every 24 hours when explicitly enabled by the user.
Default is **disabled** — no HTTP requests, no install_id file creation.

:Design reference: design.md §3.6
:Requirements: 6.1, 6.2, 6.3, 6.4, 6.6, 6.7, 6.8, 6.9
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import aiohttp

from src._io_utils import atomic_write_text

_LOGGER = logging.getLogger(__name__)

_ENTITY_ID_PATTERN = re.compile(
    r"^sensor\.|^climate\.|^light\.|^binary_sensor\.", re.MULTILINE
)

_INITIAL_BACKOFF_SECONDS: float = 60.0
_MAX_BACKOFF_SECONDS: float = 86_400.0


class TelemetryReporter:
    """Anonymous telemetry reporter with opt-in semantics.

    :param enabled: Whether telemetry is enabled (opt-in, default false).
    :param endpoint: HTTPS URL to POST telemetry payloads to.
    :param version: Current add-on version string.
    :param ha_version: Home Assistant Core version string.
    :param arch: System architecture (e.g. ``"aarch64"`` or ``"amd64"``).
    :param locale: User locale (e.g. ``"zh-cn"`` or ``"en"``).
    :param data_dir: Base directory for persistent data (default ``/data``).
    :param clock: Callable returning current UNIX timestamp.
    :param interval_seconds: Seconds between telemetry ticks (default 24h).
    """

    def __init__(
        self,
        *,
        enabled: bool,
        endpoint: str,
        version: str,
        ha_version: str,
        arch: str,
        locale: str,
        data_dir: Path = Path("/data"),
        clock: Callable[[], float] = time.time,
        interval_seconds: float = 86_400.0,
    ) -> None:
        self._enabled = enabled
        self._endpoint = endpoint
        self._version = version
        self._ha_version = ha_version
        self._arch = arch
        self._locale = locale
        self._data_dir = data_dir
        self._clock = clock
        self._interval_seconds = interval_seconds

        self._install_id_path = data_dir / "install_id.uuid"
        self._task: asyncio.Task[None] | None = None
        self._backoff: float = _INITIAL_BACKOFF_SECONDS
        self._install_id: str | None = None
        self._created_at: float | None = None

        # Try optional sentry integration
        self._sentry_configured = False
        if self._enabled:
            self._try_configure_sentry()

    def _try_configure_sentry(self) -> None:
        """Attempt to configure sentry_sdk if available (soft dependency)."""
        try:
            import sentry_sdk  # noqa: F401
            from sentry_sdk.scrubber import EventScrubber, DEFAULT_DENYLIST

            custom_denylist = list(DEFAULT_DENYLIST) + [
                "entity_id",
                "token",
                "username",
            ]
            scrubber = EventScrubber(denylist=custom_denylist)
            sentry_sdk.init(
                send_default_pii=False,
                event_scrubber=scrubber,
            )
            self._sentry_configured = True
            _LOGGER.debug("sentry_sdk configured with custom scrubber")
        except ImportError:
            _LOGGER.debug("sentry_sdk not installed; skipping integration")
        except Exception:  # noqa: BLE001
            _LOGGER.debug("sentry_sdk configuration failed; skipping")

    def _get_or_create_install_id(self) -> str:
        """Read existing install_id or generate a new one.

        Only called when ``enabled=True``.
        """
        if self._install_id is not None:
            return self._install_id

        if self._install_id_path.exists():
            self._install_id = self._install_id_path.read_text(
                encoding="utf-8"
            ).strip()
            self._created_at = self._install_id_path.stat().st_mtime
        else:
            self._install_id = str(uuid.uuid4())
            self._created_at = self._clock()
            atomic_write_text(self._install_id_path, self._install_id)
            _LOGGER.info("Generated new install_id: %s", self._install_id)

        return self._install_id

    @staticmethod
    def build_payload(
        *,
        install_id: str,
        version: str,
        ha_version: str,
        arch: str,
        locale: str,
        days_since_install: int,
        active_last_24h: bool,
    ) -> dict[str, Any]:
        """Construct telemetry payload and self-check for entity_id leaks.

        :raises RuntimeError: If the serialized payload matches entity_id
            patterns (should never happen with valid inputs).
        """
        payload: dict[str, Any] = {
            "install_id": install_id,
            "version": version,
            "ha_version": ha_version,
            "arch": arch,
            "locale": locale,
            "days_since_install": days_since_install,
            "active_last_24h": active_last_24h,
        }

        # Self-check: payload must not contain any entity_id-like strings
        serialized = json.dumps(payload, sort_keys=True)
        if _ENTITY_ID_PATTERN.search(serialized):
            raise RuntimeError("telemetry payload leaked entity_id")

        return payload

    async def run(self) -> None:
        """Main loop: report every interval_seconds.

        If ``enabled=False``, returns immediately without creating install_id
        or making any network requests.
        """
        if not self._enabled:
            return

        install_id = self._get_or_create_install_id()

        while True:
            try:
                await self._tick(install_id)
            except Exception:  # noqa: BLE001
                _LOGGER.warning(
                    "Telemetry tick failed; backing off %.0fs",
                    self._backoff,
                    exc_info=True,
                )
                await asyncio.sleep(self._backoff)
                self._backoff = min(
                    self._backoff * 2, _MAX_BACKOFF_SECONDS
                )
                continue

            # Success: reset backoff and wait for next interval
            self._backoff = _INITIAL_BACKOFF_SECONDS
            await asyncio.sleep(self._interval_seconds)

    async def _tick(self, install_id: str) -> None:
        """Execute one telemetry report cycle."""
        now = self._clock()
        days_since_install = int(
            (now - (self._created_at or now)) / 86_400
        )

        payload = self.build_payload(
            install_id=install_id,
            version=self._version,
            ha_version=self._ha_version,
            arch=self._arch,
            locale=self._locale,
            days_since_install=max(0, days_since_install),
            active_last_24h=True,
        )

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self._endpoint,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status >= 400:
                    _LOGGER.warning(
                        "Telemetry endpoint returned %d", resp.status
                    )
                    raise aiohttp.ClientResponseError(
                        request_info=resp.request_info,
                        history=resp.history,
                        status=resp.status,
                        message=f"HTTP {resp.status}",
                    )

        _LOGGER.debug("Telemetry report sent successfully")

    async def disable(self) -> None:
        """Disable telemetry: cancel internal task and delete install_id.

        Idempotent — safe to call multiple times.
        """
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        self._install_id_path.unlink(missing_ok=True)
        self._install_id = None
        self._created_at = None
        self._enabled = False
        _LOGGER.info("Telemetry disabled; install_id removed")

    def enable(self) -> None:
        """Enable telemetry and start the reporting task.

        Manages state via internal ``_task`` handle. If already running,
        this is a no-op.
        """
        if self._task is not None and not self._task.done():
            return  # Already running

        self._enabled = True
        self._task = asyncio.create_task(self.run(), name="telemetry_reporter")
