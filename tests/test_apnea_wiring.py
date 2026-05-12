""":mod:`src.apnea_wiring` — v1.7.0 apnea glue layer.

Scope of coverage
-----------------

These tests cover the *orchestrator glue* (consent state machine,
baseline persistence, session lifecycle, sensor-state projection)
rather than the pure detection algorithm — that already has dedicated
coverage in ``tests/test_apnea_detector.py``.

The wiring's contract (see module docstring):

* Without consent → always ``pending_consent``.
* With consent but < ``calibration_nights`` of baseline → ``calibrating``.
* With consent + baseline → trend projected from the night's events.
* Consent revocation clears persisted baseline and drops back to
  ``pending_consent``.
* The breathing-rate / chest-amplitude entities are only routed to
  during an active session; events outside are silently ignored.

Failing any of these is a safety regression and should block release.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.apnea_detector import ApneaTrend, UserBaseline
from src.apnea_wiring import ApneaWiring, ApneaWiringConfig


def _make(tmp_path: Path, **kwargs) -> ApneaWiring:
    """Build a fully-configured wiring pointing at an isolated baseline
    file.  Defaults to ``enabled=True`` + a 2-night calibration window
    so tests don't need to simulate a full week."""
    cfg_kwargs = dict(
        enabled=True,
        breathing_rate_source="sensor.r60abd1_breathing_rate",
        chest_amplitude_source="sensor.r60abd1_chest_amplitude",
        consent_entity="input_boolean.apnea_consent",
        baseline_path=str(tmp_path / "baseline.json"),
        calibration_nights=2,
    )
    cfg_kwargs.update(kwargs)
    cfg = ApneaWiringConfig(**cfg_kwargs)
    return ApneaWiring(cfg)


class TestConfigFromDict:
    def test_empty_quoted_literal_treated_as_empty(self) -> None:
        cfg = ApneaWiringConfig.from_dict({
            "breathing_rate_source": '""',
            "consent_entity": "input_boolean.apnea_consent",
        })
        # Literal ``""`` is a common artefact of HA Configuration UI
        # serialisation; it must not fool downstream equality checks.
        assert cfg.breathing_rate_source == ""
        assert cfg.consent_entity == "input_boolean.apnea_consent"

    def test_unknown_keys_ignored(self) -> None:
        cfg = ApneaWiringConfig.from_dict({
            "enabled": True,
            "wacky_future_knob": 42,
        })
        assert cfg.enabled is True
        assert not hasattr(cfg, "wacky_future_knob")


class TestConsentGating:
    def test_disabled_always_pending_consent(self, tmp_path: Path) -> None:
        wiring = _make(tmp_path, enabled=False)
        assert wiring.current_trend_now() == ApneaTrend.PENDING_CONSENT

    def test_enabled_no_consent_stays_pending(self, tmp_path: Path) -> None:
        wiring = _make(tmp_path)
        # Baseline exists but consent not toggled → sensor must NOT
        # leak a trend, even with data.
        wiring._baseline = UserBaseline(
            rate_bpm_median=15.0, amplitude_median=0.8,
            nights_observed=7,
        )
        assert wiring.current_trend_now() == ApneaTrend.PENDING_CONSENT

    def test_consent_toggle_logs_and_flips_flag(self, tmp_path: Path) -> None:
        wiring = _make(tmp_path)
        claimed = wiring.on_state_change(
            "input_boolean.apnea_consent", "on",
        )
        assert claimed
        assert wiring._consent is True

    def test_consent_revocation_clears_baseline(self, tmp_path: Path) -> None:
        wiring = _make(tmp_path)
        # Pre-populate baseline + consent.
        wiring._consent = True
        wiring._baseline = UserBaseline(
            rate_bpm_median=14.0, amplitude_median=0.9,
            nights_observed=7,
        )
        wiring._store.save(wiring._baseline)
        assert Path(wiring.cfg.baseline_path).exists()

        # Now revoke.
        wiring.on_state_change("input_boolean.apnea_consent", "off")
        assert wiring._consent is False
        assert wiring._baseline is None
        # File must be removed too — no latent data after revocation.
        assert not Path(wiring.cfg.baseline_path).exists()


