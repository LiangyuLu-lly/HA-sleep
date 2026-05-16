# Implementation Plan — Post-v2.0.2 Full-Pipeline Audit

本 tasks 清单基于 `bugfix.md`（12 条 bug）与 `design.md`（每条 bug 的 "实现方案" / "Property check" / "对 steering 的影响"）。
任务顺序：**探索性 property test 全量（Task 1）→ P0 串行修复（Task 2/3/4，对应 Bug 1.1 / 1.2 / 1.3）→ P1 并行修复（Task 5–9，对应 Bug 1.4 / 1.5 / 1.6 / 1.10 / 1.11）→ P2 并行修复（Task 10–13，对应 Bug 1.7 / 1.8 / 1.9 / 1.12）→ 集成验证 + 发布（Task 14）**。

所有 12 条 bug 在本轮都属"必做"（无 `*` 可选标记）。

---

## Task List

- [x] 1. 探索性 property test（必做，修复前写、修复前全部失败 / 观测到违反）
  - **Property 1: Bug Condition** — 12 条 bug 的存在性证明
  - **CRITICAL**: 本整组测试在未修复代码上必须 FAIL / 断言不成立；失败是正确结果，证明 bug 真实存在
  - **DO NOT** 在失败时去改测试或代码；先进到 Task 2 之后再逐条翻绿
  - 每条子任务对应 `design.md §6.1` 的探索性测试表一行；测试放到 `tests/` 下，文件名按 `§6.4` 的命名

  - [x] 1.1 Bug 1.1 探索测试 — 首次安装占位 sensor 缺失
    - 新增 `tests/test_bootstrap_placeholders.py::test_no_placeholder_sensors_without_bootstrap_script`
    - 使用 hypothesis-free 的 pytest-asyncio + aiohttp mock Supervisor，模拟 `SUPERVISOR_TOKEN` 已注入 / options `sleep_stage_source=""`
    - 断言：不启动 `bootstrap_placeholders.py` 时 60 s 内 `/api/states` 上没有任何 `sensor.sleep_classifier_*` 实体
    - 修复前：FAIL（占位脚本不存在，无法运行）→ 记录 counterexample "stage 未绑定 + 未跑 bootstrap ⇒ 0 个占位"
    - _Requirements: 1.1_ / _Design: §3.1_

  - [x] 1.2 Bug 1.2 探索测试 — Ingress 前端 AJAX 路径契约
    - 新增 `tests/test_web_ui_ingress_paths.py::test_frontend_uses_relative_fetch_paths`
    - 读 `sleep_classifier/web_ui.py` 全文，regex 匹配 `fetch\(\s*['"]/api/`；断言 count == 0
    - 追加 `test_aiohttp_routes_cover_api_paths`：`make_app()` 的 routes 必含 `("GET", "/api/entities")` 与 `("POST", "/api/options")`
    - 修复前：当前相对路径正确，测试 PASS；此任务重点是"契约测试 + 补 docstring 防回归"，在 Task 3 里一并跑
    - _Requirements: 1.2_ / _Design: §3.2_

  - [x] 1.3 Bug 1.3 探索测试 — SIGTERM 不转发导致丢数据
    - 新增 `tests/test_run_sh_signal_forwarding.py::test_run_sh_uses_wait_n_not_exec`
    - 读 `sleep_classifier/run.sh` 全文，断言文末不含 `exec python3` 且含 `wait -n "$PID_WEB"` 与 `trap _shutdown INT TERM`；Dockerfile 含 `ENTRYPOINT ["/sbin/tini", "-g", "--"]`
    - 修复前：FAIL（当前 run.sh 有 `exec python3 /app/web_ui.py`）
    - _Requirements: 1.3_ / _Design: §3.3, §4.1_

  - [x] 1.4 Bug 1.4 探索测试 — build.yaml 仍指向 ghcr.io
    - 新增 `tests/test_build_yaml_shape.py::test_build_from_not_ghcr`
    - yaml.parse `sleep_classifier/build.yaml`，断言 `build_from.aarch64` 与 `build_from.amd64` 均为 `python:3.11-alpine` 且不含 `ghcr.io`
    - 修复前：FAIL（aarch64 当前为 `ghcr.io/home-assistant/aarch64-base:3.19`）
    - _Requirements: 1.4_ / _Design: §3.4_

  - [x] 1.5 Bug 1.5 探索测试 — Dockerfile LABEL 数量不足 15
    - 新增 `tests/test_dockerfile_labels.py::test_dockerfile_has_15_labels`
    - 读 `sleep_classifier/Dockerfile`，用正则统计 `LABEL` 指令下的独立 key；断言 ≥ 15 且包含 5 条 `io.hass.*`（`name / description / arch / type / version`）+ 10 条 `org.opencontainers.image.*`
    - 修复前：FAIL（当前仅 4 条）
    - _Requirements: 1.5_ / _Design: §3.5_

  - [x] 1.6 Bug 1.6 探索测试 — startup 语义错
    - 新增 `tests/test_config_yaml_startup.py::test_startup_is_application`
    - yaml.parse `sleep_classifier/config.yaml`，断言 `startup == "application"`
    - 修复前：FAIL（当前值为 `services`）
    - _Requirements: 1.6_ / _Design: §3.6_

  - [x] 1.7 Bug 1.7 探索测试 — 原子写缺失
    - 新增 `tests/test_render_effective_config_atomic.py::test_mid_write_sigkill_preserves_main_file`
    - 用 `monkeypatch` 让 `Path.write_text` 在写入中途抛 `OSError` 模拟 SIGKILL；断言主文件 `/tmp/tmpdir/effective_config.json` 若已存在则保持旧内容完整合法 JSON
    - 修复前：FAIL（当前 `_OUT_PATH.write_text(...)` 非原子，中途失败主文件被截断）
    - _Requirements: 1.7_ / _Design: §3.7_

  - [x] 1.8 Bug 1.8 探索测试 — 缺 Ingress IP 白名单
    - 新增 `tests/test_web_ui_ip_guard.py::test_non_supervisor_ip_gets_200_without_guard`
    - 用 aiohttp `TestClient` + `make_mocked_request(remote="10.0.0.1")` 调 `/api/entities`；断言未加 middleware 时 status ∈ {200, 500}（不拦截）
    - 修复前：FAIL（当前 make_app 无 middleware，任意 IP 被接受）
    - _Requirements: 1.8_ / _Design: §3.8_

  - [x] 1.9 Bug 1.9 探索测试 — WS 单次 401 即 stop
    - 新增 `tests/test_ws_listener_error_classification.py::test_single_auth_error_sets_stop_event`
    - mock `iter_state_changes` 抛 1 次 `HAAuthError`；断言当前 `_task_ws_listener` 跑完后 `stop_event.is_set()` 为 True
    - 修复前：FAIL（单次即 stop，这正是 bug）
    - _Requirements: 1.9_ / _Design: §3.9_

  - [x] 1.10 Bug 1.10 探索测试 — AppArmor profile 缺失
    - 新增 `tests/test_apparmor_profile.py::test_apparmor_txt_exists_and_well_formed`
    - 断言 `sleep_classifier/apparmor.txt` 文件存在；读内容断言含 `profile sleep_classifier` / `/app/** r,` / `/data/** rwk,` / `/share/** rwk,` / `/dev/tty` / `#include <tunables/global>` 六条；yaml.parse(config.yaml) 断言 `apparmor == true`
    - 修复前：FAIL（文件不存在）
    - _Requirements: 1.10_ / _Design: §3.10_

  - [x] 1.11 Bug 1.11 探索测试 — Dockerfile 未用 ARG BUILD_FROM
    - 新增 `tests/test_dockerfile_build_from.py::test_dockerfile_uses_arg_build_from`
    - 读 `sleep_classifier/Dockerfile`，断言含 `ARG BUILD_FROM` 且出现 `FROM ${BUILD_FROM}`（或 `FROM $BUILD_FROM`）；不应存在未被 ARG 替换的硬编码 `FROM python:3.11-alpine`
    - 修复前：FAIL（当前为硬编码 FROM）
    - _Requirements: 1.11_ / _Design: §3.11_

  - [x] 1.12 Bug 1.12 探索测试 — config.yaml 缺 url 字段
    - 新增 `tests/test_config_yaml_url_and_labels.py::test_config_yaml_has_url`
    - yaml.parse(config.yaml)，断言 `url` 存在且等于 `https://github.com/LiangyuLu-lly/HA-sleep`
    - 修复前：FAIL（当前无 url 字段）
    - _Requirements: 1.12_ / _Design: §3.12_

