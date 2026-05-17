#!/usr/bin/env bash
# Entrypoint for the Sleep Classifier add-on.
# Build marker: v2.1.6 — keeps Docker COPY layer hash unique per release.
#
# Reads user-supplied options from /data/options.json (populated by the HA
# supervisor from the add-on Configuration UI), then generates an effective
# config + launches the Web UI (always) and the Python smart service (only
# when a sleep-stage entity has been bound).
#
# v2.0.2 architectural change
# ---------------------------
# Previously this script ``exec``'d the smart service in the foreground,
# so when the service exited (e.g. ``sleep_stage_source`` not configured
# yet) the whole container died and Supervisor restarted it in a tight
# loop.  During each restart the Web UI was unreachable for ~3 s, which
# was exactly when users clicked "Reload entities" → HA Ingress returned
# 502 ("Cannot connect to host ..:8099").
#
# The new pattern keeps the Web UI as the container's foreground
# process and supervises the smart service in the background:
#
#   * Web UI is ALWAYS up, even before any entity is bound.
#   * Smart service starts only when sleep_stage_source is non-empty;
#     if it crashes we restart it with exponential back-off without
#     killing the container (so Ingress never 502s during crash loops).
#
# The supervisor injects SUPERVISOR_TOKEN; we use the HA core proxy URL
# (http://supervisor/core) so the user never deals with a token.
set -euo pipefail

echo "[run.sh] sleep_classifier add-on start (v2.1.8 — PYTHONPATH guard, AppArmor py3 fix)"

# /app 是 add-on 的 PYTHONPATH 根：``from src._io_utils import ...``、
# ``from training_config.config_loader import ...`` 这类绝对导入都需要
# /app 在 sys.path[0]。WORKDIR 是 /app 但 python3 用绝对路径调时 cwd 不是 /app，
# 所以这里全局 export 一份。
export PYTHONPATH="/app${PYTHONPATH:+:${PYTHONPATH}}"

# ── v2.1.1 诊断块 ───────────────────────────────────────────────────────────
# 安装老是失败时，先把容器内 Python / arch 状态打到日志，方便远程定位。
echo "[run.sh] === diagnostics ==="
echo "[run.sh] uname:    $(uname -a 2>&1 || true)"
echo "[run.sh] which py: $(command -v python3 2>&1 || echo 'NOT_FOUND')"
echo "[run.sh] py ver:   $(python3 --version 2>&1 || echo 'CANNOT_RUN')"
echo "[run.sh] PATH:     ${PATH}"
echo "[run.sh] ls /usr/local/bin/python*: $(ls /usr/local/bin/python* 2>&1 || true)"
echo "[run.sh] ls /usr/bin/python*:       $(ls /usr/bin/python* 2>&1 || true)"
echo "[run.sh] ===================="

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
# v1.4.0: wind-down pre-cool + stage debouncing knobs are exposed in
# config.yaml but used to be silently dropped here, so user edits had
# no effect.  The defaults mirror SmartControlConfig's dataclass
# defaults so omitting them in options.json reproduces pre-v1.6 behaviour.
WIND_DOWN_MINUTES=$(opt '.wind_down_minutes // 30')
MIN_STAGE_DWELL_SECONDS=$(opt '.min_stage_dwell_seconds // 60')
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
WHITENOISE_VOLUME_FEEDBACK_ENTITY=$(opt '.whitenoise_volume_feedback_entity // ""')
FEEDBACK_ENTITY=$(opt '.feedback_entity // ""')
FEEDBACK_SCALE=$(opt '.feedback_scale // 5')

# Apnea wiring (v1.7.0)
APNEA_RATE=$(opt '.apnea_breathing_rate_source // ""')
APNEA_AMPLITUDE=$(opt '.apnea_chest_amplitude_source // ""')
APNEA_CONSENT=$(opt '.apnea_consent_entity // ""')
APNEA_CALIBRATION_NIGHTS=$(opt '.apnea_calibration_nights // 7')