class TestBaselinePersistence:
    def test_load_round_trip(self, tmp_path: Path) -> None:
        wiring1 = _make(tmp_path)
        baseline = UserBaseline(
            rate_bpm_median=15.3, amplitude_median=0.72,
            nights_observed=3,
        )
        wiring1._store.save(baseline)

        wiring2 = _make(tmp_path)
        assert wiring2._baseline is not None
        assert wiring2._baseline.rate_bpm_median == pytest.approx(15.3)
        assert wiring2._baseline.nights_observed == 3

    def test_corrupt_file_recovers_to_none(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        path.write_text("{ not valid json", encoding="utf-8")
        wiring = _make(tmp_path)
        # Must NOT crash; just starts from scratch.
        assert wiring._baseline is None

    def test_wrong_schema_recovers_to_none(self, tmp_path: Path) -> None:
        path = tmp_path / "baseline.json"
        path.write_text(
            json.dumps({"unexpected": "shape"}), encoding="utf-8",
        )
        wiring = _make(tmp_path)
        assert wiring._baseline is None


class TestSessionLifecycle:
    def test_route_rate_ignored_outside_session(self, tmp_path: Path) -> None:
        wiring = _make(tmp_path)
        # No begin_session called yet.
        claimed = wiring.on_state_change(
            "sensor.r60abd1_breathing_rate", "15.0",
            numeric_value=15.0,
        )
        assert claimed is False

    def test_rate_routed_into_live_buffer(self, tmp_path: Path) -> None:
        wiring = _make(tmp_path)
        wiring.begin_session()
        claimed = wiring.on_state_change(
            "sensor.r60abd1_breathing_rate", "14.0",
            numeric_value=14.0,
        )
        assert claimed is True
        assert wiring._session is not None
        assert wiring._session.last_rate == 14.0

    def test_amplitude_routed_into_live_buffer(self, tmp_path: Path) -> None:
        wiring = _make(tmp_path)
        wiring.begin_session()
        wiring.on_state_change(
            "sensor.r60abd1_chest_amplitude", "0.85",
            numeric_value=0.85,
        )
        assert wiring._session.last_amplitude == 0.85

    def test_tick_snapshots_current_values(self, tmp_path: Path) -> None:
        wiring = _make(tmp_path)
        wiring.begin_session()
        wiring.on_state_change(
            "sensor.r60abd1_breathing_rate", "15.0",
            numeric_value=15.0,
        )
        wiring.on_state_change(
            "sensor.r60abd1_chest_amplitude", "0.75",
            numeric_value=0.75,
        )
        wiring.tick(now=100.0)
        wiring.tick(now=130.0)
        assert len(wiring._session.samples) == 2
        assert wiring._session.samples[0].rate_bpm == 15.0
        assert wiring._session.samples[1].amplitude == 0.75


class TestEndSession:
    def test_end_with_no_session_returns_current_trend(
        self, tmp_path: Path,
    ) -> None:
        wiring = _make(tmp_path)
        # No begin_session.  Should produce pending_consent (no consent).
        result = wiring.end_session()
        assert result == ApneaTrend.PENDING_CONSENT

    def test_calibration_progresses_across_nights(
        self, tmp_path: Path,
    ) -> None:
        wiring = _make(tmp_path, calibration_nights=2)
        wiring._consent = True

        # Night 1: collect enough samples to seed a baseline.
        wiring.begin_session()
        for i in range(10):
            wiring.on_state_change(
                "sensor.r60abd1_breathing_rate", "15.0",
                numeric_value=15.0,
            )
            wiring.on_state_change(
                "sensor.r60abd1_chest_amplitude", "0.8",
                numeric_value=0.8,
            )
            wiring.tick(now=float(i * 30))
        trend_after_night1 = wiring.end_session()
        # Only 1 night observed vs 2 required → still calibrating.
        assert trend_after_night1 == ApneaTrend.CALIBRATING
        assert wiring._baseline is not None
        assert wiring._baseline.nights_observed == 1

        # Night 2: complete calibration.
        wiring.begin_session()
        for i in range(10):
            wiring.on_state_change(
                "sensor.r60abd1_breathing_rate", "15.5",
                numeric_value=15.5,
            )
            wiring.on_state_change(
                "sensor.r60abd1_chest_amplitude", "0.78",
                numeric_value=0.78,
            )
            wiring.tick(now=float(86400 + i * 30))
        trend_after_night2 = wiring.end_session()
        # Calibration done (2 nights), baseline settled, trend should
        # be GREEN (no events detected at steady breathing).
        assert wiring._baseline.nights_observed == 2
        assert trend_after_night2 == ApneaTrend.GREEN


class TestStatus:
    def test_status_never_exposes_events(self, tmp_path: Path) -> None:
        """Product invariant — the status dict MUST NOT contain numeric
        event counts / AHI.  This is checked explicitly so a refactor
        can't accidentally regress the medical-safety contract."""
        wiring = _make(tmp_path)
        wiring._consent = True
        wiring._baseline = UserBaseline(
            rate_bpm_median=15.0, amplitude_median=0.8,
            nights_observed=7,
        )
        status = wiring.status()
        forbidden = {
            "ahi", "events_per_hour", "events", "event_count",
            "apnea_count", "hypopnea_count",
        }
        assert forbidden.isdisjoint(status.keys()), (
            f"Status must not leak clinical numbers; found: "
            f"{forbidden & status.keys()}"
        )

    def test_status_reflects_calibration_progress(
        self, tmp_path: Path,
    ) -> None:
        wiring = _make(tmp_path, calibration_nights=7)
        wiring._consent = True
        wiring._baseline = UserBaseline(
            rate_bpm_median=15.0, amplitude_median=0.8,
            nights_observed=3,
        )
        status = wiring.status()
        assert status["consent"] is True
        assert status["calibration_nights_required"] == 7
        assert status["calibration_nights_completed"] == 3
