#!/usr/bin/env bash
# Mirror the project source tree into addons/sleep_classifier/rootfs/.
#
# Why this exists
# ---------------
# Home Assistant's add-on builder uses the add-on directory as the Docker
# build context.  Files outside of addons/sleep_classifier/ are therefore
# unreachable from the Dockerfile's COPY instructions.  Run this script
# once before pushing to GitHub so that the Supervisor has everything it
# needs to build the image on the Pi.
#
# Re-run after every change to src/, scripts/, config/, models/ or
# requirements.txt.  Safe to re-run; existing files are overwritten.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ROOTFS="$SCRIPT_DIR/rootfs"

echo "[prepare] repo root : $REPO_ROOT"
echo "[prepare] add-on    : $SCRIPT_DIR"
echo "[prepare] target    : $ROOTFS"

rm -rf "$ROOTFS"
mkdir -p "$ROOTFS"

copy_dir() {
    local name="$1"
    if [ -d "$REPO_ROOT/$name" ]; then
        cp -R "$REPO_ROOT/$name" "$ROOTFS/$name"
        echo "[prepare] mirrored $name/"
    else
        echo "[prepare] WARNING: $name/ not found at repo root"
    fi
}

copy_dir src
copy_dir scripts
copy_dir config
copy_dir models   # may not exist before training — leaves WARNING but won't fail

# The add-on Dockerfile only installs requirements-runtime.txt (no
# TensorFlow), so we mirror all three files: the runtime list is what
# pip actually consumes; the train/full lists are kept for developers
# who SSH into the running container to reproduce training-time errors.
for req in requirements-runtime.txt requirements-train.txt requirements.txt; do
    if [ -f "$REPO_ROOT/$req" ]; then
        cp "$REPO_ROOT/$req" "$ROOTFS/$req"
        echo "[prepare] copied $req"
    else
        echo "[prepare] WARNING: $req not found at repo root"
    fi
done

# Strip Python cache / large datasets that don't belong in an image.
find "$ROOTFS" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$ROOTFS" -type d -name "sleep-edf-telemetry" -prune -exec rm -rf {} +
find "$ROOTFS" -type f -name "*.pyc" -delete

echo "[prepare] done"
