# Post-v2.0.2 Full-Pipeline Audit Bugfix Design

## Overview

v2.0.2 修掉了"Web UI 启动期 502"和"stage 未配置时容器重启循环"两条明显断点。本次设计以 bugfix.md 的 12 条真实 bug 为输入（9 条 v2.0.2 首轮审计 + 3 条 v2.0.3 对标 `hassio-addons/app-example` 后补的商业级元数据 / 安全分），设计一个**最小入侵、零新 Python 依赖、零新 apk 依赖**的修复方案。核心决策是：

1. 信号转发走 tini `-g` + bash `wait -n` 的**原生 Linux 方案**，不引入 supervisord / s6-overlay（镜像零增量）。
2. 占位 sensor 由 run.sh 启动时用 `curl` 对 `http://supervisor/core/api/states/<id>` 直接 POST 发布，独立于 smart service（独立于 stage 绑定路径）。
3. 原子写抽成 `src/_io_utils.py::atomic_write_text()` 供 `render_effective_config.py`、`web_ui.py`、`preference_learner.py`、`apnea_wiring.py`、`user_profile.py` 复用。
4. Ingress IP 白名单做成可复用 aiohttp middleware，放在 `web_ui.py` 里（单文件已足够，不单独建 package）。
5. WebSocket 错误分类细化：HTTP 401/503、`ClientConnectorError`、握手失败归为"可恢复"，仅"auth_failed on WS handshake 连续 ≥N 次"才判定 token 失效。
6. **（v2.0.3）** `build_from` 保留并指向 Docker Hub `python:3.11-alpine`（Bug 1.11），Dockerfile 改用 `ARG BUILD_FROM` 消费；Dockerfile 扩 15 条工业级 `LABEL`（Bug 1.5 扩 + Bug 1.12）；新建 `apparmor.txt`（Bug 1.10），让商业级安全分从 5 提到 6。

上述决策产出 12 条 bug 的独立设计（§3），共享的跨 bug 决策在 §4 展开，§5 列出本轮不动的事项避免蔓延，§6 给出测试策略。

---

## Glossary

| 名词 | 定义 |
|---|---|
| **Bug_Condition (C)** | bugfix.md §2 中的 12 条独立子条件之一成立的系统状态 |
| **Property (P)** | 每条 bug 修复后容器在给定输入上应满足的可观测行为 |
| **Preservation** | v2.0.2 已稳定的行为（bugfix.md §3），不得因修复而倒退 |
| **run.sh** | Add-on 容器的 entrypoint bash 脚本，位于 `sleep_classifier/run.sh` |
| **web_ui.py** | 内嵌 aiohttp 实体选择器，位于 `sleep_classifier/web_ui.py` |
| **smart service** | `scripts/run_ha_smart_service.py` 里的 `SmartSleepService` |
| **Supervisor** | HA OS 里负责构建 + 启动 + 监管 add-on 容器的组件，占 IP 172.30.32.2 |
| **Ingress** | HA Supervisor 把 add-on 的 HTTP 服务反代到 `/api/hassio_ingress/<token>/` 的机制 |
| **tini** | `/sbin/tini`，PID1 的 init，负责收割僵尸 + 转发信号；镜像里通过 `apk add tini` 获得，大小约 200 KB |
| **atomic_write** | "写 tmp → fsync → rename"三步序列，保证中途 SIGKILL 不留半截文件 |
| **C(X)** | `isBugCondition(X)` 的简写 |
| **F / F'** | F 是修复前的函数（如 v2.0.2），F' 是修复后 |

---

## Architecture Overview

### 修复后的启动拓扑（ASCII）

```
                    HA Supervisor (IP 172.30.32.2)
                            │
                            │ docker run sleep_classifier
                            ▼
┌─────────────────────── container ───────────────────────────┐
│  /sbin/tini -g -- /run.sh        ← PID 1, -g 转发到进程组     │
│                │                                              │
│                │ bash set -m (job control) + setpgid         │
│                │                                              │
│                ├── 1. bootstrap_placeholder_sensors.sh       │
│                │      curl POST /api/states/sensor.*         │
│                │      (同步，5~15 次 REST，≤2 秒完成)          │
│                │                                              │
│                ├── 2. render_effective_config.py             │
│                │      atomic_write /data/effective_config   │
│                │                                              │
│                ├── 3. web_ui.py                              │
│                │      aiohttp :8099                          │
│                │      IP-whitelist middleware (172.30.32.2)  │
│                │      PID = PID_WEB                           │
│                │                                              │
│                └── 4. supervise_smart_service (bash loop)    │
│                      └─ python run_ha_smart_service.py       │
│                         PID = PID_SMART                       │
│                         WS reconnect + 指数退避                 │
│                                                              │
│  主进程:  wait -n PID_WEB PID_SMART_SUPERVISOR               │
│          ↑ 某一个退出就唤醒，trap INT TERM 正常触发             │
└──────────────────────────────────────────────────────────────┘
                            │
                            ▼ (state POST + WS subscribe)
                    HA Core (via supervisor/core proxy)
```

关键变化：
- **tini 加 `-g` 参数**：SIGTERM 转发到整个进程组，所有后台 Python 进程都能收到。
- **run.sh 不再 `exec`**：改用 `wait -n`，保留 trap + 进程监管。
- **占位 sensor 先于任何 Python 进程**：由 run.sh 顶部 curl 直接发，与 smart service 解耦。
- **原子写下移到 Python 辅助**：消灭"读到半截 JSON → 误判为未绑定 stage"路径。

### 进程树与信号流向

```
docker stop
    │ SIGTERM
    ▼
tini (PID 1, -g)              # tini -g 把信号 kill(-pgid, SIGTERM)
    │ SIGTERM to PGID
    ├─► bash run.sh (leader)     # trap 'cleanup' TERM
    │      │
    │      ├─► web_ui.py         # aiohttp signal handler → graceful shutdown
    │      │
    │      └─► supervise_smart_service (bash bg)
    │             │
    │             └─► python run_ha_smart_service.py
    │                    │
    │                    └── SIGTERM → asyncio signal handler
    │                           → stop_event.set()
    │                           → finally: _persist_session(partial=False)
    │
    ▼ cleanup:  kill 0 子进程剩余者, wait
    exit 0
```

### 9 条 bug 的优先级 + 依赖关系

> v2.0.3 更新：对标 `hassio-addons/app-example` 后新增 3 条 bug（1.10 / 1.11 / 1.12），集中在商业级元数据 + 安全加分。总计 12 条。

```
P0（必须先上，首次安装才跑得通）
┌──────────────────────────────────────────────────┐
│  1.1  占位 sensor                 (独立)          │
│  1.2  Ingress 相对路径            (独立, 契约测试) │
│  1.3  SIGTERM 转发                (依赖 1.1 的架构) │
└──────────────────────────────────────────────────┘
P1（版本演进 / 商业化安全分 / 对齐官方 pattern）
┌──────────────────────────────────────────────────┐
│  1.4  build.yaml 配置漂移         (独立)          │
│  1.5  io.hass.* labels             (独立)          │
│  1.6  startup=application          (独立)          │
│  1.10 自定义 AppArmor profile      (独立, 安全分+1) │
│  1.11 build_from 声明修正         (独立, 对齐 app-example) │
└──────────────────────────────────────────────────┘
P2（极端条件 / 商业化元数据补完）
┌──────────────────────────────────────────────────┐
│  1.7  atomic write                 (独立, 可复用)  │
│  1.8  Ingress IP 白名单            (独立, 中间件)  │
│  1.9  WS 错误分类                  (独立)          │
│  1.12 config.yaml 缺 url + OCI labels 不完整 (独立) │
└──────────────────────────────────────────────────┘

依赖矩阵（横读：A 必须先做）：
          1.1  1.2  1.3  1.4  1.5  1.6  1.7  1.8  1.9  1.10 1.11 1.12
    1.1    -    .    .    .    .    .    .    .    .    .    .    .
    1.2    .    -    .    .    .    .    .    .    .    .    .    .
    1.3   建议  .    -    .    .    .    .    .    .    .    .    .
    1.4    .    .    .    -    .    .    .    .    .    .   先做  .
    1.5    .    .    .    .    -    .    .    .    .    .    .    .
    1.6    .    .    .    .    .    -    .    .    .    .    .    .
    1.7    .    .    .    .    .    .    -    .    .    .    .    .
    1.8    .    .    .    .    .    .    .    -    .    .    .    .
    1.9    .    .    .    .    .    .    .    .    -    .    .    .
    1.10   .    .    .    .    .    .    .    .    .    -    .    .
    1.11   .    .    .    建议 .    .    .    .    .    .    -    .
    1.12   .    .    .    .   合并  .    .    .    .    .    .    -
```

**新增依赖说明**：
- **1.11 先于 1.4**：Bug 1.11 把"删 build_from"翻成"保留并指向 Docker Hub"；1.4 的清理要在 1.11 重新定性后才动 build.yaml。
- **1.12 与 1.5 合并**：两者都是 Dockerfile `LABEL` 的事，Bug 1.5 原本只加 4 条 HA 标签，扩展到 15 条工业级标签后吸收 1.12 里"缺完整 OCI labels"的部分；1.12 独立保留的只剩 `config.yaml` 的 `url:` 字段。

1.3 标 "建议" 依赖 1.1，是因为 1.1 引入的 bootstrap 脚本会占 0.5~2 秒；若在这期间 Supervisor 就 SIGTERM，1.3 的 wait -n 架构能更干净地清理掉 bootstrap（1.1 单独修复也能工作，但 combined 更稳）。


### v2.0.2 → v2.0.3 的业界对标（hassio-addons / HA 官方文档）

在完成 9 条 bug 的设计后（§3.1–§3.9，v2.0.2 首轮审计产出），我们对标了两份行业级参考，把本轮 audit 的覆盖面再抬一档：

**参考来源**：
- **`hassio-addons/app-example`**（Franck Nijhof 维护，HA 官方认证的 add-on 模板库）的 Dockerfile、`build.yaml`、`config.yaml` 结构。
- **HA 开发者文档 Add-on Presentation（2025）** 里关于 Ingress 源 IP 强制、AppArmor profile 安全加分、OCI label 集的工业级口径。

**对齐的项（本轮会做）**：

| 维度 | app-example 的做法 | 我们的做法（v2.0.3） | 决策依据 |
|---|---|---|---|
| Dockerfile `ARG BUILD_FROM` + `FROM ${BUILD_FROM}` | ✓（把 base image 声明留在 `build.yaml`） | ✓（保留 build_from，但镜像指向 Docker Hub `python:3.11-alpine`，绕开 ghcr.io 国内不可达） | Bug 1.11，修正 §3.4 的"删 build_from"过激决策 |
| 完整 OCI + `io.hass.*` labels（13+ 条） | ✓（`io.hass.name / description / arch / type / version` + 9 条 `org.opencontainers.image.*`） | ✓（扩到 15 条 label，通过 `ARG BUILD_NAME / BUILD_DESCRIPTION / BUILD_REF / BUILD_DATE / BUILD_REPOSITORY` 由 Supervisor 注入） | Bug 1.12 + 扩 §3.5，从商业级元数据口径看齐 |
| 自定义 AppArmor profile（`apparmor.txt`） | ✓（商业 add-on 标配，能让安全分从基础 5 分提到 6 分满分） | ✓（新建 `sleep_classifier/apparmor.txt`，允许 tini/bash/python3/jq 执行 + `/data/`、`/share/`、`/dev/tty` 读写） | Bug 1.10，新增项 |
| Ingress 源 IP 白名单 | ✓（nginx 层 `allow 172.30.32.2; deny all;`） | ✓（aiohttp middleware 等价实现） | 已在 Bug 1.8 覆盖 |
| PID 1 init + 信号转发 | s6-overlay v3（官方 base 自带） | tini `-g` + bash `wait -n`（国内网络硬约束下不能切 base） | 已在 Bug 1.3 覆盖 |

**明确不对齐的项（本轮不做，放进 §5 "本轮不做事项"）**：

| 维度 | app-example 的做法 | 我们的选择 | 原因 |
|---|---|---|---|
| Base image 切回 `ghcr.io/home-assistant/base:latest` | ✓（bashio / s6-overlay / AppArmor profile 全套开箱即用） | ✗ 保持 `python:3.11-alpine`（Docker Hub） | v2.0.1 的国内网络硬约束仍成立；**我们手动模拟官方 base 的关键能力**（init = tini，安全 = 自写 apparmor.txt，配置读取 = jq），不模拟 bashio（依赖官方 base 的 `/command/with-contenv bashio` shebang） |
| `run.sh` 的 shebang 用 `#!/command/with-contenv bashio` | ✓ | ✗ 保持 `#!/usr/bin/env bash` | bashio 与 `python:3.11-alpine` 不兼容；短期不迁移 |
| s6-overlay v3 作为 init system | ✓ | ✗ 保持 tini `-g` + `wait -n` | s6-overlay 引入 ~3 MB + learning curve；本项目只有 2 个 supervised 进程，tini + bash 已足 |
| add-on icon.png（128×128 PNG） | ✓（配 `icon: icon.png` 字段，UI 显示有品牌感） | ✗ 本轮不做 | 不是 bug；需找合适的床 / 月亮图，未来有需求再补 |