---

- [x] 2. Bug 1.1 — 首次安装占位 sensor（P0，串行第 1）

  - [x] 2.1 新增 `sleep_classifier/bootstrap_placeholders.py`
    - 按 `design.md §3.1` 的完整代码样板实现：`aiohttp.ClientSession` 并发 POST 5 个 `sensor.sleep_classifier_*` 到 `http://supervisor/core/api/states/<id>`
    - 公共 attrs = `{"reason": "awaiting_stage_binding", "source": "bootstrap"}`
    - `SUPERVISOR_TOKEN` 缺失时静默返回 0，失败降级为 stderr 警告
    - _Requirements: 1.1, 2.1_ / _Design: §3.1 实现方案_

  - [x] 2.2 修改 `sleep_classifier/run.sh` 在 render_effective_config 之前插入 bootstrap 调用
    - 在 options 解析之后、`render_effective_config.py` 之前加一行：`python3 /app/bootstrap_placeholders.py || echo "[run.sh] placeholder publish failed — continuing"`
    - _Requirements: 1.1, 2.1_ / _Design: §3.1 实现方案_

  - [x] 2.3 新增 `tests/test_bootstrap_placeholders.py`（4 条单元测试）
    - 覆盖：(a) `SUPERVISOR_TOKEN` 缺失 → 静默跳过返回 0；(b) POST 全部成功 → 5 条占位写入；(c) POST 部分失败 → stderr 记录 + 其它继续；(d) entity_id / friendly_name 含中文正确编码
    - 跑 `pytest tests/test_bootstrap_placeholders.py -v` 全绿
    - _Requirements: 1.1_ / _Design: §6.4_

  - [x] 2.4 `prepare.sh` 镜像校验 + Task 1.1 探索测试转绿
    - 跑 `bash sleep_classifier/prepare.sh` 把 bootstrap_placeholders.py 同步到 `sleep_classifier/rootfs/`（如 prepare 逻辑覆盖 sleep_classifier/ 根下脚本，则确认实际包含路径）
    - 重跑 Task 1.1 的探索测试，断言翻绿（或补充 positive-path 断言："跑 bootstrap 后 60 s 内有 ≥ 5 个占位"）
    - _Requirements: 1.1_ / _Design: §3.1 Property check_

