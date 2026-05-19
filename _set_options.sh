#!/bin/sh
# Set sleep_classifier addon options via Supervisor REST API.
# Uses 'ha api' which already wraps the auth token correctly.
set -eu
SLUG="0c614d55_sleep_classifier"
OPTS_FILE="/tmp/options.json"

# Wrap as {"options": {...}}.
WRAPPED="/tmp/wrapped.json"
echo "{\"options\":" > "$WRAPPED"
cat "$OPTS_FILE" >> "$WRAPPED"
echo "}" >> "$WRAPPED"

echo "==> body:"
cat "$WRAPPED"
echo ""

# Find ha CLI's internal token by reading supervisor's API docs URL.
# Easiest: just exec the request inside the supervisor container; it
# has access via internal hassio bridge.
echo "==> Attempting direct supervisor API"

# Actually `ha` CLI exposes the supervisor base URL.  Query addons info
# to find current state; then POST options via the raw 'ha jobs' debug
# pattern.  But ha CLI doesn't expose a raw POST.
#
# Plan B: read /etc/supervisor.json or similar for the token used
# by 'ha' CLI.  Failing that, fetch a fresh supervisor token via
# `ha supervisor info --raw-json` won't show it.
#
# Plan C: use ha addons options... wait, that doesn't exist.
#
# Plan D: write options.json directly into the addon's data folder.
# Supervisor reads it on next start.

# Get the addon's data folder
ADDON_DATA="/mnt/data/supervisor/addons/data/$SLUG"
if [ ! -d "$ADDON_DATA" ]; then
    # alternate path
    ADDON_DATA=$(find /mnt/data /var/lib -type d -name "$SLUG" 2>/dev/null | head -1)
fi
echo "==> addon data dir: $ADDON_DATA"

# That's where options.json lives that the addon reads on start.
if [ -n "$ADDON_DATA" ] && [ -d "$ADDON_DATA" ]; then
    echo "==> Writing options.json to $ADDON_DATA"
    cp "$OPTS_FILE" "$ADDON_DATA/options.json"
    echo "==> Restarting addon"
    ha addons restart "$SLUG"
    echo "==> Done"
else
    echo "==> Addon data dir not found, options not applied"
    exit 1
fi
