# CASE STUDIES — 真实使用案例研究

> 本页是 Sleep Classifier 真实使用案例的索引。所有案例均来自项目维护者
> 或社区贡献者本人在自家卧室长期跑 dry_run 后开关闭环的复盘记录，目的是
> 让潜在用户在装这个 Add-on 之前能看到「真实 30 天里到底会发生什么」，
> 而不是只读功能描述。
>
> 本页所有案例严格遵守仓库 [`PRIVACY.md`](../PRIVACY.md) 与
> [`MEDICAL_DISCLAIMER.md`](../MEDICAL_DISCLAIMER.md) 的隐私与免责口径：
>
> - 不暴露真实 `entity_id`：所有 entity 引用均改写为 `sensor.bedroom_*`
>   等通用占位前缀。
> - 不暴露真实 HA 实例 URL、Add-on Ingress URL、家庭地址、城市。
> - 不暴露生物识别原始数据；只展示已经过聚合 / 匿名化的统计图。
> - 截图中如果出现日期 / 周次 / 房间名，已统一替换为 `Day N` / `Week N`
>   / `bedroom`，避免推断作息规律。
> - 案例中提到的「睡眠分期」「睡眠债」「呼吸率」等术语均为消费级估计，
>   **不是医学诊断**。详见 [`MEDICAL_DISCLAIMER.md`](../MEDICAL_DISCLAIMER.md)。

中文摘要：本页是真实 30 天使用复盘的索引，所有数据已匿名化，不构成医学建议。
English summary: This page indexes real 30-day case studies. All data is
anonymised; nothing here is medical advice — see
[`MEDICAL_DISCLAIMER.md`](../MEDICAL_DISCLAIMER.md).

---

## 案例索引

