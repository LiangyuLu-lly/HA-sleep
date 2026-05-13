"""Render the Sleep Classifier add-on's effective config.json.

Split out of ``run.sh`` in v2.0.2 because heredoc-embedded Python is
fragile when user-supplied option values contain quotes, backslashes,
or non-ASCII text (frequent: bedroom entity names in Chinese).  Now
``run.sh`` exports every option as an ``SC_*`` env var and we read
them cleanly through :func:`os.environ.get` — no shell-level string
interpolation anywhere.

The output is written to ``/data/effective_config.json`` and consumed
by ``scripts/run_ha_smart_service.py`` at startup.  It merges:

1. The bundled defaults at ``/app/training_config/config.json``.
2. The user's entries from the Configuration tab (``SC_*`` env vars).
3. The Web UI's entity picker overrides at
   ``/data/web_ui_overrides.json`` — highest priority because those
   values came from HA's live state dump so they're guaranteed real.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List


_DEFAULTS_PATH = Path("/app/training_config/config.json")
_OPTIONS_PATH = Path("/data/options.json")
_OVERRIDES_PATH = Path("/data/web_ui_overrides.json")
_OUT_PATH = Path("/data/effective_config.json")


def _norm(value: Any) -> str:
    """Treat the literal ``""`` / ``''`` placeholder as an empty string.

    Users frequently paste the example text verbatim into the
    Configuration form, which gives us a 2-char string we must not
    forward to HA or we'll try to subscribe to ``"\\""`` as an
    entity_id.
    """
    if value is None:
        return ""
    v = str(value).strip()
    if v in ('""', "''"):
        return ""
    return v


def _csv_to_list(s: str) -> List[str]:
    return [v for v in (_norm(x) for x in s.split(",")) if v]


def _one(value: Any) -> List[str]:
    """Wrap a non-empty entity_id into a single-element list."""
    v = _norm(value)
    return [v] if v else []


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _env_float(key: str, default: float) -> float:
    raw = _env(key, str(default))
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(key: str, default: int) -> int:
    raw = _env(key, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def main() -> None:
    # --- Load defaults + overrides ----------------------------------
    try:
        base: Dict[str, Any] = json.loads(
            _DEFAULTS_PATH.read_text(encoding="utf-8"),
        )
    except FileNotFoundError:
        # Defensive: fall back to a minimal skeleton if someone moved
        # the defaults around.  The service is still usable without a
        # bundled defaults file — every option we care about has a
        # dataclass default in ``src/``.
        base = {"home_assistant": {}}

    overrides: Dict[str, Any] = {}
    if _OVERRIDES_PATH.is_file():
        try:
            overrides = json.loads(_OVERRIDES_PATH.read_text(encoding="utf-8"))
            print(f"[run.sh] applied {_OVERRIDES_PATH} (web UI picks)")
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[run.sh] WARN: could not parse {_OVERRIDES_PATH}: {exc}")

    ha = base.setdefault("home_assistant", {})
    api = ha.setdefault("api", {})

    # Supervisor proxy auth — no user token needed.
    api["base_url"] = "http://supervisor/core"
    api["access_token"] = os.environ.get("SUPERVISOR_TOKEN", "")
    api["verify_ssl"] = False
    api["area_filter"] = _norm(_env("SC_AREA"))

    # Auto-discovery tunables (CSV-joined in run.sh).
    domains = _csv_to_list(_env("SC_DOMAINS"))
    inc = _csv_to_list(_env("SC_INCLUDES"))
    exc = _csv_to_list(_env("SC_EXCLUDES"))
    if domains:
        api["controllable_domains"] = domains
    if inc:
        api["explicit_includes"] = inc
    if exc:
        api["explicit_excludes"] = exc

    # Sleep-stage entity (mandatory for live mode).
    stage = _norm(_env("SC_STAGE_SOURCE"))
    if "sleep_stage_source" in overrides:
        stage = _norm(overrides["sleep_stage_source"]) or stage
    if stage:
        api["sleep_stage_source"] = stage

    # --- Slot bindings ----------------------------------------------
    def _slot_single(override_key: str, env_key: str) -> List[str]:
        if override_key in overrides:
            v = _norm(overrides[override_key])
            return [v] if v else []
        return _one(_env(env_key))

    def _slot_multi(override_key: str, env_key: str) -> List[str]:
        if override_key in overrides:
            raw = overrides[override_key]
            if isinstance(raw, list):
                return [v for v in (_norm(x) for x in raw) if v]
            v = _norm(raw)
            return [v] if v else []
        return _csv_to_list(_env(env_key))

    slot_bindings = {
        "temperature":    _slot_single("temperature_source",  "SC_TEMP_SOURCE"),
        "humidity":       _slot_single("humidity_source",     "SC_HUM_SOURCE"),
        "illuminance":    _slot_single("illuminance_source",  "SC_LUX_SOURCE"),
        "lights":         _slot_multi("light_targets",         "SC_LIGHT_TARGETS"),
        "climates":       _slot_single("climate_target",       "SC_CLIMATE_TARGET"),
        "humidifiers":    _slot_single("humidifier_target",    "SC_HUMIDIFIER_TARGET"),
        "fans":           _slot_single("fan_target",           "SC_FAN_TARGET"),
        "switches":       _slot_multi("switch_targets",        "SC_SWITCH_TARGETS"),
    }
    api["slot_bindings"] = {k: v for k, v in slot_bindings.items() if v}

    # --- Smart control ----------------------------------------------
    sc = ha.setdefault("smart_control", {})
    sc["dry_run"] = _env("SC_DRY_RUN").lower() == "true"
    sc["min_seconds_between_actions"] = _env_int("SC_MIN_SECONDS_BETWEEN_ACTIONS", 120)
    sc["deadband_temperature_c"] = _env_float("SC_DEADBAND_T", 0.5)
    sc["deadband_humidity_pct"] = _env_float("SC_DEADBAND_H", 5.0)
    sc["deadband_brightness_pct"] = _env_float("SC_DEADBAND_B", 10.0)
    sc["wind_down_minutes"] = _env_int("SC_WIND_DOWN_MINUTES", 30)
    sc["min_stage_dwell_seconds"] = _env_float("SC_MIN_STAGE_DWELL_SECONDS", 60.0)

    # --- Preference learner -----------------------------------------
    learner = ha.setdefault("preference_learner", {})
    learner["exploration_rate"] = _env_float("SC_EXPLORATION_RATE", 0.1)
    learner["history_path"] = "/data/user_preferences.json"

    # --- Natural-sleep block (v1.2.0) -------------------------------
    # Parse track overrides ``"pink_noise=URL;rain=URL"`` → dict.
    track_overrides: Dict[str, str] = {}
    for item in _env("SC_WHITENOISE_TRACK_OVERRIDES").split(";"):
        item = item.strip()
        if not item or "=" not in item:
            continue
        k, _, v = item.partition("=")
        track_overrides[k.strip()] = v.strip()

    def _override_or(key: str, fallback: str) -> str:
        if key in overrides:
            v = _norm(overrides[key])
            if v:
                return v
        return fallback

    if isinstance(overrides.get("wake_light_targets"), list):
        wake_lights = [
            v for v in (_norm(x) for x in overrides["wake_light_targets"]) if v
        ]
    else:
        wake_lights = _csv_to_list(_env("SC_WAKE_LIGHTS"))

    natural: Dict[str, Any] = {
        "user_id": "default",
        "chronotype": _norm(_env("SC_CHRONOTYPE")) or "neutral",
        "wake_window_start": _norm(_env("SC_WAKE_START")),
        "wake_window_end": _norm(_env("SC_WAKE_END")),
        "wake_light_targets": wake_lights,
        "whitenoise_target": _override_or("whitenoise_target",
                                          _norm(_env("SC_WHITENOISE_TARGET"))),
        "whitenoise_volume_scale": _env_float("SC_WHITENOISE_VOLUME_SCALE", 1.0),
        "whitenoise_track_overrides": track_overrides,
        "whitenoise_volume_feedback_entity": _override_or(
            "whitenoise_volume_feedback_entity",
            _norm(_env("SC_WHITENOISE_VOLUME_FEEDBACK_ENTITY")),
        ),
        "feedback_entity": _override_or("feedback_entity",
                                        _norm(_env("SC_FEEDBACK_ENTITY"))),
        "feedback_scale": _env_int("SC_FEEDBACK_SCALE", 5),
    }
    by = _env_int("SC_BIRTH_YEAR", 0)
    if by > 0:
        natural["birth_year"] = by
    # Drop empty strings / empty lists so the downstream ``or`` short-
    # circuits correctly.
    natural = {k: v for k, v in natural.items() if v not in ("", [], {})}
    ha["natural_sleep"] = natural

    # --- Apnea wiring block (v1.7.0) --------------------------------
    apnea_rate = _norm(_env("SC_APNEA_RATE"))
    apnea_amplitude = _norm(_env("SC_APNEA_AMPLITUDE"))
    apnea_consent = _norm(_env("SC_APNEA_CONSENT")) or \
        "input_boolean.sleep_classifier_apnea_consent"
    ha["apnea"] = {
        "enabled": bool(apnea_rate),
        "breathing_rate_source": apnea_rate,
        "chest_amplitude_source": apnea_amplitude,
        "consent_entity": apnea_consent,
        "baseline_path": "/data/apnea_baseline.json",
        "calibration_nights": _env_int("SC_APNEA_CALIBRATION_NIGHTS", 7),
    }

    # --- Write the effective config ---------------------------------
    _OUT_PATH.write_text(
        json.dumps(base, indent=2, ensure_ascii=False), encoding="utf-8",
    )
    bound = sum(1 for v in api["slot_bindings"].values() if v)
    print(f"[run.sh] effective config written to {_OUT_PATH}")
    print(f"[run.sh] slot bindings active: {bound} role(s)")
    if stage:
        print(f"[run.sh] sleep_stage_source = {stage}")
    else:
        print(
            "[run.sh] sleep_stage_source is UNBOUND — Web UI will stay "
            "available; smart service won't start until you pick one."
        )


if __name__ == "__main__":
    main()
