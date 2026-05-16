"""Property 3: Telemetry payload 不泄露 entity_id.

Parametrize various inputs to ``TelemetryReporter.build_payload(...)``
including edge cases (version strings like ``"sensor.fake"`` that could
trigger the entity_id regex).  Assert ``json.dumps`` result does NOT match
``_ENTITY_ID_PATTERN``.  Also test that deliberately injecting a matching
value raises ``RuntimeError``.

**Validates: Requirements 6.3**
"""
from __future__ import annotations

import json
import re

import pytest

from src.telemetry_reporter import TelemetryReporter, _ENTITY_ID_PATTERN


# --- Normal payloads: must NOT match _ENTITY_ID_PATTERN ---

_NORMAL_CASES = [
    pytest.param(
        {
            "install_id": "550e8400-e29b-41d4-a716-446655440000",
            "version": "2.1.0",
            "ha_version": "2024.6.1",
            "arch": "aarch64",
            "locale": "zh-cn",
            "days_since_install": 0,
            "active_last_24h": True,
        },
        id="typical",
    ),
    pytest.param(
        {
            "install_id": "00000000-0000-0000-0000-000000000000",
            "version": "0.0.0",
            "ha_version": "0.0.0",
            "arch": "x86_64",
            "locale": "en",
            "days_since_install": 9999,
            "active_last_24h": False,
        },
        id="zeros-and-extremes",
    ),
    pytest.param(
        {
            "install_id": "abcdef12-3456-7890-abcd-ef1234567890",
            "version": "sensor.fake",
            "ha_version": "climate.thermostat",
            "arch": "amd64",
            "locale": "en-us",
            "days_since_install": 1,
            "active_last_24h": True,
        },
        id="version-looks-like-entity-id-but-not-at-line-start",
    ),
    pytest.param(
        {
            "install_id": "uuid-with-light-in-it",
            "version": "1.0.0-light",
            "ha_version": "2024.1.0",
            "arch": "armv7",
            "locale": "de",
            "days_since_install": 365,
            "active_last_24h": False,
        },
        id="substring-light-not-prefix",
    ),
    pytest.param(
        {
            "install_id": "binary_sensorish-uuid",
            "version": "2.1.0-beta.1",
            "ha_version": "2024.12.0",
            "arch": "aarch64",
            "locale": "ja",
            "days_since_install": 30,
            "active_last_24h": True,
        },
        id="substring-binary_sensor-not-at-line-start",
    ),
]


@pytest.mark.parametrize("kwargs", _NORMAL_CASES)
def test_build_payload_no_entity_id_leak(kwargs: dict) -> None:
    """build_payload result, when serialized, must not match entity_id pattern."""
    payload = TelemetryReporter.build_payload(**kwargs)
    serialized = json.dumps(payload, sort_keys=True)
    assert not _ENTITY_ID_PATTERN.search(serialized), (
        f"Payload unexpectedly matched entity_id pattern: {serialized}"
    )


# --- Deliberately injecting entity_id-like values raises RuntimeError ---
# The _ENTITY_ID_PATTERN uses re.MULTILINE + ^, so it only triggers when
# entity_id-like text appears at the start of a line.  Compact json.dumps
# produces a single line starting with '{', so normal dict values never
# trigger.  We test the RuntimeError path by monkeypatching json.dumps to
# produce multi-line output where an entity_id value lands at line-start.

_LEAK_PATTERNS = [
    pytest.param("sensor.sleep_stage", id="sensor-entity"),
    pytest.param("climate.bedroom_ac", id="climate-entity"),
    pytest.param("light.nightstand", id="light-entity"),
    pytest.param("binary_sensor.bed_occupied", id="binary_sensor-entity"),
]


@pytest.mark.parametrize("entity_value", _LEAK_PATTERNS)
def test_build_payload_raises_when_serialized_matches_pattern(
    entity_value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Self-check raises RuntimeError if serialized payload matches entity_id pattern.

    We monkeypatch json.dumps to produce output where the entity_id value
    appears at a line start (simulating a multi-line serialization scenario).
    """
    original_dumps = json.dumps

    def fake_dumps(obj, **kwargs):
        # Produce multi-line output where values start at line beginnings
        return "\n".join(f"{v}" for v in obj.values())

    monkeypatch.setattr(json, "dumps", fake_dumps)

    with pytest.raises(RuntimeError, match="entity_id"):
        TelemetryReporter.build_payload(
            install_id=entity_value,
            version="2.1.0",
            ha_version="2024.6.1",
            arch="aarch64",
            locale="zh-cn",
            days_since_install=7,
            active_last_24h=True,
        )


def test_entity_id_pattern_matches_at_line_start() -> None:
    """Verify _ENTITY_ID_PATTERN catches entity_id strings at line boundaries."""
    multiline_text = "some preamble\nsensor.sleep_stage_source\nother text"
    assert _ENTITY_ID_PATTERN.search(multiline_text) is not None

    multiline_text2 = "data\nclimate.bedroom\nmore"
    assert _ENTITY_ID_PATTERN.search(multiline_text2) is not None

    multiline_text3 = "x\nlight.nightstand\ny"
    assert _ENTITY_ID_PATTERN.search(multiline_text3) is not None

    multiline_text4 = "z\nbinary_sensor.motion\nw"
    assert _ENTITY_ID_PATTERN.search(multiline_text4) is not None


def test_entity_id_pattern_no_match_mid_line() -> None:
    """Pattern must NOT match entity_id substrings that are mid-line."""
    single_line = '{"install_id": "sensor.sleep_stage", "version": "2.1.0"}'
    assert _ENTITY_ID_PATTERN.search(single_line) is None


# --- Exhaustive: parametrize many version-like strings that must be safe ---

_TRICKY_VERSIONS = [
    "sensor.fake",
    "climate.test",
    "light.bedroom",
    "binary_sensor.motion",
    "2.1.0+sensor",
    "v1-sensor.build",
    "alpha-climate-beta",
]


@pytest.mark.parametrize("version", _TRICKY_VERSIONS)
def test_tricky_version_strings_safe_in_payload(version: str) -> None:
    """Version strings that *contain* entity_id-like substrings but won't
    match the multiline regex (pattern requires ^ line start)."""
    payload = TelemetryReporter.build_payload(
        install_id="00000000-0000-0000-0000-000000000000",
        version=version,
        ha_version="2024.6.1",
        arch="amd64",
        locale="en",
        days_since_install=0,
        active_last_24h=False,
    )
    serialized = json.dumps(payload, sort_keys=True)
    assert not _ENTITY_ID_PATTERN.search(serialized)
