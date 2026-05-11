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

# Slot bindings — sensors (single entity_id each, empty means auto-discover)
HR_SOURCE=$(opt '.heart_rate_source // ""')
MV_SOURCE=$(opt '.movement_source // ""')
BR_SOURCE=$(opt '.breathing_source // ""')
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
HR_KEYWORDS=$(opt_array_to_csv '.heart_rate_keywords // []')
MV_KEYWORDS=$(opt_array_to_csv '.movement_keywords // []')
BR_KEYWORDS=$(opt_array_to_csv '.breathing_keywords // []')
DOMAINS=$(opt_array_to_csv '.controllable_domains // []')
INCLUDES=$(opt_array_to_csv '.explicit_includes // []')
EXCLUDES=$(opt_array_to_csv '.explicit_excludes // []')

# ── Generate an effective config.json that merges user options on top of the
# ── bundled defaults.  Done in Python because jq + nested merge is awkward.
python3 - <<PY
import json, os
from pathlib import Path

base = json.loads(Path("/app/config/config.json").read_text(encoding="utf-8"))
ha = base.setdefault("home_assistant", {})
api = ha.setdefault("api", {})

# Supervisor proxy auth — no user token needed
api["base_url"] = "http://supervisor/core"
api["access_token"] = os.environ.get("SUPERVISOR_TOKEN", "")
api["verify_ssl"] = False
api["area_filter"] = """$AREA"""

def csv_to_list(s):
    return [x.strip() for x in s.split(",") if x.strip()]

def one(value):
    """Wrap a single non-empty entity_id in a list, otherwise empty list."""
    v = (value or "").strip()
    return [v] if v else []

hr = csv_to_list("""$HR_KEYWORDS""")
mv = csv_to_list("""$MV_KEYWORDS""")
br = csv_to_list("""$BR_KEYWORDS""")
domains = csv_to_list("""$DOMAINS""")
inc = csv_to_list("""$INCLUDES""")
exc = csv_to_list("""$EXCLUDES""")

if hr: api["heart_rate_keywords"] = hr
if mv: api["movement_keywords"] = mv
if br: api["breathing_keywords"] = br
if domains: api["controllable_domains"] = domains
if inc: api["explicit_includes"] = inc
if exc: api["explicit_excludes"] = exc

# ----- Slot bindings -----------------------------------------------------
# Each slot maps to a list of entity_ids; empty list ⇒ keyword scan owns it.
slot_bindings = {
    "heart_rate":     one("""$HR_SOURCE"""),
    "movement":       one("""$MV_SOURCE"""),
    "breathing":      one("""$BR_SOURCE"""),
    "temperature":    one("""$TEMP_SOURCE"""),
    "humidity":       one("""$HUM_SOURCE"""),
    "illuminance":    one("""$LUX_SOURCE"""),
    "lights":         csv_to_list("""$LIGHT_TARGETS"""),
    "climates":       one("""$CLIMATE_TARGET"""),
    "humidifiers":    one("""$HUMIDIFIER_TARGET"""),
    "fans":           one("""$FAN_TARGET"""),
    "switches":       csv_to_list("""$SWITCH_TARGETS"""),
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
