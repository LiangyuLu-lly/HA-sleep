# Bugfix Requirements Document — v2.0.2 之后全流程审计

## Introduction

v2.0.2 修复了两条明显的启动失败路径（Web UI 502 + stage 未配置时容器重启循环）。本次对 Dockerfile → run.sh → render_effective_config.py → run_ha_smart_service.py → web_ui.py → config.yaml 全链路做了第二轮系统审计，同时对照 Home Assistant 开发者文档（2025–2026）与 aiohttp / Alpine 社区规范做横向核验。

审计结论：**v2.0.2 之后仍存在 12 条可独立触发失败 / 商业化差距的真实问题**（9 条 v2.0.2 首轮审计 + 3 条 v2.0.3 对标 `hassio-addons/app-example` 后补的商业级元数据 / 安全分），覆盖「容器启动失败」「信号/停机丢数据」「Ingress 契约不合规」「首次安装体验断点」「Supervisor 新版兼容性」「商业化元数据 + 自定义 AppArmor profile 欠完整」六类。其中 3 条会直接让首次安装流程跑不通（P0），其余在极端条件或 HA/Supervisor 版本演进时会失败（P1 / P2）。

本文档只写「问题存在、如何观测、如何复现、根因、修复草案」。具体重构交给下一阶段的 design.md。

---

## 开发规范（2025–2026 HA Add-on 官方口径）

审计过程中从 HA 开发者文档与社区追回了 5 条与本项目相关的最新规范，作为后续设计依据：