这张对标表是本次 audit "从跑通 → 往商业级靠" 的分界点，后续 tasks.md 会按此执行。


---

## Bug Details

### Bug Condition

整体 bug condition 是 12 条独立子条件的析取（见 bugfix.md §1 / §"Bug Condition Function"）。每条 bug 的具体 C(X) 在下面 §3 里单独重述，此处只重申形式化总签名：

**Formal Specification:**
```
FUNCTION isBugCondition(X: SystemState): bool
  INPUT: X 是 add-on 容器某次 REBUILD → START 之后的"行为快照"
  OUTPUT: 12 条独立 bug 条件中任何一条成立 ⇒ true

  RETURN C_1_1(X) OR C_1_2(X) OR C_1_3(X)
      OR C_1_4(X) OR C_1_5(X) OR C_1_6(X)
      OR C_1_7(X) OR C_1_8(X) OR C_1_9(X)
      OR C_1_10(X) OR C_1_11(X) OR C_1_12(X)
END FUNCTION
```

### Examples

每条 bug 的具体反例在 bugfix.md §1 都给过一条；此处给出 2 个交叉型的综合反例，用来说明"为什么要一次性修完"：

- **反例 A（1.1 + 1.3 叠加）**：用户 REBUILD → START，不绑 stage，30 秒后 REBUILD；这期间 Supervisor SIGTERM，web_ui.py 被 tini 杀掉，但 `supervise_smart_service` 的 bash 后台子 shell 没有接到信号，短暂 orphan；下一次启动 `/data/user_preferences.json` 没有更新。
- **反例 B（1.1 + 1.7 叠加）**：首次安装时磁盘空间 < 10 MB，`render_effective_config.py` 写到一半被 ENOSPC 失败，下一轮启动 `jq` parse error 被 `2>/dev/null` 吞掉，supervise 认为 stage 未绑定，同时没有占位 sensor（1.1 未修），Lovelace 完全黑屏。

---

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors（v2.0.2 已有的，必须保持）：**
- 镜像构建不依赖 `ghcr.io` 可达性，`FROM python:3.11-alpine` 不回退到 HA base 镜像（3.1）。
- 用户未绑 stage 时容器不进入重启循环，Web UI 保持在线（3.2）。
- Options 里填含中文/引号/反斜杠的值时，`SC_*` 环境变量链路继续走通，不回 heredoc（3.3）。
- `SUPERVISOR_TOKEN` Bearer 授权对 `http://supervisor/core/api/states` 的访问不变（3.4）。
- v1.7.1 state_changed 分发到 ExternalStageSubscriber / ApneaWiring / LiveStateCache 的路由不变（3.5）。
- `/data/user_preferences.json.bak` 恢复路径不变（3.6）。
- `dry_run: true` 默认保持（3.7）。
- aiohttp musllinux wheel 路径不变，`.build-deps` 安装保留作 fallback（3.8）。

**Scope:**
对任意 `NOT isBugCondition(X)` 的输入，F'(X) = F(X)，即修复后的行为与 v2.0.2 完全一致。具体检验见 §6 的 preservation checking。

---

## Hypothesized Root Cause

按 bug 号整理根因；细节移交到 §3 的"根因 + 实现"里展开。

1. **1.1 占位 sensor**：publish_initial_placeholders 当前藏在 SmartSleepService.run() 里，而 run.sh 的 supervise 循环在 stage 未绑定时根本不起 Python，于是 placeholder 永远不会被发布。**根本缺口：初始化与服务启动被绑在一起。**
2. **1.2 Ingress 相对路径**：当前 web_ui.py 的 HTML 里前端已经用了相对路径（`fetch('api/entities')`），审计 grep 证实；但没有**回归测试**防止未来回退到绝对路径。**根本缺口：缺契约测试。**
3. **1.3 SIGTERM 转发**：`exec python3 web_ui.py` 让 bash 被替换，trap 失效 + 后台 bash 子 shell 成为 orphan。tini 收到 SIGTERM 只转给新的 PID1 (web_ui.py)。**根本缺口：单进程容器 + 后台 job 的组合本就不适合 `exec`。**
4. **1.4 build.yaml 配置漂移**：Dockerfile 不用 `${BUILD_FROM}` 但 build.yaml 里却写了 `build_from:` 指向 `ghcr.io/home-assistant/...`（v2.0.1 已决策不走 ghcr.io）。对标 app-example 后发现正确做法是**保留 build_from 并指向 Docker Hub**，而不是把它删掉。**根本缺口：配置与决策漂移。**（注：本轮 v2.0.3 起，该清理动作改由 Bug 1.11 承担，1.4 降为"消除矛盾"。）
5. **1.5 io.hass 标签 + OCI 元数据**：旧 legacy builder 会自动注入 `io.hass.*` 的 4 条；新 builder 不会，且从商业级 add-on 看齐还需补 9 条 `org.opencontainers.image.*`。**根本缺口：依赖已废除的自动化行为 + 工业级元数据缺失。**
6. **1.6 startup=services**：语义反了；`services` 表示 "在 HA Core 之前启动"，而我们的 add-on 必须在 Core 之后启动（因为要 REST Core）。**根本缺口：误读 HA Supervisor 规范。**
7. **1.7 原子写**：`Path.write_text` 是"open-truncate-write-close"，中途 SIGKILL 留半截。**根本缺口：该用 `os.replace()`。**
8. **1.8 Ingress IP 白名单**：aiohttp `web.run_app(host="0.0.0.0")` 不带任何源 IP 过滤。**根本缺口：没有实现 HA Add-on Presentation 规范里"只允许 172.30.32.2"这条。**
9. **1.9 WS 错误分类**：`_task_ws_listener` 把 `HAAuthError` 和 `HAAPIError` 并列 catch 并 `stop_event.set()`。`HAAuthError` 是 `HAAPIError` 的子类（`src/ha_api_client.py:129-131`），只要任何 HTTP 4xx/5xx 都会落到这里。**根本缺口：异常层次设计正确但使用错误。**
10. **1.10 AppArmor profile 缺失**：没有 `sleep_classifier/apparmor.txt`，Supervisor 回退到默认"unrestricted"profile；商业 add-on 安全评分因此停在基础 5 分（ingress=+2 / auth_api=被 ingress override / apparmor=0）。**根本缺口：没跟上 HA 2024 年后 add-on 评分规则的 apparmor 加分项。**
11. **1.11 build_from 决策过激**：原始 §3.4 决策"删 build_from"，但对标 hassio-addons/app-example 发现 HA 官方维护的 add-on 都**保留 build_from + Dockerfile 用 `ARG BUILD_FROM` 引用**，这样新老 builder 都能用。我们应该改用 Docker Hub 的 `python:3.11-alpine` 作为 build_from 值，不删字段。**根本缺口：对齐官方 pattern 而非自创方案。**
12. **1.12 config.yaml 缺 `url:` + 镜像元数据不完整**：`config.yaml` 没写 `url: https://github.com/...`，Supervisor UI 的"项目主页"链接缺失；Dockerfile `LABEL` 只有 4 条（对标 app-example 的 15 条）。**根本缺口：商业级可发现性元数据欠完整。**

---

## Correctness Properties

Property 1: Bug Condition — 12 条 bug 修复后的统一行为

_For any_ input X where `isBugCondition(X)` returns true (即 bugfix.md §2 十二条子条件之一成立), the fixed add-on SHALL 满足 §6 Fix Checking 下的对应断言，即：
- 首次安装 60 秒内 HA 中出现 ≥5 个 `sensor.sleep_classifier_*` 占位实体。
- Web UI 前端 AJAX 请求只使用相对路径（`fetch('api/...')`）。
- SIGTERM 到达后 8 秒内，smart service 完成 `_persist_session(partial=False)` 并正常退出。
- `/data/effective_config.json` 在任意写入时刻被 SIGKILL，重启后仍是**完整合法 JSON**。
- 来自 `172.30.32.2` 以外源 IP 的 HTTP 请求一律返回 403。
- HA Core 瞬时抖动（401/503/ConnectionError，< 60 s）不会让 smart service 永久停机。
- Supervisor 2026.04+ 构建时不再读到矛盾的 `build.yaml`（`build_from` 指向 Docker Hub `python:3.11-alpine`，Dockerfile 用 `ARG BUILD_FROM` 消费，新老 builder 均兼容）。
- Docker image labels 满足 HA 新 builder + 商业级元数据要求（15 条：`io.hass.*` 4 条 + `org.opencontainers.image.*` 11 条）。
- `config.yaml` 的 `startup` 字段为 `application`。
- `sleep_classifier/apparmor.txt` 存在且被 Supervisor 加载，allowlist 覆盖 tini / bash / python3 / jq / `/data/` rw / `/share/` rw / `/dev/tty` rw。
- `config.yaml` 含 `url: https://github.com/LiangyuLu-lly/HA-sleep`，Supervisor UI 显示"项目主页"链接。

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11, 2.12**

Property 2: Preservation — 非 bug 输入下行为不变

_For any_ input X where `isBugCondition(X)` returns false, the fixed add-on SHALL produce exactly the same observable behaviour as the v2.0.2 baseline, preserving:
- 镜像构建路径（Dockerfile `FROM python:3.11-alpine` via `ARG BUILD_FROM`，不走 ghcr.io）。
- 未绑定 stage 时容器保持在线，Web UI 可用。
- 中文/引号/反斜杠 options 值的 `SC_*` env 链路。
- `SUPERVISOR_TOKEN` 授权的 REST 请求。
- state_changed 路由到 4 个订阅者。
- `/data/user_preferences.json.bak` 恢复。
- `dry_run: true` 默认。
- aiohttp musllinux wheel。
- AppArmor profile 不影响正常进程启动（tini → bash → python3 → jq 链路完整允许）。

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8**


---

## 3. Per-Bug Design Specs

每条 bug 按统一模板：当前行为 / 目标行为 / 实现方案（伪码 + 文件定位）/ 对 steering 的影响 / 风险与 fallback / Property check。

---

### 3.1 Bug 1.1 — 首次安装缺占位 sensor

**当前行为**：未绑定 `sleep_stage_source` 时容器不启 smart service，`publish_initial_placeholders` 永不执行，Lovelace 全部 "Entity not available"。

**目标行为**：用户首次 REBUILD → START 后 60 秒内，HA 出现 ≥5 个 `sensor.sleep_classifier_*` 占位实体（`state="configuring"`，`attributes.reason="awaiting_stage_binding"`）。

**实现方案**（选 B 候选 2：独立 bootstrap 脚本）：

文件 `sleep_classifier/bootstrap_placeholders.py`（新增，轻量，仅 aiohttp + REST POST，沿用 runtime 已装的 aiohttp）：

