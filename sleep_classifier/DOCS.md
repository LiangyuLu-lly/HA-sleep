# Sleep Classifier — Home Assistant Add-on 用户手册

> **当前版本：v2.0.0** · [查看更新日志](#更新日志) · [GitHub](https://github.com/LiangyuLu-lly/HA-sleep)

**这个 Add-on 做什么**：从你自己的睡眠历史中学习最佳卧室环境（温度、湿度、亮度、风扇），然后在整夜各个睡眠阶段自动调节，让你每晚都睡在"最好的那几晚"的条件里。

唯一必需的输入是一个 HA 中已有的睡眠阶段实体——小米手环、Apple Watch、Fitbit、sleep_as_android、毫米波雷达等都可以。Add-on 不需要专用硬件，也不运行本地模型。

> ⚠️ **医疗免责**：本项目不提供任何医疗诊断或治疗建议。详见 [MEDICAL_DISCLAIMER.md](../MEDICAL_DISCLAIMER.md)。

---

## 🆕 v2.0.0 亮点（相对 v1.6.0）

一年内从 v1.6.0 → v2.0.0 的 8 次迭代，把一个 MVP 打磨成了可商业化的产品：

- **20 个 HA sensor**（从 v1.6.0 的 14 个扩展）—— 包括健康状态、质量 4 子分、呼吸暂停趋势
- **设备能力感知**（v1.6.2）—— 不会给不支持温控的空调发 `set_temperature`
- **真实世界稳健性**（v1.6.3+）—— 手环掉线不会锁死卧室、睡眠 session 独立统计、空调饱和时不再重复发指令
- **Zigbee2MQTT / Matter 支持**（最新）—— 即使 `supported_features=0` 也能从属性推断设备能力
- **午睡过滤**（v1.8.0）—— < 60 分钟的 session 不污染学习模型
- **数据滚动备份**（v1.8.0）—— `user_preferences.json` 损坏时自动从 `.bak` 恢复
- **用户反馈通道**（v1.9.0）—— `input_number` 直接覆盖学到的温度
- **白噪音一键降音量**（v2.0.0）—— `input_button` 触发音量降 30%
- **双语日志**（v2.0.0）—— 关键消息中英双语（`LANG=zh` 自动切换）
- **4-view Lovelace 仪表板**（v2.0.0）—— 覆盖全部 20 个 sensor
- **诊断导出命令**（v2.0.0）—— `docker exec ... python scripts/diagnostic_export.py`

完整更新日志见底部 [更新日志](#更新日志) 章节。

---

## 快速开始（5 分钟上手）

1. **安装 Add-on**：设置 → 加载项 → 加载项商店 → ⋮ → 仓库 → 粘贴 `https://github.com/LiangyuLu-lly/HA-sleep`
2. **安装 "Sleep Classifier"**：点击安装，等待构建完成（Pi 4B 约 1-3 分钟）
3. **配置**：打开"配置"标签页，填写 `sleep_stage_source`（你的睡眠阶段实体 ID）
4. **启动**：保持 `dry_run: true`，点击"启动"
5. **验证**：查看日志确认连接成功，Lovelace 上应出现 `sensor.sleep_classifier_stage` 等实体
6. **正式启用**：观察 1-2 晚确认无误后，将 `dry_run` 改为 `false`

> 💡 不知道实体 ID？打开 **开发者工具 → 状态**，搜索你手环的名字，复制对应的 entity_id。或者点击 Add-on 详情页的 **打开 Web UI**，从下拉列表中选择。

---

## 工作原理

### 三大支柱

1. **分析**：每个完整 session 记录为 `(环境参数, 阶段分布, 质量分, 时间戳)`。滚动历史（默认 60 晚）喂给偏好学习器，按 `质量 × 指数衰减` 加权——近期好夜占主导，远古异常值自然淡出。
2. **四尺度适应**：夜内按阶段调（AWAKE 暖亮、DEEP 冷暗）；周内按工作日/周末分桶；月内靠指数衰减跟踪季节；当晚用 k-NN 匹配最相似的历史夜晚。
3. **安全执行**：每次设备更新都经过死区过滤（不为 0.1°C 动空调）、冷却间隔（防止来回切换）、安全范围钳位（温度 16-28°C）。

### 四个时间尺度详解

#### 1. 夜内 — 按睡眠阶段

控制器对每个阶段施加临床偏移：

| 阶段 | 温度偏移 | 湿度偏移 | 亮度偏移 | 风扇偏移 |
|---|---|---|---|---|
| AWAKE | +2.0°C | −5% | +32% | +5% |
| LIGHT | 0（基准） | 0 | 0 | 0 |
| DEEP | −2.0°C | 0 | −8% | −5% |
| REM | −1.5°C | 0 | −8% | −5% |

v1.5.0 起这些偏移也会从你自己的数据中学习（需要足够样本量）。

#### 2. 周内 — 工作日 vs 周末

学习器按"醒来是哪天"分桶：周五晚→周六醒来 = 周末。每个桶独立计算推荐入睡时间。

#### 3. 月内 — 指数衰减

每个 session 的权重 = `(0.1 + quality/100) × 2^(-天数/半衰期)`，默认半衰期 14 天。效果：季节变化在约 3 周内自然融入推荐。

#### 4. 当晚 — k-NN 上下文匹配

根据今晚的入睡时间 + 室温，在历史中找最相似的 k 个夜晚（默认 k=5），用加权中位数给出推荐。冬天的推荐不会被夏天的数据拖偏。

---

## 算法可解释性（v3.0.0 起）

v3.0.0 在原有「学习中点 + k-NN + 指数衰减」之上叠加了 4 个本地算法模块（BAO / CAE / PP / EMST），全部默认开启、可独立关闭、加载或运行异常 ≥ 3 次自动停用并回退到 v2.1.0 行为。所有数学性质均仅在「假设 X 成立时」收敛，**不构成临床疗效声明**（详见 [MEDICAL_DISCLAIMER.md](../MEDICAL_DISCLAIMER.md)）。

### 端到端决策链路

```text
        ┌─────────────────────────────────────────────────────────────┐
        │  PSG 公开数据（MESA + SHHS, ≈ 8497 受试者夜，离线训练）       │
        │  scripts/train_population_prior.py  →  population_prior.pickle │
        └──────────────────────────────┬──────────────────────────────┘
                                       │ 出厂内置 (≤ 8 MB, SHA-256 校验)
                                       ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  PP — Population Prior                                             │
  │  按 (age_band, sex, chronotype, season) 4 维分桶（最多 180 桶）      │
  │  返回桶均值 + 方差 + n_samples + fallback_level ∈ {0,1,2,3}          │
  └──────────────────────────────┬─────────────────────────────────────┘
                                 │ prior_weight(N) = max(0.1, exp(-N/14))
                                 ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  BAO — Bayesian Optimizer (GP + Thompson Sampling)                 │
  │  历史 session  +  prior  →  GP 后验（RBF kernel）                    │
  │  N < 5 → prior-only；N ≥ 5 → explore (σ 最大) 或 exploit (TS 抽样)   │
  │  wind-down / 维度锁定 → 强制 exploit                                │
  │  决策种子 = hash(install_id + ISO-date)（可重复）                    │
  └──────────────────────────────┬─────────────────────────────────────┘
                                 │ recommend(stage, ctx) → setpoint
                                 ▼
  ┌────────────────────────────────────────────────────────────────────┐
  │  SmartEnvironmentController                                        │
  │  per-stage deltas + per-actuator anticipation + deadband + cooldown │
  │  dry_run=true 时仅打印不下发（PR1 守护）                             │
  └──────────────────────────────┬─────────────────────────────────────┘
                                 │
                ┌────────────────┼────────────────┐
                ▼                ▼                ▼
        HA service call   stage transition   完整 session 结束
                          ▲                          │
                          │                          ▼
                ┌─────────┴──────────┐   ┌──────────────────────────┐
                │  EMST (端侧 ONNX)   │   │  CAE — Causal Attribution │
                │  (HRV, motion,     │   │  do-calculus + Heckman    │
                │   breathing) 5 min │   │   + bootstrap 95% CI       │
                │  → P(下个 stage)    │   │  6 因子 DAG，反事实推断    │
                │  ≤ 50 ms 推理       │   │  ≥ 30 晚才输出归因解释     │
                │  confidence ≥ 0.6  │   │  CI 跨 0 → 标注「显著性弱」 │
                │  → 提前 60 秒下发   │   └──────────────────────────┘
                │   (climate /        │
                │    humidifier 类    │   归因结果 → sensor.attribution
                │    慢响应设备)       │   命中率   → sensor.predictor_hit_rate_7d
                └────────────────────┘
```

### 各模块的数学保证（带「在 X 假设下成立」前缀）

- **BAO**：在 RBF kernel + 加性高斯噪声假设下成立，GP-UCB 类策略累积 regret 满足次线性界 `O(√(T · γ_T))`；`prior_weight(0)=1.0`、`prior_weight(14) ≤ 0.1`，关于观测数 N 单调不增。
- **CAE**：在 6 因子 DAG 结构正确指定 + 观测 IID 假设下成立，合成 null 因子上 bootstrap 95% CI 覆盖率 ≥ 92%；任一因子非缺失观测 < 5 时点估计强制为 `NaN`、`is_significant=False`。
- **PP**：在桶内 `n_samples ≥ 50` 的假设下成立，`lookup` 兜底始终命中大样本桶或退到根桶（`fallback_level=3`）；桶均值生理范围被锁定在 `temperature ∈ [16, 28] °C`、`humidity ∈ [30, 70] %`、`brightness ∈ [0, 50] %`。
- **EMST**：在设备响应时间 ≥ 60 秒的假设下成立——**60 秒提前控制对快速响应设备（LED / 风扇 / 智能灯）无明显收益，仅对慢响应设备（空调 / 电热毯 / 地暖）有意义**；7 晚命中率 < 70% 持续 3 晚自动停用，`sensor.sleep_classifier_predictor_status` 置 `auto_disabled`。

### 可观测性

4 个新模块各自暴露独立的 health / status / metric sensor（`sensor.sleep_classifier_optimizer_health` / `_attribution` / `_prior_status` / `_predictor_hit_rate_7d` 等，共 14 个 v3 sensor），停用时 state = `disabled`，Lovelace 渲染保持一致。Web UI 顶部 sticky 区显示 4 个模块的健康状态（绿 / 琥珀 / 红 / disabled）。

> 💡 4 个 feature flag 全部关闭时，主流程**不 import** 对应模块，行为字节级等价于 v2.1.0；运行时镜像体积仍按 ~80 MB 计（依赖装在镜像里）。

---

## 配置详解

打开 Add-on 的 **配置** 标签页。表单分为几组，最低要求只需填 `sleep_stage_source`。

### 基础行为

| 选项 | 默认值 | 说明 |
|---|---|---|
| `area` | （空） | 设备发现过滤区域，留空扫描所有房间 |
| `infer_interval` | `30` | 控制决策间隔（秒） |
| `session_interval` | `1800` | 学习器存盘间隔（秒） |
| `dry_run` | `true` | 只规划不执行。**首晚务必保持 true** |
| `exploration_rate` | `0.1` | 探索噪声幅度 |
| `min_seconds_between_actions` | `120` | 同一设备两次操作的最小间隔 |
| `deadband_temperature_c` | `0.5` | 温度死区 |
| `deadband_humidity_pct` | `5` | 湿度死区 |
| `deadband_brightness_pct` | `10` | 亮度死区 |
| `wind_down_minutes` | `30` | 入睡前预冷提前量（分钟），0 = 禁用 |
| `min_stage_dwell_seconds` | `60` | 阶段去抖动（秒），0 = 禁用 |
| `log_level` | `info` | 日志级别 |

### 实体绑定

| 选项 | 示例 | 说明 |
|---|---|---|
| `sleep_stage_source` | `sensor.mi_band_8_pro_sleep_stage` | **必填**。睡眠阶段实体 |
| `temperature_source` | `sensor.bedroom_temperature` | 温度传感器 |
| `humidity_source` | `sensor.bedroom_humidity` | 湿度传感器 |
| `illuminance_source` | `sensor.bedroom_illuminance` | 光照传感器 |
| `light_targets` | `[light.bedroom_main]` | 灯光目标（列表） |
| `climate_target` | `climate.bedroom_ac` | 空调实体 |
| `fan_target` | `fan.bedroom_fan` | 风扇实体 |
| `whitenoise_target` | `media_player.bedroom_speaker` | 白噪音播放器 |
| `volume_feedback_entity` | `input_number.whitenoise_volume` | 白噪音音量反馈（实时调节） |
| `feedback_entity` | `input_number.sleep_rating` | 晨起主观评分 |

### 智能唤醒

| 选项 | 示例 | 说明 |
|---|---|---|
| `wake_window_start` | `07:00` | 唤醒窗口开始 |
| `wake_window_end` | `07:30` | 唤醒窗口结束 |
| `wake_light_targets` | `[light.bedroom_main]` | 唤醒灯光渐亮目标 |

### 呼吸暂停趋势（v1.7.0，可选）

> ⚠️ 呼吸暂停趋势仅供参考，**不构成医疗诊断**。如有疑虑请咨询专业医师。详见 [MEDICAL_DISCLAIMER.md](../MEDICAL_DISCLAIMER.md)。

| 选项 | 说明 |
|---|---|
| `apnea_breathing_rate_source` | 呼吸频率传感器，留空禁用 |
| `apnea_consent_entity` | 同意开关（`input_boolean`），必须手动开启 |
| `apnea_calibration_nights` | 校准夜数（默认 7） |

---

## 发布的实体（20 个）

所有实体前缀为 `sensor.sleep_classifier_*`。

### 状态与诊断（6 个）

- `stage` — 当前阶段（AWAKE / LIGHT / DEEP / REM）
- `confidence` — 置信度 0-100
- `quality_score` — session 质量分 0-100
- `session_duration` — session 持续秒数
- `last_action` — 最近一次设备操作摘要
- `health` — 聚合健康状态（healthy / degraded / error）

### 自然睡眠套件（4 个）

- `debt_hours` — 睡眠债（小时）
- `recommended_bedtime` — 今晚推荐入睡时间
- `wake_decision` — 智能唤醒决策
- `soundscape` — 当前音景

### 偏好学习面板（5 个）

- `learned_bedtime_workday` — 工作日入睡时间
- `learned_bedtime_weekend` — 周末入睡时间
- `learned_environment` — 最佳环境参数
- `recommendation_explain` — 推荐理由
- `per_stage_deltas` — 各阶段学习偏移状态

### 质量子分 + 呼吸（5 个）

- `quality_architecture` — 睡眠结构分
- `quality_efficiency` — 睡眠效率分
- `quality_fragmentation` — 碎片化分
- `quality_onset` — 入睡速度分
- `apnea_index` — 呼吸暂停趋势（仅供参考，非医疗诊断；详见 [MEDICAL_DISCLAIMER.md](../MEDICAL_DISCLAIMER.md)）

---

## 启动后会发生什么

1. Add-on 解析 `sleep_stage_source`，通过 WebSocket 订阅 `state_changed` 事件
2. 当阶段从 AWAKE 转为非 AWAKE 并持续 ≥5 分钟，一个新 session 开始
3. 当连续 AWAKE ≥10 分钟，session 结束：计算质量分，记录到历史
4. 历史达到 3 个 session 后，学习面板开始输出真实推荐
5. 每 `infer_interval` 秒，控制器比较推荐值与当前环境，通过死区+冷却后下发设定点

WebSocket 断线后自动指数退避重连（1s → 2s → … → 5min 上限）。

---

## 数据持久化

所有数据存储在 `/data`（Supervisor 持久卷），重启/升级/重装都不会丢失：

- `/data/user_preferences.json` — session 历史 + 学习推荐
- `/data/web_ui_overrides.json` — Web UI 设置的实体绑定
- `/data/effective_config.json` — 实际加载的合并配置
- `/data/apnea_baseline.json` — 呼吸暂停基线（如启用）

---

## 故障排除

### "No sleep stage source found"

`sleep_stage_source` 为空且自动发现未匹配到任何实体。解决：在配置中填写正确的实体 ID。

### 推荐入睡时间显示 "unknown"

对应桶（工作日或周末）的 session 数量不足（需要 ≥3 个）。查看实体属性中的 `n_workday` / `n_weekend`。

### 日志报 HTTP 401

Supervisor token 过期。重启 Add-on 即可刷新。

### 设备不响应

1. 确认 `dry_run = false`
2. 确认设备支持对应功能（`supported_features`）
3. 检查 `last_action` 的 `skipped_by_capability` 属性

### 阶段源 stale 警告

手环/雷达长时间未报告新状态。系统自动暂停控制，恢复后自动继续。

---

## 卸载

在 Add-on 信息页点击"卸载"。学习数据保留在 `/data/` 中以便重装恢复；如需彻底清除，先通过 SSH Add-on 删除 `/data/user_preferences.json`。

---

## 常见问题 FAQ

### 1. sensor 显示 "Entity not available"

等待 30 秒让 Add-on 完成初始化。如果持续显示，重启 Add-on（设置 → 加载项 → Sleep Classifier → 重启）。首次启动时 Add-on 需要 2 秒延迟等待 HA REST API 就绪。

### 2. quality_score 一直是 0

质量分需要至少 10 个 epoch（约 5 分钟，取决于 `infer_interval`）的 stage 数据才能计算。确认你的睡眠阶段源在夜间持续报告数据，且 session 已正式开始（非 AWAKE 持续 ≥5 分钟）。

### 3. learned_environment 显示 "—"

学习器需要至少 3 个完整 session 才能输出推荐。每个 session 需要满足 `min_session_minutes`（默认 60 分钟）才会被记录。耐心等待 3 个完整夜晚。

### 4. AC 没反应（空调不动）

按以下顺序排查：
1. 确认 `dry_run` 已设为 `false`
2. 检查 `sensor.sleep_classifier_last_action` 的 `skipped_by_capability` 属性——如果非空，说明你的空调实体不支持 `set_temperature` 服务
3. 确认空调实体支持 `climate.set_temperature`（开发者工具 → 服务 → 手动调用测试）
4. 检查 `skipped_unavailable`——空调可能处于离线状态

### 5. 手环断了灯一直暗

这是正常的安全行为。当阶段源超过阈值时间未更新，stale guard 会暂停控制循环，防止基于过时数据做出错误决策。检查 `sensor.sleep_classifier_health` 的 `stage_source_stale` 属性确认状态。手环重新连接后系统自动恢复。

### 6. 白噪音太响

两种解决方案：
1. 在配置中调低 `whitenoise_volume_scale`（默认 1.0，设为 0.5 即减半）
2. 配置 `whitenoise_volume_feedback_entity` 指向一个 `input_button` 实体，按一次降低 30%

### 7. 推荐温度太冷

配置 `temperature_override_entity` 指向一个 `input_number` 实体（如 `input_number.bedroom_target_temp`）。在 HA 中设置你期望的温度后，控制器会使用该值覆盖学习器的推荐。

### 8. apnea sensor 一直显示 "pending_consent"

呼吸暂停趋势功能需要显式同意才会启动。你需要：
1. 在 HA 中创建 `input_boolean.sleep_classifier_apnea_consent`
2. 将其打开（toggle on）
3. 等待 7 个校准夜（`apnea_calibration_nights` 默认值）后才会显示 green/amber/red

### 9. session 太短没记录

午睡或短于 `min_session_minutes`（默认 60 分钟）的 session 会被过滤，不进入偏好学习器。这是为了防止短午睡污染夜间推荐模型。如需调整，修改 `session_lifecycle.min_session_minutes` 配置。

### 10. 数据丢了

检查 `/data/user_preferences.json.bak` 文件是否存在。v1.8.0+ 每次写入前会自动备份到 `.bak` 文件。如果主文件损坏，Add-on 会自动尝试从 `.bak` 恢复。如需手动恢复：
```bash
docker exec -it addon_local_sleep_classifier \
  cp /data/user_preferences.json.bak /data/user_preferences.json
```

### 11. 如何导出诊断信息

运行以下命令导出系统诊断 JSON（不含任何 token 或密码）：
```bash
docker exec -it addon_local_sleep_classifier \
  python scripts/diagnostic_export.py
```
输出包含 session 数量、最后 session 时间、学习器状态、配置摘要等信息，方便提交 issue 时附带。

### 12. 若 add-on 从不启动

请先确认 HA Core 本身健康（`startup: application` 依赖 Core 先就绪）。如果 Core 处于错误状态或尚未完成初始化，本 Add-on 不会被 Supervisor 启动。排查步骤：
1. 在 Settings → System → Logs 中确认 Core 无持续报错
2. 尝试重启 HA Core（`ha core restart`），等待 Core 完全就绪后观察 Add-on 是否自动启动
3. 如果 Core 健康但 Add-on 仍不启动，检查 Add-on 日志中是否有其他错误信息

---

## Limitations（已知限制）

- **单户单房间假设**：当前版本（v2.1.0）假设单一用户 + 单一卧室 + 单一唤醒窗口。多居住者或多房间场景尚未支持，计划在 v2.2.0+ 实现。详见 [ROADMAP — Multi-resident / multi-room](../docs/ROADMAP.md#multi-resident--multi-room)。
- **非医疗设备**：所有健康相关指标（呼吸暂停趋势等）仅供参考，不可用于医学诊断或治疗决策。详见 [MEDICAL_DISCLAIMER.md](../MEDICAL_DISCLAIMER.md)。

---

## Telemetry（遥测）

本 Add-on 的遥测功能：

- **默认关闭**：安装后不会自动上报任何数据。
- **不上报 `entity_id`**：即使开启遥测，也不会发送你的设备实体标识符。
- **可一键撤回**：随时在 Add-on 配置中关闭遥测，之前上报的数据可请求删除。

隐私政策全文及数据处理细节请参阅 [PRIVACY.md](../PRIVACY.md)。

---

## 更多信息

- 项目仓库：<https://github.com/LiangyuLu-lly/HA-sleep>
- 安装指南：[INSTALL.md](https://github.com/LiangyuLu-lly/HA-sleep/blob/main/INSTALL.md)
- 常见问题：[docs/FAQ.md](https://github.com/LiangyuLu-lly/HA-sleep/blob/main/docs/FAQ.md)
- 开发者文档：`docs/` 目录


---

## 更新日志

> 下面只列出**用户能感受到的**变化；完整工程日志见 GitHub 上的 [`CHANGELOG.md`](https://github.com/LiangyuLu-lly/HA-sleep/blob/main/CHANGELOG.md)。

### v2.0.0 — 2026-05-14（当前版本）

**商业化打磨版**。面向"第一次装上就能用得好"的体验全面升级。

- ✨ **4-view Lovelace 仪表板**：从单卡片升级到完整的 4 页面仪表板（今晚/学习/健康/质量细分），覆盖全部 20 个 sensor。YAML 在 GitHub 的 `examples/lovelace_dashboard.yaml`
- 🌐 **双语日志**：关键用户可见消息中英双语（系统 LANG 包含 zh 时显示中文）
- 📋 **FAQ 常见问题**：新增 11 条常见问题解答（本文档下方）
- 🔧 **诊断导出**：`docker exec addon_xxx_sleep_classifier python scripts/diagnostic_export.py` 可导出配置 + 学习状态（不含 token）
- 🎵 **白噪音一键降音量**：创建 `input_button.sleep_classifier_too_loud`，按一下音量降 30%
- ✅ **min_ha_version 声明**：强制 HA ≥ 2024.1.0，防止老版本安装失败
- 🧪 **501 个测试**（从 483 增加）

### v1.9.0 — 2026-05-13

**用户反馈 + 边界加固**。

- 🎛️ **用户温度覆盖**：配置 `temperature_override_entity`（`input_number`），用户调一下就能手动覆盖学到的温度
- 📊 **首晚诊断报告**：第一次完整 session 结束时，Add-on 日志会输出一份详细报告（时长、阶段分布、质量分、环境快照）
- 🕐 **时区稳健性**：夏令时切换日不再崩溃
- ⏱️ **HA 重启兼容**：Add-on 启动时延迟 2 秒再发首次数据，避免抢跑 HA REST API
- 🧪 **压力测试**：1000 个事件/秒不丢数据、7 天合成数据验证学习收敛

### v1.8.0 — 2026-05-13

**可观测性 + 数据保护**。

- 🚥 **健康状态 sensor** (`sensor.sleep_classifier_health`)：一眼看系统是 `healthy` / `degraded` / `error`，属性暴露每项子状态
- 📈 **质量 4 子分 sensor**：`quality_architecture` / `_efficiency` / `_fragmentation` / `_onset`，分开看才知道哪里扣分
- 💤 **午睡过滤**：< 60 分钟的 session 不会被记录到学习器，避免污染夜间模型
- 💾 **滚动备份**：`user_preferences.json` 每次保存前复制到 `.bak`，主文件损坏时自动从备份恢复
- ✅ **端到端测试**：合成 8 小时完整夜晚测试整条链路

### v1.7.0 — 2026-05-13

**呼吸暂停趋势监测**（opt-in，带医疗免责）。

- 🫁 **apnea_index sensor**：配置毫米波雷达的呼吸率实体 + `input_boolean` 同意开关，7 晚校准后开始显示 `green` / `amber` / `red` 趋势
- ⚠️ **完全不公开 AHI 数值**：只显示等级，防止误读为医疗诊断
- 🔒 **撤销同意立即清除所有基线数据**

### v1.6.4 — 2026-05-13

**环境稳健性**。

- 🌡️ **传感器过期守卫**：15 分钟没报告的传感器视为无效，不会用旧数据做决策
- 🛑 **饱和设备抑制**：空调已经全力制冷但房间温度不降时，不再每 2 分钟重复发同一个设定点
- 🔄 **stage 切换自动重置抑制**

### v1.6.3 — 2026-05-13

**Session 生命周期重构**。

- ⏰ **Session 边界检测**：非 AWAKE 持续 5 分钟 = session 开始；AWAKE 持续 10 分钟 = session 结束
- 👤 **每个 session 独立统计**：不再把两晚的数据混在一起算 quality
- 🔁 **Session 结束后自动 rotate**：新 session_id、stage_counts 归零

### v1.6.2 — 2026-05-12

**设备能力感知**。

- 🧠 **capability gating**：启动时读每个绑定设备的 `supported_features`，不支持 `set_temperature` 的空调不会收到温度指令（避免 HA 假装执行成功但实际无动作的坑）
- 🔄 **优雅降级**：只能 on/off 的灯不会收到 `brightness_pct`；只有 preset_mode 的风扇自动转换 20%→`low` / 50%→`medium` / 80%→`high`

### v1.6.0 及更早

完整日志见 [`CHANGELOG.md`](https://github.com/LiangyuLu-lly/HA-sleep/blob/main/CHANGELOG.md)。主要里程碑：

- **v1.6.0**：`LearningPanelPublisher` 抽象 + WebSocket 断线重连 + 60 秒 bedtime 缓存
- **v1.5.0**：Per-stage deltas 也从用户数据学习（不仅学中点）
- **v1.4.0**：per-actuator 预测（AC 提前 15 分钟预冷）+ wind-down 预冷 + stage 抖动过滤
- **v1.3.0**：移除本地 CNN-BiLSTM 模型，改为订阅任意 HA 睡眠阶段实体
