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
# v1.3.0 simplification
# ---------------------
# The add-on no longer ships a CNN-BiLSTM model — it subscribes to a
# sleep-stage entity the user has already built in HA (Apple Watch,
# Fitbit, sleep_as_android, a separate add-on, …).  That removes the
# two biggest hassles of the old pipeline:
#
#   * a 9 MB ``.h5`` binary that had to be shuttled around through
#     ``rootfs/models/`` because gitignore hid the top-level copy, and
#   * ``requirements-train.txt`` pulling TensorFlow into an image that
#     never trains anything.
#
# Both are now gone.  ``models/`` is no longer copied, and only
# ``requirements-runtime.txt`` is required.
#
# Re-run after every change to src/, scripts/, training_config/ or the
# runtime requirements file.  Safe to re-run; existing files are
# overwritten.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Add-on sits at the repository root now (v1.2.3+) so the project root is
# exactly one level up, not two.  This is the layout HA Supervisor needs
# to discover the add-on directly when the user adds the repo URL.
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ROOTFS="$SCRIPT_DIR/rootfs"

echo "[prepare] repo root : $REPO_ROOT"
echo "[prepare] add-on    : $SCRIPT_DIR"
echo "[prepare] target    : $ROOTFS"

rm -rf "$ROOTFS"
mkdir -p "$ROOTFS"

# Hard-fails if a critical input is missing.  Since v1.3.0 there are no
# optional inputs — every directory below is required for the add-on to
# start.
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

# src/, scripts/ and training_config/ are baked into the image.
# ``models/`` is intentionally *not* mirrored in v1.3.0 — the add-on
# doesn't ship a local stage classifier any more.
copy_dir_required src
copy_dir_required scripts
copy_dir_required training_config

# requirements-runtime.txt is the only file the Dockerfile actually
# pip-installs.  TensorFlow is gone, so there is no train-only list.
if [ -f "$REPO_ROOT/requirements-runtime.txt" ]; then
    cp "$REPO_ROOT/requirements-runtime.txt" "$ROOTFS/requirements-runtime.txt"
    echo "[prepare] copied requirements-runtime.txt"
else
    echo "[prepare] ERROR: requirements-runtime.txt missing — the add-on" >&2
    echo "[prepare]        Dockerfile cannot pip-install without it." >&2
    exit 1
fi
# Full requirements.txt is optional (handy for in-container debugging
# when a user shells into the add-on for a hot-fix).
if [ -f "$REPO_ROOT/requirements.txt" ]; then
    cp "$REPO_ROOT/requirements.txt" "$ROOTFS/requirements.txt"
    echo "[prepare] copied requirements.txt"
fi

# Strip Python cache / large datasets that don't belong in an image.
find "$ROOTFS" -type d -name "__pycache__" -prune -exec rm -rf {} +
find "$ROOTFS" -type d -name "sleep-edf-telemetry" -prune -exec rm -rf {} +
find "$ROOTFS" -type f -name "*.pyc" -delete

# v3.0.0 训练产物兜底镜像（R7.5 / R12.1）
# ---------------------------------------------------------------------------
# 上面的 ``copy_dir_required training_config`` 已经整树拷贝了 training_config/，
# 因此 population_prior.pickle / stage_predictor.onnx 若位于源目录顶层，
# 也会被一并镜像。这里再显式 cp 一次是「腰带 + 背带」式的防御：
#
#   * 未来若把 *.pickle / *.onnx 加进 .gitignore（防止开发者误提交大文件），
#     copy_dir_required 仍能拷贝到工作树里的产物，但显式 cp 让脚本意图更清晰；
#   * 训练产物可能由 ``scripts/train_population_prior.py`` /
#     ``scripts/train_stage_predictor.py`` 直接写入 training_config/，缺失
#     时 ``2>/dev/null || true`` 让 prepare 不被 set -e 中断（构建期校验留给 CI）。
#
# 两个产物都是「可选输入」——开发者首次跑 prepare 时往往还没训练，本地 prepare
# 阶段以 WARN 级别提示即可，不应让构建挂掉。
mkdir -p "$ROOTFS/training_config"
cp -f "$REPO_ROOT/training_config/population_prior.pickle" "$ROOTFS/training_config/" 2>/dev/null || true
cp -f "$REPO_ROOT/training_config/stage_predictor.onnx"   "$ROOTFS/training_config/" 2>/dev/null || true

# 非 strict 模式校验训练产物尺寸 + 嵌入 SHA-256（R7.5 / R12.1）
# ---------------------------------------------------------------------------
# 缺失文件以 WARN 级别提示并 exit 0；只有 size / sha256 违规才 exit 1。
# 这里再叠一层 ``|| true`` 是为了即便检查脚本未来被改成更严格也不影响本地
# prepare 流程。CI 侧调用 ``scripts/check_artifacts.py --strict`` 把缺失视
# 为硬失败，那才是发布闸门。
python "$REPO_ROOT/scripts/check_artifacts.py" || true

echo "[prepare] done"
