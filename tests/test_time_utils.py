"""Tiny coverage-closing tests for :mod:`src._time_utils`.

The module is only 3 trivial helpers, but they're the single source of
truth for "what time is it" across the codebase, so we want to lock
their *contract* (naive local vs aware UTC) — not just the
implementation — in place.  Future deprecation-driven rewrites of
``datetime.now()`` will land here.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone

from src._time_utils import (
    date_from_timestamp_local,
    now_local,
    now_utc_aware,
)


class TestNowLocal:
    def test_returns_naive_datetime(self) -> None:
        # Naivety is the *whole point* of this helper — see the module
        # docstring.  An accidental switch to ``datetime.now(tz=...)``
        # would silently break the wake-window scheduler.
        result = now_local()
        assert isinstance(result, datetime)
        assert result.tzinfo is None

    def test_close_to_wallclock(self) -> None:
        # Sanity: within 5 s of `time.time()`.  This catches a bug where
        # someone "helpfully" returns UTC instead of local — on a
        # non-UTC host the gap would be hours.
        local = now_local()
        wallclock = datetime.fromtimestamp(time.time())
        delta = abs((local - wallclock).total_seconds())
        assert delta < 5.0


class TestNowUtcAware:
    def test_returns_aware_utc(self) -> None:
        # Inverse contract of now_local: ALWAYS aware, ALWAYS UTC.
        # Used at HA REST boundaries that demand ISO-8601 with a tz suffix.
        result = now_utc_aware()
        assert isinstance(result, datetime)
        assert result.tzinfo is not None
        assert result.utcoffset() == timezone.utc.utcoffset(result)


class TestDateFromTimestampLocal:
    def test_returns_local_date(self) -> None:
        # Pick a clearly-future timestamp so we don't depend on TZ
        # quirks of a specific historical date.
        ts = time.time()
        result = date_from_timestamp_local(ts)
        assert isinstance(result, date)
        # Same day as datetime.fromtimestamp's local interpretation.
        assert result == datetime.fromtimestamp(ts).date()

    def test_zero_timestamp_is_epoch_local(self) -> None:
        # ``ts=0`` is 1970-01-01 UTC; in any TZ east of UTC the local
        # date is still 1970-01-01 (or 1970-01-02 in extreme east);
        # in TZs west of UTC it's 1969-12-31.  Just assert *something
        # reasonable* — the contract is "uses fromtimestamp" not
        # "always 1970-01-01".
        result = date_from_timestamp_local(0)
        assert result.year in (1969, 1970)
