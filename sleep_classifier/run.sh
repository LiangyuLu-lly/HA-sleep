#!/usr/bin/env bash
# Entrypoint for the Sleep Classifier add-on.
#
# Reads user-supplied options from /data/options.json (populated by the HA
# supervisor from the add-on Configuration UI), then exec's the smart service
# with the matching CLI flags.
#
# The supervisor injects SUPERVISOR_TOKEN; we use the Supervisor's HA core
# proxy URL (http://supervisor/core) so the user never deals with a token.
set -euo pipefail

# ── Helpers ─────────────────────────────────────────────────────────────────
opt() { jq -r "$1" /data/options.json; }
opt_array_to_csv() { jq -r "$1 | join(\",\")" /data/options.json; }

# ── Read user options ───────────────────────────────────────────────────────
# General behaviour
AREA=$(opt '.area // ""')
INFER_INTERVAL=$(opt '.infer_interval // 30')
SESSION_INTERVAL=$(opt '.session_interval // 1800')
DRY_RUN=$(opt '.dry_run // false')
EXPLORATION_RATE=$(opt '.exploration_rate // 0.1')
MIN_SECONDS_BETWEEN_ACTIONS=$(opt '.min_seconds_between_actions // 120')
DEADBAND_T=$(opt '.deadband_temperature_c // 0.5')
DEADBAND_H=$(opt '.deadband_humidity_pct // 5')
DEADBAND_B=$(opt '.deadband_brightness_pct // 10')
LOG_LEVEL=$(opt '.log_level // "info"')

# Slot bindings — sensors
# v1.3.0: ``sleep_stage_source`` is the single mandatory binding.  The
# old HR / movement / breathing slots are gone — stage now comes from an
# external tracker (Mi Band, Apple Watch, R60ABD1, ...) via HA.
STAGE_SOURCE=$(opt '.sleep_stage_source // ""')
TEMP_SOURCE=$(opt '.temperature_source // ""')
HUM_SOURCE=$(opt '.humidity_source // ""')
LUX_SOURCE=$(opt '.illuminance_source // ""')

# Slot bindings — actionable devices (lists OR single entity_id)
LIGHT_TARGETS=$(opt_array_to_csv '.light_targets // []')
CLIMATE_TARGET=$(opt '.climate_target // ""')
HUMIDIFIER_TARGET=$(opt '.humidifier_target // ""')
FAN_TARGET=$(opt '.fan_target // ""')
SWITCH_TARGETS=$(opt_array_to_csv '.switch_targets // []')

# Auto-discovery tunables
DOMAINS=$(opt_array_to_csv '.controllable_domains // []')
INCLUDES=$(opt_array_to_csv '.explicit_includes // []')
EXCLUDES=$(opt_array_to_csv '.explicit_excludes // []')

# Natural-sleep suite (v1.2.0)
BIRTH_YEAR=$(opt '.birth_year // 0')
CHRONOTYPE=$(opt '.chronotype // "neutral"')
WAKE_START=$(opt '.wake_window_start // ""')
WAKE_END=$(opt '.wake_window_end // ""')
WAKE_LIGHTS=$(opt_array_to_csv '.wake_light_targets // []')
WHITENOISE_TARGET=$(opt '.whitenoise_target // ""')
WHITENOISE_VOLUME_SCALE=$(opt '.whitenoise_volume_scale // 1.0')
# `key=value` pairs, joined with `;` so we can survive a shell var.
WHITENOISE_TRACK_OVERRIDES=$(jq -r '.whitenoise_track_overrides // [] | join(";")' /data/options.json)
FEEDBACK_ENTITY=$(opt '.feedback_entity // ""')
FEEDBACK_SCALE=$(opt '.feedback_scale // 5')

# ── Generate an effective config.json that merges user options on top of the
# ── bundled defaults.  Done in Python because jq + nested merge is awkward.
python3 - <<PY
import json, os
from pathlib import Path

base = json.loads(Path("/app/training_config/config.json").read_text(encoding="utf-8"))
ha = base.setdefault("home_assistant", {})
api = ha.setdefault("api", {})

# ── Web-UI overrides (entity picker) ─────────────────────────────────
# If the user used the embedded picker, /data/web_ui_overrides.json
# carries their selections.  These take precedence over whatever they
# typed (or didn't type) into the Configuration form, because the picker
# only writes entity_ids that are guaranteed to exist in HA.
_overrides_path = Path("/data/web_ui_overrides.json")
_overrides = {}
if _overrides_path.is_file():
    try:
        _overrides = json.loads(_overrides_path.read_text(encoding="utf-8"))
        print(f"[run.sh] applied {_overrides_path} (web UI picks)")
    except Exception as exc:    # noqa: BLE001
        print(f"[run.sh] WARN: could not parse {_overrides_path}: {exc}")

