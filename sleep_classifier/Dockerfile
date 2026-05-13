# Sleep Classifier add-on — single-stage Alpine build (v2.0.0+).
#
# v2.0.1 build-system change
# --------------------------
# Previous versions used Home Assistant's official per-arch base images
# pulled from ``ghcr.io/home-assistant/aarch64-base:3.19``.  Those are
# nice — they ship bashio + s6-overlay pre-installed — but in networks
# where ``ghcr.io`` is slow or blocked (mainland China, some corporate
# firewalls) the Supervisor build sits at 0% CPU for 10+ minutes while
# Docker silently retries the TLS handshake.
#
# We switched to the Docker Hub official ``python:3.11-alpine`` image
# because:
#
# * Docker Hub (``registry-1.docker.io``) is mirrored by every major
#   Chinese cloud provider (Aliyun, Tencent, DaoCloud, USTC) and
#   HA OS's Docker daemon can fall back to those mirrors out of the
#   box — while ``ghcr.io`` has no equivalent.
# * ``python:3.11-alpine`` already contains Python + pip + the Alpine
#   package manager, saving us two ``apk add`` steps.
# * We don't actually use bashio or s6-overlay anywhere — ``run.sh`` is
#   a plain bash script.
#
# Side effect: ``build.yaml`` is no longer strictly required because we
# don't reference ``${BUILD_FROM}``.  Supervisor will still warn about
# it being "deprecated"; that warning is harmless and ``build.yaml``
# stays for backwards compatibility (users on older HA versions).

FROM python:3.11-alpine

ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_ROOT_USER_ACTION=ignore

# --- Project source ---------------------------------------------------------
# Supervisor ``docker build``s with the add-on folder as the context, so
# every COPY is relative to sleep_classifier/.  Run prepare.sh / prepare.bat
# locally before committing to mirror src/ scripts/ training_config/ +
# requirements-runtime.txt into ./rootfs/.
WORKDIR /app
COPY rootfs/ /app/

# --- System deps + Python deps in ONE layer ---------------------------------
# The python:3.11-alpine image has python + pip but no build toolchain.
# ``aiohttp`` ships pre-built wheels for musl aarch64 on PyPI since 3.9
# so we don't actually need gcc/musl-dev here — but we install them in a
# virtual package group and remove them at the end, costing zero final
# size but giving us a fallback if piwheels ever regresses on a wheel.
#
# Runtime deps retained:
#   * bash  — run.sh uses bash features (set -euo pipefail, etc.)
#   * jq    — run.sh parses /data/options.json with jq
#   * tini  — PID 1 signal forwarder, so SIGTERM reaches our Python service
RUN set -eux; \
    apk add --no-cache --virtual .build-deps \
        build-base linux-headers; \
    apk add --no-cache \
        bash jq tini; \
    pip3 install --upgrade pip wheel; \
    pip3 install -r /app/requirements-runtime.txt; \
    apk del --purge .build-deps; \
    find /usr/local/lib/python3.11 -type d -name '__pycache__' -prune \
        -exec rm -rf {} + 2>/dev/null || true; \
    find /usr/local/lib/python3.11 -name '*.pyc' -delete 2>/dev/null || true; \
    rm -rf /root/.cache /var/cache/apk/* /tmp/*

# Web UI for picking entity_ids out of HA's live state.  Lives inside the
# container, served via Supervisor Ingress.  See web_ui.py for details.
COPY web_ui.py /app/web_ui.py

# v2.0.2: effective-config generator extracted out of run.sh so user-
# supplied Chinese / quoted option values can't break a shell heredoc.
COPY render_effective_config.py /app/render_effective_config.py

# Belt-and-braces: strip any __pycache__ / .pyc that might have slipped
# into rootfs/ during local development so the image layer stays slim
# and Python doesn't import stale bytecode from a mismatched source.
RUN find /app -type d -name '__pycache__' -prune \
        -exec rm -rf {} + 2>/dev/null || true; \
    find /app -name '*.pyc' -delete 2>/dev/null || true

# Add-on entrypoint sits at the image root.
COPY run.sh /run.sh
RUN chmod +x /run.sh

# Persistent runtime state (preferences, effective config) lives under
# /data which the supervisor maps to a persistent volume.  Symlink the
# project's expected paths so writes survive add-on restarts/upgrades.
RUN mkdir -p /data /share && \
    ln -sf /data /app/data

# Forward SIGTERM cleanly so the service can flush user_preferences.json
# during an HA shutdown.
STOPSIGNAL SIGTERM

ENTRYPOINT ["/sbin/tini", "--"]
CMD ["/run.sh"]