```python
# sleep_classifier/bootstrap_placeholders.py
"""在 smart service 启动前把 sensor.sleep_classifier_* 占位实体发出去。

独立于 SleepStatePublisher —— 不 import src/，不读 effective_config，
仅依赖 SUPERVISOR_TOKEN + aiohttp。即使 stage 未绑定、effective_config
损坏，这一步也能先让 Lovelace "有东西可看"。

与 SleepStatePublisher.publish_initial_placeholders 的区别：
- 后者依赖 HomeAssistantClient 与 SleepStatePublisher 实例，要先做
  ping + discovery。
- 本脚本只做 5+ 次 POST /api/states/<entity_id>，失败降级为
  "best-effort + 日志警告"。
"""
from __future__ import annotations
import asyncio, json, os, sys
import aiohttp

PLACEHOLDERS = [
    ("sensor.sleep_classifier_stage",       "configuring", {"friendly_name": "Sleep stage"}),
    ("sensor.sleep_classifier_confidence",  "0",           {"friendly_name": "Sleep classifier confidence", "unit_of_measurement": "%"}),
    ("sensor.sleep_classifier_health",      "configuring", {"friendly_name": "Sleep classifier health"}),
    ("sensor.sleep_classifier_last_action", "—",           {"friendly_name": "Last sleep automation action"}),
    ("sensor.sleep_classifier_session_duration", "0",      {"friendly_name": "Sleep session duration", "unit_of_measurement": "s"}),
]

# 所有占位实体共享的 attribute（用户/测试可 grep 这一条断言占位模式在生效）
_COMMON_ATTRS = {"reason": "awaiting_stage_binding", "source": "bootstrap"}

async def post_one(session, base, token, eid, state, attrs):
    body = {"state": state, "attributes": {**_COMMON_ATTRS, **attrs}}
    try:
        async with session.post(
            f"{base}/api/states/{eid}",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
            json=body, timeout=aiohttp.ClientTimeout(total=5),
        ) as r:
            if r.status not in (200, 201):
                print(f"[bootstrap] {eid} → HTTP {r.status}", file=sys.stderr)
    except Exception as exc:   # noqa: BLE001  最多 5 秒延迟，失败降级
        print(f"[bootstrap] {eid} → {type(exc).__name__}: {exc}", file=sys.stderr)

async def main():
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    base = os.environ.get("SUPERVISOR_HA_BASE", "http://supervisor/core")
    if not token:
        print("[bootstrap] SUPERVISOR_TOKEN missing — skipping placeholders", file=sys.stderr)
        return 0
    async with aiohttp.ClientSession() as s:
        await asyncio.gather(*[post_one(s, base, token, *p) for p in PLACEHOLDERS])
    return 0

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

`run.sh` 加一行（在 render_effective_config 之前）：
```bash
echo "[run.sh] Publishing placeholder sensors"
python3 /app/bootstrap_placeholders.py || echo "[run.sh] placeholder publish failed — continuing"
```

**对 steering 的影响**：
- `structure.md`：新增 `sleep_classifier/bootstrap_placeholders.py`，属于 add-on 容器专属脚本（不在 `scripts/` 下因为它永远不从项目根执行）。**建议更新 structure.md 的 "sleep_classifier/" 目录清单，加一条 `bootstrap_placeholders.py —— 启动期抢发占位 sensor`**。
- `tech.md`：依赖不变，仍是 aiohttp。

**风险与 fallback**：
- 风险 1：SUPERVISOR_TOKEN 缺失（非 add-on 环境） → 脚本静默跳过，不阻塞 run.sh。
- 风险 2：HA Core 还没启动（`startup: services` 时会出现） → 一并由 1.6 修复（改 application）。
- 风险 3：POST 速率限制 → 并发 5 个请求足以在 1 秒内完成，HA 不会限流。

**Property check**：
```
FOR ALL X WHERE X.options.sleep_stage_source = "" DO
  WAIT 60 SECONDS AFTER container_start
  s := GET http://supervisor/core/api/states
  ASSERT |{e ∈ s : e.entity_id startswith "sensor.sleep_classifier_"
                 AND e.attributes.reason = "awaiting_stage_binding"}| >= 5
END FOR
```

---

### 3.2 Bug 1.2 — Ingress 前端 AJAX 相对路径契约测试

**当前行为**：`web_ui.py` 的内嵌 HTML 已经用 `fetch('api/entities')` / `fetch('api/options', ...)` 相对路径（审计 grep 证实）；`make_app()` 路由是 `/api/entities` / `/api/options`。没有**回归测试**防止未来改回绝对路径。

**目标行为**：保证未来 PR 不引入 `fetch('/api/...')` 绝对路径；同时在 aiohttp 层补一条防御性**双路由挂载**，让 Supervisor 在某些 session 里添加 `/ingress_entry/` 前缀时仍能路由正确。

**实现方案**：

`tests/test_web_ui_ingress_paths.py`（新增）：
```python
"""Ingress 路径契约测试：防止 fetch() 回退到绝对路径。"""
import re
from pathlib import Path

def test_frontend_uses_relative_fetch_paths():
    html = Path("sleep_classifier/web_ui.py").read_text(encoding="utf-8")
    # 禁止 /api 开头的绝对路径，允许 api/ 相对路径。
    forbidden = re.findall(r"fetch\(\s*['\"]/api/", html)
    assert not forbidden, (
        f"fetch() 使用了绝对路径，会被 Ingress 踢出命名空间：{forbidden}"
    )
    # 正向断言：fetch('api/entities') 与 fetch('api/options') 同时存在。
    assert "fetch('api/entities'" in html or 'fetch("api/entities"' in html
    assert "fetch('api/options'" in html or 'fetch("api/options"' in html

def test_aiohttp_routes_cover_api_paths():
    from sleep_classifier.web_ui import make_app
    app = make_app()
    routes = {(r.method, r.resource.canonical) for r in app.router.routes()}
    assert ("GET", "/api/entities") in routes
    assert ("POST", "/api/options") in routes
```

对 `web_ui.py` 不做行为修改，仅补 docstring 一条：
```python
# HTTP handlers
# --------------------------------------------------------------------
# IMPORTANT: 前端 fetch() 必须使用不以 '/' 开头的相对路径。Supervisor
# Ingress 会透明注入 `/api/hassio_ingress/<token>/` 前缀；绝对路径
# 脱离 Ingress 命名空间，会 404 (Supervisor 拦截) 或 500 (打到真 HA
# Core)。有回归测试在 tests/test_web_ui_ingress_paths.py 守护。
```

**对 steering 的影响**：无。`tech.md`/`structure.md` 不变。

**风险与 fallback**：
- 风险：未来 HA 改 Ingress 前缀 → 测试仍然通过，因为我们锁的是"不以 `/` 开头"，不是具体前缀。
- Fallback：若 Supervisor 未来强制要求 `X-Ingress-Path` 头，aiohttp 有原生 `reverse_url()` 可升级。本轮不做。

**Property check**：
```
FOR ALL sleep_classifier/web_ui.py (当前 + 未来 PR) DO
  grep "fetch\\(\\s*['\"]/api/"
  ASSERT count = 0
END FOR
```

---

### 3.3 Bug 1.3 — SIGTERM 转发 / 停机落盘

**当前行为**：`run.sh` 末尾 `exec python3 /app/web_ui.py` 替换了 bash，原先注册的 `trap '...' INT TERM EXIT` 失效；`supervise_smart_service` 后台 bash 子 shell 成为 orphan；tini 把 SIGTERM 送到新 PID 1（web_ui.py），smart service 错过信号。

**目标行为**：SIGTERM 到达后 ≤8 秒内，smart service 完成 `_persist_session(partial=False)` 并退出；web_ui.py 同步 graceful shutdown；容器退出码 0。

**候选方案比较**（回答澄清点 A）：

| 候选 | 描述 | 优 | 劣 | 选择 |
|---|---|---|---|---|
| **候选 1: tini `-g` + 保留 exec** | Dockerfile 改 `ENTRYPOINT ["/sbin/tini", "-g", "--"]`，让 tini 对整个进程组发信号；run.sh 维持 exec | 最小改动 | `exec` 后 bash 被替换，后台 job 被剪除了进程组血缘关系；`wait` 等不到 | ❌ 兼容性仍有坑 |
| **候选 2: tini `-g` + bash `wait -n`（不 exec）** | tini `-g` + run.sh 结尾 `wait -n $PID_WEB $PID_SMART`，trap 完整工作 | 无新依赖，解决信号 + 落盘 + job 控制 | 比 exec 多了一层 bash 监管 | ✅ **选这个** |
| **候选 3: supervisord** | `apk add supervisor` + supervisord.conf | 行业标准 | ~3 MB 额外依赖，违反 "镜像体积不爆增"；Python 3.11-alpine 的 supervisor 包实际会拉 ~6 MB | ❌ |
| **候选 4: 合并入口（Web UI + smart service 单 Python）** | 把 web_ui.py 的 `make_app()` 跟 smart service 放进同一个 asyncio 事件循环 | 一个进程无信号转发问题 | smart service 崩溃会连带 Web UI 挂掉（= 1.3 → P0 退回 v2.0.2 以前的 502） | ❌ 违反 v2.0.2 的 isolation 原则 |

**选候选 2 的理由**：
1. **无新 apk 依赖**：`tini` 本身已经在 `apk add tini` 里，只是 ENTRYPOINT 参数增加 `-g`。
2. **无新 Python 依赖**：纯 bash job-control，配合 tini 进程组转发，标准 UNIX 模式。
3. **保留 v2.0.2 的 isolation**：Web UI 仍是独立进程，smart service 崩溃不会连坐。
4. **基于 unix.stackexchange.com 736879**：一旦 `exec` 一个新二进制，信号处理器会被 `execve()` 全部清掉，bash 的 trap 无法穿过 exec 边界。用 `wait -n` 保留 bash 监管层避免这个语义。
5. **基于 krallin/tini issue #95**：tini 的 `-g` 模式把信号发给整个进程组而不是单个 PID，恰好是本场景需要的。

**实现细节**：

Dockerfile 结尾改：
```dockerfile
# -g: tini 把 SIGTERM / SIGINT 转发到整个进程组而不是仅 PID 1 的子进程。
# 这样 bash 启动的 supervise_smart_service 子 shell 也能收到信号，
# python smart service 的 asyncio signal handler 能跑到 _persist_session。
STOPSIGNAL SIGTERM
ENTRYPOINT ["/sbin/tini", "-g", "--"]
CMD ["/run.sh"]
```

run.sh 结尾改（去掉 exec）：
```bash
#!/usr/bin/env bash
# ... (现有的 options 解析保持不变) ...
set -euo pipefail

# 启动 job control，让后台进程有独立的 pgid 信息可用。
set -m

# --- (1) 占位 sensor（Bug 1.1）---
python3 /app/bootstrap_placeholders.py \
    || echo "[run.sh] placeholder publish failed — continuing"

# --- (2) 渲染 effective config（现状保持，Bug 1.7 内部改原子写）---
python3 /app/render_effective_config.py

# --- (3) Web UI（后台，不再 exec）---
python3 /app/web_ui.py &
PID_WEB=$!
echo "[run.sh] Web UI PID=$PID_WEB"

# --- (4) smart-service supervisor（后台 bash 循环，保持现有行为）---
supervise_smart_service &
PID_SMART_SUP=$!
echo "[run.sh] smart-service supervisor PID=$PID_SMART_SUP"

# --- (5) 信号处理 ---
# trap 在 exec 之前永远不会丢，因为我们根本不 exec。
# 收到 SIGTERM/SIGINT → 给两个后台进程群发 SIGTERM → wait 它们退出
# 超过 GRACE_SECONDS 仍未退 → SIGKILL。
GRACE_SECONDS=8
_shutdown() {
    echo "[run.sh] signal received — forwarding SIGTERM to children"
    # 注意：PID_SMART_SUP 是 bash 子 shell，它的 pgid 与 run.sh 不同
    # （因为 set -m 让 bash 后台 job 进了自己的 process group）。
    # 先给 supervise_smart_service（bash）SIGTERM，让它 break 循环并
    # 给其中的 python 也转发 SIGTERM。
    kill -TERM "$PID_WEB" 2>/dev/null || true
    kill -TERM -"$PID_SMART_SUP" 2>/dev/null || true    # kill pgid
    # 等最多 GRACE_SECONDS
    for i in $(seq 1 "$GRACE_SECONDS"); do
        if ! kill -0 "$PID_WEB" 2>/dev/null \
           && ! kill -0 "$PID_SMART_SUP" 2>/dev/null; then
            break
        fi
        sleep 1
    done
    # 强杀残留
    kill -KILL "$PID_WEB" 2>/dev/null || true
    kill -KILL -"$PID_SMART_SUP" 2>/dev/null || true
    exit 0
}
trap _shutdown INT TERM

# --- (6) 等任一个后台进程退出（或信号到达）---
# wait -n 在 bash 4.3+ 可用（alpine bash 5.2 OK）。它在被信号唤醒时
# 退出码 > 128，trap 会在此之前先跑 _shutdown。
wait -n "$PID_WEB" "$PID_SMART_SUP"
rc=$?
echo "[run.sh] one of the supervised processes exited rc=$rc"
_shutdown
```

`scripts/run_ha_smart_service.py` 里 asyncio signal handler 保持现状（已经有 stop_event），无需改动。

**对 steering 的影响**：
- `tech.md`：**新增一条代码约定**——"Add-on entrypoint 禁止在末尾 `exec` 替换 bash，必须用 `wait -n` 保留 trap + job control"。
- `product.md`：无。
- `structure.md`：无。

**风险与 fallback**：
- 风险 1：`wait -n` 在某些 dash 环境不可用 → 不适用（Dockerfile 确保了 `apk add bash`，Alpine bash 是 5.2，`wait -n` 自 bash 4.3 开始）。
- 风险 2：`kill -TERM -"$PID"` 用 pgid 寻址但 `set -m` 不是每个 shell 都能生成独立 pgid → 本次以 `set -m` 明确启用 job control 消除这个不确定性。
- Fallback：若发现某些 HA OS 版本上 tini `-g` 不工作（例如 tini 版本 < 0.19），降级方案是 Dockerfile 里固定 tini 版本：`apk add --no-cache "tini>=0.19"`。当前 Alpine 3.19 自带 tini 0.19.0，无需固定。

**Property check**：
```
FOR ALL X WHERE X.received_sigterm DO
  t0 := t_signal
  WAIT UNTIL container_exited OR t = t0 + 10s
  ASSERT container_exited WITHIN 8 SECONDS
  ASSERT exit_code = 0
  ASSERT mtime(/data/user_preferences.json) > t0 OR no_session_to_flush
