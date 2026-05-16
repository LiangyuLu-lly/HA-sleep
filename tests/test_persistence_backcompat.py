"""Tests for v2.0.3 persistence backward compatibility (PR3).

Task 6.9 — Validates: PR3.1, PR3.2

- Load v2.0.3 fixture, assert no schema exception.
- After apply_v2_1_0_defaults, new fields have privacy-safe defaults.
- v2.0.3 fields preserved unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src._overrides_schema import (
    DEFAULT_ONBOARDING_SKIPPED,
    DEFAULT_TELEMETRY_ENABLED,
    DEFAULT_UPGRADE_NOTIFICATIONS_ENABLED,
    apply_v2_1_0_defaults,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "v2.0.3"


@pytest.fixture
def v203_overrides() -> dict:
    """Load the v2.0.3 web_ui_overrides.json fixture."""
    path = _FIXTURES_DIR / "web_ui_overrides.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def v203_preferences() -> dict:
    """Load the v2.0.3 user_preferences.json fixture."""
    path = _FIXTURES_DIR / "user_preferences.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestV203OverridesBackcompat:
    """Loading v2.0.3 web_ui_overrides.json with v2.1.0 code."""

    def test_no_exception_on_load(self, v203_overrides):
        """apply_v2_1_0_defaults does not raise on v2.0.3 data."""
        result = apply_v2_1_0_defaults(v203_overrides)
        assert isinstance(result, dict)

    def test_new_fields_have_privacy_safe_defaults(self, v203_overrides):
        """After apply_v2_1_0_defaults, new fields use safest defaults."""
        result = apply_v2_1_0_defaults(v203_overrides)
        assert result["onboarding_skipped"] is DEFAULT_ONBOARDING_SKIPPED
        assert result["telemetry_enabled"] is DEFAULT_TELEMETRY_ENABLED
        assert result["upgrade_notifications_enabled"] is DEFAULT_UPGRADE_NOTIFICATIONS_ENABLED

    def test_v203_fields_preserved_unchanged(self, v203_overrides):
        """All v2.0.3 fields are preserved exactly as-is."""
        result = apply_v2_1_0_defaults(v203_overrides)
        # Every original key should still be there with the same value
        for key, value in v203_overrides.items():
            assert result[key] == value, (
                f"v2.0.3 field '{key}' was modified: "
                f"expected {value!r}, got {result[key]!r}"
            )

    def test_slot_bindings_intact(self, v203_overrides):
        """Specifically verify slot binding fields are not disturbed."""
        result = apply_v2_1_0_defaults(v203_overrides)
        assert result["sleep_stage_source"] == "sensor.bedroom_sleep_stage"
        assert result["temperature_source"] == "sensor.bedroom_temperature"
        assert result["light_targets"] == ["light.bedroom_main", "light.bedside_lamp"]
        assert result["climate_target"] == "climate.bedroom_ac"
        assert result["feedback_entity"] == "input_number.sleep_rating"


class TestV203PreferencesBackcompat:
    """Loading v2.0.3 user_preferences.json — no schema exception."""

    def test_no_exception_on_load(self, v203_preferences):
        """v2.0.3 user_preferences.json loads without error."""
        # user_preferences.json is not processed by apply_v2_1_0_defaults
        # (that's only for web_ui_overrides), but we verify it parses fine
        assert isinstance(v203_preferences, dict)
        assert "sessions" in v203_preferences
        assert "midpoints" in v203_preferences
        assert len(v203_preferences["sessions"]) >= 1

    def test_session_structure_valid(self, v203_preferences):
        """v2.0.3 session structure has expected fields."""
        session = v203_preferences["sessions"][0]
        assert "timestamp" in session
        assert "duration_minutes" in session
        assert "quality_score" in session
        assert "environment" in session
        assert "stages" in session


class TestNoneInput:
    """When the overrides file doesn't exist (None input)."""

    def test_none_produces_safe_defaults(self):
        """apply_v2_1_0_defaults(None) returns a dict with safe defaults."""
        result = apply_v2_1_0_defaults(None)
        assert result["onboarding_skipped"] is False
        assert result["telemetry_enabled"] is False
        assert result["upgrade_notifications_enabled"] is True


class TestEmptyDict:
    """When the overrides file is empty {}."""

    def test_empty_dict_produces_safe_defaults(self):
        """apply_v2_1_0_defaults({}) returns a dict with safe defaults."""
        result = apply_v2_1_0_defaults({})
        assert result["onboarding_skipped"] is False
        assert result["telemetry_enabled"] is False
        assert result["upgrade_notifications_enabled"] is True