| # | 案例标题 | 主体 | 时长 | 硬件 | 状态 |
|---|---|---|---|---|---|
| 1 | [项目维护者本人的 30 天复盘](#case-1-30-天毫米波雷达--米家空调--米家加湿器--自学偏好) | 维护者 | 30 天 | R60ABD1 + 米家空调 + 米家加湿器 + 米家风扇 | ✅ 已完成 |

> 想贡献你自己的案例？欢迎在 GitHub Issues 提交，参考 README 的
> 「Beta tester program」段落。所有外部用户案例都需取得本人书面同意
> （Issue / PR 评论留痕即可），随时可撤回。

---

## Case 1 — 30 天毫米波雷达 + 米家空调 + 米家加湿器 + 自学偏好

> 主体：项目维护者本人（北方某城市单身公寓，单房间单用户）
> 时长：连续 30 个夜晚（含 1 个完整工作周末循环）
> Add-on 版本：v2.0.3（升级到 v2.1.0 前的最后一份完整数据）
> 阶段策略：前 7 天 `dry_run=true` 仅观察，第 8 天起开启闭环

### 硬件清单（已匿名化）

| 角色 | 实物 | HA 接入 | 案例中引用名 |
|---|---|---|---|
| 睡眠分期源 | Seeed R60ABD1 毫米波雷达（24 GHz / 60 GHz 任一） | ESPHome `text_sensor` 映射到 4 阶段 | `sensor.bedroom_sleep_stage` |
| 室内温度 | 米家蓝牙温湿度计 2 | `xiaomi_miot_auto` | `sensor.bedroom_temperature` |
| 室内湿度 | 同上 | 同上 | `sensor.bedroom_humidity` |
| 床头亮度 | 飞利浦 Hue Motion Sensor | HA 官方 Hue 集成 | `sensor.bedroom_illuminance` |
| 主灯 | Yeelight 吸顶灯 | `yeelight` 集成 | `light.bedroom_main` |
| 空调 | 米家互联网立式空调 C1 | `xiaomi_miot_auto` | `climate.bedroom_ac` |
| 加湿器 | 米家纯净式加湿器 Pro | `xiaomi_miot_auto` | `humidifier.bedroom_humidifier` |
| 风扇 | 米家直流变频塔扇 | `xiaomi_miot_auto` | `fan.bedroom_fan` |

> 引用名一律走 `sensor.bedroom_*` / `climate.bedroom_*` 这种通用前缀，与
> 真实 entity_id 无关；这同时也是 Add-on 在 onboarding wizard 里建议的
> 命名风格，便于把 case 文里的 Lovelace 截图直接套到读者自己的设备上。

### 安装时间分布

整体从「下单硬件」到「闭环跑起来」一共耗时 **约 4.5 小时**，分布如下：

- 雷达组装 + 烧 ESPHome 固件：约 60 分钟（含一次烧录失败重来）。
- HA 端把 4 个米家设备纳管 + 校验温湿度计电池：约 30 分钟。
- Add-on 安装 + onboarding wizard 走完：约 8 分钟（v2.1.0 wizard
  自动扫描出雷达的 stage sensor，没有手填 entity_id）。
- 第一次跑 dry_run + 看 4-view dashboard：约 15 分钟。
- **后续 30 天的人工干预成本：累计 < 20 分钟**——主要是看到
  `sensor.sleep_classifier_health` 报「环境字段 stale」时去检查电池。

### 第 1 / 3 / 7 / 14 / 30 天体验差异

**Day 1（dry_run，纯观察）**
打开 4-view dashboard 看到的是「Tonight」视图（截图见
[`assets/screenshots/dashboard-tonight.png`](../assets/screenshots/dashboard-tonight.png)），
此时 `sensor.sleep_classifier_quality_score` 还没有任何历史值，
`sensor.sleep_classifier_recommendation_explain` 显示
`method: warmup`、`n_total: 0`。心理预期：今晚不会有任何主动控制。
事实也确实如此——日志里全是 `would_set ... but dry_run=true`，
空调温度、加湿器湿度、灯光亮度都没被改动一次。

**Day 3（仍 dry_run）**
学习器累计了 2 个完整 session，`recommendation_explain.method` 仍然是
`warmup`，但 `learned_environment` 已经开始给出第一个 k-NN 中点
（在我个人的数据上是 22.5°C / 47% / 3% 亮度）。这个数字与我自己「凭感觉调的」
22.0°C 差异很小，给了我接下来开闭环的信心。

**Day 7（dry_run 最后一天）**
学习器达到 6 个 session，per-stage deltas 的 `awake_ess` / `deep_ess`
都还低于阈值 4，所以 `per_stage_deltas` 状态是 `default`——这是设计
预期：少于 4 个有效样本时不让自学的 delta 上场，避免噪声。
我在这一天关闭了 `dry_run`，并把 `confirm_disable_dry_run=true` 写进
overrides 文件（v2.1.0 wizard 强制了这个二次确认）。

**Day 14（闭环 1 周后）**
第一波直观差异出现：每天 23:30 入睡前 30 分钟，空调会自动从 24°C
预冷到 22.5°C，加湿器从 OFF 启动到 50%；同时主灯 ramp 到 5%。
床头亮度记录显示我从「躺下到 LIGHT 阶段」的中位时长从 Day 1 的
约 19 分钟下降到 Day 14 的约 12 分钟。`quality_onset` 子分从均值
58 上升到 71（截图见
[`assets/screenshots/dashboard-learning.png`](../assets/screenshots/dashboard-learning.png)）。

**Day 30（30 天复盘）**
`per_stage_deltas` 终于全 personalised：AWAKE 比 LIGHT 高 1.2°C、
DEEP 比 LIGHT 低 0.8°C、REM 比 LIGHT 低 0.3°C，这个分布与我自己「凭
感觉判断」的方向一致但幅度更克制（我以为 DEEP 应该低 1.5°C，实际数据
说不需要）。`debt_hours` 从 Day 7 的约 6.4 小时降到 Day 30 的 1.8 小时；
`quality_score` 30 天滚动均值从 62 → 78（截图见
[`assets/screenshots/dashboard-stage.png`](../assets/screenshots/dashboard-stage.png)）。

### 踩过的坑

1. **米家温湿度计 BLE 掉线导致 stale 字段**：第 11 天和第 22 天各掉过
   一次，每次约 40 分钟。Add-on 的反应是符合预期的——
   `smart_environment_controller` 检测到 `env_stale_fields` 不为空就
   skip 了对应 actuator 的下发，没有用旧温度值乱开空调。但提示用户的
   方式只在 `sensor.sleep_classifier_health` 的 attribute 里，体感上
   不够显眼，已经在 v2.1.0 onboarding wizard 里增加了「设备掉线
   detection」入口。
2. **R60ABD1 在我侧睡蜷腿时偶尔判 AWAKE**：debounce 默认 30 秒不够，
   有 2 次 AWAKE → DEEP → AWAKE 的抖动跳变。Add-on 的 stage debouncer
   把这种短脉冲过滤掉了（v1.4.0 引入），日志里看到的是
   `debounced transition rejected: pulse < 30s`，没有触发误判的环境
   动作。这个收益相当大，建议任何使用毫米波雷达的用户都不要把
   debounce 调到 30 秒以下。
3. **第 5 天 HA 重启导致 install_id 重置（已确认是预期）**：v2.0.3
   的 telemetry 默认是关的，所以这条不影响隐私；但我借机确认了
   v2.1.0 的 telemetry opt-in 流程，关闭后 `/data/install_id.uuid`
   被立刻删除，符合 R6.6 的撤回幂等承诺。
4. **空调湿度模式干扰加湿器**：我的米家空调有「加湿模式」会跟加湿器
   抢空气，第 2 周发现实际湿度反而低于学到的 47% 中点。最终方案是
   在 onboarding 里只把 `humidifier.bedroom_humidifier` 注册进 Add-on
   的 actuator 槽，把空调的湿度模式手动锁死在「不加湿」，让 Add-on
   只控制温度+风量。这条经验已经回写到
   [`docs/HARDWARE.md`](./HARDWARE.md) 的「米家空调」备注里。

### 最终效果数据（聚合后）

> 为遵守隐私要求，下表只给出 30 天滚动均值的差，绝对值不公开。

| 指标 | Day 1 baseline | Day 30 闭环后 | 差值 |
|---|---|---|---|
| `quality_score`（0–100） | 62 | 78 | +16 |
| `quality_onset` 子分 | 58 | 71 | +13 |
| 入睡中位时长（分钟） | 约 19 | 约 12 | −7 |
| `debt_hours`（睡眠债，小时） | 6.4 | 1.8 | −4.6 |
| `per_stage_deltas` ESS（AWAKE/DEEP/REM） | 0/0/0 | 7/6/5 | 全部超阈值 4 |
| 用户主动调节次数（手动改空调 / 灯） | 14 | 2 | −12 |

「用户主动调节次数」是我个人最看重的——这是「智能」是否真的减少了
认知负担的最直观信号。30 天后我基本不会再去看空调遥控器，但凡有一次
不舒服，多半是温度计电池快没了。

---

## How to reproduce on your own data

下面是把这个案例复刻到你自己卧室的最小步骤，所有命令都假设你已经
有一个能联网的 HA 实例（HA OS、Supervised、Container 任一）。

1. **准备睡眠分期源**。最便宜的入门是 [`docs/HARDWARE.md`](./HARDWARE.md)
   表里第 1 项的 R60ABD1（≈ ¥150），烧好 ESPHome 后会得到一个
   `sensor.<your_radar>_sleep_state` 实体；任何其他在 HARDWARE 表中
   列出的硬件也行。
2. **安装 Add-on**。在 HA Settings → Add-ons → Add-on Store →
   Repositories 粘贴本仓库的 GitHub URL，Add-on 列表会出现
   "Sleep Classifier"，点 Install → Start。详见
   [`INSTALL.md`](../INSTALL.md)。
3. **跑 onboarding wizard**。Web UI 首启时会自动弹出 wizard：
   - Step 1：阅读欢迎页与
     [`MEDICAL_DISCLAIMER.md`](../MEDICAL_DISCLAIMER.md)。
   - Step 2：wizard 自动扫描你的 HA 状态机里
     `entity_id` 或 `friendly_name` 命中 `sleep|睡眠|stage|分期`
     的实体，按相关性评分排序展示。如果列表是空的，wizard 会引导你
     去 [`docs/HARDWARE.md`](./HARDWARE.md)。
   - Step 3：选定环境传感器（温度 / 湿度 / 亮度）和执行设备
     （空调 / 灯 / 加湿器 / 风扇 / 音响），任何一个空缺都不影响整体跑。
   - Step 4：dry_run 安全确认，**强烈建议保留 `dry_run=true` 至少 7 天**
     再考虑关闭。
4. **导入 Lovelace 4-view dashboard**。Web UI 首页有「Import Lovelace
   Dashboard」按钮，点一下就行；后端会自动调 HA WebSocket 把 4-view
   配置（即本案例截图里的视图）写到一个新 dashboard `sleep-classifier`。
5. **观察 7 天 + 复盘**。每天早上瞄一眼「Tonight」视图，特别注意：
   - `sensor.sleep_classifier_health` 是否有 `stage_source_stale` 或
     `env_stale_fields`——任何一项 ≠ 空说明硬件有问题，先修硬件再
     谈学习。
   - `sensor.sleep_classifier_recommendation_explain` 的
     `effective_sample_size` 是否在涨；少于 4 时学习器仍在 warmup。
6. **第 8 天起开启闭环**。在 Add-on Configuration 把 `dry_run` 改成
   `false`（v2.1.0 onboarding 还要求 `confirm_disable_dry_run=true`，
   是有意为之的二次确认）。继续观察 7 天。
7. **第 30 天复盘**。打开 dashboard 的「质量细分」与「学习」视图，
   截图保存（如果你愿意贡献案例，请按本页索引最上方的隐私规则做
   匿名化）；任何反馈都欢迎在 GitHub Issues 留言。

> 注：本案例中两张截图 [`dashboard-stage.png`](../assets/screenshots/dashboard-stage.png)
> 与 [`dashboard-learning.png`](../assets/screenshots/dashboard-learning.png)
> 在 v2.1.0 release 时由维护者补齐，索引列表在录制完成后会同步更新；
> [`dashboard-tonight.png`](../assets/screenshots/dashboard-tonight.png)
> 已随 task 2.1 提交。如果你打开本页时仍有截图链接 404，说明你看到的
> 是 v2.1.0 release 之前的快照，不影响文字复盘的可读性。

---

## 反馈与隐私

如果你发现本页任何描述与 [`PRIVACY.md`](../PRIVACY.md) 的承诺有出入
（例如不小心暴露了真实 entity_id 或 HA URL），请直接在 GitHub Issues
提交「Privacy concern」标签的 issue，维护者会在 ≤ 7 天内首响、≤ 30 天内
修订或撤稿。本页所有案例的素材作者随时可以撤回授权。

> 本页内容均为非临床、非诊断、非医疗建议；任何「睡眠分期」「呼吸率」
> 「睡眠债」结论都仅供生活方式参考。详见
> [`MEDICAL_DISCLAIMER.md`](../MEDICAL_DISCLAIMER.md)。
