# ROADMAP

> 本文档登记 Sleep Classifier 的发布路线图。结构如下：
>
> 1. **Maintenance Checklist** — 仅能在 GitHub UI / 平台后台手工完成、
>    CI 无法守护的发版前核对项（来自 task 2.1）。
> 2. **v2.1.0 — Commercial readiness（current）** — 本期实施的 15 条
>    requirements 索引，每行链接回 [`requirements.md`](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md)
>    的对应章节。
> 3. **v2.2.0+（deferred）** — 已识别但本期不实施的两条产品架构级改动
>    （Device ecosystem expansion / Multi-resident & multi-room），含技术
>    起点与迁移路径，以便 v2.2.0 spec 启动时能直接承接（来自 task 2.6）。
> 4. **Commercial roadmap (post-v2.1.0)** — 至少 3 个变现方向与对现有
>    MIT 功能不会被分层到付费版的承诺（来自 task 2.6）。

## Maintenance Checklist（手工步骤）

以下条目无法在 CI 或代码层自动化，必须由仓库维护者在 GitHub UI / 平台后台
手工完成。每次发版前（或新维护者接手时）请逐项核对：

- [ ] **GitHub 仓库 topics**：在仓库 Settings → General → Topics 中
      至少配置 `home-assistant`、`addon`、`sleep-tracking`、`smart-home`
      四个 topic（Requirement 1.4）。CI 没有权限写 topics，只能在文档里
      登记。
- [ ] **Add-on store 渲染验收**：在 HA OS 测试实例（amd64 + Pi 4B）上
      验证 add-on detail 页 `icon.png` / `logo.png` 不再是 Supervisor
      默认占位图（Requirement 1.3）。
- [ ] **Lovelace 4-view 截图**：发版前重拍一张真实 dashboard 截图
      （≥ 1200 px 宽）覆盖 `assets/screenshots/dashboard-tonight.png`，
      替换占位 mockup。
- [ ] **GitHub Sponsors 启用**：在仓库 Settings → Sponsorship 中启用
      Sponsors 并核对 `.github/FUNDING.yml` 与 README badge 链接的 owner
      一致（Requirements 10.1, 10.3）。
- [ ] **i18n 抽样验收**：在 HA UI 切换 `zh-cn` / `en` / `fr` 三种语言
      验证 add-on 配置页文案回退正确（Requirements 2.2, 2.3）。
- [ ] **Release 验收**：推 `v2.1.0` 试运行 tag → 验证 `release.yml`
      创建 GitHub Release 且 release body 内容来自 CHANGELOG（Requirement 4.4）。

---

## v2.1.0 — Commercial readiness（current）

v2.1.0 把 v2.0.3「能装上 / 不会崩」的工程基线推进到「拿得到第一批用户 / 留得住装上的用户 / 走得通商业化路径」的商业化基线。完整需求与 acceptance criteria 见
[`requirements.md`](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md)，对应的实施设计见
[`design.md`](../.kiro/specs/commercial-readiness-v2.1.0/design.md)。本期共覆盖 15 条 requirements：