---

- [x] 3. Bug 1.2 — Ingress 前端 AJAX 相对路径契约（P0，串行第 2）

  - [x] 3.1 在 `sleep_classifier/web_ui.py` 的 HTTP handlers 区块上方补 docstring
    - 插入 `design.md §3.2` 给出的"IMPORTANT: 前端 fetch() 必须使用不以 '/' 开头的相对路径 …"注释块
    - 不改运行逻辑（当前前端已用相对路径）
    - _Requirements: 1.2, 2.2_ / _Design: §3.2 实现方案_

  - [x] 3.2 落地 Task 1.2 的契约测试
    - Task 1.2 已创建 `tests/test_web_ui_ingress_paths.py`，本子任务只需 `pytest tests/test_web_ui_ingress_paths.py -v` 断言全绿，作为回归守护
    - _Requirements: 1.2_ / _Design: §3.2 Property check_

---

- [x] 4. Bug 1.3 — SIGTERM 转发 / 停机落盘（P0，串行第 3）

  - [x] 4.1 修改 `sleep_classifier/Dockerfile` 的 ENTRYPOINT 为 tini `-g`
    - 在镜像末尾加 `STOPSIGNAL SIGTERM` + `ENTRYPOINT ["/sbin/tini", "-g", "--"]` + `CMD ["/run.sh"]`
    - 删除原有的 `CMD ["/run.sh"]` 单独一行（若存在）
    - _Requirements: 1.3, 2.3_ / _Design: §3.3 实现方案_

  - [x] 4.2 重写 `sleep_classifier/run.sh` 末尾的启动段
    - 去掉 `exec python3 /app/web_ui.py`
    - 按 `design.md §3.3` 的 bash 样板：`set -m` + Web UI 后台启动 + supervise_smart_service 后台启动 + `_shutdown()` trap + `wait -n "$PID_WEB" "$PID_SMART_SUP"` + 8 秒 grace KILL fallback
    - _Requirements: 1.3, 2.3_ / _Design: §3.3 实现方案, §4.1_

  - [x] 4.3 `sleep_classifier/run.sh` 添加 atomic-write tmp 清理 hook
    - 在 bootstrap 调用之后加一行：`find /data -maxdepth 2 -type f -name '*.tmp.*' -mmin +60 -delete 2>/dev/null || true`（为 Bug 1.7 预埋，本子任务即落地避免回头再改 run.sh）
    - _Requirements: 1.7_ / _Design: §3.7 实现方案_

  - [x] 4.4 让 Task 1.3 的 run.sh / Dockerfile 结构测试转绿
    - 重跑 `pytest tests/test_run_sh_signal_forwarding.py -v`，断言全绿
    - E2E 手动验证移到 Task 14
    - _Requirements: 1.3_ / _Design: §3.3 Property check, §6.5_

---

- [x] 5. Bug 1.4 — build.yaml 指向 Docker Hub（P1，与 Task 6–9 可并行）

  - [x] 5.1 改写 `sleep_classifier/build.yaml`
    - 按 `design.md §3.4` 的完整样板：`build_from.aarch64` / `build_from.amd64` 都改为 `python:3.11-alpine`；补齐 `labels:` 区块（`org.opencontainers.image.title / description / source / licenses` 4 条，镜像 labels 扩展到 15 条的主体在 Task 6 Dockerfile 里写）
    - 顶部保留 v2.0.3 决策 comment（对齐 hassio-addons/app-example）
    - _Requirements: 1.4, 2.4, 1.11, 2.11_ / _Design: §3.4 实现方案, §3.11_

  - [x] 5.2 让 Task 1.4 探索测试转绿
    - 重跑 `pytest tests/test_build_yaml_shape.py -v`，断言全绿
    - 加一条 `test_labels_block_has_min_4_entries`：断言 `labels` 区块至少含 4 条 OCI 标签
    - _Requirements: 1.4_ / _Design: §3.4 Property check_

---