# Supervisor proxy auth — no user token needed
api["base_url"] = "http://supervisor/core"
api["access_token"] = os.environ.get("SUPERVISOR_TOKEN", "")
api["verify_ssl"] = False
api["area_filter"] = """$AREA"""    # _norm() applied below where needed

def _norm(value):
    """Treat literal ``""`` / ``''`` (which users frequently type into the
    HA Configuration UI thinking it means *empty*) as a real empty
    string.  Without this, downstream code mistakes a 2-char ``"\""``
    for a valid entity_id and tries to subscribe to it.
    """
    v = (value or "").strip()
    if v in ('""', "''"):
        return ""
    return v

def csv_to_list(s):
    return [v for v in (_norm(x) for x in s.split(",")) if v]

def one(value):
    """Wrap a single non-empty entity_id in a list, otherwise empty list."""
    v = _norm(value)
    return [v] if v else []

domains = csv_to_list("""$DOMAINS""")
inc = csv_to_list("""$INCLUDES""")
exc = csv_to_list("""$EXCLUDES""")

if domains: api["controllable_domains"] = domains
if inc: api["explicit_includes"] = inc
if exc: api["explicit_excludes"] = exc

# v1.3.0: stash the sleep-stage entity at the top level of ``api``;
# SmartSleepService reads it from ``home_assistant.api.sleep_stage_source``.
_stage = _norm("""$STAGE_SOURCE""")
if _stage:
    api["sleep_stage_source"] = _stage

# ----- Slot bindings -----------------------------------------------------
# Each slot maps to a list of entity_ids; empty list ⇒ keyword scan owns it.
# ``_with_override(slot, form_value)`` — if the web UI picker wrote an
# entry for this slot, use that; otherwise fall back to the Configuration
# form value.  Single-valued slots wrap a non-empty string into a list.
def _slot_single(override_key, form_value):
    if override_key in _overrides:
        v = _norm(_overrides[override_key])
        return [v] if v else []
    return one(form_value)

def _slot_multi(override_key, form_value_csv):
    if override_key in _overrides:
        raw = _overrides[override_key]
        if isinstance(raw, list):
            return [v for v in (_norm(x) for x in raw) if v]
        v = _norm(raw)
        return [v] if v else []
    return csv_to_list(form_value_csv)

slot_bindings = {
    "temperature":    _slot_single("temperature_source", """$TEMP_SOURCE"""),
    "humidity":       _slot_single("humidity_source", """$HUM_SOURCE"""),
    "illuminance":    _slot_single("illuminance_source", """$LUX_SOURCE"""),
    "lights":         _slot_multi("light_targets", """$LIGHT_TARGETS"""),
    "climates":       _slot_single("climate_target", """$CLIMATE_TARGET"""),
    "humidifiers":    _slot_single("humidifier_target", """$HUMIDIFIER_TARGET"""),
    "fans":           _slot_single("fan_target", """$FAN_TARGET"""),
    "switches":       _slot_multi("switch_targets", """$SWITCH_TARGETS"""),
}
# Drop empty slots so DiscoveryConfig.from_dict gets a tidy dict.
api["slot_bindings"] = {k: v for k, v in slot_bindings.items() if v}

sc = ha.setdefault("smart_control", {})
sc["dry_run"] = """$DRY_RUN""" == "true"
sc["min_seconds_between_actions"] = int("""$MIN_SECONDS_BETWEEN_ACTIONS""")
sc["deadband_temperature_c"] = float("""$DEADBAND_T""")
sc["deadband_humidity_pct"] = float("""$DEADBAND_H""")
sc["deadband_brightness_pct"] = float("""$DEADBAND_B""")

learner = ha.setdefault("preference_learner", {})
learner["exploration_rate"] = float("""$EXPLORATION_RATE""")
# Persist preferences on the supervisor's /data volume so they survive
# add-on reinstalls.
learner["history_path"] = "/data/user_preferences.json"

