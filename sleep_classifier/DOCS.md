# Sleep Classifier — Home Assistant Add-on 用户手册

**这个 Add-on 做什么**：从你自己的睡眠历史中学习最佳卧室环境（温度、湿度、亮度、风扇），然后在整夜各个睡眠阶段自动调节，让你每晚都睡在"最好的那几晚"的条件里。

唯一必需的输入是一个 HA 中已有的睡眠阶段实体——小米手环、Apple Watch、Fitbit、sleep_as_android、毫米波雷达等都可以。Add-on 不需要专用硬件，也不运行本地模型。

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
- `apnea_index` — 呼吸暂停趋势

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

---

## 更多信息

- 项目仓库：<https://github.com/LiangyuLu-lly/HA-sleep>
- 安装指南：[INSTALL.md](https://github.com/LiangyuLu-lly/HA-sleep/blob/main/INSTALL.md)
- 常见问题：[docs/FAQ.md](https://github.com/LiangyuLu-lly/HA-sleep/blob/main/docs/FAQ.md)
- 开发者文档：`docs/` 目录
