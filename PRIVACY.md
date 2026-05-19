# 隐私声明（Privacy Notice）

> 适用对象：Sleep Classifier Home Assistant Add-on（以下简称「本 Add-on」）  
> 维护者：[LiangyuLu-lly](https://github.com/LiangyuLu-lly) ‹liangyulu781@gmail.com›  
> 最近更新：v2.1.0  
> 关联文档：[`SECURITY.md`](SECURITY.md) · [`MEDICAL_DISCLAIMER.md`](MEDICAL_DISCLAIMER.md) · [`CONTRIBUTING.md`](CONTRIBUTING.md)

---

## English Summary (TL;DR)

Sleep Classifier is an offline-by-default Home Assistant add-on. All sleep data,
preference history, and configuration files live exclusively under the add-on's
private `/data/` volume on your own Home Assistant host. We do not transmit any
data to third parties. An optional anonymous telemetry channel is **off by
default** and, when enabled, only reports `{install_id, version, ha_version,
arch, locale, days_since_install, active_last_24h}` once per 24 hours; entity
IDs, sensor values, tokens, and preference data are never included. You can
revoke consent at any time from the Web UI; the add-on will stop the telemetry
task and delete `/data/install_id.uuid` within 30 seconds. Health-related
outputs are not medical advice — see [`MEDICAL_DISCLAIMER.md`](MEDICAL_DISCLAIMER.md).

---

## 1. 数据控制者与联系方式

- 数据控制者：本仓库的开源维护者 `LiangyuLu-lly`（个人开发者，无独立法人实体）。
- 隐私 / 数据请求联系邮箱：`liangyulu781@gmail.com`。
- 安全漏洞披露请走 [`SECURITY.md`](SECURITY.md) 的专用流程，不走此邮箱。

> 注：本 Add-on 在 GDPR 框架下属于「end-user-deployed open-source software」，
> 用户即为数据控制者；维护者仅提供软件本身，不接收、不存储、不处理任何用户睡眠
> 数据。本声明聚合了 GDPR 第 13 条要求的「数据类型 / 处理目的 / 保留期限 /
> 用户权利」四项关键信息，便于用户审计自己的部署。

## 2. 处理的数据类型

本 Add-on 在运行时会读写以下几类数据，**全部存储在用户自己的 HA 主机上**（默认
路径见 §3）。

| 类别 | 内容举例 | 用途 | 来源 |
|---|---|---|---|
| 睡眠分期 | `AWAKE` / `LIGHT` / `DEEP` / `REM` 字符串 + 时间戳 | 触发 per-stage 调节策略 | 用户已有的 HA 实体（手环 / 雷达 / 第三方 add-on） |
| 环境读数 | 温度、湿度、亮度、风扇档位 | 学习用户偏好 + 闭环调节 | 用户已有的 HA 传感器 |
| 偏好历史 | 历史 sleep session 的中位数、k-NN 邻居池、per-stage delta | 个性化推荐 | `PreferenceLearner` 计算产物 |
| 睡眠质量分 | 0–100 分 + 子分（结构 / 效率 / 碎片化 / 入睡） | 训练学习器、显示给用户 | `SleepQualityScore` 计算产物 |
| 主观反馈 | `input_number.*` 晨起评分（1–5） | 与客观分融合 | 用户在 HA UI 主动提交 |
| 配置覆盖 | Web UI 选中的 entity_id、设备槽位绑定、`dry_run` 标志 | 控制器决策 | 用户在 Web UI 主动操作 |
| 安装标识 | UUIDv4 字符串（`install_id`） | 仅在用户开启遥测时统计活跃安装数 | 首次开启遥测时本地随机生成 |

**永不收集**：HA 长效访问令牌、Supervisor token、家庭住址、生物识别原始信号、
具体 entity_id 列表、温湿度数值、用户姓名、邮箱、IP 地址。

## 3. 数据存储位置

所有持久化文件都位于 Home Assistant Supervisor 分配给本 Add-on 的私有
volume 内，只有本 Add-on 与同主机上的 root 可访问：

| 文件 | 内容 | 由谁写入 |
|---|---|---|
| `/data/options.json` | HA Supervisor 配置表单结果 | HA Supervisor |
| `/data/effective_config.json` | 启动期渲染后的最终运行时配置 | `run.sh` |
| `/data/web_ui_overrides.json` | Web UI 选中的实体 ID、设备槽位、开关 | 本 Add-on（Web UI） |
| `/data/user_preferences.json` | 偏好学习历史 | 本 Add-on（`PreferenceLearner`） |
| `/data/install_id.uuid` | 匿名安装标识（仅在 `telemetry_enabled = true` 且首次启动后存在） | 本 Add-on（`TelemetryReporter`） |
| `/data/last_upgrade_check.json` | 上次 GitHub release 检查结果（仅在 `upgrade_notifications_enabled = true` 时存在） | 本 Add-on（`UpgradeNotifier`） |

> 写入策略：所有 `/data/*.json` 一律走 `src/_io_utils.atomic_write_json`，避免
> 写入中断造成损坏；技术细节见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

`/share/` 目录可选用于把诊断导出给其它 add-on 使用，默认不写入睡眠原始数据。
`/config/` 与 `/media/` 默认不被本 Add-on 读写。

## v3.0.0 算法栈数据流

> 适用版本：v3.0.0 起；本节是
> [Requirement 14.3](.kiro/specs/algorithmic-moat-v3.0.0/requirements.md)
> 的权威映射，列举 v3.0.0 引入的「4 个算法护城河」模块各处理哪些数据、
> 写到哪些文件、是否离开本地。**所有问题的答案均为「否」**——4 个模块
> 全部为纯本地推理 + 纯本地持久化，无任何新出站网络请求。

### 模块逐一披露

| 模块 | 读取的数据 | 写入的文件 | 写入函数 | 是否离开本地 |
|---|---|---|---|---|
| **BAO（Bayesian Optimizer，贝叶斯优化器）** | `/data/user_preferences.json` 中的历史 session 观测（环境读数 + 睡眠质量分），最多滚动 60 个（FIFO） | `/data/bao_model.pickle`：GP 后验状态（kernel 超参、观测点坐标、cholesky 分解缓存） | `_io_utils.atomic_write_bytes` | **否**。文件全部留在 add-on 的私有 `/data/` volume；不进入遥测 payload，不上传任何外部服务 |
| **CAE（Causal Attribution Engine，因果归因引擎）** | session 完成后由主流程注入的 6 维干扰因子（HRV、噪声、光、温度漂移、湿度漂移、体动）+ 当晚睡眠质量分 | `/data/causal_factors.jsonl`：每行一条 `{install_id_hash, ts_hour, factors, effect, ci_low, ci_high, ...}`，FIFO 90 行 | `_io_utils.atomic_append_jsonl` | **否**。`install_id_hash` 字段仅存 `sha256(install_id)` 的前 16 字节十六进制，**永远不存原始 `install_id`**（R14.2 隐私契约）；时间戳精度截断到小时；不进入遥测 payload |
| **PP（Population Prior，人群先验）** | `training_config/population_prior.pickle`（**只读**，由 prepare 脚本在镜像构建期 COPY 进 add-on，运行时挂载到容器内只读路径） | **不写任何文件**；运行时仅 `Path.read_bytes` + `pickle.loads` + `hashlib.sha256` 校验 | —（永不写） | **否**。pickle 内**只**含按 `(age_band, sex, chronotype, season)` 4 维分桶的聚合标量（均值、方差、`n_samples`），**不含**任何 EDF 波形、annotation、subject ID、时间戳等可还原个体的信息；用户填写的 `age_band / sex / chronotype` 仅写入 `/data/web_ui_overrides.json` 的 `v3_user_profile` 子字段，亦不出本机（与 [`docs/POPULATION_PRIOR.md`](docs/POPULATION_PRIOR.md) §7 一致） |
| **EMST（End-Model Stage Predictor，端侧 stage 预测）** | `training_config/stage_predictor.onnx`（≤ 80 KB，INT8 量化，**只读**，prepare 脚本镜像进来）+ 运行时来自 HA 的 5 分钟时间窗 (HRV、体动、呼吸率) 三通道时序 | `/data/predictor_audit.jsonl`：每次提前预测的命中审计（预测 stage、实际 stage、置信度、时间戳），按 7 晚滚动 prune | `_io_utils.atomic_append_jsonl` | **否**。ONNX 推理由 `onnxruntime` 的 CPU provider 在本机执行，无任何模型权重 / 中间张量 / 推理输入向外网传输 |

### 出口清单（与 §5 第三方传输总表一致）

v3.0.0 **不新增**任何出站网络请求 ——

- BAO / CAE / PP / EMST 全部为本地推理；没有任何 endpoint 接收它们的中间状态或输出。
- §5 表中已有的 telemetry endpoint 与 GitHub Releases API 的 payload **不**因 v3.0.0 而扩展字段；遥测 payload 的硬正则自检（§5「强约束」）会拒绝任何包含 entity_id / 因子值 / setpoint 数值 / GP 超参的 payload。
- 即便用户主动开启 `telemetry_enabled = true`，BAO 的 GP 状态、CAE 的 effect / CI、PP 的桶 key、EMST 的命中率均**不**进入遥测 payload（与 R14.2「不可还原个体」红线一致）。

### 用户的删除路径

与 §4「数据保留期限」一致，用户可随时删除 v3.0.0 新增文件以重置算法栈：

- `rm /data/bao_model.pickle` —— 下次启动 BAO 退化为「无观测」冷启动，仅用 PP 推荐。
- `rm /data/causal_factors.jsonl` —— CAE 历史归因清零；新一晚 session 后会重新累积。
- `rm /data/predictor_audit.jsonl` —— EMST 的命中率统计清零，不影响推理本身。
- 关闭 `population_prior_enabled` —— BAO 立即停用 PP 影响，等价于 v2.x 默认行为。

> 一句话总结：**v3.0.0 算法栈不引入任何新出站网络请求；4 个模块全部本地推理 + 本地持久化。**

## 4. 数据保留期限

- 默认情况下**无限期保留**：偏好学习希望尽量长的历史以便季节性建模（指数衰减
  半衰期 14 天，但旧数据仍参与极小权重计算）。
- 用户可随时手动删除 `/data/user_preferences.json` 与 `/data/web_ui_overrides.json`：
  - 通过 HA Add-on 配置页面的 "重置偏好" 按钮（v2.1.0+）；或
  - 通过 SSH/Terminal add-on 直接删除文件后重启本 Add-on。
- 卸载本 Add-on 时，HA Supervisor 会清除整个 `/data/` volume；维护者无法访问。

## 5. 第三方传输：默认零外发

| 出口 | 默认 | 触发条件 | 数据内容 | 频率 |
|---|---|---|---|---|
| 项目自有 telemetry endpoint | **关闭（opt-in）** | 用户在 Web UI 显式打开 `telemetry_enabled` | `{install_id, version, ha_version, arch, locale, days_since_install, active_last_24h}` | 24 小时 1 次 |
| GitHub Releases API（`/repos/.../releases/latest`） | **开启** | `upgrade_notifications_enabled = true`（默认） | 匿名 `GET`，`User-Agent: sleep-classifier/<version>`，无 install_id、无 token | 24 小时 1 次 |
| Sentry / Glitchtip（可选 extras） | **关闭** | 用户主动 `pip install sleep-classifier[telemetry]` 并打开开关 | 异常堆栈，移除 `entity_id` / `token` / `username` 等敏感字段 | 触发时一次性 |
| 其它任何域名 | — | 永不触发 | — | — |

**强约束**：遥测 payload 在序列化前会做正则自检，若包含任何形如
`^sensor\.`、`^climate\.`、`^light\.`、`^binary_sensor\.` 的字符串即抛运行时
异常并放弃发送。这是源代码层面的硬保证，不依赖维护者的自律。

## 6. 可选匿名遥测 — 内容与撤回方式

> 本节是 [Requirement 6](.kiro/specs/commercial-readiness-v2.1.0/requirements.md)
> 与 [`MEDICAL_DISCLAIMER.md`](MEDICAL_DISCLAIMER.md) 之外的「opt-in 范围」唯一权威说明。

### 6.1 我们会收集什么

仅以下七个字段：

```json
{
  "install_id":            "<uuid4>",
  "version":               "2.1.0",
  "ha_version":            "2024.10.4",
  "arch":                  "aarch64",
  "locale":                "zh-cn",
  "days_since_install":    42,
  "active_last_24h":       true
}
```

### 6.2 我们不会收集什么

- 任何 entity ID（`sensor.*` / `climate.*` / `light.*` / `binary_sensor.*`）。
- 任何环境数值（温度、湿度、亮度、风扇档位）。
- HA 长效访问令牌、Supervisor token、用户姓名、邮箱、IP 地址。
- 偏好学习产物（中位数、k-NN 邻居池）。
- 睡眠分期具体序列、session 时长、唤醒决策。

### 6.3 用途

聚合统计：版本分布、活跃安装数、地区/架构占比，用于决定下一版本优先修哪些
问题。**不会**用于个性化广告、用户画像、商业转售。

### 6.4 如何撤回

1. 打开 Web UI（HA Sidebar → Sleep Classifier）。
2. 把「Anonymous telemetry」开关切到 OFF。
3. 后台 task 在 ≤ 30 秒内停止下一次定时任务，并删除 `/data/install_id.uuid`。
4. 之后可随时再次打开；每次重新打开都会生成新的 `install_id`，与历史无任何关联。

撤回是**幂等的**：连续 N 次切到 OFF 与单次切到 OFF 的最终系统状态完全相同。

## 7. 用户权利（GDPR Article 15–21）

由于所有数据都在用户自己的 HA 主机上，用户即数据控制者，因此可以随时：

- **访问 / 导出**：直接读取 `/data/*.json` 文件即可。
- **更正**：编辑对应 JSON 文件后重启本 Add-on。
- **删除（被遗忘权）**：删除 `/data/*.json` 或卸载本 Add-on。
- **限制处理**：把 `dry_run` 切回 `true`，本 Add-on 仅观察不下发设定点。
- **数据可携带**：导出的 JSON 即标准格式，可直接迁移到其它 HA 实例。
- **反对自动决策**：关闭 Web UI 中的「Apply learned setpoints」开关。

## 8. 第三方依赖与数据流

运行时唯一外部依赖是 [`aiohttp`](https://docs.aiohttp.org/)（HTTP/WebSocket
客户端）。可选 extras 为 `sentry-sdk`，仅在用户主动安装并打开崩溃上报时启用。

本 Add-on 不内嵌任何 Google Analytics、Facebook Pixel、广告 SDK 或商业分析
工具。所有 npm/pip 依赖清单见 [`requirements-runtime.txt`](requirements-runtime.txt)
与 [`pyproject.toml`](pyproject.toml)。

## 9. 儿童与敏感人群

本 Add-on 的睡眠分析依赖用户已有的睡眠分期硬件，不主动采集任何生物识别原始
信号。不建议 16 周岁以下儿童使用本 Add-on 调节睡眠环境；若家长选择使用，
应自行评估硬件供应商的隐私声明。健康相关声明详见
[`MEDICAL_DISCLAIMER.md`](MEDICAL_DISCLAIMER.md)。

## 10. 隐私声明的更新

本声明会随版本演进更新，重大变更（新增数据出口、收集字段扩展）将在
[`CHANGELOG.md`](CHANGELOG.md) 中以 `[Privacy]` 前缀标记，并在 Web UI 顶部
banner 提示用户重新确认。

## 11. 投诉与申诉

若你认为本 Add-on 的实际行为与本声明不符，请：

1. 通过 [`SECURITY.md`](SECURITY.md) 流程发送私下披露邮件（适用于安全相关问题）；
2. 或在 GitHub 仓库 [Issues](https://github.com/LiangyuLu-lly/HA-sleep/issues)
   开一个 `[Privacy]` 标签的 issue（不要附带任何个人数据）；
3. 欧盟用户也可向所在国家的数据保护监管机构投诉。
