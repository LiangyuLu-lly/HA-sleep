"""End-to-end 8-hour night integration test (F1).

Drives :class:`SmartSleepService` with a synthetic stage sequence that
mimics a realistic full-night hypnogram:

    AWAKE 30 min → LIGHT 2 h → DEEP 1.5 h → REM 1 h →
    LIGHT 1.5 h → DEEP 1 h → REM 0.5 h → AWAKE 10 min

The test verifies:
- Session correctly starts and ends via the lifecycle state machine.
- quality_score lands in a reasonable range (40-90).
- The learner receives exactly 1 session.
- stage_counts are non-zero for all sleep stages.
- env_by_stage has at least 2 stage snapshots.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path
from typing import List, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.data_structures import SleepStage


# ---------------------------------------------------------------------------
# Synthetic hypnogram
# ---------------------------------------------------------------------------

def _build_stage_sequence(epoch_seconds: float = 30.0) -> List[Tuple[SleepStage, int]]:
    """Return (stage, n_epochs) pairs for a full-night hypnogram.

    Total duration: 30 + 120 + 90 + 60 + 90 + 60 + 30 + 10 = 490 min.
    """
    minutes_per_epoch = epoch_seconds / 60.0
    return [
        (SleepStage.AWAKE, int(30 / minutes_per_epoch)),
        (SleepStage.LIGHT, int(120 / minutes_per_epoch)),
        (SleepStage.DEEP, int(90 / minutes_per_epoch)),
        (SleepStage.REM, int(60 / minutes_per_epoch)),
        (SleepStage.LIGHT, int(90 / minutes_per_epoch)),
        (SleepStage.DEEP, int(60 / minutes_per_epoch)),
        (SleepStage.REM, int(30 / minutes_per_epoch)),
        (SleepStage.AWAKE, int(10 / minutes_per_epoch)),
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(tmp_path: Path) -> Path:
    from training_config.config_loader import get_default_config
    cfg = get_default_config()
    ha = cfg.setdefault("home_assistant", {})
    ha["api"] = {
        "base_url": "http://localhost:8123",
        "access_token": "test-token",
        "verify_ssl": False,
        "sleep_stage_source": "sensor.test_sleep_stage",
    }
    ha["preference_learner"] = {
        "enabled": True,
        "history_path": str(tmp_path / "user_preferences.json"),
    }
    ha["smart_control"] = {"enabled": True, "dry_run": True}
    ha["natural_sleep"] = {
        "profile_path": str(tmp_path / "user_profile.json"),
    }
    ha["session_lifecycle"] = {
        "onset_dwell_seconds": 150.0,   # 5 epochs @ 30 s
        "wake_dwell_seconds": 300.0,    # 10 epochs @ 30 s
        "min_session_minutes": 60,
    }
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def _args(config_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        config=str(config_path),
        model="models/best_model.h5",
        base_url=None, token=None, area=None,
        infer_interval=30.0,
        session_interval=86400.0,   # no checkpoint during test
        duration=None, dry_run=True,
        verbose=False,
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.integration
async def test_full_night_e2e(tmp_path: Path) -> None:
    """Drive the inference loop + session lifecycle with a synthetic night."""
    from scripts.run_ha_smart_service import SmartSleepService
    from src.external_stage_subscriber import ExternalStageSubscriber
    from src.smart_environment_controller import SmartEnvironmentController, SmartControlConfig

    cfg_path = _write_config(tmp_path)
    svc = SmartSleepService(_args(cfg_path))

    # Build a minimal engine that we'll feed stages into.
    engine = ExternalStageSubscriber(
        stage_entity_id="sensor.test_sleep_stage",
        min_stage_dwell_seconds=0.0,   # no debounce for test
    )

    # Stub controller — we only care about session lifecycle, not HA calls.
    controller = MagicMock()
    controller.apply = AsyncMock(return_value=[])
    controller.capability_stats = MagicMock(return_value={})
    controller.feedback_score = MagicMock()

    # Stub publisher so we don't need a real HA connection.
    svc.publisher = MagicMock()
    svc.publisher.publish_stage = AsyncMock()
    svc.publisher.publish_duration = AsyncMock()
    svc.publisher.publish_last_action = AsyncMock()
    svc.publisher.publish_quality = AsyncMock()
    svc.publisher.publish_quality_sub_scores = AsyncMock()
    svc.publisher.publish_wake_decision = AsyncMock()
    svc.publisher.publish_soundscape = AsyncMock()
    svc.publisher.publish_health = AsyncMock()

    # Set a fake environment so the controller has something to work with.
    svc.last_env.temperature_c = 22.0
    svc.last_env.humidity_pct = 55.0
    svc.last_env.brightness_pct = 2.0
    now = time.time()
    svc._env_ts = {
        "temperature_c": now,
        "humidity_pct": now,
        "brightness_pct": now,
    }

    # Build the stage sequence.
    epoch_seconds = 30.0
    sequence = _build_stage_sequence(epoch_seconds)

    # Drive the inference loop tick-by-tick.
    tick_count = 0
    onset_detected = False
    for stage, n_epochs in sequence:
        for _ in range(n_epochs):
            # Feed the engine so infer() returns the right stage.
            engine.observe(
                "sensor.test_sleep_stage",
                stage.name,
                attributes={"confidence": 0.92},
            )
            inferred_stage, conf = engine.infer()

            # Run the session lifecycle state machine.
            svc._maybe_advance_session_lifecycle(inferred_stage, controller)

            # Once onset is detected, back-date session_started_at so
            # the nap filter sees a realistic duration (8+ hours).
            if svc._in_session and not onset_detected:
                onset_detected = True
                # Pretend the session started 8 hours ago.
                svc.session_started_at = time.time() - 8 * 3600

            # Accumulate if in session (mirrors _task_inference_loop).
            if svc._in_session:
                svc.stage_counts[inferred_stage.name] = (
                    svc.stage_counts.get(inferred_stage.name, 0) + 1
                )
                svc.stage_sequence.append(inferred_stage)
                svc._track_per_stage_env(inferred_stage)

            tick_count += 1

    # --- Assertions ---

    # 1. Session should have started (onset after initial AWAKE → LIGHT)
    #    and ended (final AWAKE block triggers wake detection).
    #    After wake detection, _reset_session_state is called, so
    #    _in_session should be False and learner should have 1 session.
    assert svc._in_session is False, "Session should have ended"

    # 2. Learner received exactly 1 session.
    assert svc.learner is not None
    sessions = svc.learner.sessions()
    assert len(sessions) == 1, f"Expected 1 session, got {len(sessions)}"

    # 3. Quality score in reasonable range.
    score = sessions[0].quality_score
    assert 40.0 <= score <= 90.0, f"Quality score {score} out of range [40, 90]"

    # 4. stage_counts are non-zero (from the persisted session).
    sc = sessions[0].stage_counts
    assert sc.get("LIGHT", 0) > 0
    assert sc.get("DEEP", 0) > 0
    assert sc.get("REM", 0) > 0

    # 5. env_by_stage has at least 2 stage snapshots.
    ebs = sessions[0].env_by_stage
    assert len(ebs) >= 2, f"Expected >= 2 env_by_stage entries, got {len(ebs)}"