- [x] 6. Bug 1.5 + Bug 1.12 — Dockerfile 15 条 LABEL + config.yaml 加 url（P1，合并，与其它 P1 可并行）

  - [x] 6.1 在 `sleep_classifier/Dockerfile` 的 `FROM ${BUILD_FROM}` 之后、`ENV` 之前插入 8 条 `ARG` 声明
    - 按 `design.md §3.5` 给出的列表：`BUILD_FROM` / `BUILD_ARCH` / `BUILD_VERSION` / `BUILD_NAME` / `BUILD_DESCRIPTION` / `BUILD_REPOSITORY` / `BUILD_REF` / `BUILD_DATE`，每个均带本地默认值
    - _Requirements: 1.5, 2.5_ / _Design: §3.5 实现方案_

  - [x] 6.2 在 Dockerfile 紧接 ARG 之后插入 15 条 `LABEL`
    - 按 `design.md §3.5` 的标签清单表：5 条 `io.hass.*` + 1 条 `maintainer` + 10 条 `org.opencontainers.image.*`（含 `created / revision / version`）
    - 值通过 `${BUILD_*}` 引用 ARG，确保 Supervisor 注入 / 本地 build 默认值兜底都能工作
    - _Requirements: 1.5, 2.5, 1.12, 2.12_ / _Design: §3.5 实现方案, §3.12_

  - [x] 6.3 修改 `sleep_classifier/config.yaml` 加 `url:` 字段
    - 在 `slug: sleep_classifier` 之后、`description:` 之前加 `url: "https://github.com/LiangyuLu-lly/HA-sleep"` + v2.0.3 决策注释
    - _Requirements: 1.12, 2.12_ / _Design: §3.12 实现方案_

  - [x] 6.4 让 Task 1.5 / 1.12 探索测试转绿
    - 重跑 `pytest tests/test_dockerfile_labels.py tests/test_config_yaml_url_and_labels.py -v`，断言全绿
    - 断言 key 数 ≥ 15，断言 5 条 `io.hass.*` key 全在
    - _Requirements: 1.5, 1.12_ / _Design: §3.5 Property check, §3.12 Property check_

  - [x] 6.5 E2E：Pi 4B 上 `docker inspect <image> | jq '.[0].Config.Labels | length'` 断言 ≥ 15
    - 记入 Task 14 的 check-list（本子任务仅登记，不执行）
    - _Requirements: 1.5_ / _Design: §6.5_

---

- [x] 7. Bug 1.6 — startup=application（P1，可并行）

  - [x] 7.1 修改 `sleep_classifier/config.yaml` 的 `startup:` 值
    - 从 `services` 改为 `application`
    - 顶部加 v2.0.3 决策注释（"本 add-on 通过 homeassistant_api: true 调用 HA Core，必须等 Core 就绪"）
    - _Requirements: 1.6, 2.6_ / _Design: §3.6 实现方案_

  - [x] 7.2 让 Task 1.6 探索测试转绿
    - 重跑 `pytest tests/test_config_yaml_startup.py -v`
    - _Requirements: 1.6_ / _Design: §3.6 Property check_

  - [x] 7.3 更新 `sleep_classifier/DOCS.md` 的 FAQ 段
    - 加一条："若 add-on 从不启动，请先确认 HA Core 本身健康（startup: application 依赖 Core 先就绪）"
    - _Requirements: 2.6_ / _Design: §3.6 风险与 fallback_

---

- [x] 8. Bug 1.10 — 自定义 AppArmor profile（P1，可并行）

  - [x] 8.1 新增 `sleep_classifier/apparmor.txt`
    - 按 `design.md §3.10` 的完整 profile 样板落地：`#include <tunables/global>` + `profile sleep_classifier flags=(attach_disconnected,mediate_deleted)` + `#include <abstractions/base>` + `#include <abstractions/python>`
    - allowlist：tini / bash / env / sh / python3* / jq / find / busybox 用 `rmix`；`/app/** r`；`/run.sh rix`；`/data/** rwk`；`/share/** rwk`；`/dev/tty rw` / `/dev/null rw` / `/dev/urandom r`；network inet/inet6 stream+dgram + unix stream；signal receive term/int/kill；`/proc/*/status|stat|meminfo|cpuinfo|loadavg` r；`/tmp rwk`；`/etc/localtime` / `/etc/timezone` / `/usr/share/zoneinfo/**` r
    - _Requirements: 1.10, 2.10_ / _Design: §3.10 实现方案_

  - [x] 8.2 修改 `sleep_classifier/config.yaml` 声明 `apparmor: true`
    - 在 ingress 相关字段附近加 `apparmor: true` + v2.0.3 决策注释
    - _Requirements: 1.10, 2.10_ / _Design: §3.10 实现方案_

  - [x] 8.3 让 Task 1.10 探索测试转绿
    - 重跑 `pytest tests/test_apparmor_profile.py -v`，断言文件存在 + 6 条关键 allowlist 命中 + config.yaml `apparmor == true`
    - _Requirements: 1.10_ / _Design: §3.10 Property check_

  - [x] 8.4 E2E：Pi 4B 上 `docker inspect <container> --format '{{.AppArmorProfile}}'` 返回 `"sleep_classifier"` + HA 日志 grep `apparmor="DENIED"` 无命中
    - 登记到 Task 14 的 check-list
    - _Requirements: 2.10_ / _Design: §6.5_

