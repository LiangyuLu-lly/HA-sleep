# HARDWARE — 推荐睡眠分期硬件清单

> Sleep Classifier 自 v1.3.0 起**不再训练或运行本地睡眠分期模型**，
> 它只订阅 HA 中已有的睡眠分期实体（state ∈ `{awake, light, deep, rem}`
> 或可映射到这四个阶段的字符串）并据此学习用户偏好、闭环调节卧室环境。
>
> **没有这样一个实体，Add-on 装上后 `sleep_stage_source` 会留空、`SleepStateSubscriber`
> 进入「等绑定」空闲态，不会下发任何指令。** 因此「先有睡眠分期硬件 / 软件」
> 是装这个 Add-on 的硬性前提。本页给出经过验证或社区报告可用的若干款硬件、
> 它们的 HA 接入路径与 entity_id 形态，便于你在试用 Add-on 前完成最小可用配置。

> 本页不是医疗器械推荐。所有硬件输出的「睡眠分期」均为消费级算法估计，
> 不构成临床诊断。详见 [`MEDICAL_DISCLAIMER.md`](../MEDICAL_DISCLAIMER.md)。

---

## Affiliate Disclosure（合规披露）

为遵守 [FTC Endorsement Guides](https://www.ftc.gov/business-guidance/resources/ftc-endorsement-guides-what-people-are-asking)
与 [中华人民共和国《广告法》第十四条](http://www.npc.gov.cn/zgrdw/npc/xinwen/2015-04/25/content_1934594.htm)
关于「广告应当具有可识别性，能够使消费者辨明其为广告」的要求，本页特此声明：

- **当前版本（v2.1.0）所有硬件链接均为非 affiliate 的中立链接**：
  指向官方店铺或公开的 HA 集成文档，本项目维护者**不**从这些链接获取任何
  分成、佣金或返点。
- **未来若启用 affiliate 计划**（已在 [`ROADMAP.md`](./ROADMAP.md) 的
  「Commercial roadmap」中登记），本页顶部将把每条带返点的链接以
  「Affiliate Link · 广告 / Ad」前缀显式标注，并在该硬件条目下方
  追加「本链接可能为本项目带来佣金，价格不变 / This link may earn the
  project a commission at no extra cost to you.」中英双语披露。
- **变现承诺**：affiliate 计划永远不会影响硬件**收录与排序**——本页排序
  按「维护者自测验证天数 → 社区报告样本量 → 价格升序」三档稳定排序，
  与是否带返点无关；任何被发现因 affiliate 偏袒的条目可由用户在 GitHub
  Issues 公开质询。
- **用户随时可比价**：每款硬件均给出官方型号、参考价区间与 HA 接入路径，
  你可以直接在京东 / 淘宝 / 闲鱼 / 海外渠道自行下单，本页不锁定特定渠道。

> 中文摘要：本页**当前不带任何返利链接**；若未来引入 affiliate，会在每条
> 链接前显式标注「广告 / Ad」并保持收录与排序中立。
>
> English summary: This page currently contains **no affiliate links**.
> If affiliate links are introduced in the future, each will be prefixed
> with `Ad` and accompanied by an FTC-style disclosure; ranking will
> remain neutral.

---

## 兼容性矩阵（Compatibility Matrix）

| # | 类别 | 型号 | 参考价区间 | HA 接入路径 | 典型 entity_id | 4 阶段支持度 | dry_run ≥ 7 天验证 |
|---|---|---|---|---|---|---|---|
| 1 | 毫米波雷达 | **Seeed Studio R60ABD1**（XIAO ESP32C6 + 60 GHz） | ¥150 – ¥220 / ≈ $25 – $35 | ESPHome（[官方组件 `seeed_mr60bha2`](https://esphome.io/components/sensor/seeed_mr60bha2.html)）+ `text_sensor` 把 presence/motion 映射到 stage | `sensor.bedroom_radar_sleep_state` | ✅ AWAKE / ✅ LIGHT / ⚠ DEEP / ⚠ REM（DEEP 与 REM 仅基于呼吸率 / 体动启发式区分，准确度低于可穿戴） | ✅ 维护者自测（30 天） |
| 2 | 毫米波雷达 | **Linptech ES1（24 GHz Aqara 系）** | ¥260 – ¥320 / ≈ $40 – $50 | Aqara HomeKit / Matter 桥；或经 [`xiaomi_miot_auto`](https://github.com/al-one/hass-xiaomi-miot) 接入 | `binary_sensor.linptech_es1_sleep_state` + 自建 `template sensor` 做 4 阶段映射 | ✅ AWAKE / ✅ LIGHT / ❌ DEEP / ❌ REM（仅给出 sleeping / awake 二元，需用户在 HA 内拼 template 把 LIGHT 推给 DEEP / REM 桶） | 🟡 社区报告（n=2，HA 论坛） |
| 3 | 智能手环 | **小米手环 8 / 9**（Mi Band 8 / 9） | ¥249 – ¥369 / ≈ $35 – $55 | [`xiaomi_miot_auto`](https://github.com/al-one/hass-xiaomi-miot) 或 [Mi Hub HACS 集成](https://github.com/PiotrMachowski/Home-Assistant-custom-components-Xiaomi-Cloud-Map-Extractor) 同账号同步；睡眠分期来自小米运动健康 App 云端 | `sensor.mi_band_9_sleep_stage` | ✅ AWAKE / ✅ LIGHT / ✅ DEEP / ✅ REM（完整 4 阶段，但每天仅在凌晨 ≈ 06:30 后批量回填，不是实时） | 🟡 社区报告（n=5，知乎 + GitHub Issues） |
| 4 | 智能手环 | **华为手环 8 / 9 · Huawei Band 8 / 9** | ¥299 – ¥449 / ≈ $45 – $65 | 经第三方 [`xha-huawei-health`](https://github.com/anonym4ik/xha-huawei-health) 桥接 Huawei Health → HA；TruSleep 2.0 输出 4 阶段 | `sensor.huawei_band_9_sleep_phase` | ✅ AWAKE / ✅ LIGHT / ✅ DEEP / ✅ REM（TruSleep 2.0 在 PSG 对照中达成约 87% 一致性，厂方公开数据） | 🟡 社区报告（n=3，主要为中文 IT 博客复刻） |
| 5 | 智能手表 | **Apple Watch SE / Series 9+ 经 HomePass 同步** | ¥1999 – ¥3499 / ≈ $250 – $500 | iOS 端用 [HomePass / Health Auto Export](https://www.healthexportapp.com/) 把 HealthKit 的 Sleep Stages 推到 HA REST sensor | `sensor.apple_watch_sleep_state`（值域：`awake / core / deep / rem` → 需 template 映射 `core → light`） | ✅ AWAKE / ✅ LIGHT（核心睡眠重命名）/ ✅ DEEP / ✅ REM | 🟡 社区报告（n=4，Reddit r/homeassistant） |
| 6 | 智能手表 | **Withings ScanWatch 2 / Sleep Tracking Mat** | ¥1999 – ¥2599 / ≈ $300 – $400 | HA 官方 [Withings 集成](https://www.home-assistant.io/integrations/withings/)（OAuth2，原生支持） | `sensor.withings_sleep_state`（值域：`awake / light / deep / rem`，与本 Add-on 直接同名） | ✅ AWAKE / ✅ LIGHT / ✅ DEEP / ✅ REM（Withings 是少数不需要任何 template 映射的硬件） | 🟡 社区报告（n=2，HA Community Forum） |

> **图例 / Legend**
> - ✅ 维护者自测 ≥ 7 天 dry_run，输出能正确驱动 `PreferenceLearner`。
> - 🟡 社区报告：来自 GitHub Issues / HA 论坛 / 知乎 / Reddit 的可复现案例，
>   未经维护者本人 7 天 dry_run 重复验证。
> - ⚠ 该阶段语义存在但准确度受限（雷达 / 算法本身限制）。
> - ❌ 该阶段不输出原生信号，需用户在 HA 内做 template / automation 映射。

---

## entity_id 命名约定

Add-on 通过 `web_ui_overrides.json` 的 `sleep_stage_source` 字段或
`config.yaml` 的 `sleep_stage_source` option 读一个 entity_id；该 entity 的
`state` 必须能被映射到 [`SleepStage`](../src/data_structures.py) 枚举的
四个值之一：`awake / light / deep / rem`（大小写不敏感、空白容忍）。

- 多数手环 / 手表集成的状态字段直接落在 `awake / light / deep / rem`，
  可零配置接入。
- 雷达类设备通常输出 `presence` + `breathing_rate` + `motion`，
  需要在 HA 中做 template 把这些原始信号合成出 4 阶段。本仓库的
  [`examples/`](../examples/) 目录会逐步收录这类 template。
- Apple HealthKit 的 `core` 阶段在临床上等价于 NREM-N1/N2，对应本 Add-on
  的 `LIGHT`；映射在 template 中一行搞定（见示例链接）。

> ⚠️ entity_id 区分大小写、不要用中文。HA 实体 ID 仅允许 `[a-z0-9_]`。

---

## 选购建议（Buying Guide）

按预算 / 体验由低到高排序，给出几条决策线索：

1. **预算 ¥150 以内、不想戴东西睡觉** → 选**毫米波雷达**（R60ABD1 + ESPHome）。
   优点：无感、覆盖伴侣 / 宠物的二人卧室；缺点：DEEP / REM 准确度受限，
   学习收敛慢于可穿戴。
2. **预算 ¥300 以内、能接受戴手环** → 选**小米手环 9** 或**华为手环 9**。
   完整 4 阶段、电池一周、`xiaomi_miot_auto` / `xha-huawei-health` 集成
   稳定，是目前性价比最高的入门方案。
3. **已有 Apple Watch / 不愿换生态** → **Apple Watch + HomePass**，
   一次性配置后免维护，但需要常年戴表睡觉（部分用户体验不佳）。
4. **追求最小折腾且预算充足** → **Withings ScanWatch / Sleep Tracking Mat**，
   HA 官方 OAuth 集成、4 阶段命名与本 Add-on 直接对齐、夜间无需充电。
5. **想凑集多源做交叉验证** → 雷达 + 手环并存，把雷达 entity 设为
   `sleep_stage_source`、手环数据走 `examples/` 中的辅助 template
   做趋势对照（这一组合在维护者自测 30 天数据中表现最稳）。

---

## 我没有以上任何硬件，能先「干跑」体验吗？

可以，但功能受限：

- **`dry_run = true`** 默认开启，Add-on 不会真的下发 HA service call，
  你可以先把 Add-on 装上、绑一个**任意**值域为 `awake/light/deep/rem` 的
  `input_select` 或 helper sensor 当成「假分期源」，观察 Web UI 与
  20 个 `sensor.sleep_classifier_*` 实体的输出。
- 但**没有真实分期数据，`PreferenceLearner` 不会学到任何有意义的中点**，
  Lovelace 看到的「学习到的环境参数」会一直是默认 fallback。

正式使用本 Add-on 之前，建议至少先入手上表中的 1 款硬件并完成 ≥ 7 天的
真实数据采集（保持 `dry_run=true`，观察推荐值是否合理），再切到
`dry_run=false` 让 Add-on 实际下发指令。

---

## 想把你的硬件加进这张表？

欢迎按以下流程提交：

1. 在你 HA 实例上以 `dry_run=true` 跑 ≥ 7 天，导出一份
   `/data/user_preferences.json` 与 1 张 Lovelace 4-view 截图（匿名化）。
2. 提 PR 修改本页，按现有表格 schema 添加一行；新增条目首次合入时
   标 🟡 社区报告。
3. 若维护者本人后续完成 ≥ 7 天 dry_run 复刻，会在下一版升级该条目为
   ✅ 维护者自测。

合规与隐私要求与 [`PRIVACY.md`](../PRIVACY.md) 一致：截图与日志中
**不得**暴露真实 `entity_id`、HA 实例 URL、家庭住址或生物识别原始数据。

---

## 参考链接

- HA 官方集成索引：<https://www.home-assistant.io/integrations/>
- ESPHome：<https://esphome.io/>
- xiaomi_miot_auto：<https://github.com/al-one/hass-xiaomi-miot>
- xha-huawei-health：<https://github.com/anonym4ik/xha-huawei-health>
- Withings 官方集成文档：<https://www.home-assistant.io/integrations/withings/>
- FTC Endorsement Guides：<https://www.ftc.gov/business-guidance/resources/ftc-endorsement-guides-what-people-are-asking>
- 中华人民共和国《广告法》：<http://www.npc.gov.cn/zgrdw/npc/xinwen/2015-04/25/content_1934594.htm>