# ----- Natural-sleep block (v1.2.0) ------------------------------------
# ``SmartSleepService`` reads everything under home_assistant.natural_sleep
# and treats each sub-field as independently optional.  We drop empty
# strings / zero ints so absent fields stay absent (vs. confusing the
# dataclass with "" defaults).
# Parse track overrides ``"pink_noise=URL;rain=URL"`` → dict.
track_overrides_raw = """$WHITENOISE_TRACK_OVERRIDES"""
track_overrides: dict[str, str] = {}
for item in track_overrides_raw.split(";"):
    item = item.strip()
    if not item or "=" not in item:
        continue
    k, _, v = item.partition("=")
    track_overrides[k.strip()] = v.strip()

def _override_or(key, fallback):
    """Use the web UI's pick if present (and non-empty after normalisation),
    otherwise the Configuration form value."""
    if key in _overrides:
        v = _norm(_overrides[key])
        if v != "":
            return v
    return fallback

# Wake-light targets accept multi-select.
_wake_lights_override = _overrides.get("wake_light_targets")
if isinstance(_wake_lights_override, list):
    _wake_lights = [v for v in (_norm(x) for x in _wake_lights_override) if v]
else:
    _wake_lights = csv_to_list("""$WAKE_LIGHTS""")

natural = {
    "user_id": "default",
    "chronotype": _norm("""$CHRONOTYPE""") or "neutral",
    "wake_window_start": _norm("""$WAKE_START"""),
    "wake_window_end": _norm("""$WAKE_END"""),
    "wake_light_targets": _wake_lights,
    "whitenoise_target": _override_or("whitenoise_target", _norm("""$WHITENOISE_TARGET""")),
    "whitenoise_volume_scale": float("""$WHITENOISE_VOLUME_SCALE"""),
    "whitenoise_track_overrides": track_overrides,
    "feedback_entity": _override_or("feedback_entity", _norm("""$FEEDBACK_ENTITY""")),
    "feedback_scale": int("""$FEEDBACK_SCALE"""),
}
try:
    by = int("""$BIRTH_YEAR""" or 0)
except Exception:
    by = 0
if by > 0:
    natural["birth_year"] = by
# Drop empty strings so Python's ``or`` short-circuits in the service.
natural = {k: v for k, v in natural.items() if v != ""}
ha["natural_sleep"] = natural

# Write the effective config inside /data so we don't mutate the read-only
# image layer.
out_path = Path("/data/effective_config.json")
out_path.write_text(json.dumps(base, indent=2, ensure_ascii=False),
                    encoding="utf-8")
bound = sum(1 for v in api["slot_bindings"].values() if v)
print(f"[run.sh] effective config written to {out_path}")
print(f"[run.sh] slot bindings active: {bound} role(s)")
PY

# ── Map log level ───────────────────────────────────────────────────────────
case "$LOG_LEVEL" in
  debug)   PY_LOG_FLAG="-v" ;;
  info)    PY_LOG_FLAG="" ;;
  warning) PY_LOG_FLAG="" ; export PYTHONLOGLEVEL=WARNING ;;
  error)   PY_LOG_FLAG="" ; export PYTHONLOGLEVEL=ERROR ;;
  *)       PY_LOG_FLAG="" ;;
esac

AREA_LABEL="${AREA:-<all rooms>}"
echo "[run.sh] Starting Sleep Classifier add-on (area=$AREA_LABEL, infer_interval=${INFER_INTERVAL}s, dry_run=$DRY_RUN)"
echo "[run.sh] Using SUPERVISOR_TOKEN to authenticate against http://supervisor/core"

# ── Web UI (entity picker via Supervisor Ingress) ──────────────────────────
# Started in the background so the user can open it from the add-on detail
# page while the inference loop runs in the foreground.  We trap SIGTERM
# to make sure the helper dies cleanly when the supervisor restarts us
# (otherwise the port would be held by an orphan).
export WEB_UI_PORT=8099
python3 /app/web_ui.py &
WEB_UI_PID=$!
echo "[run.sh] Web UI started (PID $WEB_UI_PID) on :$WEB_UI_PORT"
trap 'kill $WEB_UI_PID 2>/dev/null || true' EXIT INT TERM

# ── Hand off to the Python service ──────────────────────────────────────────
# We pass --config explicitly so the service reads the merged file we just
# generated, not the bundled defaults.  HA_TOKEN env var is honoured by
# scripts/run_ha_smart_service.py as the highest priority.
export HA_TOKEN="${SUPERVISOR_TOKEN:-}"
cd /app
exec python3 scripts/run_ha_smart_service.py \
    --config /data/effective_config.json \
    --base-url "http://supervisor/core" \
    --area "$AREA" \
    --infer-interval "$INFER_INTERVAL" \
    --session-interval "$SESSION_INTERVAL" \
    $PY_LOG_FLAG