---

- [x] 9. Bug 1.11 — Dockerfile 用 ARG BUILD_FROM（P1，可并行）

  - [x] 9.1 修改 `sleep_classifier/Dockerfile` 的 FROM 段
    - 删除硬编码 `FROM python:3.11-alpine`
    - 加 `ARG BUILD_FROM=python:3.11-alpine` 与 `FROM ${BUILD_FROM}` 两行，上面保留 v2.0.1 / v2.0.3 决策 comment
    - 注意：本子任务的 ARG 与 Task 6.1 的 ARG 声明需合并到同一段（如 Task 6 先做，Task 9.1 只改 FROM；如 Task 9 先做，Task 6.1 在此基础上扩 ARG 列表）
    - _Requirements: 1.11, 2.11_ / _Design: §3.11 实现方案_

  - [x] 9.2 让 Task 1.11 探索测试转绿
    - 重跑 `pytest tests/test_dockerfile_build_from.py -v`
    - _Requirements: 1.11_ / _Design: §3.11 Property check_

---

- [x] 10. Bug 1.7 — 原子写通用化（P2，可并行）

  - [x] 10.1 新增 `src/_io_utils.py`
    - 按 `design.md §3.7` 的完整代码落地 `atomic_write_text(path, data, *, encoding="utf-8")` 与 `atomic_write_json(path, obj, *, indent=2)`
    - 策略：`tempfile.mkstemp(prefix=path.name + ".tmp.", dir=path.parent)` → fdopen write + fsync → `os.replace` → 异常路径 unlink tmp
    - reStructuredText docstring，`from __future__ import annotations`
    - _Requirements: 1.7, 2.7_ / _Design: §3.7 实现方案_

  - [x] 10.2 替换 `sleep_classifier/render_effective_config.py` 的写入调用
    - `_OUT_PATH.write_text(...)` 改为 `atomic_write_text(_OUT_PATH, json.dumps(...))`
    - 顶部加 `from src._io_utils import atomic_write_text`（或按现有 import 结构等价）
    - _Requirements: 1.7_ / _Design: §3.7 实现方案_

  - [x] 10.3 替换 `sleep_classifier/web_ui.py::api_save` 的写入调用
    - 当前已走 tmp+replace 但无 fsync；统一改为 `atomic_write_text(_OVERRIDES_PATH, json.dumps(cleaned, indent=2, ensure_ascii=False))`
    - _Requirements: 1.7_ / _Design: §3.7 实现方案_

  - [x] 10.4 替换 `src/preference_learner.py::save` 的写入调用
    - 改为 `atomic_write_json(self._history_path, payload)`
    - 保留 `.bak` 备份路径（preservation 3.6）
    - _Requirements: 1.7, 3.6_ / _Design: §3.7 实现方案_

  - [x] 10.5 替换 `src/user_profile.py::save` 的写入调用
    - 改为 `atomic_write_json(self._path, existing)`
    - _Requirements: 1.7_ / _Design: §3.7 实现方案_

  - [x] 10.6 替换 `src/apnea_wiring.py` 的写入调用
    - 改为 `atomic_write_json(self._path, payload)`
    - _Requirements: 1.7_ / _Design: §3.7 实现方案_

  - [x] 10.7 新增 `tests/test_io_utils_atomic_write.py`（5 条）
    - 覆盖：(a) 正常写 + 读回等值；(b) 写前主文件存在时 mid-write 异常不损坏主文件；(c) 异常路径 tmp 文件被清理；(d) 父目录自动创建；(e) UTF-8 中文内容正确落盘
    - _Requirements: 1.7_ / _Design: §6.4_

  - [x] 10.8 让 Task 1.7 探索测试转绿
    - 重跑 `pytest tests/test_render_effective_config_atomic.py tests/test_io_utils_atomic_write.py -v`
    - _Requirements: 1.7_ / _Design: §3.7 Property check_

---

- [x] 11. Bug 1.8 — Ingress IP 白名单 middleware（P2，可并行）

  - [x] 11.1 在 `sleep_classifier/web_ui.py` 加 `ingress_ip_guard` middleware
    - 按 `design.md §3.8` 的代码样板：`_DEFAULT_ALLOWED_IPS = {"172.30.32.2"}`；env `SUPERVISOR_IP_WHITELIST` 覆盖；env `WEB_UI_DISABLE_INGRESS_GUARD=1` 豁免；`request.remote.removeprefix("::ffff:")` 处理 IPv6 映射
    - `make_app()` 改为 `web.Application(middlewares=[ingress_ip_guard])`
    - 非允许 IP 记 warning 日志并返回 `web.Response(status=403, text="Forbidden")`
    - _Requirements: 1.8, 2.8_ / _Design: §3.8 实现方案_

  - [x] 11.2 新增 `tests/test_web_ui_ip_guard.py`（6 条）
    - 覆盖：(a) `172.30.32.2` 通过；(b) `::ffff:172.30.32.2` 通过；(c) `10.0.0.1` → 403；(d) `127.0.0.1` → 403；(e) `WEB_UI_DISABLE_INGRESS_GUARD=1` 时任意 IP 通过；(f) `SUPERVISOR_IP_WHITELIST="fd00::2,127.0.0.1"` 覆盖默认
    - 用 `aiohttp.test_utils.make_mocked_request` + middleware 直接调用
    - _Requirements: 1.8_ / _Design: §6.4_

  - [x] 11.3 让 Task 1.8 探索测试转绿
    - 重跑 `pytest tests/test_web_ui_ip_guard.py -v`
    - E2E（Task 14）：从另一机器 `curl http://<pi-ip>:8099/` 期望 403
    - _Requirements: 1.8_ / _Design: §3.8 Property check_

