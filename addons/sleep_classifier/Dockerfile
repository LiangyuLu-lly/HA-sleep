# Sleep Classifier add-on — single-stage Alpine build.
#
# Design notes
# ------------
# * `${BUILD_FROM}` is injected by HA Supervisor based on the per-arch
#   mapping in `build.yaml` (sibling file).  Without that build.yaml the
#   variable is empty and the build fails with
#       base name (${BUILD_FROM}) should not be blank
# * We deliberately use a single RUN that installs build deps, compiles
#   the wheels we need, then `apk del`s the toolchain again.  Inside one
#   layer this nets out to roughly the runtime footprint only (~60 MB
#   for python + numpy + scipy + h5py + PyWavelets).
# * No virtualenv: Alpine's `python3 -m venv` silently lacks ensurepip,
#   which breaks `pip install` from inside the venv.  System-wide
#   `pip3 install --break-system-packages` is the supported escape
#   hatch on Alpine 3.19 (PEP 668).
# * Why no TensorFlow:  inference uses the pure-numpy fallback paths in
#   src/cnn_extractor.py / bilstm_analyzer.py / sleep_classifier.py.
#   Numerical parity with the Keras-trained checkpoint is enforced by
#   tests/test_numpy_keras_equivalence.py (max abs diff < 1e-3).

ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

# ── Project source ──────────────────────────────────────────────────────────
# Supervisor `docker build`s with the add-on folder as the context, so every
# COPY is relative to addons/sleep_classifier/.  Run `prepare.sh` / `prepare.bat`
# before pushing to mirror src/, scripts/, config/, models/ + requirements
# files into ./rootfs/.
WORKDIR /app
COPY rootfs/ /app/

# ── Build + runtime deps in ONE layer, then drop the toolchain ─────────────
# Steps (single RUN so the toolchain doesn't end up in the final image):
#   1. install build toolchain + hdf5 dev headers
#   2. install runtime libs we keep (bash, jq, tini, libhdf5, libstdc++)
#   3. pip install requirements-runtime.txt (no TF, ~30 MB of wheels)
#   4. apk del the toolchain again
#   5. wipe pip cache + /var/cache/apk to keep the layer slim
RUN set -eux; \
    apk add --no-cache --virtual .build-deps \
        build-base linux-headers python3-dev hdf5-dev pkgconfig; \
    apk add --no-cache \
        python3 py3-pip bash jq tini \
        hdf5 libstdc++; \
    pip3 install --upgrade pip wheel; \
    pip3 install \
        --extra-index-url https://www.piwheels.org/simple/ \
        -r /app/requirements-runtime.txt; \
    apk del --purge .build-deps; \
    find /usr/lib/python3* -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true; \
    find /usr/lib/python3* -name '*.pyc' -delete 2>/dev/null || true; \
    rm -rf /root/.cache /var/cache/apk/* /tmp/*

# Add-on entrypoint sits at the image root.
COPY run.sh /run.sh
RUN chmod +x /run.sh

# Persistent runtime state (preferences, effective config) lives under /data
# which the supervisor maps to a persistent volume.  Symlink the project's
# expected paths so writes survive add-on restarts/upgrades.
RUN mkdir -p /data /share && \
    ln -sf /data /app/data

# Forward SIGTERM cleanly so the service can flush user_preferences.json
# during an HA shutdown.
STOPSIGNAL SIGTERM

ENTRYPOINT ["/sbin/tini", "--"]
CMD [ "/run.sh" ]