1. **Builder 迁移**：Supervisor 2026.04.0 起不再自动注入 `BUILD_FROM` 构建参数，`build.yaml` 已废弃；Dockerfile 必须用显式 `FROM ghcr.io/home-assistant/base:latest` 或其他镜像。Supervisor 仍向下兼容读 `build.yaml` 但会输出 deprecation 警告，后续版本会移除。引用：[Migrating app builds to Docker BuildKit](https://developers.home-assistant.io/blog/2026/04/02/builder-migration)。
2. **Labels 不再自动生成**：旧 legacy builder 会自动注入 `io.hass.type / io.hass.name / io.hass.description / io.hass.url`；新 BuildKit builder 不再推断，需要开发者在 Dockerfile 用 `LABEL` 显式写。引用同上。
3. **Ingress 契约**：`ingress_port` 默认 8099（无需在 config.yaml 重复声明）；**应用服务器必须仅允许来自 `172.30.32.2` 的连接，拒绝其它所有 IP**。引用：[Add-on Presentation — Ingress](https://developers.home-assistant.io/docs/add-ons/presentation)。
4. **Ingress 前端路径规范**：前端 fetch 必须使用**相对路径**（`./api/entities`、`api/entities`），不能使用绝对路径 `/api/entities`。Supervisor 会把 `/api/hassio_ingress/<token>/` 作为前缀透明注入，绝对路径会脱离 Ingress 命名空间导致 404 / 500。引用：社区主线讨论 [Can't get HA add-on Ingress to work](https://community.home-assistant.io/t/cant-get-ha-add-on-ingress-to-work-what-am-i-doing-wrong/766070)。
5. **`startup` 语义**：`services` 表示"在 HA Core 之前启动"，适合数据库/消息中间件之类 HA 依赖的服务；`application` 表示"在 HA Core 之后启动"，适合使用 HA API 的 add-on。**使用 `homeassistant_api: true` 的 add-on 应当用 `application`**，否则 Core 还没就绪就发 REST 请求会 502/503。引用：[Config.json startup option](https://community.home-assistant.io/t/config-json-startup-option/60781)。
6. **`SUPERVISOR_TOKEN` 生命周期**：Supervisor 为每个 add-on 容器启动时注入 `SUPERVISOR_TOKEN` 环境变量，容器运行期间保持不变；**但 Supervisor 可在任何时候重启 add-on，导致容器内缓存的 token 指向一个已经失效的会话**。Supervisor 日志中的 "Updated Home Assistant API token" 指的是 Supervisor 自身对 HA Core 的长效令牌更新，不是 add-on env 的更新。引用：[Supervisor API endpoints](https://developers.home-assistant.io/docs/api/supervisor/endpoints/)。

> 为符合许可限制，所有引用内容均经重述。具体原文请点链接。

---

## Bug Condition C(X) 的形式化定义

```pascal
// X: 任意"刚做完 REBUILD → START"的 add-on 系统状态
// 包含：options.json 的取值、HA Core / Supervisor 版本、宿主网络条件、时序
TYPE SystemState = RECORD
  options: Options               // /data/options.json 的用户填值
  ha_version: Version            // HA Core 版本
  supervisor_version: Version    // Supervisor 版本
  ha_core_ready: bool            // 启动时 HA Core REST API 是否已就绪
  arch: {aarch64, amd64}
  t_since_start_seconds: float   // 从容器启动计时
END

FUNCTION isBugCondition(X: SystemState): bool
  // 从"首次安装成功 → 稳定运行 30 分钟 → Lovelace 有至少 1 个可交互的
  // sleep_classifier sensor → 能响应 REBUILD / RESTART / 配置变更"任何一条
  // 失败，都视为 C(X) 成立。
  RETURN NOT all_of(
    R1_web_ui_reachable_within(T=60s),
    R2_smart_service_starts_after_user_binds_stage,
    R3_sigterm_flushes_user_preferences_within(T=10s),
    R4_effective_config_never_partially_written,
    R5_web_ui_rejects_non_supervisor_clients,
    R6_ha_core_restart_does_not_kill_service_permanently,
    R7_supervisor_2026_04_plus_builds_cleanly,
    R8_lovelace_shows_sensor_before_stage_is_bound,
    R9_web_ui_fetch_uses_relative_paths
  )
END FUNCTION
```

若存在某个 X 使 `isBugCondition(X) == true`，就意味着"v2.0.2 后流程仍有缺口"。下面每条 bug 都给出一个这样的 X（preservation check）。

---

## Bug Analysis

### 1. Current Behavior (Defect)

按优先级（能否让首次安装跑不通）排列。

#### P0 — 可直接让用户"装上就用不了"

1.1 WHEN 用户按 `.\sync-to-ha.ps1 -SkipPull` → REBUILD → START 的标准流程首次启动 add-on 且没有立刻去 Web UI 绑定 `sleep_stage_source` THEN 系统不在 HA 中发布任何 `sensor.sleep_classifier_*` 实体，Lovelace 全部显示 "Entity not available"，用户无法通过 Lovelace 确认 add-on 是否活着。

1.2 WHEN `web_ui.py` 的前端 JavaScript 向 `api/entities` 和 `api/options` 发请求且 Ingress 把路径前缀 `/api/hassio_ingress/<session>/` 注入后，`api/options` 的 POST 路径在一类 Ingress 会话中被解析为脱离前缀的绝对 `/api/options` THEN 浏览器收到 404（Supervisor Ingress 层返回）或 500（命中了不存在的 HA Core 路由），保存操作静默失败，用户的实体选择不能落盘。

1.3 WHEN Supervisor 在 add-on 容器运行过程中发送 SIGTERM（REBUILD、重启、HA OS 更新都会触发）THEN `run.sh` 尾部 `exec python3 /app/web_ui.py` 之前注册的 `trap 'kill $SUPERVISOR_PID ...' INT TERM EXIT` 被 exec 替换掉，`supervise_smart_service` 后台子 shell 变成 `web_ui.py` 的孤儿进程，tini 只把 SIGTERM 送给 PID 1 上的 Web UI，smart service 接收不到停机信号，10 秒后被 SIGKILL，`user_preferences.json`、`apnea_baseline.json` 等 "flush on shutdown" 承诺全部失效。

#### P1 — Supervisor / HA Core 版本演进 + 商业化安全分 + 对齐官方 pattern

1.4 WHEN Supervisor 升级到 2026.04.0+ 且 `build.yaml` 被读取 THEN Supervisor 日志输出 deprecation 警告；若未来版本移除兼容路径，构建时会报 `base name (${BUILD_FROM}) should not be blank` 的错（尽管当前 Dockerfile 用的是 `FROM python:3.11-alpine`，`build.yaml` 里的 `build_from` 被 Supervisor 读进去但不会被 Dockerfile 消费，逻辑上矛盾）。

1.5 WHEN 使用 Docker BuildKit 或者未来的 builder 版本时 THEN Dockerfile 缺少 `LABEL io.hass.version="${BUILD_VERSION}"`、`io.hass.type="addon"`、`io.hass.arch="..."`，image 不带 HA Supervisor 识别的元数据，部分场景下 UI 版本比对 / 依赖验证会显示 "unknown"。

1.6 WHEN `config.yaml` 配 `startup: services` 时 THEN Supervisor 会在 HA Core 之前启动 add-on，但本 add-on 的 `SmartSleepService.run()` 启动时立即调用 `http://supervisor/core/api/` ping，Core 尚未就绪，`ha.ping()` 返回 False 导致 `rc=2`，外层 bash supervise 循环进入 2 s → 60 s 指数退避；正常情况下前两次重试就会追上 Core，但**重试期间不会发任何诊断日志**，用户看到的是"装完了静默等 1 分钟才有东西"。

1.10 WHEN Supervisor 启动 add-on 容器时 THEN 由于 `sleep_classifier/` 下没有 `apparmor.txt`，Supervisor 无法加载自定义 AppArmor profile，容器以默认 "unconfined" / `docker-default` 策略运行。HA 2024 年起的 add-on 评分规则把"自定义 apparmor profile"列为 **+1 分** 加分项，商业级 add-on（hassio-addons 仓库下的 80+ 个）一贯配齐；我们目前的安全分停在基础 5 分（ingress=+2 / auth_api=被 ingress override / apparmor=0），距离满分 6 分差这一项。对标参考：[Add-on security ratings](https://developers.home-assistant.io/docs/add-ons/presentation#add-on-ratings)。

1.11 WHEN Supervisor 构建 add-on 镜像时 THEN `sleep_classifier/build.yaml` 的 `build_from` 字段仍写 `ghcr.io/home-assistant/aarch64-base:3.19`（v2.0.0 遗留），但 `sleep_classifier/Dockerfile` 硬编码 `FROM python:3.11-alpine`（v2.0.1 的国内网络决策），两者不一致。对标 `hassio-addons/app-example` 后确认 HA 官方维护的 add-on **都保留 build_from 并通过 `ARG BUILD_FROM` + `FROM ${BUILD_FROM}` 消费它**，这是新老 builder 都认的 pattern。我们需要把 Dockerfile 改成 `ARG BUILD_FROM=python:3.11-alpine` + `FROM ${BUILD_FROM}`，build.yaml 的值同步改 `python:3.11-alpine`，消除漂移并回到官方 pattern。

#### P2 — 极端条件、数据保护、商业化元数据补完

1.7 WHEN `render_effective_config.py` 写 `/data/effective_config.json` 的过程中容器被 SIGKILL（磁盘满、OOM、硬件掉电）THEN `Path.write_text` 不是原子操作，磁盘上留下一个不完整的 JSON，下一次 `run.sh` 启动时 `jq -r '.home_assistant.api.sleep_stage_source // ""' /data/effective_config.json 2>/dev/null` 返回空字符串（`2>/dev/null` 吞掉了 jq 的 parse error），supervisor 循环认为未绑定 stage，陷入"永远等用户"状态。Web UI 仍然可见，但再点"重新加载实体列表"也无法触发再生成——因为 Web UI 不主动重写 effective_config。

1.8 WHEN 外部客户端（非 Supervisor）直接访问 `http://<addon-container-ip>:8099/` 时 THEN `aiohttp` 的 `web.run_app(host="0.0.0.0", port=8099)` 会响应，没有任何 IP 白名单过滤。Home Assistant Add-on 安全规范要求应用服务器必须仅允许 `172.30.32.2` 的连接。虽然默认 Docker 网络下外部可达性有限，但在 `host_network: true`、被误转发到公网、或用户自建反向代理时会泄露 HA states 列表（含实体名称、area、device_class 等）。

1.9 WHEN HA Core 重启或 WebSocket 暂时断开 THEN `_task_ws_listener` 捕获到 `HAAuthError | HAAPIError` 就立即 `self.stop_event.set()` 让整个 smart service 退出（见 `scripts/run_ha_smart_service.py:611-615`）。Core 重启期间 `/api/websocket` 会短暂返回 401 或 503，这两种都被归为 `HAAPIError`，smart service 被外层 bash 循环重建没问题，但**每次 Core 重启用户会看到 add-on 日志"exited rc=X"的噪声**，且重建期间 `publisher` 对象被销毁后 `publish_initial_placeholders` 在 2 s 延迟之后重跑，跨越这段时间的 sensor 状态会先变 "Entity not available" 再回来。

1.12 WHEN 用户打开 Supervisor UI 的 add-on 详情页 THEN 页面上没有可点击的"项目主页"链接，因为 `sleep_classifier/config.yaml` 没有 `url:` 字段；同时 Dockerfile 的 `LABEL` 只有 4 条（`io.hass.type` / `io.hass.version` / `io.hass.arch` / `org.opencontainers.image.version`），对标 `hassio-addons/app-example` 的 15 条工业级标准（5 条 `io.hass.*` + 10 条 `org.opencontainers.image.*`）差距明显：缺 `io.hass.name`、`io.hass.description`、`maintainer`、`org.opencontainers.image.title`、`vendor`、`authors`、`licenses`、`url`、`source`、`documentation`、`created`、`revision`。对用户体验的影响是"从 add-on 页跳不到 GitHub"+"对 HACS / 镜像扫描工具不友好"。

---

### 2. Expected Behavior (Correct)

以下是每条 bug 被修复后的预期系统行为，**只描述行为契约**，不规定实现手段（实现在 design.md）。

#### P0

2.1 WHEN 用户刚完成 REBUILD → START 且未绑定 `sleep_stage_source` THEN 系统 SHALL 在 60 秒内通过 Supervisor 代理 POST 至少 5 个占位 `sensor.sleep_classifier_*` 实体（`state="configuring"`，`attributes.reason="awaiting_stage_binding"`），让 Lovelace 立刻可见一个"add-on 活着、在等你绑定"的信号。

2.2 WHEN `web_ui.py` 的前端 JavaScript 发起 AJAX 请求时 THEN 前端 SHALL 统一使用**相对路径且不以 `/` 开头**（例如 `api/entities`、`api/options`）；aiohttp 路由 SHALL 同时挂载 `/api/entities` 与 `/ingress_entry/api/entities` 以兼容 Supervisor 在不同 session 里可能添加的前缀。审计中发现 `index.html` 里 `fetch('api/options', ...)` 与 aiohttp 路由 `app.router.add_post("/api/options", api_save)` 已经一致（相对路径正确）；需要补的是添加一条"路径契约测试"防回归。

2.3 WHEN add-on 容器收到 SIGTERM THEN 系统 SHALL 把信号同时转发给 Web UI 进程和 smart service 进程，两者 SHALL 在 8 秒内完成 preferences flush 并正常退出；实现手段可选：（a）把 Web UI 与 smart service 都做成 tini 的并行子进程并通过 `process_group` 统一转发；（b）让 run.sh 不用 exec 而是把 web_ui.py 作为后台进程、主循环用 `wait -n` 阻塞。

#### P1

2.4 WHEN Supervisor 版本 ≥ 2026.04.0 THEN 系统 SHALL 不依赖 `build.yaml`，Dockerfile 自身已经用 `FROM python:3.11-alpine`（与 v2.0.1 的国内网络决策一致），`build.yaml` 应当被删除或改为仅保留 `labels:`（未来新 builder 支持 labels 块）。

2.5 WHEN 镜像被构建 THEN 镜像 SHALL 携带 `io.hass.version`、`io.hass.type="addon"`、`io.hass.arch="aarch64|amd64"`、`org.opencontainers.image.version` 四个 LABEL（现在一个都没有），`BUILD_VERSION` / `BUILD_ARCH` 这两个 builder 注入的 ARG 也要在 Dockerfile 声明 `ARG`。

2.6 WHEN `config.yaml` 里配置 `startup` 字段 THEN 其值 SHALL 为 `application`（而非现在的 `services`），因为本 add-on 使用 `homeassistant_api: true`，必须等 HA Core 起来后才能工作；改成 `application` 不会影响 HA OS 的 boot-order，但会让 Supervisor 在 Core 就绪前先不启动 add-on，消除头 30 秒的 ping 失败噪声。

2.10 WHEN Supervisor 启动 add-on 容器时 THEN `sleep_classifier/apparmor.txt` SHALL 存在且语法合法（`profile sleep_classifier flags=(attach_disconnected,mediate_deleted) { ... }`），allowlist 至少覆盖：`/sbin/tini`、`/bin/bash`、`/usr/local/bin/python3*`、`/usr/bin/jq`（`rmix` 执行）；`/app/** r`；`/data/** rwk`；`/share/** rwk`；`/dev/tty rw`；`signal (receive) set=(term, int, kill) peer=unconfined`。`config.yaml` SHALL 声明 `apparmor: true`，Supervisor SHALL 把 profile 加载到容器（`docker inspect --format '{{.AppArmorProfile}}'` 返回 `"sleep_classifier"`）；add-on 的 HA 安全分 SHALL 从 5 分提升到 6 分（满分）。

2.11 WHEN Supervisor 构建镜像时 THEN `Dockerfile` SHALL 使用 `ARG BUILD_FROM=python:3.11-alpine` + `FROM ${BUILD_FROM}` pattern；`build.yaml` 的 `build_from` SHALL 保留并写 `python:3.11-alpine`（不含 `ghcr.io`）；新老 builder 都能消费此配置。

2.12 WHEN 用户在 Supervisor UI 查看 add-on 详情页 THEN `config.yaml` SHALL 含 `url: "https://github.com/LiangyuLu-lly/HA-sleep"` 字段，UI 显示可点击的"项目主页"链接；Dockerfile SHALL 声明 15 条 `LABEL`（5 条 `io.hass.*` + 10 条 `org.opencontainers.image.*`），`docker inspect <image>` 全部可见，对齐 `hassio-addons/app-example` 的工业级元数据基线。

#### P2

2.7 WHEN `render_effective_config.py` 写入 `/data/effective_config.json` THEN 系统 SHALL 先写入临时文件 `effective_config.json.tmp`，`fsync()` 后用 `os.replace()` 原子重命名到目标路径。任何写入过程中的 SIGKILL / 掉电都只会让磁盘上残留 `.tmp`（下次启动前可清理或忽略），主文件要么是旧版完整 JSON 要么是新版完整 JSON。

2.8 WHEN aiohttp Web UI 接受连接时 THEN 系统 SHALL 通过 middleware 检查 `request.remote`（或 `X-Forwarded-For` 首跳），只接受 `172.30.32.2`；来自其他来源的请求 SHALL 返回 403；dev 环境用 env 变量 `WEB_UI_DISABLE_INGRESS_GUARD=1` 可豁免。

2.9 WHEN HA Core 触发 WebSocket 401 / 503 THEN 系统 SHALL 将其识别为"可恢复"错误（而不是 "auth 错误"），进入 WebSocket 重连循环，**不 `stop_event.set()`**；仅当连续 N 次（例如 10 次）auth 失败才判定 token 真的失效并 stop。

---

### 3. Unchanged Behavior (Regression Prevention)

修复上述 12 条 bug 时，以下已工作行为必须保持不变（回归防护）：

3.1 WHEN add-on 镜像被构建且宿主网络能通 Docker Hub THEN 构建流程 SHALL CONTINUE TO 不依赖 `ghcr.io` 的可达性（v2.0.1 的国内网络修复），`docker-compose.yaml` / `build.yaml` 的修改不能让 `FROM python:3.11-alpine` 这行回归。

3.2 WHEN 用户未绑定 `sleep_stage_source` 时 THEN 容器 SHALL CONTINUE TO 不进入重启循环（v2.0.2 supervise 循环保持），Web UI 保持在线并能显示候选实体列表。

3.3 WHEN 用户在 options 里填了含中文 / 引号 / 反斜杠的字符串值 THEN 配置渲染 SHALL CONTINUE TO 通过 `SC_*` 环境变量传给 `render_effective_config.py`（v2.0.2 的 heredoc 移除保持），不再有 shell 级字符串插值。

3.4 WHEN `SUPERVISOR_TOKEN` 环境变量存在 THEN `web_ui.py._fetch_states()` SHALL CONTINUE TO 用它作 Bearer token 请求 `http://supervisor/core/api/states`；不引入额外的授权流程。

3.5 WHEN smart service 正常运行且 `sleep_stage_source` 合法 THEN `_route_state_change` SHALL CONTINUE TO 把 state_changed 事件分发到 `ExternalStageSubscriber`、`SubjectiveFeedbackListener`、`ApneaWiring`、`LiveStateCache`（v1.7.1 行为保持）。

3.6 WHEN `/data/user_preferences.json.bak` 存在且主文件损坏 THEN 系统 SHALL CONTINUE TO 从 `.bak` 恢复（v1.8.0 行为保持）。

3.7 WHEN add-on 在 dry-run 模式运行 THEN 系统 SHALL CONTINUE TO 不向 HA 发任何 `call_service` 请求（默认 `dry_run: true` 保持）。

3.8 WHEN Python 3.11-alpine 镜像被构建且 PyPI 可达 THEN `pip install aiohttp>=3.9.0` SHALL CONTINUE TO 直接取 musllinux_1_2 aarch64 预编译 wheel（aiohttp 3.9+ 已有此 wheel 覆盖 Python 3.9–3.13 × linux/darwin/windows），构建过程中的 `.build-deps`（build-base / linux-headers）保留作为 fallback 不变。

---

## 从需求推导 Bug Condition 与 Property

把 12 条 bug 合并到统一的 bug condition 与修复检查 / 回归保持检查：

### Bug Condition Function

```pascal
FUNCTION isBugCondition(X: SystemState): bool
  INPUT: X ∈ SystemState
  OUTPUT: bool

  // 12 条可独立触发失败的子条件，任何一条成立都让 C(X) 成立。
  RETURN
    (X.options.sleep_stage_source = ""           // 1.1 首次安装占位实体
       AND NOT has_placeholder_sensors_after(X, 60_seconds))
    OR (X.web_ui_fetches_use_absolute_paths)    // 1.2 Ingress 前端路径
    OR (X.received_sigterm                        // 1.3 SIGTERM 丢数据
       AND NOT smart_service_flushed_within(X, 8_seconds))
    OR (build_yaml.build_from contains "ghcr.io"  // 1.4 build.yaml 配置漂移
       AND dockerfile_from = "python:3.11-alpine")
    OR (X.image_labels.count < 15                  // 1.5 + 1.12 labels 缺失
       OR NOT io_hass_labels_present)
    OR (X.config_yaml.startup = "services"        // 1.6 startup 语义
       AND uses_homeassistant_api)
    OR (X.partially_written_effective_config)     // 1.7 原子写
    OR (X.request_source_ip ≠ "172.30.32.2"       // 1.8 Ingress IP 过滤
       AND web_ui_accepts_request)
    OR (X.ha_core_transient_error                  // 1.9 Auth 误杀
       AND smart_service_permanent_stopped)
    OR (NOT file_exists("sleep_classifier/apparmor.txt")  // 1.10 AppArmor profile
       OR X.config_yaml.apparmor != true
       OR X.container.apparmor_profile = "unconfined")
    OR (NOT dockerfile_uses_arg_build_from        // 1.11 build_from pattern
       OR build_yaml.build_from contains "ghcr.io")
    OR (X.config_yaml.url is absent               // 1.12 url + OCI labels 不完整
       OR X.image_labels.count < 15)
END FUNCTION
```

### Property Specifications

**Fix Checking — 九条 bug 修复后的正确行为（FOR ALL X WHERE C(X)）：**

```pascal
// P1.1 — 首次安装时占位实体就绪
FOR ALL X WHERE X.options.sleep_stage_source = "" DO
  WAIT UNTIL t = 60s AFTER container_start
  states ← GET /api/states
  ASSERT exists(s ∈ states : s.entity_id startswith "sensor.sleep_classifier_"
                         AND s.state ∈ {"configuring", "awaiting_stage_binding", …})
END FOR

// P1.3 — SIGTERM 后偏好必落盘
FOR ALL X WHERE X.received_sigterm DO
  ASSERT preferences_file_mtime_after_sigterm > preferences_file_mtime_before_sigterm
     OR preferences_unchanged_since_last_session
  ASSERT t(exit) - t(sigterm) ≤ 8s
END FOR

// P1.7 — 原子写
FOR ALL X, FOR ALL t ∈ [0, write_duration] DO
  kill_at(t)
  effective ← read("/data/effective_config.json") ON startup
  ASSERT is_valid_json(effective)
  ASSERT effective = previous_effective OR effective = new_effective
END FOR

// P1.8 — Ingress 源 IP 过滤
FOR ALL X WHERE X.request_source_ip ≠ "172.30.32.2" DO
  resp ← GET http://addon:8099/
  ASSERT resp.status = 403
END FOR

// P1.9 — 可恢复 WS 错误不终止服务
FOR ALL X WHERE X.ha_core_restart_transient AND transient_duration < 60s DO
  ASSERT smart_service_resumed_within(X, 90s)
  ASSERT stop_event_not_set_during_transient
END FOR

// P1.10 — 自定义 AppArmor profile 已加载
FOR ALL X DO
  ASSERT file_exists("sleep_classifier/apparmor.txt")
  content ← read("sleep_classifier/apparmor.txt")
  ASSERT "profile sleep_classifier" IN content
  ASSERT "/app/** r," IN content
  ASSERT "/data/** rwk," IN content
  ASSERT "/share/** rwk," IN content
  ASSERT "/dev/tty" IN content
  cfg ← yaml.parse("sleep_classifier/config.yaml")
  ASSERT cfg.apparmor = true
  // 运行时（Pi 4B E2E）
  ASSERT docker_inspect(container).AppArmorProfile = "sleep_classifier"
  // 且 HA 日志中无 apparmor="DENIED" 命中
  ASSERT grep("apparmor=\"DENIED\"", ha_logs) = []
END FOR

// P1.11 — build_from 用 ARG pattern 指向 Docker Hub
FOR ALL X DO
  dockerfile ← read("sleep_classifier/Dockerfile")
  ASSERT "ARG BUILD_FROM" IN dockerfile
  ASSERT "FROM ${BUILD_FROM}" IN dockerfile OR "FROM $BUILD_FROM" IN dockerfile
  build_yaml ← yaml.parse("sleep_classifier/build.yaml")
  ASSERT build_yaml.build_from.aarch64 = "python:3.11-alpine"
  ASSERT build_yaml.build_from.amd64 = "python:3.11-alpine"
  ASSERT "ghcr.io" NOT IN str(build_yaml.build_from)
END FOR

// P1.12 — url + 15 条 OCI labels
FOR ALL X DO
  cfg ← yaml.parse("sleep_classifier/config.yaml")
  ASSERT cfg.url starts_with "https://"
  ASSERT cfg.url = "https://github.com/LiangyuLu-lly/HA-sleep"
  labels ← docker_inspect(image).Config.Labels
  ASSERT |keys(labels)| >= 15
  ASSERT labels["io.hass.name"] = "Sleep Classifier"
  ASSERT labels["io.hass.description"] != ""
  ASSERT labels["io.hass.type"] = "addon"
  ASSERT labels["io.hass.version"] != "dev"
  ASSERT labels["io.hass.arch"] IN {"aarch64", "amd64"}
  ASSERT labels["org.opencontainers.image.title"] != ""
  ASSERT labels["org.opencontainers.image.licenses"] = "MIT"
  ASSERT labels["org.opencontainers.image.source"] starts_with "https://github.com/"
  ASSERT labels["org.opencontainers.image.documentation"] endsWith "DOCS.md"
END FOR
```

**Preservation Checking — 原有行为不被破坏（FOR ALL X WHERE NOT C(X)）：**

```pascal
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
  // 即：当系统处于已知无 bug 的 12 个子条件反面都成立的状态时，
  // 修复后的行为与 v2.0.2 完全一致。具体分量：
  //   * 用户未绑定 stage 时容器保持在线（3.2）
  //   * 用户填中文 options 时渲染正确（3.3）
  //   * dry_run 默认保持（3.7）
  //   * SUPERVISOR_TOKEN 授权路径保持（3.4）
  //   * 国内网络镜像构建保持（3.1）—— Bug 1.11 的 ARG BUILD_FROM
  //     pattern 默认值仍是 python:3.11-alpine，不走 ghcr.io
  //   * .bak 恢复路径保持（3.6）
  //   * aiohttp wheel 走 musllinux 不走源码编译（3.8）
  //   * v1.7.1 的 state_changed 分发保持（3.5）
  //   * Bug 1.10 的 AppArmor profile allowlist 覆盖 tini → bash →
  //     python3 → jq 执行链 + /data/ /share/ rwk，所有 v2.0.2
  //     既有进程的正常路径不被 DENIED
END FOR
```

---

## 验证方案

### 探索性 Property Test（"bug exists" 证据）

写成 pseudocode，修复前失败、修复后通过：

```pascal
// Property: first-install Web UI reachability
//
// 对任意有效 options（含 sleep_stage_source 可空）+ 启动后任意时刻 t ∈ [5, 60]s，
// Ingress 访问 Web UI 应返回 200。
// v2.0.2 之前在 t ∈ [3, 8] 的几个点上会失败（supervise restart 窗口），
// v2.0.2 之后单独 Web UI 不再被 kill——但 1.3（SIGTERM）与 1.2（Ingress 路径）
// 叠加会让此 property 在 hypothesis 下仍被 shrink 出反例。

property test_first_install_web_ui_reachability:
  @given(
    options ← strategies.fixed_dictionaries({
      "sleep_stage_source": strategies.one_of(just(""), entity_id_strategy()),
      "temperature_source": strategies.one_of(just(""), entity_id_strategy()),
      ...全部可为空的 slot...
    }),
    t ← strategies.floats(min_value=5, max_value=60),
  )
  def test(options, t):
    container = start_addon_container(options=options)
    time.sleep(t)
    response = requests.get(
      f"http://{container.ip}:8099/",
      headers={"X-Ingress-Path": "/api/hassio_ingress/testsession"},
      source_ip="172.30.32.2",
    )
    assert response.status_code == 200
    container.kill()
```

### 确定性 Example Tests

按 bug 编号列出，用于 CI：

| Bug | 测试类型 | 测试手段 |
|---|---|---|
| 1.1 占位实体 | example | 起容器 → 不绑定 stage → 60s 后查 /api/states → 断言出现 `sensor.sleep_classifier_stage` 之类占位 |
| 1.2 Ingress 路径 | example | grep `web_ui.py` HTML，断言无 `fetch('/api/` 绝对路径 |
| 1.3 SIGTERM flush | example | 启容器 → docker stop → 日志包含 "Signal 15" 且 `/data/user_preferences.json` mtime 被更新 |
| 1.4 build.yaml | example | 断言 `sleep_classifier/build.yaml` 存在 `build_from` 且不含 `ghcr.io` |
| 1.5 io.hass + OCI label | example | 构建后 `docker inspect` 结果 labels 总数 ≥ 15，含 `io.hass.type=addon` / `io.hass.name` / `io.hass.description` / `org.opencontainers.image.*` |
| 1.6 startup | example | YAML 解析后断言 `startup == "application"` |
| 1.7 原子写 | property | hypothesis 生成中断偏移，断言 json 完整 |
| 1.8 Ingress IP | example | aiohttp test server 发两个请求（本地 vs 172.30.32.2），断言前者 403 |
| 1.9 Auth 误杀 | example | mock 注入 1 次 HAAPIError → 断言 stop_event 未被 set |
| 1.10 AppArmor profile | example | 断言 `sleep_classifier/apparmor.txt` 文件存在且含 `profile sleep_classifier` / `/app/** r,` / `/data/** rwk,` / `/share/** rwk,` / `/dev/tty` / `#include <tunables/global>`；`config.yaml` 含 `apparmor: true`；E2E：`docker inspect --format '{{.AppArmorProfile}}'` 返回 `"sleep_classifier"` |
| 1.11 build_from pattern | example | grep Dockerfile 断言 `ARG BUILD_FROM` + `FROM ${BUILD_FROM}`；parse build.yaml 断言 `build_from.{aarch64,amd64}` 都是 `python:3.11-alpine` |
| 1.12 url + OCI labels | example | parse config.yaml 断言 `url` 以 `https://` 开头；docker inspect 断言 label 总数 ≥ 15 |

### 环境验证

- `pytest tests/` 本地跑通（不含容器集成）
- Pi 4B HA OS 16.3.1 + HA Core 2026.4.2 手动走一次 `sync-to-ha.ps1 → REBUILD → START`，肉眼确认：
  - Web UI 能打开
  - 30 秒内 Lovelace 有 5 个以上 `sensor.sleep_classifier_*`
  - 绑定 stage + RESTART 后 smart service 正常运行
  - 再次 RESTART 时日志出现 "Signal 15 — shutting down" 且在 8 秒内退出
  - （Bug 1.10）`docker inspect <container> --format '{{.AppArmorProfile}}'` 返回 `"sleep_classifier"`；HA 日志 grep `apparmor="DENIED"` 无命中
  - （Bug 1.11）`docker build` 日志中 `FROM` 行展开为 `python:3.11-alpine`，不是 `ghcr.io/...`
  - （Bug 1.12）Supervisor UI 的 add-on 详情页显示可点击的"Project homepage"链接；`docker inspect <image> | jq '.[0].Config.Labels | length'` 返回 ≥ 15

完成后请确认是否进入下一阶段（design.md 设计具体修复手段）。