---

- [x] 12. Bug 1.9 — WS 错误分类 + auth 失败累计（P2，可并行）

  - [x] 12.1 改写 `scripts/run_ha_smart_service.py::_task_ws_listener` 的 exception 分支
    - 按 `design.md §3.9` 的样板：把 `HAAuthError` 子类放在 `HAAPIError` 父类之前 catch；加 `MAX_AUTH_FAILURES = 10` 常量与 `auth_failures` 局部计数器
    - 成功处理任意 state event 后重置 `auth_failures = 0` 与 `backoff = 1.0`
    - `HAAuthError` 单次 → `auth_failures += 1` + warn + 重连；连续 ≥ 10 次 → `stop_event.set()` + error log
    - `HAAPIError` / 裸 `Exception` → warn + 重连（指数退避 1→300 秒 + ±20% jitter）
    - 重连段 `await ha.connect_websocket(); await ha.subscribe_state_changes()` 同样把 HAAuthError 计入 auth_failures
    - 不改 `src/ha_api_client.py`（异常层次已正确）
    - _Requirements: 1.9, 2.9_ / _Design: §3.9 实现方案, §4.5, §4.8_

  - [x] 12.2 新增 `tests/test_ws_listener_error_classification.py`（6 条）
    - 覆盖：(a) 单次 `HAAuthError` → `stop_event` 未 set；(b) 连续 10 次 `HAAuthError` → `stop_event.set()`；(c) `HAAPIError` 500 → 重连；(d) `CancelledError` → re-raise；(e) `aiohttp.ClientConnectorError` → 重连；(f) auth 成功一次后 counter 重置到 0
    - 使用 `asyncio.wait_for(..., timeout=...)` 避免 backoff 卡住测试
    - _Requirements: 1.9_ / _Design: §6.4_

  - [x] 12.3 让 Task 1.9 探索测试转绿
    - 重跑 `pytest tests/test_ws_listener_error_classification.py -v`
    - _Requirements: 1.9_ / _Design: §3.9 Property check_

---

- [x] 13. 更新 steering（tech.md / structure.md）

  - [x] 13.1 更新 `.kiro/steering/tech.md`，新增 4 条代码约定
    - (a) Add-on entrypoint 禁止在末尾 `exec` 替换 bash，必须用 `tini -g` + `wait -n` 保留 trap + job control
    - (b) `/data` 下 JSON 必须走 `src._io_utils.atomic_write_json`，禁止直接 `Path.write_text`
    - (c) HA 相关异常 catch 顺序：`HAAuthError` 子类在前、`HAAPIError` 父类在后；auth 错误须用 `MAX_AUTH_FAILURES` 计数，不可单次触发 stop
    - (d) 本项目配置读取走 `jq + bash`，不用 bashio；未来迁移 bashio 须先评估是否切回 HA 官方 base image（见 design §4.9）
    - _Requirements: 2.3, 2.7, 2.9_ / _Design: §7_

  - [x] 13.2 更新 `.kiro/steering/structure.md`
    - `src/` 目录清单新增一行：`_io_utils.py —— atomic write helpers (JSON / 文本)，I/O 相关纯函数辅助`
    - `sleep_classifier/` 目录清单新增：`bootstrap_placeholders.py —— 启动期抢发占位 sensor` 与 `apparmor.txt —— 自定义 AppArmor profile，+1 安全分`
    - _Requirements: 2.1, 2.7, 2.10_ / _Design: §7_

---

