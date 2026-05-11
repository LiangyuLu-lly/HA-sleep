"""Match a soundscape to the user's current sleep stage.

Why
---
The literature is unanimous that *steady* low-frequency-rich noise
during deep sleep enlarges slow-wave activity and improves memory
consolidation, while during sleep onset the **type** of noise matters
more than the level (most users find rain or wind more relaxing than
pure white noise; Stanchina 2005).  When the user is in REM, *any*
audible noise tends to fragment dreams (Massar 2024 review), so we
either fade out or use ultra-low pink noise.  At final wake we want a
gentle natural-sound "dawn chorus" that supports cortisol awakening.

This module owns the **policy** mapping ``stage → soundscape`` and
optional volume; the actual playback is delegated to HA via
``media_player.play_media`` so the user can pick whatever speaker fits
(Sonos, Google, Apple HomePod, an MQTT-bridged BT speaker, etc.).

Soundscape catalog (URL-keyed, user-overridable)
------------------------------------------------
Defaults are royalty-free assets bundled with the add-on under
``rootfs/share/sleep_classifier/sounds/`` (so HA can serve them from
``http://supervisor/share/sleep_classifier/sounds/<file>``).  Users may
substitute any URL or media-source URI in the Configuration tab.

References
~~~~~~~~~~
* Papalambros NA et al. **Acoustic Enhancement of Sleep Slow
  Oscillations and Concomitant Memory Improvement in Older Adults**,
  *Front Hum Neurosci* 11 (2017) 109.  Pink-noise pulses during
  N3 → ↑ SWA, ↑ memory.
* Stanchina ML et al. **The influence of white noise on sleep in
  subjects exposed to ICU noise**, *Sleep Med* 6 (2005) 423-428.
* Mart​íns DF et al. **Effects of white noise on sleep onset
  latency in adult patients with insomnia**, *Sleep Med Rev* 64
  (2022) 101647.
* Riedy SM et al. **Noise as a sleep aid: a systematic review**,
  *Sleep Med Rev* 55 (2021) 101385.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from src.data_structures import SleepStage

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Soundscape catalog
# ---------------------------------------------------------------------------


class Soundscape(str, Enum):
    """Identifiers used as keys into :data:`DEFAULT_TRACKS`.

    Naming intentionally mirrors what users tend to type into Sonos
    search ("brown noise", "rain", "ocean") so the default tracks feel
    intuitive in the Configuration tab.
    """

    OFF = "off"
    PINK_NOISE = "pink_noise"      # broadband, slope -3 dB/oct
    BROWN_NOISE = "brown_noise"    # slope -6 dB/oct, low-frequency rich
    WHITE_NOISE = "white_noise"    # flat
    RAIN = "rain"
    WIND = "wind"
    OCEAN = "ocean"
    DAWN_CHORUS = "dawn_chorus"    # gentle birdsong + soft bells


# Default URL/URI per soundscape.
#
# Why these are *empty by default*
# --------------------------------
# Earlier versions shipped hard-coded ``/share/sleep_classifier/sounds/*.mp3``
# paths, but the add-on does not bundle audio assets — that would have
# made the image 50+ MB larger and put us into licensing territory for
# every track.  Pointing at non-existent files just produces silent
# ``media_player`` 404s in production.
#
# We therefore ship an empty catalogue and surface it to the user as a
# **required Configuration field** if they enable the soundscape feature.
# The user supplies their own URLs via ``track_overrides`` — typical
# values:
#
#   * ``media-source://media_source/local/sleep/pink.mp3``
#     (file dropped into HA's "media" folder)
#   * ``http://192.168.1.10/sounds/rain.mp3``
#     (hosted on a NAS or local web server)
#   * ``spotify:track:0VjIjW4GlUZAMYd2vXMi3b``
#     (Spotify integration installed)
#   * ``https://www.soundjay.com/.../pink-noise-1.mp3``
#     (any public CC-licensed source)
#
# When a soundscape has no URL, the matcher silently skips
# ``play_media`` (it still publishes the policy to HA Lovelace so the
# user knows it *would* have switched).
DEFAULT_TRACKS: Dict[Soundscape, str] = {}


# ---------------------------------------------------------------------------
# Stage → policy
# ---------------------------------------------------------------------------


@dataclass
class SoundscapePolicy:
    """The chosen soundscape + volume for a given moment.

    ``volume_pct`` is in 0-100 and the caller is responsible for
    converting to whatever scale the user's ``media_player`` accepts
    (HA usually maps to 0.0-1.0 internally).
    """

    soundscape: Soundscape
    volume_pct: float
    fade_seconds: float = 5.0
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "soundscape": self.soundscape.value,
            "volume_pct": round(self.volume_pct, 1),
            "fade_seconds": round(self.fade_seconds, 1),
            "reason": self.reason,
        }


# Stage policy derived from the references in the module docstring.
# Volumes are conservative; users can scale globally with ``volume_scale``.
_STAGE_POLICY: Dict[SleepStage, SoundscapePolicy] = {
    # AWAKE before sleep onset: relaxing nature sound at 30 % to mask
    # bedroom noise without drawing attention.  Riedy 2021 review.
    SleepStage.AWAKE: SoundscapePolicy(
        soundscape=Soundscape.RAIN,
        volume_pct=30.0,
        fade_seconds=10.0,
        reason="pre-sleep masking",
    ),
    # LIGHT (N1-N2): pink noise at moderate volume.  Mart​íns 2022
    # showed reduced SOL with continuous pink/brown noise during N1.
    SleepStage.LIGHT: SoundscapePolicy(
        soundscape=Soundscape.PINK_NOISE,
        volume_pct=22.0,
        fade_seconds=15.0,
        reason="onset / light-sleep masking",
    ),
    # DEEP (N3): brown noise — low-frequency-rich, doesn't mask the
    # body's slow-wave entrainment.  Papalambros 2017 used auditory
    # pulses but a continuous low-noise floor is the consumer-grade
    # equivalent and what we ship by default.
    SleepStage.DEEP: SoundscapePolicy(
        soundscape=Soundscape.BROWN_NOISE,
        volume_pct=18.0,
        fade_seconds=20.0,
        reason="deep sleep / SWA support",
    ),
    # REM: noise during REM fragments dreams (Massar 2024).  Either
    # fade entirely or drop to a barely-audible continuous tone.
    SleepStage.REM: SoundscapePolicy(
        soundscape=Soundscape.OFF,
        volume_pct=0.0,
        fade_seconds=20.0,
        reason="REM — silence to preserve dreams",
    ),
}


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------


class WhiteNoiseMatcher:
    """Stateless mapper from inference output to a :class:`SoundscapePolicy`.

    Stateless because the actual *playback state* lives in HA
    (``media_player`` entity); this class only computes "what should we
    be playing right now", and the caller decides whether to issue a
    state-change.

    Customisation hooks:

    * ``user_overrides`` — Configuration-supplied dict mapping a stage
      name to either a :class:`Soundscape` value (string) or a full
      override dict ``{"soundscape": ..., "volume_pct": ...}``.  This
      is what the add-on ``config.yaml`` exposes.
    * ``volume_scale`` — multiplier applied on top of the per-stage
      default, so the user can globally turn the system louder /
      quieter without rewriting every policy entry.
    * ``track_overrides`` — per-soundscape URL override (e.g. swap the
      default pink_noise.mp3 for a Spotify URI).
    """

    def __init__(
        self,
        *,
        media_player_entity: Optional[str] = None,
        user_overrides: Optional[Dict[str, Any]] = None,
        volume_scale: float = 1.0,
        track_overrides: Optional[Dict[str, str]] = None,
        is_pre_wake: Optional[Any] = None,    # callable(now) -> bool
    ) -> None:
        self.media_player_entity = media_player_entity
        self.volume_scale = float(max(0.0, min(2.0, volume_scale)))
        self._stage_overrides = self._normalise_user_overrides(user_overrides or {})
        self._tracks: Dict[Soundscape, str] = dict(DEFAULT_TRACKS)
        if track_overrides:
            for k, v in track_overrides.items():
                try:
                    self._tracks[Soundscape(k)] = str(v)
                except ValueError:
                    logger.warning("Unknown soundscape key %r in track_overrides", k)
        self._is_pre_wake = is_pre_wake

    # ------------------------------------------------------------------ #
    # Policy resolution
    # ------------------------------------------------------------------ #

    def policy_for(
        self,
        stage: SleepStage,
        confidence: float = 1.0,
        *,
        now: Any = None,
    ) -> SoundscapePolicy:
        """Return the policy that should be active for ``stage``.

        ``confidence`` lets the caller signal "we're not sure"; below
        0.5 we keep the *previous* policy (handled by the caller via
        debouncing) — here we only attenuate volume by 20 % to soften
        any possible misclassification artefacts.

        ``now`` is forwarded to ``is_pre_wake`` so the morning dawn
        chorus can be selected during the light-ramp window even if
        the user is still classified as DEEP/LIGHT.
        """
        # Pre-wake override: dawn chorus from light-ramp start onward,
        # regardless of the inferred stage.  Geerdink 2016 shows even
        # short light + sound bursts pre-wake reduce inertia.
        if self._is_pre_wake is not None and now is not None:
            try:
                if self._is_pre_wake(now):
                    return self._scale(SoundscapePolicy(
                        soundscape=Soundscape.DAWN_CHORUS,
                        volume_pct=35.0,
                        fade_seconds=60.0,
                        reason="pre-wake dawn chorus",
                    ))
            except Exception:    # noqa: BLE001
                logger.exception("is_pre_wake callback raised; ignoring")

        base = self._stage_overrides.get(stage) or _STAGE_POLICY.get(
            stage,
            SoundscapePolicy(
                soundscape=Soundscape.OFF, volume_pct=0.0, reason="unknown stage",
            ),
        )
        if confidence < 0.5:
            base = SoundscapePolicy(
                soundscape=base.soundscape,
                volume_pct=base.volume_pct * 0.8,
                fade_seconds=base.fade_seconds,
                reason=base.reason + " (low confidence)",
            )
        return self._scale(base)

    def media_url(self, soundscape: Soundscape) -> Optional[str]:
        """Return the resolved URL/URI for a given soundscape, if any."""
        return self._tracks.get(soundscape)

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _scale(self, policy: SoundscapePolicy) -> SoundscapePolicy:
        return SoundscapePolicy(
            soundscape=policy.soundscape,
            volume_pct=max(0.0, min(100.0, policy.volume_pct * self.volume_scale)),
            fade_seconds=policy.fade_seconds,
            reason=policy.reason,
        )

    def _normalise_user_overrides(
        self, raw: Dict[str, Any],
    ) -> Dict[SleepStage, SoundscapePolicy]:
        """Coerce ``{"DEEP": "rain"}`` or ``{"DEEP": {"soundscape": "rain"}}``
        into a usable :class:`SoundscapePolicy` map.
        """
        out: Dict[SleepStage, SoundscapePolicy] = {}
        for k, v in raw.items():
            try:
                stage = SleepStage[k.upper()]
            except KeyError:
                logger.warning("Unknown stage %r in whitenoise overrides", k)
                continue
            if isinstance(v, str):
                try:
                    sc = Soundscape(v)
                except ValueError:
                    logger.warning("Unknown soundscape %r for stage %s", v, k)
                    continue
                base = _STAGE_POLICY.get(stage)
                if base is None:
                    continue
                out[stage] = SoundscapePolicy(
                    soundscape=sc,
                    volume_pct=base.volume_pct,
                    fade_seconds=base.fade_seconds,
                    reason="user override",
                )
            elif isinstance(v, dict):
                try:
                    sc = Soundscape(v.get("soundscape", "off"))
                except ValueError:
                    sc = Soundscape.OFF
                out[stage] = SoundscapePolicy(
                    soundscape=sc,
                    volume_pct=float(v.get("volume_pct", 25.0)),
                    fade_seconds=float(v.get("fade_seconds", 10.0)),
                    reason="user override",
                )
            else:
                logger.warning(
                    "Whitenoise override for %s must be str or dict, got %r",
                    k, type(v),
                )
        return out