| # | 主题 | 链接 |
|---|---|---|
| 1 | Branding Assets — add-on store 图标 / logo / 截图墙 / topics | [Requirement 1](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-1-branding-assets--让用户在-add-on-store-看到一眼能记住的图标) |
| 2 | Internationalization — 至少中英双语，消除「英文配置 + 中文文档」的体验割裂 | [Requirement 2](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-2-internationalization--至少中英双语消除英文配置--中文文档的体验割裂) |
| 3 | Hardware Recommendation Page — 解除「必须先有睡眠阶段实体」的最大商业化阻断 | [Requirement 3](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-3-hardware-recommendation-page--解除必须先有睡眠阶段实体的最大商业化阻断) |
| 4 | Release Engineering — CI 跑测试 + 自动构建验证 + 版本号单一来源 | [Requirement 4](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-4-release-engineering--ci-跑测试--自动构建验证--版本号单一来源) |
| 5 | Legal Document Set — GDPR / 健康数据合规与漏洞披露通道 | [Requirement 5](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-5-legal-document-set--gdpr健康数据合规与漏洞披露通道) |
| 6 | Opt-in Anonymous Telemetry — 默认关闭、可一键撤回的匿名遥测 | [Requirement 6](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-6-opt-in-anonymous-telemetry--让维护者看到用户在哪些版本哪些时区遇到问题) |
| 7 | Onboarding Wizard — Web UI 首启自动扫描 + 一键绑定 | [Requirement 7](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-7-onboarding-wizard--web-ui-首启时自动扫描--一键绑定) |
| 8 | One-Click Lovelace Dashboard Importer | [Requirement 8](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-8-one-click-lovelace-dashboard-importer) |
| 9 | Upgrade Notifier — Web UI 与 HA notification 中弹出新版本提示 | [Requirement 9](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-9-upgrade-notifier--用户能在-web-ui-看到有新版本可用) |
| 10 | Monetization Path — README 增加赞助与未来变现路径 | [Requirement 10](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-10-monetization-path--readme-增加赞助与未来变现路径) |
| 11 | User Evidence — 加 testimonial 与 30 天真实案例图 | [Requirement 11](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-11-user-evidence--加-testimonial-与-30-天真实案例图) |
| 12 | Device Ecosystem Beyond HA — 明确 v2.2.0+ ROADMAP（本期 deferred） | [Requirement 12](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-12-device-ecosystem-beyond-ha--明确-v220-roadmap本期-deferred) |
| 13 | Multi-Resident / Multi-Room — 明确 v2.2.0+ ROADMAP（本期 deferred） | [Requirement 13](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-13-multi-resident--multi-room--明确-v220-roadmap本期-deferred) |
| 14 | Medical Advisor Placeholder Section | [Requirement 14](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-14-medical-advisor-placeholder-section) |
| 15 | Single Case Study — 30 天数据 blog 发 Reddit / HN | [Requirement 15](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-15-single-case-study--30-天数据-blog-发-reddit--hn) |

> 实施进度跟踪：见同 spec 下的 [`tasks.md`](../.kiro/specs/commercial-readiness-v2.1.0/tasks.md)。
> 本期严格遵守 PR1–PR6 不变量（测试套件 ≥ 92% 覆盖、20 个 sensor 契约不变、`/data/*.json`
> 向后兼容、运行时镜像不引入除 `aiohttp` 之外的新硬依赖、`tini -g` SIGTERM 链路保留、
> `config.yaml` 新字段全部 optional），完整定义见 requirements.md「Preservation Requirements」章节。

---

## v2.2.0+（deferred）

下列两条产品架构级改动在 v2.1.0 内**不实施**，但在本期 ROADMAP 中显式承接，避免在 v2.2.0
spec 启动时遗漏需求（Requirement 12.5 / 13.5）。

### Device ecosystem expansion

> 关联：[Requirement 12](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-12-device-ecosystem-beyond-ha--明确-v220-roadmap本期-deferred)

v2.0.3 / v2.1.0 仅支持 Home Assistant 一种生态。本节列出至少 3 个 v2.2.0+ 可行的扩展方向，
均处于「需求承接」阶段，未启动具体设计：

- **Matter sleep tracker integration**：随着 Matter 1.4+ 引入「sleep tracking cluster」与
  Apple / Google / Samsung 生态的渐进采纳，理论上可让本 add-on 直接订阅 Matter 设备而不
  必经 HA 中继。可行性问题：需要新增 Matter controller 依赖，会突破 PR4「运行时镜像不引入
  新硬依赖」的约束，估计要拆成独立 Python 包发布，再让 HA add-on 通过 import 复用。
- **SmartThings webhook bridge**：SmartThings 提供基于 webhook 的事件推送（含部分睡眠相关
  能力），可在不引入 SmartThings SDK 的前提下做一个独立 bridge 进程，把 SmartThings 事件
  转换为本 add-on 既有的 `SleepStage` 数据结构。无需改动学习器核心。
- **Apple Health export**：Apple Health 不开放 cloud API，但用户可手动从 iPhone 「健康」app
  导出 XML / CSV。v2.2.0+ 可提供一个一次性导入工具（脚本，不是 add-on），把历史睡眠分期与
  心率回填到 `preference_learner` 的 session 历史，作为冷启动加速手段。

**技术起点（重要承诺）**：本 add-on 的核心学习器（[`src/preference_learner.py`](../src/preference_learner.py)、
[`src/sleep_quality_score.py`](../src/sleep_quality_score.py)、[`src/sleep_debt.py`](../src/sleep_debt.py)）
按 steering 规则被设计为**纯 Python 模块、不做 I/O、不依赖 aiohttp / HA**。任何想把
Sleep Classifier 接入 HA 之外生态的贡献者，都可以把这几个模块从 add-on 中**原样抽出**复用，
不必复刻一遍学习算法。这是 v2.2.0+ device ecosystem 工作明确的、可执行的技术起点。

### Multi-resident / multi-room

