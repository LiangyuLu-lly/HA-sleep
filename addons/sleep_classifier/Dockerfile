# Sleep Classifier add-on — multistage build.
#
# Why multistage?
#   * Build deps (gcc, libhdf5-dev, pkg-config) only live in the *builder*
#     stage.  The final runtime image carries python3, jq, bash plus a
#     pre-built virtualenv with our pinned wheels — nothing else.
#   * On Pi 4B this brings the on-device image from ~650 MB (TF flavour)
#     down to roughly 60 MB, and the `pip install` step from 25 min to
#     ~3 min because we install ONLY `requirements-runtime.txt` (no
#     TensorFlow — see tests/test_numpy_keras_equivalence.py for the
#     numerical-parity contract that justifies dropping TF at runtime).

ARG BUILD_FROM

# ── Stage 1: builder ───────────────────────────────────────────────────────
FROM ${BUILD_FROM} AS builder

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Build-time toolchain — needed for h5py / scipy wheels when piwheels has
# no pre-built artefact for the current arch.  Discarded in the runtime
# stage.
RUN \
    if command -v apk >/dev/null 2>&1; then \
        apk add --no-cache \
            python3 python3-dev py3-pip py3-virtualenv \
            build-base linux-headers \
            hdf5 hdf5-dev pkgconfig ; \
    else \
        apt-get update && apt-get install -y --no-install-recommends \
            python3 python3-dev python3-pip python3-venv \
            build-essential libhdf5-dev pkg-config ca-certificates && \
        rm -rf /var/lib/apt/lists/* ; \
    fi

# Create an isolated virtualenv so we can COPY it verbatim into the final
# stage without dragging the host's site-packages along.
RUN python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH

# Install only the *runtime* wheels.  This is the single biggest reason
# the final image is ~60 MB instead of ~650 MB.
COPY rootfs/requirements-runtime.txt /tmp/requirements-runtime.txt
RUN pip install --upgrade pip wheel && \
    pip install \
        --extra-index-url https://www.piwheels.org/simple/ \
        -r /tmp/requirements-runtime.txt

# Drop bytecode + test data we don't need at runtime to keep the venv slim.
RUN find /opt/venv -type d -name '__pycache__' -prune -exec rm -rf {} + && \
    find /opt/venv -type d -name 'tests' -prune -exec rm -rf {} + && \
    find /opt/venv -name '*.pyc' -delete


# ── Stage 2: runtime ───────────────────────────────────────────────────────
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

# Runtime libraries only — no compilers, no -dev headers.
# * python3 + bash : interpreter / shell
# * jq             : run.sh parses /data/options.json with jq
# * libhdf5        : runtime-only shared library for h5py
# * tini           : init for clean signal forwarding (HA sends SIGTERM)
RUN \
    if command -v apk >/dev/null 2>&1; then \
        apk add --no-cache \
            python3 bash jq tini \
            hdf5 libstdc++ ; \
    else \
        apt-get update && apt-get install -y --no-install-recommends \
            python3 bash jq tini ca-certificates \
            libhdf5-103 libgomp1 && \
        rm -rf /var/lib/apt/lists/* ; \
    fi

# Carry over the pre-built virtualenv from the builder stage.
COPY --from=builder /opt/venv /opt/venv

# ── Project source ─────────────────────────────────────────────────────────
# The supervisor uses this add-on folder as the Docker build context, so
# everything below is relative to addons/sleep_classifier/.  Run
# `prepare.sh` / `prepare.bat` before pushing to GitHub to mirror src/,
# scripts/, config/, models/ plus the requirements files into ./rootfs/.
WORKDIR /app
COPY rootfs/ /app/

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
