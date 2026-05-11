"""Listen for the user's morning subjective sleep rating.

Why this is its own module
--------------------------
HA's idiomatic way to capture a single number from the user is the
``input_number`` helper.  The user creates one (e.g. ``input_number.
sleep_rating`` with bounds 1-5) in the **Helpers** section of HA, and
then either nudges it from a Lovelace card every morning or has a
voice-assistant routine push to it.

When that helper's state changes, our add-on:

1. Buffers the latest rating + timestamp.
2. Lets the next session checkpoint pick it up and feed it as a
   ``subjective_score`` into both the quality scorer (raises/lowers the
   final score) and the user profile's Bayesian update (so the system
   learns what the user *feels* is right, not just what the model
   measures).
3. Persists the rating alongside the session in
   ``data/user_preferences.json`` so historical correlation between
   subjective and objective scores can later be analysed.

The implementation is deliberately minimal: a single async coroutine
that consumes ``HomeAssistantClient.iter_state_changes()`` events and a
small in-memory ring buffer of the most recent ratings.

Design choices
~~~~~~~~~~~~~~
* **TTL on stored ratings.**  A rating older than ``max_age_hours``
  (default 18 h) is considered stale — likely from yesterday's session
  — and not applied to tonight's quality blend.  This guards against
  the case where the user forgot to give feedback in the morning and
  the helper still sits at last week's value.
* **Range validation.**  HA helpers are configured with min/max but
  voice integrations sometimes push out-of-range values; we clamp
  silently and log once.
* **No HA session writes.**  We *only read* the helper to keep the
  privilege scope minimal.  If users want to clear it after consumption
  they can wire that themselves in an HA automation.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class FeedbackSnapshot:
    """The most recent rating, with bookkeeping for staleness checks."""

    score: float
    received_at: float    # unix timestamp
    raw_value: str        # what HA actually sent, for traceability
    entity_id: str

    def is_fresh(self, *, max_age_hours: float = 18.0) -> bool:
        """True iff the rating arrived within ``max_age_hours``."""
        return (time.time() - self.received_at) <= max_age_hours * 3600


# ---------------------------------------------------------------------------
# Listener
# ---------------------------------------------------------------------------


class SubjectiveFeedbackListener:
    """Tracks the latest valid rating from a single ``input_number`` helper.

    Usage::

        listener = SubjectiveFeedbackListener(
            entity_id="input_number.sleep_rating", scale=5,
        )

        # In the WS event handler:
        listener.on_state_change(event)

        # At session checkpoint:
        snap = listener.consume()
        if snap is not None:
            quality = blend_subjective(obj, snap.score)
    """

    def __init__(
        self,
        entity_id: str,
        *,
        scale: int = 5,
        max_age_hours: float = 18.0,
        on_change: Optional[Callable[[FeedbackSnapshot], Awaitable[None]]] = None,
    ) -> None:
        if not entity_id:
            raise ValueError("entity_id is required")
        self.entity_id = entity_id
        self.scale = int(scale)
        self.max_age_hours = float(max_age_hours)
        self._latest: Optional[FeedbackSnapshot] = None
        self._on_change = on_change
        # Track whether we've already applied the current rating to a
        # session so the next checkpoint doesn't double-count it.
        self._consumed = False
        # One-shot warn flag for out-of-range values, keyed by raw text.
        self._warned: set[str] = set()

    # ------------------------------------------------------------------ #
    # Inputs
    # ------------------------------------------------------------------ #

    def on_state_change(self, event: Any) -> Optional[FeedbackSnapshot]:
        """Process a HA ``state_changed`` event.

        Accepts either :class:`StateChangeEvent` from
        :mod:`src.ha_api_client` or a duck-typed object with an
        ``entity_id`` and ``new_state.state`` attribute path.  Returns
        the snapshot if it was accepted, ``None`` otherwise.
        """
        eid = getattr(event, "entity_id", None)
        if eid != self.entity_id:
            return None
        new_state = getattr(event, "new_state", None)
        if new_state is None:
            return None
        raw = str(getattr(new_state, "state", ""))
        if raw in ("", "unknown", "unavailable", "None"):
            return None
        try:
            value = float(raw)
        except ValueError:
            if raw not in self._warned:
                logger.warning(
                    "Feedback %s: ignoring non-numeric state %r",
                    self.entity_id, raw,
                )
                self._warned.add(raw)
            return None
        if not 0.0 < value <= self.scale * 1.5:
            # Implausible value (e.g. negative or wildly off-scale).
            if raw not in self._warned:
                logger.warning(
                    "Feedback %s: implausible value %s (scale=%d)",
                    self.entity_id, raw, self.scale,
                )
                self._warned.add(raw)
            return None
        # Clamp to declared scale.
        value = max(1.0, min(float(self.scale), value))
        snap = FeedbackSnapshot(
            score=value,
            received_at=time.time(),
            raw_value=raw,
            entity_id=eid,
        )
        self._latest = snap
        self._consumed = False
        logger.info(
            "Subjective feedback %s = %.1f / %d (will apply on next session)",
            eid, value, self.scale,
        )
        if self._on_change is not None:
            try:
                # Schedule the callback without blocking the event handler.
                loop = asyncio.get_event_loop()
                loop.create_task(self._on_change(snap))
            except RuntimeError:
                pass    # no running loop (e.g. unit tests)
        return snap

    # ------------------------------------------------------------------ #
    # Outputs
    # ------------------------------------------------------------------ #

    def peek(self) -> Optional[FeedbackSnapshot]:
        """Return the latest fresh snapshot without marking it consumed."""
        if self._latest is None:
            return None
        if not self._latest.is_fresh(max_age_hours=self.max_age_hours):
            return None
        return self._latest

    def consume(self) -> Optional[FeedbackSnapshot]:
        """Return the latest fresh snapshot and mark it as applied.

        Subsequent calls return ``None`` until a new state change comes
        in, so a single rating only influences ONE session.
        """
        if self._consumed:
            return None
        snap = self.peek()
        if snap is None:
            return None
        self._consumed = True
        return snap

    def reset(self) -> None:
        """Forget any stored feedback (used by tests + manual override)."""
        self._latest = None
        self._consumed = False

    # ------------------------------------------------------------------ #
    # Diagnostics
    # ------------------------------------------------------------------ #

    def status(self) -> Dict[str, Any]:
        """Return a small dict for logging / HA attribute publish."""
        if self._latest is None:
            return {"entity_id": self.entity_id, "latest": None}
        return {
            "entity_id": self.entity_id,
            "latest_score": self._latest.score,
            "latest_received_at": self._latest.received_at,
            "is_fresh": self._latest.is_fresh(max_age_hours=self.max_age_hours),
            "consumed": self._consumed,
        }