END FOR
```

---

### 3.4 Bug 1.4 — `build.yaml` 配置漂移

**当前行为**：`sleep_classifier/build.yaml` 存在且写着 `build_from: aarch64: ghcr.io/...`；Dockerfile 实际硬编码 `FROM python:3.11-alpine`（v2.0.1 的国内网络决策），`build_from` 的值被 Supervisor 读到但从未被消费。新 BuildKit builder 会继续读 labels 但忽略 build_from；老 legacy builder 看到"build_from 指向 ghcr.io 但 Dockerfile 不引用"这个矛盾可能输出警告。

**目标行为**：消除配置漂移。Dockerfile 改用 `ARG BUILD_FROM=python:3.11-alpine` + `FROM ${BUILD_FROM}`，`build.yaml` 的 `build_from` 同步改为 Docker Hub 的 `python:3.11-alpine`，两头一致。

> **v2.0.3 决策反转（对标 hassio-addons/app-example）**：原 §3.4 的决策是"删 build_from 只留 labels"。对标后发现 HA 官方维护的 `hassio-addons/app-example` 在新 builder 下**依然保留 build_from**，并通过它把 `BUILD_FROM` 注入到 Dockerfile 的 `ARG`。这是新老 builder 都能消费的正解。本轮改为保留 build_from，具体落地见 Bug 1.11。本节 Bug 1.4 降级为"确认 build.yaml 与 Dockerfile 的 build_from 值一致、不再指向 ghcr.io"。

**实现方案**：`sleep_classifier/build.yaml` 改为：
```yaml
# v2.0.3: build_from 指向 Docker Hub 官方 python:3.11-alpine。
# v2.0.1 因 ghcr.io 在中国大陆不可达切到 Docker Hub，build_from 的值
# 同步修正到 docker.io/library/python:3.11-alpine，消除与 Dockerfile
# 的漂移。Dockerfile 通过 ARG BUILD_FROM 消费此值，新老 builder 都 OK。
build_from:
  aarch64: python:3.11-alpine
  amd64: python:3.11-alpine

# Labels（15 条，见 §3.5 扩展）— Supervisor 构建时把 config.yaml 的
# version / name / description 等透传为 BUILD_* ARG。
labels:
  org.opencontainers.image.title: "Sleep Classifier"
  org.opencontainers.image.description: "Closed-loop smart-home sleep automation: learns your ideal bedtime + bedroom environment from HA sleep-stage data"
  org.opencontainers.image.source: "https://github.com/LiangyuLu-lly/HA-sleep"
  org.opencontainers.image.licenses: "MIT"