- [x] 14. 集成验证 + 版本号同步 + CHANGELOG + 发布

  - [x] 14.1 全量 pytest 绿
    - 跑 `pytest tests/ -v`（用 `--run` 意义上的单次执行，`pytest` 默认就是非 watch）
    - 原 501 条测试全绿 + 新增 ~38 条测试全绿，预计约 539 条
    - preservation 检查：3.1–3.8 对应的既有测试（如 `test_preference_learner.py` 的 `.bak` 恢复路径 / `test_smart_sleep_service_*.py` 的 state_changed 分发 / dry_run 默认等）全部保持绿
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8_ / _Design: §6.3_

  - [x] 14.2 版本号 2.0.2 → 2.0.3 同步
    - `sleep_classifier/config.yaml`：`version: "2.0.2"` → `version: "2.0.3"`
    - `setup.py`：`version="2.0.0"` → `version="2.0.3"`（顺带把 v2.0.0 的历史遗留补齐）
    - `pyproject.toml`：`version = "2.0.0"` → `version = "2.0.3"`
    - 检查 `src/__init__.py`（如有 `__version__`）同步
    - _Requirements: 2.5, 2.12_ / _Design: §8_

  - [x] 14.3 更新根目录 `CHANGELOG.md`
    - 加 `## [2.0.3] — YYYY-MM-DD` 段，分 "Fixed"（Bug 1.1–1.9）/ "Added"（Bug 1.10–1.12，AppArmor profile、完整 OCI labels、config.yaml url）/ "Changed"（build_from 指向 Docker Hub、startup application、run.sh wait -n）三小节
    - 每条对应到 bugfix 编号
    - _Requirements: all_ / _Design: §8_

  - [x] 14.4 更新 `sleep_classifier/CHANGELOG.md`
    - 添加同样的 2.0.3 段，可复用根目录 CHANGELOG 条目；面向 add-on 用户的视角（HA UI 会渲染这份）
    - _Requirements: all_ / _Design: §8_

  - [x] 14.5 （若 prepare.sh 需要）重跑 `bash sleep_classifier/prepare.sh`
    - 把改动后的 `src/*` / `scripts/*` / `training_config/*` / `requirements-runtime.txt` 镜像到 `sleep_classifier/rootfs/`
    - 确认 `sleep_classifier/rootfs/src/_io_utils.py` 存在
    - _Requirements: 2.1_ / _Design: §结构不变量_

  - [x] 14.6 推送到 HA Pi 4B：`.\sync-to-ha.ps1 -SkipPull`（或标准流程）
    - PowerShell（Windows 开发机）执行 sync 脚本
    - 在 HA UI：Settings → Add-ons → Sleep Classifier → REBUILD → START
    - _Requirements: all_ / _Design: §6.5_

  - [x] 14.7 Pi 4B E2E check-list 手动验证（按 design §6.5 全表）
    - [ ] Web UI 30 秒内能打开
    - [ ] Lovelace 60 秒内有 ≥ 5 个 `sensor.sleep_classifier_*`，`attributes.reason == "awaiting_stage_binding"`（Bug 1.1）
    - [ ] 绑 stage + RESTART 后 smart service 正常 publish 真实 stage（3.2 preservation）
    - [ ] 再次 RESTART 时日志出现 `[run.sh] signal received — forwarding SIGTERM`，容器 8 秒内退出，`/data/user_preferences.json` mtime 被更新（Bug 1.3）
    - [ ] `docker inspect <image> | jq '.[0].Config.Labels | length'` ≥ 15，且含 5 条 `io.hass.*`（Bug 1.5 / 1.12）
    - [ ] 从另一机器 `curl http://<pi-ip>:8099/` 返回 403（Bug 1.8）
    - [ ] `docker inspect <container> --format '{{.AppArmorProfile}}'` 返回 `"sleep_classifier"`（Bug 1.10）
    - [ ] `docker logs <container> 2>&1 | grep 'apparmor="DENIED"'` 无命中（Bug 1.10）
    - [ ] `docker build` 日志 FROM 行展开为 `python:3.11-alpine`（Bug 1.11）
    - [ ] Supervisor UI add-on 详情页显示可点击 "Project homepage" 跳 GitHub（Bug 1.12）
    - [ ] 用一次 Core 重启（HA OS 重启或 `ha core restart`），观察 add-on 日志 `auth_failures` warn 不触发永久 stop，Core 回来后 smart service 自动恢复（Bug 1.9）
    - _Requirements: all_ / _Design: §6.5_

---

## 预计新增 / 修改文件清单（给用户提前预告范围）

### 新增（8 个）

| 路径 | 来源 Task | 说明 |
|---|---|---|
| `sleep_classifier/bootstrap_placeholders.py` | 2.1 | 启动期抢发 5 个占位 sensor |
| `sleep_classifier/apparmor.txt` | 8.1 | 自定义 AppArmor profile，+1 安全分 |
| `src/_io_utils.py` | 10.1 | 通用 atomic_write_text / atomic_write_json |
| `tests/test_bootstrap_placeholders.py` | 1.1 / 2.3 | 4 条单元测试 |
| `tests/test_web_ui_ingress_paths.py` | 1.2 | 2 条契约测试 |
| `tests/test_run_sh_signal_forwarding.py` | 1.3 | run.sh / Dockerfile 结构测试 |
| `tests/test_build_yaml_shape.py` | 1.4 | build.yaml 结构测试 |
| `tests/test_dockerfile_labels.py` | 1.5 | LABEL ≥ 15 + io.hass.* 5 条 |
| `tests/test_config_yaml_startup.py` | 1.6 | startup == "application" |
| `tests/test_render_effective_config_atomic.py` | 1.7 | 原子写探索测试 |
| `tests/test_io_utils_atomic_write.py` | 10.7 | 5 条单元测试 |
| `tests/test_web_ui_ip_guard.py` | 1.8 / 11.2 | 6 条 middleware 测试 |
| `tests/test_ws_listener_error_classification.py` | 1.9 / 12.2 | 6 条错误分类测试 |
| `tests/test_apparmor_profile.py` | 1.10 | apparmor.txt 结构 + config.yaml apparmor: true |
| `tests/test_dockerfile_build_from.py` | 1.11 | Dockerfile 含 ARG BUILD_FROM + FROM ${BUILD_FROM} |
| `tests/test_config_yaml_url_and_labels.py` | 1.12 | config.yaml url + LABEL 断言 |

