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
ARG BUILD_FROM=library/python:3.12-slim-bookworm

FROM ${BUILD_FROM}

ARG BUILD_FROM=library/python:3.12-slim-bookworm
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
RUN echo "=== /app contents (build-time) ===" && ls -la /app && echo "=== /app/src contents ===" && ls -la /app/src 2>&1 | head -30 && echo "=== END ===" 

# --- System deps + Python deps in ONE layer ---------------------------------
# The python:3.11-alpine image already has python + pip.  ``aiohttp`` ships
# pre-built ``musllinux`` wheels for both ``aarch64`` and ``x86_64`` on PyPI
# since 3.9, so we do **not** need a C toolchain at all.
#
# Earlier versions installed ``build-base linux-headers`` as a virtual
# ``.build-deps`` package and then ``apk del`` it at the end.  This had a
# nasty side-effect: pulling in ``build-base`` triggers a ``musl`` upgrade
# (e.g. ``1.2.5-r22 -> 1.2.5-r23``), and the upgraded ``musl`` ABI is
# incompatible with the ``libpython3.11.so.1.0`` shipped in the base
# image.  The result was an addon that built fine but crash-looped at
# runtime with::
#
#     Error loading shared library libpython3.11.so.1.0: No such file
#     Error relocating /usr/local/bin/python3: Py_BytesMain: symbol not found
#
# Skipping ``build-base`` entirely sidesteps the ABI break, shaves ~270 MB
# off the image, and cuts build time by ~5 min on aarch64.
#
# Runtime deps retained:
#   * python3 + py3-pip — apk 装的 python（arch 一定与 base image 匹配，避免
#                        docker hub multi-arch manifest 解析错误导致的
#                        libpython3.11.so 找不到 / Py_BytesMain symbol 缺失）
#   * bash  — run.sh uses bash features (set -euo pipefail, etc.)
#   * jq    — run.sh parses /data/options.json with jq
#   * tini  — PID 1 signal forwarder, so SIGTERM reaches our Python service
#
# 中国大陆构建加速：用阿里云 alpine mirror + 清华 pypi mirror 替代海外源，
# 把构建时间从 ~10min 压到 ~1min。失败时自动回退官方源（|| true）。
#
# CACHE_BUST: 改这行可以强制 buildx 重做整个 RUN（绕过 layer cache）。
# 当 Supervisor 的 buildx cache 残留了旧 layer 时，
# 增加 v 后缀（v3、v4...）即可让所有缓存全部 miss。
ARG CACHE_BUST=v9
RUN set -eux; \
    echo "cache-bust=${CACHE_BUST}"; \
    # v3.0.0: switched to python:3.12-slim-bookworm (Debian glibc) so
    # onnxruntime's manylinux wheel installs cleanly.  Alpine musl was
    # ABI-incompatible with the glibc-built Python wheel (manylinux
    # .so files reference libpython3.12.so.1.0 which Alpine doesn't
    # ship).  The Debian base lets us `pip install onnxruntime` with
    # zero hacks; size cost ~+30 MB which we accept for full EMST
    # functionality on aarch64 (Pi 4B).
    apt-get update; \
    apt-get install -y --no-install-recommends \
        bash jq tini wget ca-certificates; \
    rm -rf /var/lib/apt/lists/*; \
    # Tsinghua mirror for faster pip in mainland China.  Falls back to
    # PyPI on failure (|| true).
    python3 -m pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple --break-system-packages 2>/dev/null || true; \
    python3 -m pip config set global.trusted-host pypi.tuna.tsinghua.edu.cn --break-system-packages 2>/dev/null || true; \
    # 网络超时 / retries 加到 v3.0.2：Pi 4B 国内网络访问 files.pythonhosted.org
    # 偶发 ReadTimeout，--timeout=300 + --retries=10 显著提高单次 build
    # 成功率。--prefer-binary 避免在 wheel 取不到时 fallback 到 sdist
    # 而在 add-on 镜像里跑 numpy 源码编译（aarch64 上要 30+ min 还会 OOM）。
    python3 -m pip install --no-cache-dir --break-system-packages \
        --timeout 300 --retries 10 --upgrade pip wheel; \
    # 第一次尝试用清华源；失败则 fall back 到 PyPI 官方源（aliyun 也行，
    # 但 onnxruntime aarch64 wheel 仅 PyPI 主仓和 pythonhosted 有完整副本）。
    python3 -m pip install --no-cache-dir --break-system-packages \
        --timeout 300 --retries 10 --prefer-binary \
        -r /app/requirements-runtime.txt \
        || python3 -m pip install --no-cache-dir --break-system-packages \
        --timeout 300 --retries 10 --prefer-binary \
        --index-url https://pypi.org/simple \
        -r /app/requirements-runtime.txt; \
    # Strip Python cache to keep image slim.
    find /usr -type d -name '__pycache__' -prune \
        -exec rm -rf {} + 2>/dev/null || true; \
    find /usr -name '*.pyc' -delete 2>/dev/null || true; \
    rm -rf /root/.cache /tmp/*

# v3.0.0: install onnxruntime directly via pip — already pulled by
# requirements-runtime.txt, but a separate RUN block here lets us
# verify it imports cleanly (build-time sanity check; failure is
# loud and immediate instead of silent at runtime).
ARG ORT_CACHE_BUST=v5
RUN set -eux; \
    echo "ort-cache-bust=${ORT_CACHE_BUST}"; \
    python3 -c "import onnxruntime; print('[onnxruntime] OK', onnxruntime.__version__)"

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
# COPY_BUST 用 build_version 间接打破 layer cache：每次 bump 版本时这一层
# 一定 cache miss，避免 supervisor 重用旧 run.sh。
ARG COPY_BUST=${BUILD_VERSION}
RUN echo "copy-bust=${COPY_BUST}"
COPY run.sh /run.sh
RUN chmod +x /run.sh; echo "=== run.sh head (build-time) ===" && head -40 /run.sh && echo "=== END run.sh head ==="

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

ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["/run.sh"]
