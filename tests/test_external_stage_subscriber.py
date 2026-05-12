"""Tests for :mod:`src.external_stage_subscriber`.

The subscriber replaces the v1.2 in-house CNN-BiLSTM engine.  These tests
lock in the behaviours users will rely on most:

1.  *Wide vocabulary tolerance* — every Mi Band, Withings, Apple-Watch,
    R60ABD1, and Chinese-locale variant must map to the canonical
    :class:`SleepStage` enum without per-device adapter code.
2.  *Hold-on-noise* — when the device emits ``unknown`` / ``unavailable``
    / bool / unparseable garbage, the cached stage must NOT regress to a
    default; we hold the last known good value.
3.  *Confidence rescaling* — devices use either ``0..1`` or ``0..100``,
    both must collapse to ``[0, 1]``.
4.  *Drop-in compatibility* — ``buffer_ready`` / ``infer`` / ``push_hr``
    / ``push_movement`` must exist so swapping it into the inference
    loop is a zero-churn refactor.
5.  *Staleness reporting* — ``is_stale`` must respect the user-supplied
    window and ignore the bootstrap period (no updates yet).
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from src.data_structures import SleepStage
from src.external_stage_subscriber import (
    ExternalStageSubscriber,
    _parse_confidence,
    _parse_stage,
)


ENTITY = "sensor.bedroom_sleep_stage"


# ---------------------------------------------------------------------------
# Vocabulary coverage                                                        #
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # ---- English (canonical) ----
        ("AWAKE", SleepStage.AWAKE),
        ("awake", SleepStage.AWAKE),
        ("Awake", SleepStage.AWAKE),
        ("wake", SleepStage.AWAKE),
        ("LIGHT", SleepStage.LIGHT),
        ("light_sleep", SleepStage.LIGHT),
        ("LightSleep", SleepStage.LIGHT),
        ("N1", SleepStage.LIGHT),
        ("n2", SleepStage.LIGHT),
        ("DEEP", SleepStage.DEEP),
        ("deep_sleep", SleepStage.DEEP),
        ("N3", SleepStage.DEEP),
        ("sws", SleepStage.DEEP),
        ("REM", SleepStage.REM),
        ("rem", SleepStage.REM),
        ("R", SleepStage.REM),
        # ---- Chinese (Mi Home, native ESPHome) ----
        ("清醒", SleepStage.AWAKE),
        ("浅睡", SleepStage.LIGHT),
        ("浅度睡眠", SleepStage.LIGHT),
        ("深睡", SleepStage.DEEP),
        ("深度睡眠", SleepStage.DEEP),
        ("快速眼动", SleepStage.REM),
        ("快速眼动睡眠", SleepStage.REM),
        # ---- Numeric: 0-based convention ----
        (0, SleepStage.AWAKE),
        (1, SleepStage.LIGHT),
        (2, SleepStage.DEEP),
        (3, SleepStage.REM),
        # ---- Numeric: 1-based convention (Mi Band / Withings) ----
        # Note: 1 is ambiguous (0-based LIGHT vs 1-based AWAKE); we treat
        # it as 0-based LIGHT because that's our canonical convention.
        # 4 is unambiguously 1-based REM.
        (4, SleepStage.REM),
        # ---- Numeric as string ----
        ("0", SleepStage.AWAKE),
        ("3", SleepStage.REM),
        # ---- Whitespace tolerance ----
        ("  DEEP  ", SleepStage.DEEP),
    ],
)
def test_parse_stage_recognises_all_known_vocabularies(raw: Any, expected: SleepStage) -> None:
    """Every documented input alias must collapse to the canonical enum.

    A regression here breaks the product for whichever tracker emits
    the affected value, so we lock in every alias explicitly.
    """
    assert _parse_stage(raw) is expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "unknown",
        "Unknown",
        "unavailable",
        "none",
        "null",
        "garbage",
        "stage 7",
        True,         # bool must not match int branch
        False,
        object(),
    ],
)
def test_parse_stage_returns_none_for_noise(raw: Any) -> None:
    """Unparseable values must yield ``None`` (caller will hold prior stage).

    Returning a default like AWAKE here would silently corrupt the
    learner's stage_counts ledger, so we'd rather lose one tick than
    lie.
    """
    assert _parse_stage(raw) is None


# ---------------------------------------------------------------------------
# Confidence extraction                                                      #
# ---------------------------------------------------------------------------


def test_confidence_defaults_to_one_when_attrs_missing() -> None:
    """Bare devices have no confidence attribute — assume authoritative."""
    assert _parse_confidence(None) == 1.0
    assert _parse_confidence({}) == 1.0


@pytest.mark.parametrize(
    "attrs,expected",
    [
        ({"confidence": 0.87}, 0.87),
        ({"probability": 0.5}, 0.5),
        ({"conf": 0.25}, 0.25),
        ({"score": 92}, 0.92),         # 0..100 auto-rescaled
        ({"score": 100.0}, 1.0),
        ({"confidence": -0.1}, 0.0),   # clamp
        ({"confidence": 2.0}, 0.02),   # >1 → percentage → 0.02 after rescale
        ({"confidence": "0.7"}, 0.7),  # string-encoded numbers OK
        ({"confidence": "nan"}, 1.0),  # nan path is treated as parse failure
    ],
)
def test_confidence_handles_known_vendor_conventions(attrs: Any, expected: float) -> None:
    out = _parse_confidence(attrs)
    if expected != expected:    # NaN comparison
        assert out != out
    else:
        assert abs(out - expected) < 1e-9


def test_confidence_picks_first_known_key() -> None:
    """If multiple keys are present, the canonical ``confidence`` wins.

    We don't want a noisy ``score`` to override the device's own
    confidence estimate.
    """
    assert _parse_confidence({"confidence": 0.9, "score": 50}) == 0.9


# ---------------------------------------------------------------------------
# Subscriber lifecycle                                                       #
# ---------------------------------------------------------------------------


def test_constructor_rejects_blank_entity_id() -> None:
    """Misconfigured users typically leave the field blank — fail loud."""
    with pytest.raises(ValueError, match="stage_entity_id"):
        ExternalStageSubscriber(stage_entity_id="")
    with pytest.raises(ValueError):
        ExternalStageSubscriber(stage_entity_id=None)  # type: ignore[arg-type]


def test_bootstrap_state_is_conservative() -> None:
    """Before any update, report LIGHT @ low confidence.

    The controller treats confidence < 0.5 as "don't crank the AC", so
    cold-start can't trigger a hot/cold spike before the device reports.
    """
    sub = ExternalStageSubscriber(ENTITY)
    stage, conf = sub.current()
    assert stage is SleepStage.LIGHT
    assert conf < 0.5
    assert not sub.buffer_ready()
    assert not sub.is_stale()           # uninitialised ≠ stale


def test_observe_updates_cache_and_marks_ready() -> None:
    sub = ExternalStageSubscriber(ENTITY)
    accepted = sub.observe(ENTITY, "DEEP", attributes={"confidence": 0.85})
    assert accepted is True
    assert sub.buffer_ready()
    stage, conf = sub.current()
    assert stage is SleepStage.DEEP
    assert abs(conf - 0.85) < 1e-9


def test_observe_ignores_unrelated_entities() -> None:
    """Reuse: the caller routes *every* state_changed event through us,
    so we must drop ones for other entities silently."""
    sub = ExternalStageSubscriber(ENTITY)
    sub.observe(ENTITY, "DEEP")
    accepted = sub.observe("sensor.kitchen_temperature", 22.4)
    assert accepted is False
    assert sub.current()[0] is SleepStage.DEEP   # unchanged


def test_observe_holds_stage_on_garbage_value() -> None:
    """Critical safety: when the device flaps to ``unknown`` we keep the
    last good stage, so the controller doesn't get a phantom AWAKE."""
    sub = ExternalStageSubscriber(ENTITY)
    sub.observe(ENTITY, "DEEP", attributes={"confidence": 0.9})
    sub.observe(ENTITY, "unavailable")
    assert sub.current() == (SleepStage.DEEP, 0.9)


