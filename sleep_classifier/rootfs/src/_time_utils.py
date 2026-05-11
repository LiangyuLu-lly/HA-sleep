"""Time helpers — single source of truth for "what time is it now".

Why this module exists
----------------------

1. **Deprecation**: ``datetime.utcnow()`` and ``datetime.utcfromtimestamp()``
   are deprecated as of Python 3.12 and slated for removal.  Centralising
   the replacement here lets us upgrade in one place.

2. **Local vs UTC semantics**.  Users configure the smart-wake window
   in *local* time, e.g. ``"07:00-07:30"``.  An HA OS Pi 4B in
   Asia/Shanghai (UTC+8) treats that as 07:00 wall-clock local;
   ``datetime.utcnow()`` would put the planner in UTC and a 7-AM window
   would land at midnight local — wrong by 8 hours.

   We therefore prefer **naive local datetime** for all internal use:

   * ``datetime.now()`` (no argument) reads the TZ env var that HA
     Supervisor injects from the user's HA timezone setting.
   * Other internal datetimes — config strings, file timestamps from
     ``time.time()``, NightRecord ISO dates — are also naive, so we
     avoid the awareness-mixing TypeError trap.

3. **Testing**.  Tests already pass an explicit ``now=datetime(...)``
   datetime to all entry points, so this helper is only a runtime
   default.  Switching to it does not affect any of the existing 749
   tests.
"""
from __future__ import annotations

from datetime import datetime, timezone


def now_local() -> datetime:
    """Return the current wall-clock time as a naive local datetime.

    Equivalent to ``datetime.now()`` but discoverable by name and
    documented so callers know *why* we're naive.
    """
    return datetime.now()


def now_utc_aware() -> datetime:
    """Return a timezone-aware UTC datetime.

    Use this when interfacing with external systems that require an
    ISO-8601 string with explicit ``+00:00`` (e.g. HA REST timestamps).
    Internal scheduling should keep using :func:`now_local`.
    """
    return datetime.now(timezone.utc)


def date_from_timestamp_local(ts: float) -> "datetime.date":   # type: ignore[name-defined]
    """``datetime.fromtimestamp(ts).date()`` — deprecation-safe wrapper.

    Used by :class:`SleepDebtTracker` to bucket sessions by *local*
    wake date, so a 23:30 → 06:00 sleep contributes to the morning's
    bucket the user thinks of as today.
    """
    return datetime.fromtimestamp(ts).date()
