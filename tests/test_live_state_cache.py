""":mod:`src.live_state_cache` — v1.7.1 core 落地 safety layer.

These tests lock in the three contracts that actually distinguish a
toy from a deployable add-on:

1. An entity that's been yanked off the mesh (``unavailable``) stays
   out of controller dispatch.
2. An AC that's currently ``off`` is correctly classified so the
   controller can inject a ``set_hvac_mode`` before ``set_temperature``.
3. A user who manually toggles a bound entity buys themselves a grace
   window during which the add-on won't fight them.
"""
from __future__ import annotations

import pytest

from src.live_state_cache import EntitySnapshot, LiveStateCache


class TestAvailability:
    def test_seeded_on_state_returns_available(self) -> None:
        cache = LiveStateCache()
        cache.seed_from_registry("light.bedroom", "on", {}, now=100.0)
        assert cache.is_available("light.bedroom") is True
        assert cache.is_off("light.bedroom") is False

    def test_seeded_unavailable_returns_false(self) -> None:
        cache = LiveStateCache()
        cache.seed_from_registry(
            "light.bedroom", "unavailable", {}, now=100.0,
        )
        assert cache.is_available("light.bedroom") is False

    def test_seeded_unknown_returns_false(self) -> None:
        cache = LiveStateCache()
        cache.seed_from_registry(
            "light.bedroom", "unknown", {}, now=100.0,
        )
        assert cache.is_available("light.bedroom") is False

    def test_missing_entity_is_optimistically_available(self) -> None:
        # Called before seeding — default to True to preserve
        # pre-v1.7.1 behaviour rather than silently lock out
        # untracked entities.
        cache = LiveStateCache()
        assert cache.is_available("light.random") is True

    def test_state_transition_to_unavailable_flips(self) -> None:
        cache = LiveStateCache()
        cache.seed_from_registry("light.bedroom", "on", {}, now=100.0)
        cache.on_state_change("light.bedroom", "unavailable", now=200.0)
        assert cache.is_available("light.bedroom") is False


class TestOffStateDetection:
    def test_climate_off_returns_true(self) -> None:
        cache = LiveStateCache()
        cache.seed_from_registry("climate.bedroom", "off", {}, now=100.0)
        assert cache.is_off("climate.bedroom") is True

    def test_climate_cool_returns_false(self) -> None:
        cache = LiveStateCache()
        cache.seed_from_registry("climate.bedroom", "cool", {}, now=100.0)
        assert cache.is_off("climate.bedroom") is False

    def test_light_off_returns_true(self) -> None:
        cache = LiveStateCache()
        cache.seed_from_registry("light.bedroom", "off", {}, now=100.0)
        assert cache.is_off("light.bedroom") is True

    def test_unavailable_not_classified_as_off(self) -> None:
        """``unavailable`` is handled by availability, not off —
        the controller shouldn't try to turn on something that's
        physically unreachable."""
        cache = LiveStateCache()
        cache.seed_from_registry(
            "climate.bedroom", "unavailable", {}, now=100.0,
        )
        assert cache.is_off("climate.bedroom") is False


class TestUserOverrideDetection:
    def test_change_outside_self_action_window_flags_user(self) -> None:
        cache = LiveStateCache(user_override_grace_seconds=600.0)
        cache.seed_from_registry("light.bedroom", "off", {}, now=100.0)
        # No self-dispatch recorded, so this change is attributed
        # to the user.
        cache.on_state_change("light.bedroom", "on", now=200.0)
        assert cache.under_user_override("light.bedroom", now=250.0)

    def test_change_within_self_action_window_is_self_echo(self) -> None:
        cache = LiveStateCache(user_override_grace_seconds=600.0)
        cache.seed_from_registry("light.bedroom", "off", {}, now=100.0)
        # Simulate: we just dispatched turn_on at ts=200.
        cache.record_self_dispatch("light.bedroom", now=200.0)
        # HA echoes our change at ts=201 (1 s later, well within
        # _SELF_ACTION_WINDOW=5 s).
        cache.on_state_change("light.bedroom", "on", now=201.0)
        # Should NOT be flagged as user override.
        assert not cache.under_user_override("light.bedroom", now=250.0)

    def test_grace_window_expires(self) -> None:
        cache = LiveStateCache(user_override_grace_seconds=600.0)
        cache.seed_from_registry("light.bedroom", "off", {}, now=100.0)
        cache.on_state_change("light.bedroom", "on", now=200.0)
        # Within grace → still overridden.
        assert cache.under_user_override("light.bedroom", now=500.0)
        # Past grace → controller may resume.
        assert not cache.under_user_override("light.bedroom", now=1000.0)

    def test_multiple_user_changes_extend_grace(self) -> None:
        """A user who keeps fiddling with the light should keep
        getting the grace window — we don't want to pounce the
        moment they pause to think."""
        cache = LiveStateCache(user_override_grace_seconds=600.0)
        cache.seed_from_registry("light.bedroom", "off", {}, now=100.0)
        cache.on_state_change("light.bedroom", "on", now=200.0)
        cache.on_state_change("light.bedroom", "off", now=400.0)
        cache.on_state_change("light.bedroom", "on", now=600.0)
        # At t=900, we're within 600 s of the last user change.
        assert cache.under_user_override("light.bedroom", now=900.0)

    def test_self_dispatch_before_seed_is_recorded(self) -> None:
        # Controller may call record_self_dispatch for an entity
        # we haven't seeded yet (tests, mostly).  Must not crash.
        cache = LiveStateCache()
        cache.record_self_dispatch("light.bedroom", now=100.0)
        # A change 1 s later is self-echo.
        cache.on_state_change("light.bedroom", "on", now=101.0)
        assert not cache.under_user_override("light.bedroom", now=150.0)


class TestStats:
    def test_skip_counters_accumulate_per_entity(self) -> None:
        cache = LiveStateCache()
        cache.count_skip_unavailable("light.a")
        cache.count_skip_unavailable("light.a")
        cache.count_skip_unavailable("light.b")
        cache.count_skip_user_override("climate.c")
        cache.count_auto_turn_on("humidifier.d")

        stats = cache.stats()
        assert stats["skipped_unavailable"]["light.a"] == 2
        assert stats["skipped_unavailable"]["light.b"] == 1
        assert stats["skipped_user_override"]["climate.c"] == 1
        assert stats["auto_turn_on_injected"]["humidifier.d"] == 1