def test_observe_updates_confidence_only_on_real_change() -> None:
    """A no-op observe shouldn't reset confidence to 1.0."""
    sub = ExternalStageSubscriber(ENTITY)
    sub.observe(ENTITY, "DEEP", attributes={"confidence": 0.6})
    sub.observe(ENTITY, "unknown")               # held
    assert sub.current() == (SleepStage.DEEP, 0.6)


def test_staleness_reports_after_window() -> None:
    sub = ExternalStageSubscriber(ENTITY, stale_after_seconds=10)
    sub.observe(ENTITY, "LIGHT")
    assert not sub.is_stale()
    # Manually backdate the last update beyond the window.
    sub._last_update_ts = time.time() - 100
    assert sub.is_stale()


def test_status_payload_round_trips_for_publishing() -> None:
    """``status()`` is published as a diagnostic sensor; must be
    JSON-serialisable and contain the fields a user would look at."""
    sub = ExternalStageSubscriber(ENTITY)
    sub.observe(ENTITY, "REM", attributes={"confidence": 0.72})
    s = sub.status()
    assert s["current_stage"] == "REM"
    assert s["confidence"] == 0.72
    assert s["updates_received"] == 1
    assert s["stage_entity_id"] == ENTITY
    assert s["stale"] is False
    import json
    json.dumps(s)                                # must not raise


def test_drop_in_compatibility_with_legacy_engine_surface() -> None:
    """Inference loop calls ``buffer_ready`` / ``infer`` / ``push_hr`` /
    ``push_movement``.  The new subscriber must accept all of them."""
    sub = ExternalStageSubscriber(ENTITY)
    sub.push_hr(70.0)            # no-op accepted
    sub.push_movement(0.3)       # no-op accepted
    assert not sub.buffer_ready()
    sub.observe(ENTITY, "LIGHT")
    assert sub.buffer_ready()
    assert sub.infer() == sub.current()


def test_numeric_string_2_maps_to_deep() -> None:
    """Mi Band exports as the string ``"2"`` for deep sleep — common
    regression source."""
    sub = ExternalStageSubscriber(ENTITY)
    sub.observe(ENTITY, "2")
    assert sub.current()[0] is SleepStage.DEEP


def test_chinese_label_round_trip() -> None:
    """Chinese-locale ESPHome firmwares emit ``深睡`` / ``浅睡`` etc."""
    sub = ExternalStageSubscriber(ENTITY)
    sub.observe(ENTITY, "深睡")
    assert sub.current()[0] is SleepStage.DEEP
    sub.observe(ENTITY, "浅睡")
    assert sub.current()[0] is SleepStage.LIGHT
    sub.observe(ENTITY, "清醒")
    assert sub.current()[0] is SleepStage.AWAKE
