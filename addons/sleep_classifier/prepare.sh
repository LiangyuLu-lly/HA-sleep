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

# Snapshot any pre-existing model weights *before* the wipe so they survive
# even when the source ``models/*.h5`` is hidden by .gitignore.  This is
# the common case on a fresh CI checkout where the repo intentionally
# refuses to track 9 MB of binary weights at the project root but does
# track the same file under ``rootfs/models/`` (so the Docker COPY works).
SNAPSHOT_DIR="$(mktemp -d -t sleep_classifier_models.XXXXXX)"
if [ -d "$ROOTFS/models" ]; then
    find "$ROOTFS/models" -maxdepth 1 -type f \( -name '*.h5' -o -name '*.hdf5' \) \
         -exec cp -p {} "$SNAPSHOT_DIR/" \;
    if [ -n "$(ls -A "$SNAPSHOT_DIR" 2>/dev/null)" ]; then
        echo "[prepare] snapshotted $(ls "$SNAPSHOT_DIR" | wc -l | tr -d ' ') model weight file(s) for restore"
    fi
fi

rm -rf "$ROOTFS"
mkdir -p "$ROOTFS"

# Hard-fails if a critical input is missing; soft-warns on optional ones.
copy_dir_required() {
    local name="$1"
    if [ -d "$REPO_ROOT/$name" ]; then
        cp -R "$REPO_ROOT/$name" "$ROOTFS/$name"
        echo "[prepare] mirrored $name/"
    else
        echo "[prepare] ERROR: required directory $name/ not found at $REPO_ROOT/$name" >&2
        echo "[prepare]        Run this script from a clean repo checkout." >&2
        exit 1
    fi
}

copy_dir_optional() {
    local name="$1"
    if [ -d "$REPO_ROOT/$name" ]; then
        cp -R "$REPO_ROOT/$name" "$ROOTFS/$name"
        echo "[prepare] mirrored $name/"
    else
        echo "[prepare] WARNING: $name/ not found — add-on may use random weights"
    fi
}

# src / scripts / config are baked into the image; without them the
# Dockerfile's COPY of rootfs/ produces a stub that crashes at startup.
copy_dir_required src
copy_dir_required scripts
copy_dir_required config
# models/ is optional: the add-on can boot with bootstrap weights and
# log a clear warning, but a build with no model is still valid for
# users who train remotely and copy the .h5 in via /share later.
copy_dir_optional models

# Restore any *.h5 / *.hdf5 weights we snapshotted earlier if (and only
# if) the freshly copied source didn't already provide them.  This is
# what keeps CI green: the source ``models/*.h5`` is invisible to git
# (gitignored) but the previous rootfs snapshot has the real bytes.
if [ -n "$(ls -A "$SNAPSHOT_DIR" 2>/dev/null)" ]; then
    mkdir -p "$ROOTFS/models"
    restored=0
    for f in "$SNAPSHOT_DIR"/*; do
        [ -f "$f" ] || continue
        name="$(basename "$f")"
        if [ ! -f "$ROOTFS/models/$name" ]; then
            cp -p "$f" "$ROOTFS/models/$name"
            restored=$((restored + 1))
        fi
    done
    if [ "$restored" -gt 0 ]; then
        echo "[prepare] restored $restored model weight file(s) from snapshot"
    fi
fi
rm -rf "$SNAPSHOT_DIR"

# requirements-runtime.txt is the only file the Dockerfile actually
# pip-installs (no TensorFlow); missing it means a broken image.
if [ -f "$REPO_ROOT/requirements-runtime.txt" ]; then
    cp "$REPO_ROOT/requirements-runtime.txt" "$ROOTFS/requirements-runtime.txt"
    echo "[prepare] copied requirements-runtime.txt"
else
    echo "[prepare] ERROR: requirements-runtime.txt missing — the add-on" >&2
    echo "[prepare]        Dockerfile cannot pip-install without it." >&2
    exit 1
fi
# Train/full lists are nice-to-have for in-container debugging but not
# strictly required.
for req in requirements-train.txt requirements.txt; do
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