> 实际"新增"主项 3 个（bootstrap_placeholders.py / apparmor.txt / _io_utils.py），其余 13 个均为 tests/ 下新文件（共 ~38 条测试）。

### 修改（9 个）

| 路径 | 来源 Task | 说明 |
|---|---|---|
| `sleep_classifier/run.sh` | 4.2 / 4.3 / 2.2 | 去 exec + wait -n 架构 + tmp 清理 + bootstrap 调用 |
| `sleep_classifier/Dockerfile` | 4.1 / 6.1 / 6.2 / 9.1 | tini -g ENTRYPOINT + 8 条 ARG + 15 条 LABEL + ARG BUILD_FROM FROM |
| `sleep_classifier/config.yaml` | 6.3 / 7.1 / 8.2 / 14.2 | 加 url / startup=application / apparmor: true / version=2.0.3 |
| `sleep_classifier/build.yaml` | 5.1 | build_from → python:3.11-alpine + 4 条 OCI labels |
| `sleep_classifier/web_ui.py` | 3.1 / 10.3 / 11.1 | docstring + atomic_write + ingress_ip_guard middleware |
| `sleep_classifier/render_effective_config.py` | 10.2 | atomic_write_text |
| `sleep_classifier/DOCS.md` | 7.3 | FAQ 加一条 "若 add-on 从不启动…" |
| `src/preference_learner.py` | 10.4 | atomic_write_json（保留 .bak 备份） |
| `src/user_profile.py` | 10.5 | atomic_write_json |
| `src/apnea_wiring.py` | 10.6 | atomic_write_json |
| `scripts/run_ha_smart_service.py` | 12.1 | _task_ws_listener 错误分类 + MAX_AUTH_FAILURES 计数 |
| `setup.py` | 14.2 | version 2.0.0 → 2.0.3 |
| `pyproject.toml` | 14.2 | version 2.0.0 → 2.0.3 |
| `CHANGELOG.md` | 14.3 | 新增 2.0.3 段 |
| `sleep_classifier/CHANGELOG.md` | 14.4 | 新增 2.0.3 段 |
| `.kiro/steering/tech.md` | 13.1 | 新增 4 条代码约定 |
| `.kiro/steering/structure.md` | 13.2 | 目录清单补 3 个文件 |

### 镜像同步（prepare.sh 驱动）

| 路径 | 说明 |
|---|---|
| `sleep_classifier/rootfs/src/_io_utils.py` | Task 14.5 prepare 后产生 |
| `sleep_classifier/rootfs/src/preference_learner.py` | 同步改动 |
| `sleep_classifier/rootfs/src/user_profile.py` | 同步改动 |
| `sleep_classifier/rootfs/src/apnea_wiring.py` | 同步改动 |
| `sleep_classifier/rootfs/scripts/run_ha_smart_service.py` | 同步改动 |

---

## 任务并行 / 串行规则

- **Task 1**（探索测试）：12 条子任务互相独立，可并行；但必须全部在 Task 2 开始前写完跑完。
- **P0（Task 2 → 3 → 4）**：严格串行。Bug 1.3 的 run.sh 重构依赖 Bug 1.1 的 bootstrap 调用在 run.sh 里已存在。
- **P1（Task 5 / 6 / 7 / 8 / 9）**：可并行，但 Task 6（Dockerfile ARG + LABEL）与 Task 9（Dockerfile ARG BUILD_FROM）改同一文件，建议同一人做或合并到同次 PR；若分开，合并时注意 ARG 块合并。
- **P2（Task 10 / 11 / 12）**：可并行，文件无冲突（Task 10 改 render/_io_utils/preference_learner/user_profile/apnea_wiring + web_ui；Task 11 改 web_ui；Task 12 改 run_ha_smart_service）。Task 10 与 Task 11 都动 web_ui.py，建议 Task 10 先做 atomic_write 替换，Task 11 再加 middleware。
- **Task 13**（steering 更新）：在 Task 2–12 全部完成后、Task 14 之前做，确保约定与实际代码一致。
- **Task 14**（集成 + 发布）：最后一步，依赖前面全部。

---

## 完成标准

- 14 个主任务全部勾上 `[x]`
- `pytest tests/ -v` 全绿，测试数 ≥ 539（501 原有 + ~38 新增）
- Pi 4B E2E check-list 14.7 的 11 项全部手动勾上
- CHANGELOG 双写（根目录 + sleep_classifier/）
- 版本号三处（config.yaml / setup.py / pyproject.toml）均为 `2.0.3`
- steering tech.md / structure.md 已更新
