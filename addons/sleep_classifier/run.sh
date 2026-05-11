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
AREA=$(opt '.area // "bedroom"')
INFER_INTERVAL=$(opt '.infer_interval // 30')
SESSION_INTERVAL=$(opt '.session_interval // 1800')
DRY_RUN=$(opt '.dry_run // false')
EXPLORATION_RATE=$(opt '.exploration_rate // 0.1')
MIN_SECONDS_BETWEEN_ACTIONS=$(opt '.min_seconds_between_actions // 120')
DEADBAND_T=$(opt '.deadband_temperature_c // 0.5')
DEADBAND_H=$(opt '.deadband_humidity_pct // 5')
DEADBAND_B=$(opt '.deadband_brightness_pct // 10')
LOG_LEVEL=$(opt '.log_level // "info"')

HR_KEYWORDS=$(opt_array_to_csv '.heart_rate_keywords // []')
MV_KEYWORDS=$(opt_array_to_csv '.movement_keywords // []')
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

hr = csv_to_list("""$HR_KEYWORDS""")
mv = csv_to_list("""$MV_KEYWORDS""")
domains = csv_to_list("""$DOMAINS""")
inc = csv_to_list("""$INCLUDES""")
exc = csv_to_list("""$EXCLUDES""")

if hr: api["heart_rate_keywords"] = hr
if mv: api["movement_keywords"] = mv
if domains: api["controllable_domains"] = domains
if inc: api["explicit_includes"] = inc
if exc: api["explicit_excludes"] = exc

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
print(f"[run.sh] effective config written to {out_path}")
PY

# ── Map log level ───────────────────────────────────────────────────────────
case "$LOG_LEVEL" in
  debug)   PY_LOG_FLAG="-v" ;;
  info)    PY_LOG_FLAG="" ;;
  warning) PY_LOG_FLAG="" ; export PYTHONLOGLEVEL=WARNING ;;
  error)   PY_LOG_FLAG="" ; export PYTHONLOGLEVEL=ERROR ;;
  *)       PY_LOG_FLAG="" ;;
esac

echo "[run.sh] Starting Sleep Classifier add-on (area=$AREA, infer_interval=${INFER_INTERVAL}s)"
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
