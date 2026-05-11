"""Tests for :mod:`src.whitenoise_matcher` — soundscape policy."""
from __future__ import annotations

from datetime import datetime

import pytest

from src.data_structures import SleepStage
from src.whitenoise_matcher import (
    DEFAULT_TRACKS,
    Soundscape,
    WhiteNoiseMatcher,
)


# ---------------------------------------------------------------------------
# Default policy
# ---------------------------------------------------------------------------


class TestDefaultPolicy:
    def test_deep_uses_brown_noise(self) -> None:
        m = WhiteNoiseMatcher()
        p = m.policy_for(SleepStage.DEEP)
        assert p.soundscape == Soundscape.BROWN_NOISE
        assert p.volume_pct > 0

    def test_rem_silences_audio(self) -> None:
        """Massar 2024: audible noise during REM fragments dreams."""
        m = WhiteNoiseMatcher()
        p = m.policy_for(SleepStage.REM)
        assert p.soundscape == Soundscape.OFF
        assert p.volume_pct == 0.0

    def test_light_uses_pink_noise(self) -> None:
        m = WhiteNoiseMatcher()
        p = m.policy_for(SleepStage.LIGHT)
        assert p.soundscape == Soundscape.PINK_NOISE

    def test_awake_uses_relaxing_nature(self) -> None:
        m = WhiteNoiseMatcher()
        p = m.policy_for(SleepStage.AWAKE)
        assert p.soundscape in (Soundscape.RAIN, Soundscape.OCEAN)


# ---------------------------------------------------------------------------
# Customisation
# ---------------------------------------------------------------------------


class TestUserOverrides:
    def test_string_override(self) -> None:
        m = WhiteNoiseMatcher(user_overrides={"DEEP": "ocean"})
        assert m.policy_for(SleepStage.DEEP).soundscape == Soundscape.OCEAN

    def test_dict_override_with_volume(self) -> None:
        m = WhiteNoiseMatcher(user_overrides={
            "DEEP": {"soundscape": "white_noise", "volume_pct": 50}
        })
        p = m.policy_for(SleepStage.DEEP)
        assert p.soundscape == Soundscape.WHITE_NOISE
        assert p.volume_pct == 50.0

    def test_unknown_stage_warns(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING"):
            WhiteNoiseMatcher(user_overrides={"BANANA": "ocean"})
        assert any("BANANA" in r.message or "Unknown stage" in r.message
                   for r in caplog.records)

    def test_unknown_soundscape_warns(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        with caplog.at_level("WARNING"):
            WhiteNoiseMatcher(user_overrides={"DEEP": "metal"})
        assert any("metal" in r.message.lower() for r in caplog.records)

    def test_volume_scale_clamped(self) -> None:
        m = WhiteNoiseMatcher(volume_scale=10.0)   # clamps to 2.0
        p = m.policy_for(SleepStage.DEEP)
        # Default brown_noise volume is 18 → 2 * 18 = 36 (not 180).
        assert p.volume_pct <= 100.0

    def test_track_override(self) -> None:
        m = WhiteNoiseMatcher(track_overrides={"pink_noise": "spotify:track:abc"})
        assert m.media_url(Soundscape.PINK_NOISE) == "spotify:track:abc"


# ---------------------------------------------------------------------------
# Pre-wake hook
# ---------------------------------------------------------------------------


class TestPreWakeHook:
    def test_pre_wake_returns_dawn_chorus(self) -> None:
        triggered = {"value": False}

        def is_pre_wake(now: datetime) -> bool:
            return triggered["value"]

        m = WhiteNoiseMatcher(is_pre_wake=is_pre_wake)
        ref = datetime(2026, 5, 12, 6, 50)
        # Before wake window: normal stage policy applies.
        assert m.policy_for(SleepStage.DEEP, now=ref).soundscape != Soundscape.DAWN_CHORUS
        # Once we're in the pre-wake ramp, dawn chorus takes over.
        triggered["value"] = True
        assert m.policy_for(SleepStage.DEEP, now=ref).soundscape == Soundscape.DAWN_CHORUS

    def test_callback_failure_does_not_raise(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        def boom(_now: datetime) -> bool:
            raise RuntimeError("boom")
        m = WhiteNoiseMatcher(is_pre_wake=boom)
        with caplog.at_level("ERROR"):
            # Must fall back to stage policy gracefully.
            p = m.policy_for(SleepStage.DEEP, now=datetime(2026, 5, 12, 7, 0))
        assert p.soundscape == Soundscape.BROWN_NOISE


# ---------------------------------------------------------------------------
# Confidence handling
# ---------------------------------------------------------------------------


def test_low_confidence_attenuates_volume() -> None:
    m = WhiteNoiseMatcher()
    full = m.policy_for(SleepStage.DEEP, confidence=0.9).volume_pct
    soft = m.policy_for(SleepStage.DEEP, confidence=0.3).volume_pct
    assert soft < full


# ---------------------------------------------------------------------------
# Default tracks completeness
# ---------------------------------------------------------------------------


def test_every_audible_soundscape_has_default_track() -> None:
    audible = [s for s in Soundscape if s != Soundscape.OFF]
    for s in audible:
        assert s in DEFAULT_TRACKS, s