# ── Export to env so the Python generator script sees them.  We use
# env-var passing rather than heredoc ``"""$VAR"""`` injection because
# any user-provided value can contain quotes / backslashes / Chinese
# text that would break the heredoc quoting.
export SC_AREA="$AREA"
export SC_DRY_RUN="$DRY_RUN"
export SC_MIN_SECONDS_BETWEEN_ACTIONS="$MIN_SECONDS_BETWEEN_ACTIONS"
export SC_DEADBAND_T="$DEADBAND_T"
export SC_DEADBAND_H="$DEADBAND_H"
export SC_DEADBAND_B="$DEADBAND_B"
export SC_WIND_DOWN_MINUTES="$WIND_DOWN_MINUTES"
export SC_MIN_STAGE_DWELL_SECONDS="$MIN_STAGE_DWELL_SECONDS"
export SC_EXPLORATION_RATE="$EXPLORATION_RATE"
export SC_STAGE_SOURCE="$STAGE_SOURCE"
export SC_TEMP_SOURCE="$TEMP_SOURCE"
export SC_HUM_SOURCE="$HUM_SOURCE"
export SC_LUX_SOURCE="$LUX_SOURCE"
export SC_LIGHT_TARGETS="$LIGHT_TARGETS"
export SC_CLIMATE_TARGET="$CLIMATE_TARGET"
export SC_HUMIDIFIER_TARGET="$HUMIDIFIER_TARGET"
export SC_FAN_TARGET="$FAN_TARGET"
export SC_SWITCH_TARGETS="$SWITCH_TARGETS"
export SC_DOMAINS="$DOMAINS"
export SC_INCLUDES="$INCLUDES"
export SC_EXCLUDES="$EXCLUDES"
export SC_BIRTH_YEAR="$BIRTH_YEAR"
export SC_CHRONOTYPE="$CHRONOTYPE"
export SC_WAKE_START="$WAKE_START"
export SC_WAKE_END="$WAKE_END"
export SC_WAKE_LIGHTS="$WAKE_LIGHTS"
export SC_WHITENOISE_TARGET="$WHITENOISE_TARGET"
export SC_WHITENOISE_VOLUME_SCALE="$WHITENOISE_VOLUME_SCALE"
export SC_WHITENOISE_TRACK_OVERRIDES="$WHITENOISE_TRACK_OVERRIDES"
export SC_WHITENOISE_VOLUME_FEEDBACK_ENTITY="$WHITENOISE_VOLUME_FEEDBACK_ENTITY"
export SC_FEEDBACK_ENTITY="$FEEDBACK_ENTITY"
export SC_FEEDBACK_SCALE="$FEEDBACK_SCALE"
export SC_APNEA_RATE="$APNEA_RATE"
export SC_APNEA_AMPLITUDE="$APNEA_AMPLITUDE"
export SC_APNEA_CONSENT="$APNEA_CONSENT"
export SC_APNEA_CALIBRATION_NIGHTS="$APNEA_CALIBRATION_NIGHTS"

# ── Publish placeholder sensors (Bug 1.1 fix) ──────────────────────────────
# Best-effort: even if stage is not bound yet, Lovelace will show
# "configuring" entities instead of "Entity not available".
echo "[run.sh] Publishing placeholder sensors"
python3 /app/bootstrap_placeholders.py || echo "[run.sh] placeholder publish failed — continuing"

# ── Clean up stale atomic-write temp files (Bug 1.7 prevention) ─────────────
find /data -maxdepth 2 -type f -name '*.tmp.*' -mmin +60 -delete 2>/dev/null || true

# ── Generate an effective config.json that merges user options on top of
# ── the bundled defaults.  Done in Python (in a separate file, not a
# ── heredoc) because jq + nested merge is awkward AND heredoc-embedded
# ── Python is fragile when user input contains quotes / Chinese.
# PYTHONPATH=/app 在脚本顶部已 export，让 ``from src._io_utils import ...`` 找到 /app/src/。
python3 /app/render_effective_config.py

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
# The Web UI runs as a background process alongside the smart-service
# supervisor.  Both are managed by bash with job control (set -m) and a
# _shutdown() trap that forwards SIGTERM and waits up to 8 seconds for
# graceful exit before SIGKILL.
export WEB_UI_PORT=8099