> 关联：[Requirement 13](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-13-multi-resident--multi-room--明确-v220-roadmap本期-deferred)

v2.1.0 仍假设「单户 + 单房间 + 单一 wake_window」。v2.2.0+ 计划放开此假设。当前已识别的
技术挑战如下：

1. **每用户独立的 preference 文件**：当前 [`/data/user_preferences.json`](../src/preference_learner.py)
   是单文件 / 单租户结构。v2.2.0+ 需要拆成 `/data/preferences/{user_id}.json`，并在
   `PreferenceLearner` 内部按 user_id 维护多份 in-memory state。
2. **设备槽位的 per-room 分组**：当前 `web_ui_overrides.json` 把灯、空调、加湿器、风扇绑成
   一组。多房间场景下，每个房间需要一组独立绑定，且 `SmartEnvironmentController` 必须能
   并行下发到多组槽位（同事件循环里多 task，不允许阻塞主循环）。
3. **不同 wake_window 的协同控制**：夫妻不同体感、孩子另一房间需要分别决定唤醒时刻；
   `smart_wake.py` 的「单一最佳唤醒时刻」算法要扩展为「per-resident wake windows + 共享
   设备的冲突调解」。
4. **Lovelace dashboard 多租户呈现**：[`sleep_classifier/lovelace_template.py`](../sleep_classifier/lovelace_template.py)
   的 4-view 模板假设单一用户。多用户需要新增 Tenant Switcher view。

**`/data/user_preferences.json` 迁移路径（保持向后兼容）**：

- v2.2.0+ 启动时检测旧版 single-tenant 文件存在 → 把内容包装成
  `{"users": {"default": <旧 JSON>}}` 写入新结构（仍走 `src._io_utils.atomic_write_json`），
  原文件保留为备份。
- 旧版用户在升级后**默认表现为「default 单用户」模式**，与 v2.1.0 行为完全一致；只有显式
  在 Web UI 创建第二个 user profile 才进入多用户模式。
- 迁移代码必须可重入：连续启动 N 次，文件结构稳定不变（与 PR3 持久化向后兼容契约同源）。
- 迁移失败（磁盘已满 / 权限异常）时回滚到旧结构，不阻塞启动；以 WARNING 记录由用户在下次
  启动重试。

---

## Commercial roadmap (post-v2.1.0)

> 关联：[Requirement 10](../.kiro/specs/commercial-readiness-v2.1.0/requirements.md#requirement-10-monetization-path--readme-增加赞助与未来变现路径)

v2.1.0 仅在 README / `.github/FUNDING.yml` / 本节中铺设变现入口；**不实施任何付费功能**。
下列至少 3 个变现方向均属于「post-v2.1.0 探索」，仅当社区规模与志愿维护能力到达瓶颈时启动：

1. **托管服务（Managed hosting）**：给不愿自己维护 HA 实例 + Add-on 的用户提供包月托管
   服务（数据仍归用户所有，可一键导出后取消订阅）。这是一个**额外**的 SaaS 增量服务，
   并非现有 add-on 的「付费版」。
2. **付费技术支持（Paid support / SLA）**：给企业 / 重度自托管用户提供工单制响应、
   优先级 issue triage、定制 Lovelace 仪表板等服务。社区版仍按「best-effort」原则在
   GitHub Issues 处理。
3. **推荐硬件套件（Affiliate hardware bundles）**：与 [`docs/HARDWARE.md`](./HARDWARE.md)
   联动，向需要打包采购毫米波雷达 + 智能插座 + 加湿器的用户提供经过验证的硬件套件
   affiliate 链接。affiliate 披露已在 README 与 HARDWARE.md 顶部声明，符合 FTC 与中国
   《广告法》要求（Requirement 3.4 / Requirement 10.6）。

### 不会发生的事 — MIT 功能永远不会被移到付费版

**项目对所有 v2.0.3 / v2.1.0 用户的明确承诺**：

> 现有 MIT License 下交付的全部功能（包括但不限于 preference learner、smart environment
> controller、smart wake、sleep quality score、sleep debt、Lovelace 4-view dashboard、
> Web UI、Add-on 容器化部署、telemetry / upgrade notifier 这两个本期新增模块），
> **永远不会**被移除、关闭、或迁移到付费版本。任何未来的付费方向**只能是增量服务**
> （hosting、support、hardware affiliate），不能是现有功能的「专业版」「企业版」分层。

这个承诺与 README 顶部的 sponsor 段、`.github/FUNDING.yml` 一同构成 v2.1.0 商业化路径的
社区契约（Requirement 10.5）。