```

**对 steering 的影响**：无（这是 add-on 容器层的配置，不影响 src/ 结构）。

**风险与 fallback**：
- 风险 1：老版 Supervisor 读 `build_from` 时验证格式（早期版本只接受 `ghcr.io/*` 形式的镜像名）→ 实际测试确认 2024+ Supervisor 已放开，Docker Hub 短名（不带注册表前缀）解析为 `docker.io/library/python:3.11-alpine`。
- 风险 2：未来 Supervisor 强制指定完整镜像 URL → fallback 是显式写 `docker.io/library/python:3.11-alpine`。

**Property check**：
```
bf := yaml.parse("sleep_classifier/build.yaml").build_from
ASSERT bf is present
ASSERT bf.aarch64 matches /^(docker.io\/library\/)?python:3\.11-alpine$/
ASSERT bf.amd64 matches /^(docker.io\/library\/)?python:3\.11-alpine$/
ASSERT NOT (bf.aarch64 contains "ghcr.io")
```

---

### 3.5 Bug 1.5 — `io.hass.*` + 完整 OCI labels 缺失

**当前行为**：镜像没有 `io.hass.version` / `io.hass.type` / `io.hass.arch` / `io.hass.name` / `io.hass.description`，也没有 `org.opencontainers.image.*` 系列的完整 9 条。旧 legacy builder 会自动注入 `io.hass.*` 4 条，新 BuildKit builder 不会；商业级 add-on（hassio-addons/app-example）一贯声明 15+ 条完整元数据。

**目标行为**：Dockerfile 显式写 `LABEL`，共 15 条（5 条 `io.hass.*` + 10 条 `org.opencontainers.image.*`），`docker inspect <image>` 全部可见。所有 `BUILD_*` ARG 由 `build.yaml` + Supervisor 构建矩阵注入；本地开发 `docker build` 时有默认值兜底。

**实现方案**：Dockerfile 顶部加（紧接在 `FROM ${BUILD_FROM}` 之后，`ENV` 之前）：
```dockerfile
# --- Build args injected by HA Supervisor ------------------------------------
# Supervisor 构建 add-on 时会自动注入下列 ARG：
#   BUILD_FROM          - build.yaml 里每个 arch 对应的 base image
#   BUILD_ARCH          - 当前构建 arch (amd64 / aarch64)
#   BUILD_VERSION       - config.yaml 里的 version 字段
#   BUILD_NAME          - config.yaml 里的 name
#   BUILD_DESCRIPTION   - config.yaml 里的 description
#   BUILD_REPOSITORY    - "owner/repo" 形式的 GitHub 仓库名
#   BUILD_REF           - 构建对应的 git SHA
#   BUILD_DATE          - RFC3339 构建时间戳
# 本地 docker build 不经过 Supervisor 时用默认值兜底，不让 build 失败。
ARG BUILD_FROM=python:3.11-alpine
ARG BUILD_ARCH=amd64
ARG BUILD_VERSION=dev
ARG BUILD_NAME="Sleep Classifier"
ARG BUILD_DESCRIPTION="Closed-loop smart-home sleep automation"
ARG BUILD_REPOSITORY=LiangyuLu-lly/HA-sleep
ARG BUILD_REF=unknown
ARG BUILD_DATE=1970-01-01T00:00:00Z

# --- Labels (15 industrial-grade) --------------------------------------------
# 对标 hassio-addons/app-example：5 条 io.hass.* + 10 条 OCI 镜像标准。
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
```

**标签清单（15 条）**：

| # | Label | 值来源 | 用途 |
|---|---|---|---|
| 1 | `io.hass.name` | ARG BUILD_NAME | Supervisor UI 显示名 |
| 2 | `io.hass.description` | ARG BUILD_DESCRIPTION | Supervisor UI 简介 |
| 3 | `io.hass.arch` | ARG BUILD_ARCH | 架构匹配校验 |
| 4 | `io.hass.type` | literal `"addon"` | HA 识别这是 add-on（而非 core） |
| 5 | `io.hass.version` | ARG BUILD_VERSION | 版本比对 / 升级提示 |
| 6 | `maintainer` | literal | Docker 传统标签 |
| 7 | `org.opencontainers.image.title` | ARG BUILD_NAME | OCI 标准"显示名" |
| 8 | `org.opencontainers.image.description` | ARG BUILD_DESCRIPTION | OCI 标准简介 |
| 9 | `org.opencontainers.image.vendor` | literal | 发布方 |
| 10 | `org.opencontainers.image.authors` | literal | 作者邮箱/链接 |
| 11 | `org.opencontainers.image.licenses` | literal `"MIT"` | SPDX license id |
| 12 | `org.opencontainers.image.url` | from BUILD_REPOSITORY | 项目主页 |
| 13 | `org.opencontainers.image.source` | from BUILD_REPOSITORY | 源码仓库 |
| 14 | `org.opencontainers.image.documentation` | from BUILD_REPOSITORY | 文档链接 |
| 15 | `org.opencontainers.image.created` / `revision` / `version` | from BUILD_DATE/REF/VERSION | 三合一表示构建快照 |

`config.yaml` 的 `name / version / description` 会被 Supervisor 透传为 `BUILD_NAME / BUILD_VERSION / BUILD_DESCRIPTION`；`arch:` 列表会在构建矩阵里分别注入 `BUILD_ARCH=aarch64 / amd64`。

**对 steering 的影响**：无（Dockerfile 层的元数据，不改变 src/ 约定）。

**风险与 fallback**：
- 风险 1：旧 legacy builder 也会注入部分同名 ARG → Docker 行为是后 declare 覆盖前值，ARG 默认值被正确 override，无害。
- 风险 2：本地 `docker build` 不经 Supervisor 时 `BUILD_REF` = "unknown" / `BUILD_DATE` = epoch → 影响可接受（只影响本地构建的镜像元数据，不影响运行）。
- Fallback：无必要。

**Property check**：
```
labels := docker inspect sleep_classifier:<version> | jq '.[0].Config.Labels'
ASSERT labels["io.hass.type"] = "addon"
ASSERT labels["io.hass.version"] != "" AND labels["io.hass.version"] != "dev"
ASSERT labels["io.hass.arch"] IN {"aarch64", "amd64"}
ASSERT labels["io.hass.name"] = "Sleep Classifier"
ASSERT labels["io.hass.description"] != ""
ASSERT labels["org.opencontainers.image.licenses"] = "MIT"
ASSERT labels["org.opencontainers.image.source"] startswith "https://github.com/"
ASSERT labels["org.opencontainers.image.documentation"] endswith "DOCS.md"
ASSERT |keys(labels)| >= 15
```

---

### 3.6 Bug 1.6 — `startup: services` 语义错

**当前行为**：`config.yaml` 写 `startup: services`，表示 Supervisor 会在 HA Core **之前**启动本 add-on。本 add-on 使用 `homeassistant_api: true`，开机头 30 秒内 HA Core REST 还未就绪，`SmartSleepService.run()` 里的 `ha.ping()` 会返回 False，supervise 循环触发 2→4→…→60 秒退避；表面现象是"装完要静默等 1 分钟才有东西"。

**目标行为**：`startup: application`，Supervisor 在 HA Core 就绪后再启动本 add-on，首次启动即可立即 ping 成功。

**实现方案**：`sleep_classifier/config.yaml`：
```yaml
# ← OLD: startup: services
# ← NEW: 本 add-on 通过 homeassistant_api: true 调用 HA Core，必须等
# Core 就绪。services = 在 Core 之前启动（适合 DB / MQTT broker），
# application = 在 Core 之后启动（适合 HA API 消费方）。
# 参见 HA Supervisor add-on docs。
startup: application
```

**澄清点 C：`run.sh` 开头 `jq -r ... /data/options.json` 在 startup=application 下还能读到吗？**

能。Supervisor 的启动序列是：
1. Supervisor 根据 add-on 的 options 渲染 `/data/options.json`。
2. 根据 `startup` 字段决定启动顺序，但**与 options.json 的写入无关**——options.json 在容器挂卷时已就绪。
3. `startup: application` 仅推迟容器 `docker run` 的时刻，不影响 `/data/options.json` 可见性。

所以 `jq -r '.sleep_stage_source // ""' /data/options.json` 在 application 下仍能读到，和 services 下完全一样。

**对 steering 的影响**：无。

**风险与 fallback**：
- 风险：HA Core 永远起不来 → add-on 也永远不启动 → 用户诊断难度上升。这属于极端故障，优先级低；可以在 DOCS.md 里加一句"若 add-on 从不启动，请先确认 HA Core 本身健康"。
- Fallback：若未来需要"先占实体再等 Core"，退回 `startup: services` 并把 bootstrap_placeholders.py 做成带 60 秒 retry loop 即可。

**Property check**：
```
cfg := yaml.parse("sleep_classifier/config.yaml")
ASSERT cfg.startup = "application"
```

---

### 3.7 Bug 1.7 — 原子写

**当前行为**：`render_effective_config.py` 末尾 `_OUT_PATH.write_text(json.dumps(...))` 不原子；写一半断电/OOM 留半截 JSON。`/data/effective_config.json` 下次启动被 `jq 2>/dev/null` 解析失败，supervise 判定 stage 未绑定。

**目标行为**：写入被 SIGKILL 时，磁盘上主文件要么是旧版本要么是新版本，都是合法 JSON。

**实现方案**（回答澄清点之一：抽公共辅助）：

新增 `src/_io_utils.py`（注意下划线前缀，与 `src/_time_utils.py` 一致，表示 private helper）：
```python
"""Atomic-write helpers shared by render_effective_config / web_ui / learner.

Why not use a third-party library:
- tech.md 硬性要求运行时只依赖 aiohttp。atomic-write 这点代码不值得
  pull 一个 dep。
"""
from __future__ import annotations
import json, os, tempfile
from pathlib import Path
from typing import Any

def atomic_write_text(path: Path, data: str, *, encoding: str = "utf-8") -> None:
    """Write `data` to `path` atomically.

    Strategy: write to `<name>.tmp.<pid>` in the same directory (so rename
    is same-filesystem), fsync, os.replace. A kill anywhere before the
    os.replace leaves a stale .tmp file but the main file is intact.

    Note: 同目录 tmp 很关键——os.replace 跨文件系统会退化为 copy+unlink
    而不是原子 rename。我们写的 /data 是单一 volume，安全。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".tmp.", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        # 清理 tmp，避免 /data 长期残留半截文件
        try: os.unlink(tmp_name)
        except OSError: pass
        raise

def atomic_write_json(path: Path, obj: Any, *, indent: int = 2) -> None:
    atomic_write_text(path, json.dumps(obj, indent=indent, ensure_ascii=False))
```

替换使用点：
- `sleep_classifier/render_effective_config.py`：`_OUT_PATH.write_text(...)` → `atomic_write_text(_OUT_PATH, json.dumps(...))`。
- `sleep_classifier/web_ui.py::api_save`：现状已经走 tmp+replace 但没 fsync，改用 `atomic_write_text` 统一。
- `src/preference_learner.py::save`：现状走 tmp+`os.replace` 但 fh 没 `fsync`，改用 `atomic_write_json`。保留 `.bak` 备份路径（3.6 preservation）。
- `src/user_profile.py::save`：同上替换。
- `src/apnea_wiring.py`：同上替换。

在 `run.sh` 启动时做一次清理，把上次残留的 `*.tmp.*` 文件删除：
```bash
# 清理上次启动可能遗留的 atomic_write tmp 残留
find /data -maxdepth 2 -type f -name '*.tmp.*' -mmin +60 -delete 2>/dev/null || true
```

**对 steering 的影响**：
- `structure.md`：新增 `src/_io_utils.py`，一句话说明 "atomic write helpers; I/O 相关纯函数辅助"。
- `tech.md`：**新增一条代码约定**——"对 /data 下的 JSON 必须用 `src._io_utils.atomic_write_json`，禁止直接 `Path.write_text`"。

**风险与 fallback**：
- 风险：`tempfile.mkstemp` 会在 /data 下创建 mode 0600 的文件，可能与既有 644 不一致 → 写完 `os.replace` 后 HA add-on 场景只有容器自己读，无影响。
- Fallback：若发现某个文件系统不支持 `fsync`（罕见），`os.fsync` 会抛 `OSError`；此时降级为"写了就走"并记日志。不在本轮实现。

**Property check（hypothesis / 手工）**：
```
FOR ALL (N = bytes_to_write) DO
  FOR k IN [0, N-1] DO
    以 "write k 字节后立刻 kill" 模拟
    重启后 f := effective_config.json
    ASSERT is_valid_json(f)
    ASSERT f IN {previous_config, new_config}
  END FOR
END FOR
```

---

### 3.8 Bug 1.8 — Ingress IP 白名单

**当前行为**：`web.run_app(host="0.0.0.0", port=8099)` 响应所有源；HA Add-on 规范要求只接受 `172.30.32.2`。

**目标行为**：非 Supervisor 源 IP 的请求一律 403；`WEB_UI_DISABLE_INGRESS_GUARD=1` 豁免（给开发者用）。

**实现方案**（回答澄清点 D：IPv6 兼容 & 候选：单 middleware）：

在 `web_ui.py` 里加一个 aiohttp middleware：

```python
# ---------------------------------------------------------------------------
# Ingress IP allowlist middleware
# ---------------------------------------------------------------------------

# HA Supervisor 的 docker network (``hassio``) 在 HA OS 上是固定的：
# IPv4 172.30.32.2，见 HA Supervisor docs → Networking。
# 如果未来 HA 把 Supervisor 放到 IPv6 网段，我们通过 env 兜底：
# SUPERVISOR_IP_WHITELIST 可填 "172.30.32.2,fd00::2,::1"。
# 多值以逗号分隔；空白容错。
_DEFAULT_ALLOWED_IPS = {"172.30.32.2"}

def _parse_allowed_ips(raw: str) -> set:
    return {ip.strip() for ip in raw.split(",") if ip.strip()}

_ALLOWED_IPS = (
    _parse_allowed_ips(os.environ.get("SUPERVISOR_IP_WHITELIST", ""))
    or _DEFAULT_ALLOWED_IPS
)
_DISABLE_GUARD = os.environ.get("WEB_UI_DISABLE_INGRESS_GUARD", "") == "1"

@web.middleware
async def ingress_ip_guard(request: web.Request, handler):
    """仅允许 Supervisor ingress 源 IP 的请求通过。

    aiohttp 的 request.remote 在 run_app(host='0.0.0.0') 下是 TCP 对端
    IP (Docker 容器网络里就是 Supervisor IP)，不经 nginx 反代所以不需要
    去解析 X-Forwarded-For。若未来 HA 换网络栈，SUPERVISOR_IP_WHITELIST
    环境变量提供向前兼容。
    """
    if _DISABLE_GUARD:
        return await handler(request)
    remote = request.remote or ""
    # IPv6 的 ::ffff:172.30.32.2 映射地址也接受
    normalized = remote.removeprefix("::ffff:")
    if normalized not in _ALLOWED_IPS:
        logger.warning("Rejected Web UI request from non-Supervisor IP: %s", remote)
        return web.Response(status=403, text="Forbidden")
    return await handler(request)

def make_app() -> web.Application:
    app = web.Application(middlewares=[ingress_ip_guard])   # ← 加入中间件
    app.router.add_get("/", index)
    app.router.add_get("/api/entities", api_entities)
    app.router.add_post("/api/options", api_save)
    return app
```

**为什么不做独立 middleware 模块**（回答澄清点："放 web_ui.py 里一次写完 vs 可复用 middleware"）：
- 只有一个 HTTP server，没有复用需求。
- 独立模块会跨越 "sleep_classifier/" 和 "src/" 的目录契约（web_ui.py 在前者，src 在后者），不必要地破坏 Docker build context 单向依赖。
- 约 30 行代码，就地可读比抽象更清晰。

**关于 X-Forwarded-For**：当前 Supervisor 不通过 nginx 反代 add-on，直接 TCP 连到 :8099。所以 `request.remote` 拿到的就是 Supervisor IP，**无需**解析 XFF 首跳。这是相对 HA Core 侧 `trusted_proxies` 的重要差别。

**对 steering 的影响**：
- `tech.md`：**新增一条代码约定**——"Web UI 必须挂 `ingress_ip_guard` middleware；不得把 host 从 0.0.0.0 改成 127.0.0.1（那样 Supervisor 连不进来）"。

**风险与 fallback**：
- 风险 1：未来 HA 改用 IPv6 → `SUPERVISOR_IP_WHITELIST` env 可扩展，不改代码。
- 风险 2：某些用户用 `host_network: true`（我们不开）时源 IP 变成宿主网络 → 文档里说明 `host_network` 与本 middleware 不兼容，`WEB_UI_DISABLE_INGRESS_GUARD=1` 豁免。
- Fallback：本 middleware 出错时让请求通过还是拦截？**选拦截**（fail-closed），更符合安全 middleware 设计。

**Property check**：
```
FOR ALL ip IN {"10.0.0.1", "192.168.1.1", "::1", "127.0.0.1"} DO
  r := GET http://localhost:8099/ with source_ip = ip
  ASSERT r.status = 403
END FOR

FOR ip IN {"172.30.32.2", "::ffff:172.30.32.2"} DO
  r := GET http://localhost:8099/ with source_ip = ip
  ASSERT r.status = 200
END FOR

// 豁免路径
WITH env WEB_UI_DISABLE_INGRESS_GUARD=1 DO
  FOR ALL ip DO
    r := GET /
    ASSERT r.status = 200
  END FOR
END WITH
```

---

### 3.9 Bug 1.9 — WS 错误分类

**当前行为**：`_task_ws_listener` 把 `HAAuthError | HAAPIError` 并列 catch 并 `stop_event.set()`。由于 `HAAuthError` 是 `HAAPIError` 的子类（`src/ha_api_client.py:131`），任何 REST 错误都会走到这里。Core 重启期间的 401/503 全部误判为"auth 坏了永久 stop"。

**目标行为**：
- `HAAuthError` 连续出现 ≥N（默认 10）次才 `stop_event.set()`；单次 401 视为瞬时错误。
- `HAAPIError`（非 auth 子类，例如 500/503/timeout）视为可恢复，进入重连。
- 把 "ha.connect_websocket() 过程中的 ws handshake 失败" 与 "状态流中途断开" 合并到同一条重连路径。

**澄清点 E：`HAAuthError` / `HAAPIError` 的具体子类？需不需要改 `src/ha_api_client.py`？**

当前 `src/ha_api_client.py:129-131`：
```python
class HAAPIError(RuntimeError): ...
class HAAuthError(HAAPIError): ...
```

层次已经正确（auth 是 API 的子类），**不需要改 ha_api_client.py**。需要改的是 `_task_ws_listener` 的 catch 顺序：先 catch 子类 `HAAuthError` 做计数，再 catch 父类 `HAAPIError` 做重连。

**实现方案**：

`scripts/run_ha_smart_service.py::_task_ws_listener`（和 `sleep_classifier/rootfs/scripts/run_ha_smart_service.py` 的镜像）：

```python
async def _task_ws_listener(self, ha, discovery, engine):
    """Stream state-changed events; reconnect with exponential backoff.

    v2.0.3 — Error classification refinement:
        HAAuthError 连续 MAX_AUTH_FAILURES 次才视为 token 永久失效；
        HAAPIError (非 auth 子类) 与未分类异常都走重连路径。
    """
    backoff = 1.0
    max_backoff = 300.0
    MAX_AUTH_FAILURES = 10      # Core 重启一般 1~3 次 401 即恢复
    auth_failures = 0
    while not self.stop_event.is_set():
        try:
            async for event in ha.iter_state_changes():
                self._route_state_change(event, discovery, engine)
                if self.stop_event.is_set():
                    break
                backoff = 1.0
                auth_failures = 0    # 任何成功事件重置计数
            if self.stop_event.is_set():
                return
            logger.warning("HA WebSocket closed gracefully — reconnecting")
        except asyncio.CancelledError:
            logger.info("WebSocket task cancelled")
            raise
        except HAAuthError as exc:
            # 子类先 catch：累计计数
            auth_failures += 1
            logger.warning(
                "WebSocket auth error (%d/%d): %s",
                auth_failures, MAX_AUTH_FAILURES, exc,
            )
            if auth_failures >= MAX_AUTH_FAILURES:
                logger.error(
                    "Auth failed %d times consecutively — stopping service; "
                    "check SUPERVISOR_TOKEN / HA Core auth state",
                    MAX_AUTH_FAILURES,
                )
                self.stop_event.set()
                return
            # 还没到上限：走重连路径（与 HAAPIError 同样的 backoff）
        except HAAPIError as exc:
            # 父类：HTTP 4xx/5xx、超时、连接失败等
            logger.warning(
                "WebSocket API error (%s); reconnecting in %.1fs",
                exc, backoff,
            )
        except Exception as exc:    # noqa: BLE001
            logger.warning(
                "WebSocket transport error (%s); reconnecting in %.1fs",
                exc, backoff,
            )
        # ----- Backoff sleep（所有 recoverable 分支共用）-----
        jitter = backoff * 0.2
        try:
            await asyncio.wait_for(
                self.stop_event.wait(),
                timeout=backoff + random.uniform(-jitter, jitter),
            )
            return
        except asyncio.TimeoutError:
            pass
        backoff = min(max_backoff, backoff * 2.0)
        try:
            await ha.connect_websocket()
            await ha.subscribe_state_changes()
        except HAAuthError as exc:
            # 重连握手失败也计入 auth_failures
            auth_failures += 1
            logger.warning(
                "Reconnect auth failed (%d/%d): %s",
                auth_failures, MAX_AUTH_FAILURES, exc,
            )
        except Exception as exc:    # noqa: BLE001
            logger.warning("Reconnect attempt failed: %s", exc)
```

**区分策略**（回答澄清点 E 的补充）：
- **永久错误**：`HAAuthError` 连续 ≥ 10 次 → Token 真坏了，停 service，让 bash supervise 循环用新容器重启（Supervisor 重启时会注入新 token）。
- **瞬时错误**：
  - `HAAuthError` 单次（HA Core 重启期间 401） → 计数 + 重连。
  - `HAAPIError` 5xx / `aiohttp.ClientConnectorError` / timeout → 重连。
  - Raw `Exception`（如 asyncio 内部） → 重连（保底）。

**对 steering 的影响**：
- `tech.md`：**新增一条代码约定**——"HA 相关异常的 catch 顺序必须 auth 子类在前、API 父类在后；auth 错误须用 `MAX_AUTH_FAILURES` 计数，不可单次触发 stop"。

**风险与 fallback**：
- 风险 1：真的 token 坏了，用户要等 10 次重试才看到错 → 默认 backoff 1→300 秒指数，10 次不触顶也要约 15 分钟。可接受：add-on 层面用户本就没实时干预 token 的通道。
- 风险 2：HA Core 升级期间 > 15 分钟 → 仍被误判为 token 坏。罕见场景；若用户报告，可调 `MAX_AUTH_FAILURES` 为 env 可调（本轮不做）。
- Fallback：若未来 HA 提供 token-valid 诊断 REST，可改为"先 GET /api/，200 就不 stop"。本轮不做。

**Property check**：
```
// 瞬时错误不停止
FOR ALL X WHERE inject 3 consecutive HAAuthError followed by success DO
  ASSERT stop_event.is_set() = False
  ASSERT next_state_change_event_was_processed
END FOR

// 永久错误停止
FOR ALL X WHERE inject 10 consecutive HAAuthError DO
  ASSERT stop_event.is_set() = True WITHIN 300 * (2^10-1) 秒上限
END FOR
```


---

### 3.10 Bug 1.10 — 自定义 AppArmor profile 缺失（商业化安全分）

**当前行为**：`sleep_classifier/` 下没有 `apparmor.txt`，也没有在 `config.yaml` 声明 AppArmor profile。Supervisor 因此回退到默认"unrestricted"模式。HA 2024 年起的 add-on 评分规则（见 [Add-on ratings](https://developers.home-assistant.io/docs/add-ons/presentation#add-on-ratings)）把自定义 apparmor profile 列为 **+1 分** 加分项，商业级 add-on（hassio-addons 仓库下的 80+ 个 add-on）一贯配齐；我们目前卡在基础 5 分（ingress=+2 / auth_api=被 ingress override / 没有 apparmor=0）。

**目标行为**：
1. 新建 `sleep_classifier/apparmor.txt`，使用 AppArmor 2.x 的"add-on profile"模板，允许 add-on 必需的系统调用 + 读写路径，拒绝其他。
2. Supervisor 启动 add-on 时自动识别同目录下的 `apparmor.txt` 并加载，把 add-on 评分从 5 分提到 6 分（满分）。
3. 容器内所有既有进程（tini、bash、python3、jq、`/app/` 下的 Python 模块）不受功能影响，保持 v2.0.2 的所有行为。

**实现方案**：新建 `sleep_classifier/apparmor.txt`：

```
#include <tunables/global>

profile sleep_classifier flags=(attach_disconnected,mediate_deleted) {
  #include <abstractions/base>
  #include <abstractions/python>

  # --- Init system (tini) + shell ---
  /sbin/tini                         rmix,
  /bin/bash                          rmix,
  /usr/bin/env                       rmix,
  /bin/sh                            rmix,

  # --- Runtime binaries ---
  /usr/local/bin/python3*            rmix,
  /usr/bin/jq                        rmix,
  /usr/bin/find                      rmix,
  /bin/busybox                       rmix,   # Alpine coreutils 走 busybox

  # --- Application code ---
  /app/**                            r,
  /app/bootstrap_placeholders.py     r,
  /app/render_effective_config.py    r,
  /app/web_ui.py                     r,
  /app/requirements-runtime.txt      r,
  /run.sh                            rix,

  # --- Persistent volumes (read-write) ---
  /data/**                           rwk,    # options.json / effective_config.json / user_preferences.json
  /share/**                          rwk,    # 跨 add-on 共享目录，map: share:rw

  # --- Terminal / logging ---
  /dev/tty                           rw,     # Python logging 偶尔会打 tty
  /dev/null                          rw,
  /dev/urandom                       r,
  /dev/random                        r,

  # --- Network (Supervisor proxy 需要的最小集) ---
  network inet stream,
  network inet6 stream,
  network inet dgram,   # DNS
  network inet6 dgram,
  network unix stream,  # supervisor socket

  # --- Signals we need to receive ---
  signal (receive) set=(term, int, kill) peer=unconfined,
  signal (receive) set=(term, int) peer=@{profile_name},
  signal (send) set=(term, int, kill) peer=@{profile_name},  # 自己给子进程发

  # --- /proc 只读（logging / psutil 偶尔要看） ---
  /proc/*/status                     r,
  /proc/*/stat                       r,
  /proc/meminfo                      r,
  /proc/cpuinfo                      r,
  /proc/loadavg                      r,
  /proc/sys/kernel/random/uuid       r,

  # --- tmp / pip cache fallback (若 BUILD 阶段未清干净) ---
  /tmp/                              rw,
  /tmp/**                            rwk,

  # --- Timezone (tzdata) ---
  /etc/localtime                     r,
  /etc/timezone                      r,
  /usr/share/zoneinfo/**             r,

  # --- Deny everything else by default (AppArmor 默认拒绝) ---
  # 无需显式 deny，profile 内未 allow 的路径/能力一律被拒。
}
```

**关键设计说明**：
- **`flags=(attach_disconnected,mediate_deleted)`**：`attach_disconnected` 处理 Docker `mount --rbind` 挂载的卷在 AppArmor 眼里 "disconnected" 的情况；`mediate_deleted` 让 profile 对已删除文件的描述符仍然生效。两个 flag 是 HA add-on profile 的通用配置。
- **`#include <abstractions/python>`**：AppArmor 发行版提供的 Python 常用路径 abstraction，避免自己逐条枚举 `/usr/local/lib/python3.11/` 下几百个 `.py` 文件。
- **`/app/** r` + `/run.sh rix`**：Python 代码只读，shell 入口用 `rix`（读 + 执行 + 继承 profile）。
- **`/data/** rwk` + `/share/** rwk`**：`rwk` 含锁（lock），兼容 `fsync` + `os.replace` 的 atomic_write 路径（Bug 1.7）。
- **`signal (receive) set=(term, int, kill) peer=unconfined`**：允许 Supervisor（运行在宿主，对 add-on 的 profile 来说是 unconfined）向我们发 TERM/INT/KILL，配合 Bug 1.3 的 tini `-g` 信号转发。
- **没有 `capability` 规则**：AppArmor 对进程不授予任何 Linux capabilities，add-on 以非 root 或 root 但 unprivileged 方式跑不依赖特权操作。

**Supervisor 加载机制**：Supervisor 检测到 add-on 目录下有 `apparmor.txt`，会在 `docker run` 时自动把 profile 加载并附加到容器：等价于 `docker run --security-opt apparmor=sleep_classifier ...`。add-on 开发者无需在 `config.yaml` 声明（但加 `apparmor: true` 字段更显式，下面实现方案给出）。

**config.yaml 同步改**：
```yaml
# v2.0.3: 声明自定义 apparmor profile（sleep_classifier/apparmor.txt）
apparmor: true
```

**对 steering 的影响**：
- `structure.md`：`sleep_classifier/` 目录清单新增 `apparmor.txt —— 自定义 AppArmor profile，加 +1 安全分`。

**风险与 fallback**：
- 风险 1：profile 过严，意外拒绝某个合法文件访问 → **首次部署务必在 HA 日志里搜 `apparmor="DENIED"` 关键字**，有则补 allow 规则；测试阶段可临时切到 `complain` 模式（把 `profile ... flags=(complain)` 加 flag）记录所有 DENY 但不拦截。
- 风险 2：HA OS 未启用 AppArmor（极少见，Generic x86 HA OS 默认启用；自建 Docker 环境可能没装 apparmor-utils）→ Supervisor 会降级为 unrestricted，add-on 仍能跑，只是安全分回到 5 分。
- 风险 3：未来新增 Python 依赖引入新系统调用 → profile 里的 `#include <abstractions/python>` + `/app/** r` 通常足够；真有新需求再补 rule。
- Fallback：若 profile 导致启动失败，改 config.yaml 的 `apparmor: false` 即可关闭，功能上完全不影响。

**Property check**：
```
// 文件存在 + 基本结构合法
content := read("sleep_classifier/apparmor.txt")
ASSERT "profile sleep_classifier" IN content
ASSERT "/app/** r," IN content
ASSERT "/data/** rwk," IN content
ASSERT "/share/** rwk," IN content
ASSERT "/dev/tty" IN content
ASSERT "#include <tunables/global>" IN content

// config.yaml 声明
cfg := yaml.parse("sleep_classifier/config.yaml")
ASSERT cfg.apparmor = True

// 运行时加载（Pi 4B 人工验证）
// $ docker inspect <container> --format '{{ .AppArmorProfile }}'
// 应该返回 "sleep_classifier" 而非 "unconfined" 或 "docker-default"
```

---

### 3.11 Bug 1.11 — `build_from` 决策对齐官方 pattern

**当前行为**：
- `sleep_classifier/Dockerfile` 硬编码 `FROM python:3.11-alpine`（v2.0.1 的国内网络决策）。
- `sleep_classifier/build.yaml` 仍写 `build_from: aarch64: ghcr.io/...`（Supervisor 读但 Dockerfile 不消费）。
- 原始 §3.4 的决策是"删 build_from 只留 labels"，但对标 `hassio-addons/app-example` 后发现 HA 官方维护的所有 add-on **都保留 build_from**，并通过它向 Dockerfile 的 `ARG BUILD_FROM` 注入 base image 名。

**目标行为**：
- Dockerfile 用 `ARG BUILD_FROM=python:3.11-alpine` + `FROM ${BUILD_FROM}`，保留 "v2.0.1 国内网络决策" 的事实（不走 ghcr.io）。
- `build.yaml` 的 `build_from` 改写为 Docker Hub 的 `python:3.11-alpine`，让 Supervisor 注入的值跟 Dockerfile 默认值一致。
- 新老 builder 都兼容：老 builder 读 `build_from` → 注入 ARG；新 builder 不读 `build_from` → Dockerfile 的 `ARG BUILD_FROM=python:3.11-alpine` 默认值兜底。

**实现方案**：

Dockerfile 改（把 v2.0.1 的大段 comment 保留，但 FROM 行换成 ARG 驱动）：
```dockerfile
# --- Base image -------------------------------------------------------------
# v2.0.1 因国内 ghcr.io 不可达切到 Docker Hub 的官方 python:3.11-alpine。
# v2.0.3 对标 hassio-addons/app-example，改用 ARG BUILD_FROM + FROM ${BUILD_FROM}
# pattern，兼容新老 Supervisor builder。build.yaml 的 build_from 同步改为
# python:3.11-alpine，两处值一致不漂移。
ARG BUILD_FROM=python:3.11-alpine
FROM ${BUILD_FROM}
```

build.yaml 改（内容见 §3.4 的 "实现方案"）。本节不重复贴。

**为什么不切 HA 官方 base (`ghcr.io/home-assistant/base:latest`)**：见 §4.9"为什么不切回 HA 官方 base image"的专门讨论。简言之：国内网络硬约束让 ghcr.io 不可达；而官方 base 提供的 bashio / s6-overlay / apparmor 样板，我们用 tini + bash + 自写 apparmor.txt 手动模拟其中对我们有用的部分。

**对 steering 的影响**：无（Dockerfile / build.yaml 是 add-on 容器的构建配置，不进 src/）。

**风险与 fallback**：
- 风险 1：若 Docker Hub 在某用户环境也不可达 → 用户可在 build.yaml 里改 `build_from` 指向任意 Python 3.11 Alpine 镜像的 mirror（如 Aliyun、USTC）；无需改 Dockerfile。
- 风险 2：`python:3.11-alpine` 的 tag 漂移（Alpine minor 版本升级）→ 可在 build_from 里显式锁 `python:3.11.10-alpine3.19`。本轮不锁，跟随上游。
- Fallback：无必要。

**Property check**：
```
dockerfile := read("sleep_classifier/Dockerfile")
ASSERT "ARG BUILD_FROM" IN dockerfile
ASSERT "FROM ${BUILD_FROM}" IN dockerfile OR 'FROM $BUILD_FROM' IN dockerfile
ASSERT NOT ("FROM python:3.11-alpine" IN dockerfile WHERE not preceded by ARG)

build_yaml := yaml.parse("sleep_classifier/build.yaml")
ASSERT build_yaml.build_from.aarch64 = "python:3.11-alpine"
ASSERT build_yaml.build_from.amd64 = "python:3.11-alpine"
ASSERT NOT (build_yaml.build_from.aarch64 contains "ghcr.io")
```

---

### 3.12 Bug 1.12 — `config.yaml` 缺 `url:` + 镜像缺完整 OCI labels

**当前行为**：
- `sleep_classifier/config.yaml` 没写 `url:` 字段，Supervisor UI 的"项目主页"链接缺失，用户从 add-on 详情页跳不到 GitHub。
- 镜像 `LABEL` 仅 4 条（见 §3.5 修改前），对标 `hassio-addons/app-example` 的 15 条工业级标准差距明显。本节覆盖 `config.yaml` 的 `url` 补充；label 扩展到 15 条的工作已并入 §3.5 Bug 1.5。

**目标行为**：
- `config.yaml` 加 `url: https://github.com/LiangyuLu-lly/HA-sleep`，Supervisor UI 显示可点击的"Project homepage"链接。
- 镜像 `LABEL` 扩展到 15 条（见 §3.5）。

**实现方案**：

`sleep_classifier/config.yaml` 在 `name:` 附近加一行：
```yaml
name: Sleep Classifier
version: "2.0.3"
slug: sleep_classifier
# v2.0.3: Supervisor UI 的"项目主页"链接。对商业级 add-on 标配。
url: "https://github.com/LiangyuLu-lly/HA-sleep"
description: >-
  ...（保持不变）
```

labels 15 条的 Dockerfile 扩展见 §3.5。

**对 steering 的影响**：无。

**风险与 fallback**：
- 风险：Supervisor 对 `url:` 字段做严格 HTTPS/schema 校验 → 实测 2024+ Supervisor 接受任何以 `http(s)://` 开头的字符串。
- Fallback：若用户环境 Supervisor 版本低不支持 `url:` → 字段被忽略，不影响 add-on 启动。

**Property check**：
```
cfg := yaml.parse("sleep_classifier/config.yaml")
ASSERT cfg.url = "https://github.com/LiangyuLu-lly/HA-sleep"
ASSERT cfg.url starts_with "https://"

// labels 断言（与 §3.5 Property check 合并）
```


---

## 4. 跨 Bug 的设计决策（含澄清点 A–E 的最终回答）

本节把分散在 §3 各条里的决策归总，方便 review 时一次看完。

### 4.1 进程管理策略：run.sh 的 `wait -n` 优于 supervisord（澄清点 A 最终回答）

选择 **候选 2（tini `-g` + bash `wait -n`）**。

**理由**：
1. 最小依赖：tini 本来就在 `apk add tini` 里，仅 ENTRYPOINT 加 `-g` 参数；`wait -n` 是 bash 4.3+ 原生，Alpine 3.19 自带 bash 5.2。
2. 不改变容器架构：Web UI 与 smart service 仍是独立进程，保留 v2.0.2 的 isolation 原则（smart service 崩溃 ≠ Web UI 不可用）。
3. Signal 语义正确：
   - tini `-g` → `kill(-pgid, SIGTERM)` 转发到整个进程组；
   - bash `set -m` 让后台 job 进入独立 pgid；
   - `trap` 在未 `exec` 的 bash 里**不会丢**。
4. 拒绝候选 4（合并入口）的理由：合并后 smart service 任何异常会拖垮 Web UI，回到 v2.0.2 之前的 502 循环，与 1.3 的修复目标矛盾。
5. 拒绝候选 3（supervisord）的理由：~6 MB 镜像增量违反 "不爆增" 约束；且本场景只有 2 个进程，supervisord 过度工程化。

**trade-off**：`wait -n` 不等到**所有**子进程退出，只等到**任意一个**退出。`_shutdown` 函数里手动二次 `kill` + 轮询等待补齐这个语义。

### 4.2 占位 sensor 策略：独立 bootstrap Python 脚本（澄清点 B 最终回答）

选择 **候选 2（新增独立 bootstrap 脚本）**。

**理由**：
1. curl 直接 POST（候选 1）在 options.json 含中文 entity_id 时仍然 OK，但 bash + jq + Chinese 一直是本项目的脆弱点（v2.0.2 的 heredoc 决策就是来源），避免继续叠。
2. 让 smart service 发（候选 3）=依赖 stage 绑定路径，正是 1.1 的病根。
3. Web UI 发（候选 4）= Web UI 启动晚于 bash opts 解析，且每次 HA 重启 Web UI 都已在线，"Web UI 启动"不是合适的 hook。
4. 独立脚本（候选 2）= 解耦 + 可单独测试 + 重用 runtime 已装的 aiohttp，零新依赖。

### 4.3 原子写通用化（答"共用辅助"）

新增 `src/_io_utils.py`，提供 `atomic_write_text` / `atomic_write_json`。替换 5 处 write_text 使用点（见 §3.7）。保留 `preference_learner.py` 与 `user_profile.py` 已有的 `.bak` 备份机制（preservation 3.6）。

### 4.4 Ingress middleware 放在 `web_ui.py` 里（答"一次写完 vs 可复用"）

就地写 30 行 middleware。不抽公共包的理由见 §3.8。env 变量 `SUPERVISOR_IP_WHITELIST` / `WEB_UI_DISABLE_INGRESS_GUARD` 提供灵活性。

### 4.5 WebSocket 重连分类：可恢复 vs 永久（答"区分策略"）

| 错误 | 分类 | 处理 |
|---|---|---|
| `HAAuthError`（WS auth_failed / REST 401） 单次 | 可恢复 | auth_failures += 1，重连 |
| `HAAuthError` 连续 ≥ 10 次 | 永久 | `stop_event.set()`，让 bash supervise 重启容器 |
| `HAAPIError` HTTP 5xx | 可恢复 | 重连 |
| `aiohttp.ClientConnectorError` / Timeout | 可恢复 | 重连 |
| `CancelledError` | 协作停机 | 正常 raise |
| 其它 `Exception` | 可恢复（保底） | 重连 |

**没被选中的策略**：按 HTTP 状态码精细分类（401 vs 403 vs 410）——过度工程化，我们只需区分"一次失败"和"长期失败"。

### 4.6 澄清点 C：`options.json` 读取时机

`startup: application` 不影响 `/data/options.json` 可见性。Supervisor 在容器 `docker run` 之前已挂载卷并写好 options.json；startup 字段只控制 `docker run` 发生的时机。所以 run.sh 顶部 `jq -r ... /data/options.json` 在 application 下仍然工作。

### 4.7 澄清点 D：IPv6 兼容

短期：Supervisor 在 HA OS 上固定使用 IPv4 `172.30.32.2`（由 hassio docker network 管理）。中长期通过 `SUPERVISOR_IP_WHITELIST` env 支持扩展；middleware 里对 IPv6 映射地址 `::ffff:172.30.32.2` 已处理。若未来 HA 明确迁移到 IPv6-only，用户可设 `SUPERVISOR_IP_WHITELIST="fd00::2"` 覆盖默认值，不需改代码。

### 4.8 澄清点 E：`HAAuthError` / `HAAPIError` 子类 & 是否改 `src/ha_api_client.py`

- 当前层次正确（`HAAuthError` ⊂ `HAAPIError`）。
- **不改 `src/ha_api_client.py`**。
- 仅修 `scripts/run_ha_smart_service.py::_task_ws_listener` 的 catch 顺序 + 加 `auth_failures` 计数器。详情见 §3.9。

### 4.9 为什么不切回 HA 官方 base image（对标澄清点 A）

**背景**：对标 `hassio-addons/app-example` 时发现 HA 官方维护的 add-on 都基于 `ghcr.io/hassio-addons/base:20.1.1`（或 `ghcr.io/home-assistant/<arch>-base:latest`）。这些 base image 开箱即用：
- `s6-overlay v3` 作为 init system（比 tini 更"官方"）。
- `bashio` shell 库（`/command/with-contenv bashio`），封装配置读取、HA API 调用。
- 预装 `tzdata`、`jq`、apparmor profile 样板，减少 add-on 自己 `apk add`。

**理论上我们应该切回去**，对齐官方 pattern。**实际不能切**，硬约束如下：

| 阻碍 | 描述 |
|---|---|
| **v2.0.1 国内网络决策** | `ghcr.io` 在中国大陆家用网络 TLS 握手 10+ 分钟失败；Docker Hub 有 Aliyun/Tencent/USTC 官方 mirror，HA OS 的 Docker daemon 能自动 fallback。切回 ghcr.io 会让相当一部分 Pi 4B 用户首次装 add-on 直接卡住。 |
| **Docker Hub 不提供 `ghcr.io/hassio-addons/base` 的镜像** | 这是 HA 生态自己的私有镜像，不在 Docker Hub 发布；我们没法把它 mirror 过来。 |

**替代方案**：**保持 `python:3.11-alpine`（Docker Hub），手动模拟官方 base 的关键能力**：

| 官方 base 提供 | 我们的实现 | 理由 |
|---|---|---|
| `/sbin/tini` + s6-overlay v3 作为 init | `apk add tini` + `tini -g --` + bash `wait -n` | tini `-g` 已能实现进程组信号转发（Bug 1.3），且不引入 ~3 MB 的 s6-overlay |
| `tzdata` 全套时区 | Alpine 的 `apk add --no-cache tzdata`（按需）/ Python 自带 tz 处理 | 当前代码用 `_time_utils.py` 纯 Python 时区，无需系统 tzdata |
| `bashio` 配置读取库 | `jq + bash` 直接读 `/data/options.json`（现状） | bashio 依赖 `#!/command/with-contenv bashio` shebang（属于 s6-overlay 约定）；`python:3.11-alpine` 没 `/command/` 目录，引入 bashio 需先切 base image，循环依赖 |
| AppArmor profile 样板 | 自写 `sleep_classifier/apparmor.txt`（Bug 1.10） | 可以精细到只允许我们用到的路径，而不是通用样板 |
| `jq`、Python、pip 预装 | `python:3.11-alpine` 自带 Python/pip；`apk add jq` | 体积差不多 |

**明确模拟/不模拟表**：

- ✅ 模拟：init system（tini）、AppArmor profile、工业级 labels、SIGTERM 传播。
- ❌ 不模拟：bashio（shell 函数库），理由是与 `python:3.11-alpine` 不兼容（要求 `/command/with-contenv` 存在）；s6-overlay（init system），理由是 tini 已够用且 +3 MB 不值。
- ❌ 不模拟：官方 base 的 "rolling version" 机制（`base:latest` 自动跟 Alpine 升级）；我们锁定 `python:3.11-alpine` 的主版本，minor 跟随上游。

**决策**：**保持 `python:3.11-alpine`，用 ARG BUILD_FROM 消费**（Bug 1.11），目录下自写 `apparmor.txt`（Bug 1.10），Dockerfile 里显式写 15 条 LABEL（Bug 1.5 扩展 + Bug 1.12）。这是"国内网络硬约束 ∩ 商业级元数据"的最优解。

---

## 5. 本轮不做的事情（范围蔓延防护）

明确列出审计中看到但**本轮不碰**的技术债，避免 PR 蔓延：

1. **重写 supervise_smart_service 为 Python asyncio.create_subprocess_exec**：理论更干净，但需要把 bash 循环搬到 Python 里，改动面 5× 于本次。留给未来 v2.1。
2. **把 Web UI 合并进 smart service（候选 4）**：被 §4.1 明确拒绝，不留悬念。
3. **HTTP 状态码级别的错误分类**（401 vs 403 vs 410 vs 419 ...）：§4.5 已说明不做。
4. **把 `bootstrap_placeholders.py` 进一步 merge 到 `SleepStatePublisher.publish_initial_placeholders`**：两者职责分开更清晰（一个先于 discovery，一个后于）。
5. **WebSocket 重连的 auth_failures 阈值 env 化**：默认 10 够用；暴露 env 会增加用户手册负担。
6. **`io.hass.*` label 之外的 OCI label 补全**（如 `org.opencontainers.image.created` / `vcs.ref`）：~~BuildKit 的 `--label build-arg-injection` 是未来事，本轮不做。~~ **v2.0.3 起已做**（Bug 1.5 扩展到 15 条）。
7. **重构 `render_effective_config.py` 分模块**：当前 ~200 行尚可；不做。
8. **`config.yaml` 里 `homeassistant: "2024.1.0"` 字段的 min-version 校验**：bugfix.md 没列为 bug（用户环境 HA Core 2026.4.2 远高于 floor），不改。
9. **Docker multi-stage build 减体积**：v2.0.1 的单 stage Alpine + aiohttp wheel 决策仍有效；本轮不改。
10. **把 `web_ui.py` 的内嵌 HTML 拆到独立文件**：~100 行，内嵌便于镜像审计；不拆。
11. **迁移到 bashio（HA 官方 shell 库）**：bashio 依赖 `#!/command/with-contenv bashio` shebang，与 `python:3.11-alpine` base image 不兼容（要求 `/command/` 目录，属 s6-overlay 约定）。短期保持 `jq + bash` 读取 `/data/options.json`（v2.0.2 的决策）。**未来（v2.1+）如要迁移 bashio，须先评估是否连带切回 HA 官方 base image（见 §4.9 结论：国内网络硬约束下不切）。**structure.md / tech.md 的代码约定同步加一条"本项目配置读取走 jq + bash，不用 bashio"。
12. **add-on `icon.png`（128×128 PNG）**：商业级 add-on 一般配图标（床 / 月亮 / 睡眠相关），Supervisor UI 显示有品牌感。本轮不做：不是 bug；需找合适素材（License-friendly）；未来有需求再补。对应地 `config.yaml` 也不加 `icon: icon.png` 字段。
13. **迁移到 s6-overlay v3 作为 init system**：HA 官方 base image 都用 s6，但本项目只有 2 个 supervised 进程（Web UI + smart service supervisor），tini `-g` + bash `wait -n`（Bug 1.3 决策）已足够处理信号转发与进程监管。s6-overlay 引入 ~3 MB 镜像增量 + learning curve，收益不足。**Bug 1.3 的 wait -n 架构 + Bug 1.10 的 apparmor.txt 已覆盖 s6 能提供的大部分运维保证**。
14. **在 run.sh 中签名 build 或 cosign 镜像**：供应链安全属于下一个量级的议题，v2.1+ 考虑。

---

## 6. Testing Strategy

### Validation Approach

两阶段：
1. **探索阶段**：在未修复的代码上写失败测试，确认 bug 可复现 + 根因假设正确。
2. **确定阶段**：修复 + 确保测试转绿 + 补 preservation 测试确保 v2.0.2 行为不倒退。

### 6.1 Exploratory Bug Condition Checking

目标：surface 每条 bug 的反例。按 bug 列出可以本地（pytest）跑的探索性测试：

| Bug | 探索测试 | 环境 |
|---|---|---|
| 1.1 占位 sensor | 启容器 / mock Supervisor → 不绑 stage → 60s 后查 /api/states → 无 `sleep_classifier_*` | E2E（启容器） |
| 1.2 Ingress 路径 | grep `fetch('/api/` → 断言 count = 0 | 单元（pytest） |
| 1.3 SIGTERM | 起容器 → docker stop → 容器退出时间 > 10s 视为失败 | E2E |
| 1.4 build.yaml | read "sleep_classifier/build.yaml" → 断言 `build_from` 存在且不含 `ghcr.io` | 单元 |
| 1.5 labels | 构建镜像 → `docker inspect` → 断言含 15 条 label（5 `io.hass.*` + 10 `org.opencontainers.image.*`） | 集成（docker build） |
| 1.6 startup | yaml.parse → 断言 startup = "application" | 单元 |
| 1.7 原子写 | mock `os.replace` 在 write 中途抛异常 → 断言旧文件完整 | 单元 |
| 1.8 IP 白名单 | aiohttp TestClient 用伪造 remote="10.0.0.1" → 断言 403 | 单元 |
| 1.9 WS 错误 | mock `iter_state_changes` 连续抛 3 次 HAAuthError 然后正常 → 断言 stop_event 未被 set | 单元 |
| 1.10 AppArmor profile | read "sleep_classifier/apparmor.txt" → 断言 `profile sleep_classifier` 存在；断言 `/app/** r,` / `/data/** rwk,` / `/share/** rwk,` / `/dev/tty` / `#include <tunables/global>` 全命中；yaml.parse(config.yaml) → `apparmor == true` | 单元 + E2E（Pi 4B `docker inspect --format '{{.AppArmorProfile}}'` 返回 "sleep_classifier"） |
| 1.11 build_from | read Dockerfile → 断言 `ARG BUILD_FROM` 存在且 `FROM ${BUILD_FROM}`；read build.yaml → 断言 `build_from.aarch64 == "python:3.11-alpine"` 且不含 ghcr.io | 单元 |
| 1.12 url + OCI labels | yaml.parse(config.yaml) → 断言 `url` 以 `https://` 开头；docker inspect → 断言 label 总数 ≥ 15（主要断言延续 1.5） | 单元 + 集成 |

### 6.2 Fix Checking

目标：证明对每条 bug 的 C(X) 成立输入，F'(X) 产出正确行为。

```
FOR ALL X WHERE isBugCondition(X) DO
  result := F_prime(X)
  ASSERT correctnessProperty_applies(X, result)
END FOR
```

具体断言见 §3 每条 bug 的 "Property check" 小节。

### 6.3 Preservation Checking

目标：对 `NOT isBugCondition(X)` 的输入，F'(X) = F(X)。

**推荐用 property-based testing**，但本项目目前没有 hypothesis 依赖（`tech.md` 明确说）；所以以**确定性回归测试**落地：

| Preservation 点 | 测试方式 |
|---|---|
| 3.1 镜像构建不依赖 ghcr.io | 单元：grep Dockerfile 确保 `FROM python:3.11-alpine` |
| 3.2 stage 未绑定容器在线 | E2E：不绑 stage → 60s 后 Web UI 仍 200 |
| 3.3 中文 options 能走通 | 单元：mock `SC_AREA="卧室"` + 反斜杠 → `render_effective_config.py` 输出有效 JSON |
| 3.4 SUPERVISOR_TOKEN 链路 | 单元：mock supervisor REST → 断言 Bearer 头含 token |
| 3.5 state_changed 分发 | 单元（已有）`test_smart_sleep_service_*.py`，跑原样保持绿 |
| 3.6 .bak 恢复 | 单元（已有）`test_preference_learner.py`，跑原样保持绿 |
| 3.7 dry_run 默认 | 单元：mock 空 options → 断言 ctrl_cfg.dry_run = True |
| 3.8 aiohttp wheel | 单元：检查 Dockerfile 的 `.build-deps` 安装 + pip install 路径 |

### 6.4 Unit Tests（新增估算）

| 测试文件 | 新增测试数 | 类型 |
|---|---|---|
| `tests/test_bootstrap_placeholders.py` | 4 | 覆盖 SUPERVISOR_TOKEN 缺失、POST 失败、POST 成功、Chinese 实体 ID |
| `tests/test_web_ui_ingress_paths.py` | 2 | 相对路径契约、路由挂载契约 |
| `tests/test_web_ui_ip_guard.py` | 6 | 允许 172.30.32.2 / ::ffff:172.30.32.2；拒绝 10.x / 127.0.0.1 / None；env 豁免；env 覆盖默认 |
| `tests/test_io_utils_atomic_write.py` | 5 | 正常写、tmp 清理、fsync 失败、跨目录退化、并发 |
| `tests/test_ws_listener_error_classification.py` | 6 | 单次 401 不停、连续 10 次 401 停、500 不停、CancelledError、ClientConnectorError、auth 成功后重置计数 |
| `tests/test_build_yaml_shape.py` | 3 | build_from 存在、指向 python:3.11-alpine（非 ghcr.io）、有 labels |
| `tests/test_config_yaml_startup.py` | 1 | startup = "application" |
| `tests/test_render_effective_config_atomic.py` | 3 | 正常写用 atomic_write、mid-write kill 不损坏主文件、tmp 清理 |
| `tests/test_apparmor_profile.py`（Bug 1.10） | 3 | 文件存在、含 `profile sleep_classifier` / `/app/** r` / `/data/** rwk` / `/share/** rwk` / `/dev/tty` / `#include <tunables/global>`；config.yaml 里 `apparmor: true` |
| `tests/test_dockerfile_build_from.py`（Bug 1.11） | 2 | Dockerfile 含 `ARG BUILD_FROM` + `FROM ${BUILD_FROM}`；build.yaml 的 build_from 不含 ghcr.io |
| `tests/test_config_yaml_url_and_labels.py`（Bug 1.12） | 3 | config.yaml 有 `url` 字段且以 `https://` 开头；Dockerfile 的 LABEL 数量 ≥ 15；5 个 `io.hass.*` 全在 |

**合计约 38 条新增测试。** 现有 501 条保持绿。

### 6.5 Integration / E2E Tests

以下只能在真容器里跑（需要 sleep_classifier prepare.sh → docker build → start）：

- 1.1 占位 sensor 60 秒可见
- 1.3 SIGTERM 8 秒内退出 + mtime 更新
- 1.5 io.hass labels 在 docker inspect 可见
- 3.2 stage 未绑容器保持在线

E2E 不进 CI（树莓派 4B 目标环境本地验证即可），仅列 check-list 给用户：

```
Pi 4B 手动验证 check-list:
[ ] sync-to-ha.ps1 → REBUILD → START
[ ] Web UI 30 秒内能打开
[ ] Lovelace 60 秒内有 ≥5 个 sensor.sleep_classifier_*
[ ] 其中每个的 attributes 含 reason=awaiting_stage_binding
[ ] 绑 stage + RESTART 后 smart service 正常 publish 真实 stage
[ ] 再次 RESTART → 日志 "signal received — forwarding SIGTERM"
[ ] 容器 8 秒内退出，/data/user_preferences.json mtime 更新
[ ] docker inspect 镜像 → labels 总数 ≥ 15，含 io.hass.type=addon / io.hass.name / io.hass.version / org.opencontainers.image.source
[ ] 用 `curl http://<pi-ip>:8099/` 从另一机器 → 403
[ ] (Bug 1.10) `docker inspect <container> --format '{{.AppArmorProfile}}'` 返回 "sleep_classifier"（不是 "unconfined" / "docker-default"）
[ ] (Bug 1.10) HA 日志 grep `apparmor="DENIED"` → 无命中
[ ] (Bug 1.11) docker inspect image → `.Config.Env` 里 `BUILD_FROM=python:3.11-alpine`（或 build log 里可见 FROM 行展开为此值）
[ ] (Bug 1.12) Supervisor UI 的 add-on 详情页显示可点击的"Project homepage"链接，跳转到 GitHub
```

---

## 7. 与 Steering 文档的契合点

| Steering 文件 | 本次是否要更新 | 更新内容 |
|---|---|---|
| `tech.md` | **是** | 新增 4 条代码约定：(a) Add-on entrypoint 不用 `exec`，必须 `wait -n`；(b) /data 下 JSON 走 `src._io_utils.atomic_write_json`；(c) HA 异常 catch：auth 子类在前、API 父类在后，auth 错误须累计计数；(d) 本项目配置读取走 `jq + bash`，不用 bashio；未来迁移 bashio 须先评估是否切回 HA 官方 base image（见 design §4.9）。 |
| `structure.md` | **是** | 新增 `src/_io_utils.py`；`sleep_classifier/` 目录清单加 `bootstrap_placeholders.py`、`apparmor.txt`。 |
| `product.md` | 否 | 产品定位不变 |
| `language.md` | 否 | 文档继续中文，代码标识符英文，本 design 本身已符合 |

---

## 8. 本 Design 的最终决策摘要（给 reviewer 的 TL;DR）

1. **进程管理**：tini `-g` + bash `wait -n`（不 exec），零新依赖。
2. **占位 sensor**：独立 `bootstrap_placeholders.py`，aiohttp 复用 runtime 依赖。
3. **原子写**：`src/_io_utils.py` 提供通用 `atomic_write_json`，5 处调用点替换。
4. **Ingress middleware**：就地 30 行写在 `web_ui.py`，env 提供 IPv6 扩展位。
5. **WS 错误分类**：子类优先 catch，auth 错误累计 10 次才永久 stop。
6. **不改 `src/ha_api_client.py`**（异常层次已正确）。
7. **不引入新 Python / apk 依赖**，镜像体积零增量（tini `-g` 是参数不是新二进制）。
8. **不合并 Web UI + smart service**（保留 v2.0.2 的 isolation）。
9. **（v2.0.3 新增）15 条 OCI / HA labels**：对标 `hassio-addons/app-example`，从 4 条扩到 15 条；Dockerfile 用 `ARG BUILD_NAME / BUILD_DESCRIPTION / BUILD_REF / BUILD_DATE / BUILD_REPOSITORY` 由 Supervisor 注入。
10. **（v2.0.3 新增）自定义 AppArmor profile**：新建 `sleep_classifier/apparmor.txt`，允许 tini/bash/python3/jq 执行 + `/app/` 读 + `/data/` `/share/` `/dev/tty` 读写；`config.yaml` 加 `apparmor: true`；商业级安全分从 5 分提到 6 分（满分）。
11. **（v2.0.3 新增）build_from 保留并指向 Docker Hub**：原 §3.4 的"删 build_from"决策反转。对标 `hassio-addons/app-example` 后改为 Dockerfile 用 `ARG BUILD_FROM=python:3.11-alpine` + `FROM ${BUILD_FROM}`，`build.yaml` 的 `build_from` 同步改为 `python:3.11-alpine`（不再 ghcr.io），新老 builder 兼容。
12. **（v2.0.3 新增）`config.yaml` 加 `url:`**：Supervisor UI 的"项目主页"链接补全，对齐商业级 add-on 元数据。
13. **（v2.0.3 新增）不切回 HA 官方 base image**：国内网络硬约束；手动模拟官方 base 的关键能力（init = tini、安全 = 自写 apparmor.txt、配置读取 = jq），不模拟 bashio 与 s6-overlay。

确认无误后可进入 tasks.md 阶段，把上述 12 条 bug 拆成 ~20 个可独立完成的任务。
