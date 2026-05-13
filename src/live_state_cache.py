"""Live entity-state cache with user-override detection (v1.7.1).

Why this exists
===============

Three production failure modes the controller hit in v1.7.0 and
earlier:

1. **Off-state climate entity.**  Firing ``climate.set_temperature=19``
   against an AC whose current state is ``"off"`` is HA-legal (returns
   200) but does nothing — the AC stays off.  The user wakes up in a
   26 °C bedroom and concludes the add-on is broken.

2. **Unavailable entities.**  A bulb that dropped off the Zigbee mesh
   shows state ``"unavailable"``.  ``light.turn_on`` returns 200 but
   the user sees no light change.  The controller needs to stop
   trying and log clearly.

3. **User manual override.**  At 03:30 the user gets up for the
   bathroom and toggles the bedside light on.  30 s later the
   controller's next tick decides "stage=DEEP, brightness should be
   0%" and forces the light off in the user's face.

The fix for all three is a single shared abstraction — a per-entity
live cache that tracks:

* **Current state** (on / off / unavailable / any other HA state
  string).
* **When the state last changed** + whether that change came from
  us or externally.

With this, the controller can:

* Refuse to plan against an entity whose state is unavailable.
* Inject a ``turn_on`` before ``set_temperature`` against an off-state
  climate.
* Pause auto-control on an entity for a configurable grace window
  after a user-initiated change.

Design constraints
------------------

* **No I/O.**  The cache is populated by pushes from the orchestrator
  (which already owns the HA WebSocket connection).  Keeping it I/O-
  free means the controller stays easy to test.
* **Per-entity, not global.**  A user can manually toggle the bedroom
  light without implying "don't touch the AC".
* **Confident source attribution.**  We can't perfectly distinguish
  "user physically pressed a wall switch" from "an automation fired":
  both look like the same ``state_changed`` event to us.  We use the
  simpler but robust heuristic: *any* state_changed we see within
  ``_SELF_ACTION_WINDOW`` seconds of our last outgoing command is
  assumed to be our own echo; anything outside that window is
  treated as external.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Seconds after we dispatch a service call in which the resulting
# state_changed event is assumed to be our own echo.  HA's state
# machine typically updates within 500 ms of a successful service
# call; 5 s is safe for slow integrations (e.g. ZWave) without being
# so long that a genuine user touch gets misattributed.
_SELF_ACTION_WINDOW: float = 5.0

# Default grace period after a user manually changes an entity,
# during which the controller will not issue auto-commands against
# it.  Long enough to cover the "bathroom trip" pattern (get up,
# toggle light on, come back, toggle off, fall asleep) — typically
# 5-10 min end to end.  Configurable via ``user_override_grace_seconds``.
_DEFAULT_USER_OVERRIDE_GRACE_SECONDS: float = 600.0


# HA state strings that mean "the device is not reachable right now".
# We never send service calls against an entity in any of these states.
# ``"off"`` is NOT included — off is a legitimate state that the
# controller needs to know about (so it can turn_on before setpoint),
# not a reason to skip entirely.
_UNAVAILABLE_STATES: frozenset[str] = frozenset({
    "unavailable", "unknown", "none", "",
})


# HA state strings that mean "the device is off and won't respond to
# setpoint commands" for domains where turn_on is meaningful.  For
# ``climate``, state is the HVAC mode, so "off" means the AC is
# switched off and won't cool/heat regardless of target_temp.  For
# lights and fans, "off" is the literal off state.
_OFF_STATES: frozenset[str] = frozenset({"off"})


# ---------------------------------------------------------------------------
# Snapshot dataclass
# ---------------------------------------------------------------------------


@dataclass
class EntitySnapshot:
    """One entity's most recent state + change provenance.

    ``last_state`` is the raw HA state string (e.g. ``"cool"``,
    ``"on"``, ``"off"``, ``"unavailable"``); callers use the
    helper methods on :class:`LiveStateCache` rather than parsing
    it directly.

    ``last_user_change_ts`` is ``0.0`` if we have never observed a
    change we attributed to the user (either the entity has been
    stable since boot, or every change was our own echo).
    """

    entity_id: str
    last_state: str
    last_state_ts: float
    last_user_change_ts: float = 0.0
    # When *we* last dispatched a service to this entity.  Used to
    # classify incoming state_changed events as self-echo vs user
    # override.  0.0 means we've never controlled it.
    last_self_action_ts: float = 0.0
    attributes: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class LiveStateCache:
    """Per-entity live state tracker + user-override detector.

    Shared by the orchestrator (which pushes updates from the HA
    WebSocket) and the controller (which queries before planning
    actions).  Not thread-safe; both access paths run on the same
    asyncio loop so single-reader-single-writer holds.
    """

    def __init__(
        self,
        *,
        user_override_grace_seconds: float = _DEFAULT_USER_OVERRIDE_GRACE_SECONDS,
    ) -> None:
        self._snapshots: Dict[str, EntitySnapshot] = {}
        self._grace_seconds = float(user_override_grace_seconds)
        # Bookkeeping — how many times each entity_id was skipped for
        # which reason.  Surfaced on the diagnostic sensor so users can
        # tell when the system is deliberately silent vs misbehaving.
        self._skipped_unavailable: Dict[str, int] = {}
        self._skipped_user_override: Dict[str, int] = {}
        self._auto_turn_on_injected: Dict[str, int] = {}

    # ------------------------------------------------------------------ #
    # Population — called by the orchestrator                            #
    # ------------------------------------------------------------------ #

    def seed_from_registry(
        self,
        entity_id: str,
        state: str,
        attributes: Optional[Dict[str, Any]] = None,
        *,
        now: Optional[float] = None,
    ) -> None:
        """Called once per bound entity at boot, using values fresh
        out of ``GET /api/states``.  Resets any stale cache entry.
        """
        ts = time.time() if now is None else now
        self._snapshots[entity_id] = EntitySnapshot(
            entity_id=entity_id,
            last_state=str(state),
            last_state_ts=ts,
            attributes=dict(attributes or {}),
        )

    def on_state_change(
        self,
        entity_id: str,
        new_state: str,
        attributes: Optional[Dict[str, Any]] = None,
        *,
        now: Optional[float] = None,
    ) -> None:
        """Called by the orchestrator's state_changed routing whenever
        a tracked entity updates.  Classifies the change as self-echo
        vs user override based on proximity to our last dispatch.
        """
        ts = time.time() if now is None else now
        snap = self._snapshots.get(entity_id)
        if snap is None:
            # First time we're seeing this entity — treat as seed
            # (can't classify as user change since we have no
            # baseline).
            self._snapshots[entity_id] = EntitySnapshot(
                entity_id=entity_id,
                last_state=str(new_state),
                last_state_ts=ts,
                attributes=dict(attributes or {}),
            )
            return

        # Ignore no-op state updates (HA sometimes replays identical
        # state_changed events on attribute-only changes).
        if str(new_state) == snap.last_state:
            if attributes is not None:
                snap.attributes.update(attributes)
            return

        # Classify: self-echo if within the window of our last dispatch,
        # user override otherwise.
        is_self_echo = (
            snap.last_self_action_ts > 0.0
            and ts - snap.last_self_action_ts <= _SELF_ACTION_WINDOW
        )

        snap.last_state = str(new_state)
        snap.last_state_ts = ts
        if attributes is not None:
            snap.attributes.update(attributes)
        if not is_self_echo:
            snap.last_user_change_ts = ts
            logger.info(
                "User override detected on %s (state -> %r); "
                "auto-control paused for %.0f s.",
                entity_id, new_state, self._grace_seconds,
            )

    def record_self_dispatch(
        self, entity_id: str, *, now: Optional[float] = None,
    ) -> None:
        """Called by the controller right before (or after) it fires a
        service call, so the subsequent state_changed echo won't be
        misclassified as a user override.
        """
        ts = time.time() if now is None else now
        snap = self._snapshots.get(entity_id)
        if snap is None:
            # Accept the write even if we haven't seeded the entity
            # yet (common in tests).  on_state_change will seed later.
            self._snapshots[entity_id] = EntitySnapshot(
                entity_id=entity_id,
                last_state="",
                last_state_ts=0.0,
                last_self_action_ts=ts,
            )
            return
        snap.last_self_action_ts = ts

    # ------------------------------------------------------------------ #
    # Controller-facing queries                                          #
    # ------------------------------------------------------------------ #

    def is_available(self, entity_id: str) -> bool:
        """True iff the entity is in a state HA considers usable.

        Missing from cache returns True — the controller's default
        is optimistic (use the entity) rather than conservative
        (skip it), matching pre-v1.7.1 behaviour.  Orchestrator
        seeding ensures this optimism gap is closed in production.
        """
        snap = self._snapshots.get(entity_id)
        if snap is None:
            return True
        return snap.last_state.lower() not in _UNAVAILABLE_STATES

    def is_off(self, entity_id: str) -> bool:
        """True iff the entity is currently off (and the
        controller should turn it on first before setpoint)."""
        snap = self._snapshots.get(entity_id)
        if snap is None:
            return False
        return snap.last_state.lower() in _OFF_STATES

    def under_user_override(
        self,
        entity_id: str,
        *,
        now: Optional[float] = None,
    ) -> bool:
        """True iff the user changed this entity within the grace
        window and the controller should hold off auto-commands."""
        snap = self._snapshots.get(entity_id)
        if snap is None or snap.last_user_change_ts == 0.0:
            return False
        ts = time.time() if now is None else now
        return (ts - snap.last_user_change_ts) <= self._grace_seconds

    def current_state(self, entity_id: str) -> Optional[str]:
        """Return the last-known raw state string, or ``None`` if the
        entity was never seeded / seen.
        """
        snap = self._snapshots.get(entity_id)
        return snap.last_state if snap else None

    # ------------------------------------------------------------------ #
    # Stat counters (for the diagnostic sensor)                          #
    # ------------------------------------------------------------------ #

    def count_skip_unavailable(self, entity_id: str) -> None:
        self._skipped_unavailable[entity_id] = (
            self._skipped_unavailable.get(entity_id, 0) + 1
        )

    def count_skip_user_override(self, entity_id: str) -> None:
        self._skipped_user_override[entity_id] = (
            self._skipped_user_override.get(entity_id, 0) + 1
        )

    def count_auto_turn_on(self, entity_id: str) -> None:
        self._auto_turn_on_injected[entity_id] = (
            self._auto_turn_on_injected.get(entity_id, 0) + 1
        )

    def stats(self) -> Dict[str, Dict[str, int]]:
        """Summary for the diagnostic ``last_action`` sensor."""
        return {
            "skipped_unavailable": dict(self._skipped_unavailable),
            "skipped_user_override": dict(self._skipped_user_override),
            "auto_turn_on_injected": dict(self._auto_turn_on_injected),
        }


__all__ = [
    "LiveStateCache",
    "EntitySnapshot",
]