# ── Smart-service supervisor (background) ──────────────────────────────────
# Runs the Python smart service in a retry loop so one crash doesn't
# bring down the container (= Web UI unreachable = Ingress 502).
# Exponential back-off capped at 60 s.  Skips startup entirely when
# the sleep-stage binding is missing — user must pick one in Web UI
# or the Configuration form first.
export HA_TOKEN="${SUPERVISOR_TOKEN:-}"

supervise_smart_service() {
    local backoff=2
    local max_backoff=60
    while true; do
        # Re-check the effective config on every iteration so after the
        # user binds a stage entity in Web UI → Restart add-on, the
        # supervisor picks it up without another Web UI regeneration.
        local stage
        stage=$(jq -r '.home_assistant.api.sleep_stage_source // ""' \
                    /data/effective_config.json 2>/dev/null || echo "")
        if [[ -z "$stage" || "$stage" == "null" || "$stage" == '""' ]]; then
            echo "[run.sh] sleep_stage_source not bound yet."
            echo "[run.sh] Open the Web UI (left sidebar) to pick a sleep-stage entity,"
            echo "[run.sh] or fill 'sleep_stage_source' under the Configuration tab,"
            echo "[run.sh] then click Restart on the add-on detail page."
            # Sleep for 30 s and re-check (instead of exiting) so the Web
            # UI stays up and users can iterate.  A genuine config change
            # from the Supervisor will kill the container anyway.
            sleep 30
            continue
        fi

        echo "[run.sh] Launching smart service (stage=$stage)"
        cd /app
        if python3 scripts/run_ha_smart_service.py \
                --config /data/effective_config.json \
                --base-url "http://supervisor/core" \
                --area "$AREA" \
                --infer-interval "$INFER_INTERVAL" \
                --session-interval "$SESSION_INTERVAL" \
                $PY_LOG_FLAG ; then
            echo "[run.sh] Smart service exited cleanly — will re-check binding in 10 s."
            sleep 10
            backoff=2
        else
            local rc=$?
            echo "[run.sh] Smart service exited rc=$rc; restarting in ${backoff}s"
            sleep "$backoff"
            backoff=$(( backoff * 2 ))
            if (( backoff > max_backoff )); then backoff=$max_backoff; fi
        fi
    done
}

# ── Process supervision (Bug 1.3 fix) ──────────────────────────────────────
# Enable job control so background processes get their own process groups.
set -m

# Start Web UI as a background process (no longer exec'd).
python3 /app/web_ui.py &
PID_WEB=$!
echo "[run.sh] Web UI PID $PID_WEB on :$WEB_UI_PORT"

# Start smart-service supervisor as a background process.
supervise_smart_service &
PID_SMART_SUP=$!
echo "[run.sh] Smart-service supervisor PID $PID_SMART_SUP"

# Graceful shutdown handler: forward SIGTERM to children, wait up to 8s,
# then SIGKILL any stragglers.
_shutdown() {
    echo "[run.sh] Received shutdown signal — forwarding to children"
    kill "$PID_WEB" "$PID_SMART_SUP" 2>/dev/null || true
    local deadline=8
    local elapsed=0
    while (( elapsed < deadline )); do
        # If both are gone, we're done.
        kill -0 "$PID_WEB" 2>/dev/null || kill -0 "$PID_SMART_SUP" 2>/dev/null || break
        sleep 1
        elapsed=$(( elapsed + 1 ))
    done
    # Force-kill anything still alive.
    kill -9 "$PID_WEB" "$PID_SMART_SUP" 2>/dev/null || true
    exit 0
}

trap _shutdown INT TERM

# Block until one of the children exits, then trigger shutdown.
wait -n "$PID_WEB" "$PID_SMART_SUP"
_shutdown
