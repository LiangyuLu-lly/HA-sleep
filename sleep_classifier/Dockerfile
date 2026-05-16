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
# Side effect: ``build.yaml`` is no longer strictly required because
# Supervisor can infer the base image from the Dockerfile ARG default.
# ``build.yaml`` stays for backwards compatibility (users on older HA
# versions) and to let Supervisor inject per-arch BUILD_FROM values.

# --- Build args injected by HA Supervisor ------------------------------------
# Supervisor 构建 add-on 时会自动注入下列 ARG：
#   BUILD_FROM          - base image (默认从 Dockerfile ARG 取)
#   BUILD_ARCH          - 当前构建 arch (amd64 / aarch64)
#   BUILD_VERSION       - config.yaml 里的 version 字段
#   BUILD_NAME          - config.yaml 里的 name
#   BUILD_DESCRIPTION   - config.yaml 里的 description
#   BUILD_REPOSITORY    - "owner/repo" 形式的 GitHub 仓库名
#   BUILD_REF           - 构建对应的 git SHA
#   BUILD_DATE          - RFC3339 构建时间戳
#
# v2.1.0 修复：删除 build.yaml（已被新版 Supervisor 标 deprecated）。
# 当 build.yaml 解析失败时 Supervisor silently fall back 到自带的
# HA base image（``m.haospeed.com/home-assistant/base:latest`` 在国内
# 加速源场景下），那个 image 没装 pip，会让构建在 ``pip: not found``
# 处挂掉。删除 build.yaml 后 Supervisor 会用 Dockerfile 里的 ARG 默认
# 值 ``library/python:3.11-alpine``，docker daemon 自动展开成
# ``docker.io/library/python:3.11-alpine``。
#
# 镜像 OCI labels 之前由 build.yaml 提供，现在内嵌到 Dockerfile 的
# LABEL 块（见下方）。
ARG BUILD_FROM=library/python:3.11-alpine

FROM ${BUILD_FROM}

ARG BUILD_FROM=library/python:3.11-alpine
ARG BUILD_ARCH=amd64
ARG BUILD_VERSION=dev
ARG BUILD_NAME="Sleep Classifier"
ARG BUILD_DESCRIPTION="Closed-loop smart-home sleep automation"
ARG BUILD_REPOSITORY=LiangyuLu-lly/HA-sleep
ARG BUILD_REF=unknown
ARG BUILD_DATE=1970-01-01T00:00:00Z

# --- Labels (15 industrial-grade) --------------------------------------------
# 对标 hassio-addons/app-example：5 条 io.hass.* + 1 条 maintainer + 10 条 OCI 镜像标准。
# Supervisor UI / HA Dashboard 会读取这些标签来显示 add-on 元数据。
LABEL \
    io.hass.name="${BUILD_NAME}" \
    io.hass.description="${BUILD_DESCRIPTION}" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}" \
    maintainer="LiangyuLu <https://github.com/LiangyuLu-lly>" \
    org.opencontainers.image.title="${BUILD_NAME}" \
    org.opencontainers.image.description="${BUILD_DESCRIPTION}" \
    org.opencontainers.image.vendor="LiangyuLu" \
    org.opencontainers.image.authors="LiangyuLu <https://github.com/LiangyuLu-lly>" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.url="https://github.com/${BUILD_REPOSITORY}" \
    org.opencontainers.image.source="https://github.com/${BUILD_REPOSITORY}" \
    org.opencontainers.image.documentation="https://github.com/${BUILD_REPOSITORY}/blob/main/sleep_classifier/DOCS.md" \
    org.opencontainers.image.created="${BUILD_DATE}" \
    org.opencontainers.image.revision="${BUILD_REF}" \
    org.opencontainers.image.version="${BUILD_VERSION}"

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
    python3 -m pip install --no-cache-dir --upgrade pip wheel; \
    python3 -m pip install --no-cache-dir -r /app/requirements-runtime.txt; \
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

# v2.0.3: bootstrap placeholder sensors script — publishes 5 "configuring"
# entities before smart service starts, so Lovelace never shows "not available".
COPY bootstrap_placeholders.py /app/bootstrap_placeholders.py

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
# -g: tini 把 SIGTERM / SIGINT 转发到整个进程组而不是仅 PID 1 的子进程。
# 这样 bash 启动的 supervise_smart_service 子 shell 也能收到信号，
# python smart service 的 asyncio signal handler 能跑到 _persist_session。
STOPSIGNAL SIGTERM

ENTRYPOINT ["/sbin/tini", "-g", "--"]
CMD ["/run.sh"]
