"""Periodic GitHub release checker and HA persistent notification dispatcher.

Checks ``https://api.github.com/repos/{owner}/{repo}/releases/latest`` every
24 hours (configurable) and fires a ``persistent_notification.create`` service
call when a newer version is detected.

Privacy contract:
- Anonymous GET; no ``install_id`` sent.
- ``User-Agent: sleep-classifier/{version}`` (GitHub API requires a UA).
- ``upgrade_notifications_enabled = false`` → constructor returns immediately
  from ``run()``, no HTTP ever issued.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

try:
    from packaging.version import InvalidVersion, Version
    from packaging.version import parse as _parse_version

    _HAS_PACKAGING = True
except ImportError:  # pragma: no cover
    _HAS_PACKAGING = False

from src._io_utils import atomic_write_json

if TYPE_CHECKING:
    from src.ha_api_client import HAAPIClient

logger = logging.getLogger(__name__)

_STATE_FILENAME = "last_upgrade_check.json"

# Exponential backoff parameters (same pattern as telemetry)
_BACKOFF_INITIAL: float = 60.0
_BACKOFF_MAX: float = 86_400.0


class UpgradeNotifier:
    """Periodically checks GitHub for new releases and notifies the user.

    :param enabled: If ``False``, :meth:`run` returns immediately.
    :param current_version: The running version string (e.g. ``"2.1.0"``).
    :param owner: GitHub repository owner.
    :param repo: GitHub repository name.
    :param ha_client: :class:`HAAPIClient` instance for calling HA services.
    :param data_dir: Persistent data directory (default ``/data``).
    :param interval_seconds: Normal polling interval (default 24 h).
    """

    def __init__(
        self,
        *,
        enabled: bool,
        current_version: str,
        owner: str,
        repo: str,
        ha_client: "HAAPIClient",
        data_dir: Path = Path("/data"),
        interval_seconds: float = 86_400.0,
    ) -> None:
        self._enabled = enabled
        self._current_version = current_version
        self._owner = owner
        self._repo = repo
        self._ha_client = ha_client
        self._data_dir = data_dir
        self._interval_seconds = interval_seconds
        self._state_path = data_dir / _STATE_FILENAME
        self._backoff: float = _BACKOFF_INITIAL

    async def run(self) -> None:
        """Main loop: polls GitHub releases every *interval_seconds*.

        If *enabled* is ``False``, returns immediately without issuing any
        network request or writing any file.
        """
        if not self._enabled:
            return

        while True:
            try:
                await self._tick()
            except Exception:  # noqa: BLE001 — never bubble to main loop
                logger.exception("upgrade_notifier tick failed")

            await asyncio.sleep(self._interval_seconds)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _tick(self) -> None:
        """Single check-and-notify cycle."""
        latest_tag = await self._fetch_latest()
        if latest_tag is None:
            return  # network failure; backoff already applied inside _fetch_latest

        # Reset backoff on success
        self._backoff = _BACKOFF_INITIAL

        # Strip leading 'v' if present for comparison
        latest_version = latest_tag.lstrip("v")

        state = self._load_state()
        already_notified = (
            state.get("latest") == latest_tag and state.get("notified", False)
        )

        if self.is_newer(self._current_version, latest_version):
            # Persist check result
            new_state: dict[str, Any] = {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "latest": latest_tag,
                "notified": already_notified,
            }

            if not already_notified:
                await self._notify(latest_tag)
                new_state["notified"] = True

            atomic_write_json(self._state_path, new_state)
        else:
            # Current version is up-to-date; still persist the check timestamp
            atomic_write_json(
                self._state_path,
                {
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "latest": latest_tag,
                    "notified": state.get("notified", False),
                },
            )

    async def _fetch_latest(self) -> str | None:
        """GET the latest release tag from GitHub.

        Returns the tag name string on success, or ``None`` on any failure.
        Failures are silently logged with exponential backoff.
        """
        url = (
            f"https://api.github.com/repos/{self._owner}/{self._repo}/releases/latest"
        )
        headers = {
            "User-Agent": f"sleep-classifier/{self._current_version}",
            "Accept": "application/vnd.github+json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        tag = data.get("tag_name")
                        if isinstance(tag, str) and tag:
                            return tag
                        logger.warning(
                            "upgrade_notifier: unexpected response shape from GitHub"
                        )
                        return None

                    # 403 (rate limit), 404, 5xx — silent backoff
                    logger.debug(
                        "upgrade_notifier: GitHub returned %d, backing off %.0fs",
                        resp.status,
                        self._backoff,
                    )
                    await asyncio.sleep(self._backoff)
                    self._backoff = min(self._backoff * 2, _BACKOFF_MAX)
                    return None

        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            logger.debug(
                "upgrade_notifier: network error (%s), backing off %.0fs",
                exc,
                self._backoff,
            )
            await asyncio.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, _BACKOFF_MAX)
            return None

    async def _notify(self, latest_tag: str) -> None:
        """Send a persistent notification to HA about the available upgrade."""
        try:
            await self._ha_client.call_service(
                "persistent_notification",
                "create",
                title="Sleep Classifier 更新可用",
                message=(
                    f"新版本 {latest_tag} 已发布。"
                    f"当前版本: {self._current_version}。\n"
                    f"请前往 GitHub Releases 查看详情。"
                ),
                notification_id="sleep_classifier_upgrade",
            )
        except Exception:  # noqa: BLE001
            logger.warning("upgrade_notifier: failed to create persistent notification")

    def _load_state(self) -> dict[str, Any]:
        """Load persisted state from disk, or return empty dict if missing."""
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    # ------------------------------------------------------------------
    # Public static helpers
    # ------------------------------------------------------------------

    @staticmethod
    def is_newer(current: str, latest: str) -> bool:
        """Compare two version strings using PEP 440 semantics.

        Returns ``True`` only when *latest* is strictly newer than *current*.
        Returns ``False`` if either string is not a valid PEP 440 version
        (conservative approach — never trigger false-positive upgrade banners).

        :param current: The currently running version (e.g. ``"2.1.0"``).
        :param latest: The latest released version (e.g. ``"2.1.1"``).
        """
        if _HAS_PACKAGING:
            try:
                cur = Version(current)
                lat = Version(latest)
            except InvalidVersion:
                return False
            return lat > cur
        else:  # pragma: no cover — fallback when packaging unavailable
            # Simple lexicographic fallback; conservative (may miss some upgrades)
            try:
                cur_parts = tuple(int(x) for x in current.split("."))
                lat_parts = tuple(int(x) for x in latest.split("."))
            except (ValueError, AttributeError):
                return False
            return lat_parts > cur_parts
