# Requirements Document

> Spec: commercial-readiness-v2.1.0

## Introduction

v2.0.3 已经修复了所有 P0/P1 启动与运行时缺陷，但项目仍停留在「个人 MVP」阶段：可以装上能用，但既拿不到第一批用户、也留不住装上的用户、更走不通商业化路径。最近一次审计列出了 **15 条 commercial readiness 差距**，分为「硬阻断（拿不到用户）」「软阻断（留不住用户）」「增量优化（变不了现）」三档。

本 spec 覆盖全部 15 条差距：

- 第 1–9 条进入 v2.1.0 实施范围（branding、i18n、CI/CD、legal、observability、onboarding、dashboard、release engineering、user evidence）。
- 第 10、11、14、15 条作为 README/文档/链接级别的轻量改动同样进入 v2.1.0。
- 第 12（设备生态过窄）和第 13（单户单房间假设）属于产品架构级别改动，明确 deferred 到 v2.2.0+，但本文档仍记录其问题陈述与 ROADMAP 链路，避免遗漏。

**核心约束**：v2.1.0 必须保持向后兼容 v2.0.3 的全部行为契约，特别是：
- 537 个测试 100% 通过、覆盖率 ≥ 92% 不下降。
- 现有 20 个 `sensor.sleep_classifier_*` 实体 ID 与语义不变。
- `/data/user_preferences.json`、`/data/web_ui_overrides.json`、`/data/effective_config.json` 文件格式保持读写兼容（迁移用 forward-only schema）。
- v2.0.3 的 Add-on 安装路径（HA Supervisor → Repositories → 粘贴 URL）仍然有效。
- 运行时镜像不引入除 `aiohttp` 之外的新硬依赖。

---

## Glossary

- **Add-on**: Home Assistant Supervisor 管理的容器化扩展，本项目即一个 add-on。
- **Add-on_Manifest**: `sleep_classifier/config.yaml`，由 HA Supervisor 解析以渲染配置 UI。
- **Branding_Asset**: 用户在 HA add-on store / GitHub README 中看到的视觉资产，包括 `icon.png`、`logo.png` 与 README 截图墙。
- **Bug_Condition**: 形式化的「commercial readiness 缺口」状态，本文档用 `C(X)` 记号表达「在条件 X 下此差距仍然成立」。
- **CI_Pipeline**: 由 `.github/workflows/*.yml` 定义的、push/PR/tag 时自动执行的检查与构建流程。
- **Dashboard_Importer**: Web UI 的「一键导入 Lovelace 仪表板」按钮，调用 HA REST API `/api/lovelace/dashboards` 创建预制 dashboard。
- **Hardware_Recommendation_Page**: `docs/HARDWARE.md`，列出推荐的睡眠分期硬件（如 Seeed R60ABD1 毫米波雷达），README 顶部链接到此页。
- **Legal_Document_Set**: `PRIVACY.md`、`SECURITY.md`、`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`MEDICAL_DISCLAIMER.md` 五份文档的总称。
- **Onboarding_Wizard**: Web UI 首次启动时弹出的引导流程，自动扫描候选实体并一键完成绑定。
- **Release_Engineering**: 版本号自动同步、CHANGELOG 自动生成、tag 触发 GitHub Release 的发布工程。
- **Telemetry_Reporter**: opt-in 的匿名遥测组件，向开发者上报 `install_count`、`active_count`、`version_distribution` 三类聚合指标。
- **Translations_Pack**: `sleep_classifier/translations/{en,zh-cn}.yaml`，HA Supervisor 渲染配置 UI 时按用户 HA 语言选择对应文件。
- **Upgrade_Notifier**: 检测到 GitHub 有新 release tag 时，在 Web UI 与 HA notification 中弹出升级提示的组件。
- **Version_Source_of_Truth**: 单一版本号来源（`pyproject.toml` 的 `[project] version`），其它三处（`setup.py`、`sleep_classifier/config.yaml`、可能的 `__version__`）由发布脚本自动同步。

---

## Bug Condition C(X) 的形式化定义（commercial readiness 视角）

虽然本 spec 是 feature 而非 bugfix，但「未达到商业化门槛」本身可被形式化为 v2.0.3 的状态不变量。下面用与 `post-v2.0.2-full-pipeline-audit/bugfix.md` 一致的 Pascal 风格定义「之前未改进」的状态，便于每条 user story 给出 preservation check。

```pascal
// X: 任意"v2.0.3 仓库 + 一个潜在新用户"的复合状态
// 包含：仓库内容、GitHub 元数据、用户的语言偏好、用户的硬件清单
TYPE CommercialReadinessState = RECORD
  repo_files: Set<Path>             // 仓库内存在的文件路径集合
  github_topics: Set<string>        // 仓库 GitHub topics
  has_demo_media: bool              // README 是否有 GIF / 截图墙
  user_locale: {en, zh-cn, other}   // 用户 HA UI 语言
  user_owns_sleep_stage_entity: bool // 用户 HA 中是否已有睡眠分期实体
  ci_workflows: Set<WorkflowName>   // .github/workflows/ 下的工作流
  legal_docs_present: Set<DocName>  // 仓库根存在的 legal 文档
  telemetry_consent: {opt_in, opt_out, undefined}
  onboarding_path: {manual_yaml, web_ui_form, wizard}
  dashboard_setup: {copy_paste_yaml, one_click}
  upgrade_notification: {none, in_app}
  version_sources: int              // 散落的版本号来源数量
  monetization_links: Set<string>   // README 中的赞助/付费链接
  testimonials: int                 // README 中的真实用户证据条数
END

FUNCTION isCommercialReadinessGap(X: CommercialReadinessState): bool
  // 任意一条不满足，C(X) 即成立。
  RETURN NOT all_of(
    G1_branding_assets_complete(X),
    G2_i18n_supports_at_least_en_zh_cn(X),
    G3_hardware_recommendation_page_exists(X),
    G4_release_engineering_automated(X),
    G5_legal_document_set_complete(X),
    G6_telemetry_optin_and_anonymous(X),
    G7_onboarding_wizard_minimizes_friction(X),
    G8_dashboard_one_click_import(X),
    G9_upgrade_notifier_active(X),
    G10_monetization_links_visible(X),
    G11_user_evidence_present(X),
    G12_v2_2_roadmap_acknowledges_device_ecosystem(X),
    G13_v2_2_roadmap_acknowledges_multi_room(X),
    G14_medical_advisor_section_exists(X),
    G15_case_study_blog_published(X)
  )
END FUNCTION
```

每条 user story 给出一个 X 使得 v2.0.3 状态下 C(X) 成立、v2.1.0 完成后 C(X) 不再成立。

---

## Requirements

### Requirement 1: Branding Assets — 让用户在 add-on store 看到一眼能记住的图标

**User Story:** 作为一名在 HA add-on store 浏览的潜在用户，我希望 Sleep Classifier 有清晰可识别的品牌资产（图标、logo、demo 截图、GitHub topics），以便我能在几十个候选 add-on 中快速选中并产生信任。

**Bug Condition X:** v2.0.3 状态下，`sleep_classifier/icon.png` 不存在、`sleep_classifier/logo.png` 不存在、GitHub 仓库 topics 集合为空、`README.md` 顶部没有 demo GIF 或 Lovelace 截图墙。HA add-on store 渲染本 add-on 时显示默认占位图标，仓库在 GitHub 搜索 `home-assistant addon sleep` 时排序靠后。

#### Acceptance Criteria

1. THE Add-on_Repository SHALL 在 `sleep_classifier/icon.png` 提供一张正方形 128×128 像素、PNG 格式、透明背景或与 HA UI 主色（`#03a9f4`）协调的图标文件。
2. THE Add-on_Repository SHALL 在 `sleep_classifier/logo.png` 提供一张矩形 250×100 像素、PNG 格式的横向 logo 文件。
3. WHEN HA Supervisor 加载 add-on detail 页面，THE Add-on_UI SHALL 显示步骤 1 与步骤 2 提供的资产，而不是 Supervisor 默认占位图。
4. THE Add-on_Repository SHALL 在 GitHub 仓库 topics 字段配置至少 `home-assistant`、`addon`、`sleep-tracking`、`smart-home` 四个 topic。
5. THE README.md SHALL 在「30 天你会看到什么」表格之前的位置插入至少 1 张 Lovelace 4-view dashboard 截图（PNG，宽度 ≥ 1200 px）和 1 段 demo GIF 或视频链接。
6. IF `sleep_classifier/icon.png` 或 `sleep_classifier/logo.png` 中**任意一个**资产的尺寸或格式不符合规范（即使另一个资产合规），THEN THE CI_Pipeline SHALL 在 `addon-lint` 任务中返回非零退出码并阻止合并；只有当全部 branding 资产均合规时方可放行。

#### Property-Based Correctness Properties

- **P1.1（尺寸不变量）**: 对仓库内任一被声明为 branding asset 的 PNG 文件 `f`，`PIL.Image.open(f).size` 必须落在该资产声明的尺寸约束内（icon=128×128、logo=250×100，公差 0）。
- **P1.2（路径不变量）**: HA Supervisor 文档要求图标路径必须是 `<addon_slug>/icon.png` 与 `<addon_slug>/logo.png`；该路径不得随 prepare.sh 重新生成而被复制到 `rootfs/` 之外。

---

### Requirement 2: Internationalization — 至少中英双语，消除「英文配置 + 中文文档」的体验割裂

**User Story:** 作为一名母语为中文的 HA 用户，我希望 add-on 的配置项 description 与错误提示能跟随我的 HA UI 语言切换为中文，以便我不需要在英文配置项与中文 DOCS.md 之间来回切换。

**Bug Condition X:** v2.0.3 状态下，`sleep_classifier/translations/` 目录不存在；HA Supervisor 渲染 add-on 配置页时直接使用 `config.yaml` 的 `name`、`description`、各 `options.*` 字段的字面英文值；`user_locale = zh-cn` 的用户看到的是英文配置 + 中文 DOCS.md 的混合界面。

#### Acceptance Criteria

1. THE Add-on_Repository SHALL 创建 `sleep_classifier/translations/` 目录，并至少提供 `en.yaml` 与 `zh-cn.yaml` 两个文件。
2. WHEN HA Supervisor 加载 add-on 配置页面且用户的 HA UI 语言为 `zh-cn`，THE Add-on_UI SHALL 渲染 `zh-cn.yaml` 中提供的 `name`、`description` 与所有 `configuration.*.name` / `configuration.*.description` 字段。
3. WHEN HA Supervisor 加载 add-on 配置页面且用户的 HA UI 语言不是 `zh-cn` 也不是 `en`，THE Add-on_UI SHALL 回退到 `en.yaml`；该回退规则只在 supervisor 配置页加载这一时机生效，不影响 add-on 运行时日志、Web UI 内的其它字符串。
4. THE Translations_Pack SHALL 覆盖 `config.yaml` 的 `options:` 块中所有用户可见的字段（截至 v2.0.3 共 ≥ 30 个 key），每个 key 都有对应的 `name` 与 `description` 翻译条目。
5. WHEN `config.yaml` 新增、删除或重命名一个 `options.*` key，THE CI_Pipeline SHALL 在 `addon-lint` 或新增的 `translations-coverage` job 中校验 `en.yaml` 与 `zh-cn.yaml` 的 key 集合与 `config.yaml` 严格一致；IF 缺漏，THEN job SHALL 返回非零退出码。
6. THE Translations_Pack SHALL 不翻译 HA 实体 ID、HA 服务名、Python 标识符、CLI 命令等技术性字符串（保持英文）。

#### Property-Based Correctness Properties

- **P2.1（覆盖率不变量）**: `set(keys(en.yaml.configuration))` ≡ `set(keys(zh-cn.yaml.configuration))` ≡ `set(keys(config.yaml.options))`，三者集合相等。
- **P2.2（回退安全性）**: 对任意 `locale ∈ {en, zh-cn, fr, de, ja, …}` 与任意 key `k`，渲染后字段 `f(locale, k)` 永不为空字符串：locale 缺失时回退到 en，en 缺失时回退到 `config.yaml` 的字面值。

---

### Requirement 3: Hardware Recommendation Page — 解除「必须先有睡眠阶段实体」的最大商业化阻断

**User Story:** 作为一名想试用 Sleep Classifier 但目前手上只有米家温湿度计、没有睡眠分期硬件的潜在用户，我希望 README 顶部能直接告诉我有哪些经过验证的硬件可以买，以便我能下单一个 R60ABD1（约 ¥150）就开始用，而不是装完才发现无法启动。

**Bug Condition X:** v2.0.3 状态下，`docs/HARDWARE.md` 不存在；README 没有「需要哪些硬件」一节；用户在不具备睡眠分期实体的情况下安装本 add-on 后，`sleep_stage_source` 留空导致 supervise 循环陷入「等用户绑定」状态（即 v2.0.3 P0 中已修复但仍需用户主动获取硬件的场景）。

#### Acceptance Criteria

1. THE Add-on_Repository SHALL 创建 `docs/HARDWARE.md` 文件，列出至少 3 类（毫米波雷达、智能手环、智能手表）共 ≥ 5 款经过验证可输出 HA 睡眠分期实体的硬件。
2. THE `docs/HARDWARE.md` SHALL 为每款硬件提供：型号名称、价格区间、HA 接入路径（原生集成 / ESPHome / 第三方 add-on）、典型 entity_id 命名示例、是否支持本 add-on 所需的 4 阶段（AWAKE/LIGHT/DEEP/REM）。
3. THE README.md SHALL 在顶部「30 天你会看到什么」表格之前增加一段「Hardware Required」section，链接到 `docs/HARDWARE.md`，并明确标注「No sleep-stage sensor? Start here.」（中英双语）。
4. WHERE 推荐硬件链接为 affiliate 链接，THE README.md SHALL 在 `docs/HARDWARE.md` 顶部明确披露「affiliate disclosure」段落，符合 FTC 与中国《广告法》要求。
5. WHEN 用户在 Web UI Onboarding_Wizard 中点击「我没有睡眠分期硬件」按钮，THE Web_UI SHALL 跳转或弹出指向 `docs/HARDWARE.md` 的链接（实现见 Requirement 7）。
6. THE `docs/HARDWARE.md` SHALL 在「兼容性矩阵」表格中明确说明哪款硬件在 dry_run 模式下经过 ≥ 7 天真实验证。

---

### Requirement 4: Release Engineering — CI 跑测试 + 自动构建验证 + 版本号单一来源

**User Story:** 作为本项目的维护者，我希望每次 push 与 tag 都触发完整的 CI 流水线（测试 + addon-lint + 镜像构建 + release 发布），以便我不会再像 v1.6.0 那样手工漏改 `setup.py` 的版本号导致 PyPI 与 add-on 版本漂移。

**Bug Condition X:** v2.0.3 状态下，`.github/workflows/test.yml` 已存在并跑测试与 addon-lint，但 `release.yml` 缺失；版本号在 `pyproject.toml`、`setup.py`、`sleep_classifier/config.yaml` 三处独立维护；推 tag 不会触发 GitHub Release，CHANGELOG.md 由人工编辑；未跑「在干净环境构建 add-on 镜像」的端到端验证。

#### Acceptance Criteria

1. THE `.github/workflows/` 目录 SHALL 至少包含三个工作流：`test.yml`（已有，保留并扩展 Python 3.10 矩阵项）、`addon-build.yml`（新增）、`release.yml`（新增）。
2. WHEN 一个 push 或 PR 命中 `main` 分支，THE CI_Pipeline SHALL 在所有支持的 Python 版本（3.10, 3.11, 3.12）上完整执行 `pytest tests/ --timeout=60`；IF 任一版本失败 OR 覆盖率 < 92%，THEN CI_Pipeline SHALL 在 job 结束后返回非零退出码并将整个 workflow 标记为失败（即「先跑完再失败」，不在首条失败用例上提前 abort 整个矩阵），以便维护者一次性看到所有版本的失败信息。
3. WHEN 一个 push 命中 `main` 分支，THE addon-build.yml 工作流 SHALL 在干净的 Ubuntu runner 上用 `docker buildx build --platform linux/arm64,linux/amd64 sleep_classifier/` 验证镜像可成功构建（不需要推送到 registry）。
4. WHEN 一个符合 `v[0-9]+.[0-9]+.[0-9]+` 格式的 git tag 被推送，THE release.yml 工作流 SHALL 自动：（a）从 CHANGELOG.md 中提取该版本段落作为 release body；（b）创建 GitHub Release；（c）附带 `sleep_classifier/` 打包后的 zip 作为 release asset。
5. THE Version_Source_of_Truth SHALL 是 `pyproject.toml` 的 `[project] version`；THE Add-on_Repository SHALL 提供 `scripts/sync_version.py`，由 release.yml 在 tag 时自动同步到 `setup.py`、`sleep_classifier/config.yaml`、`src/__init__.py`（如存在）。
6. IF 用户在 `pyproject.toml` 与 `sleep_classifier/config.yaml` 中维护了不一致的版本号且未运行 `sync_version.py`，THEN THE CI_Pipeline SHALL 在 `version-consistency` 步骤中返回非零退出码。
7. THE addon-build.yml 工作流 SHALL 与 `prepare.sh` 在 `rootfs/` 同步检查（已存在）联动：先跑 `prepare.sh`，再跑 buildx，确保 docker context 看到的是最新的 `src/`。

#### Property-Based Correctness Properties

- **P4.1（版本一致性不变量）**: 对 v2.1.0 之后的任意 commit `c`（落在 main 分支），从 `pyproject.toml`、`setup.py`、`sleep_classifier/config.yaml` 读出的版本号字符串严格相等。
- **P4.2（CI 单调性）**: 若 commit `c1` 是 `c2` 的祖先且 `c1` 上 CI 全绿，则不存在「`c2` 上 `c1` 已通过的测试反而失败」的情形（除非 `c2` 主动修改了对应测试）——通过启用 `pytest --strict-markers` 与 fail-fast=false 矩阵保证。

---

### Requirement 5: Legal Document Set — GDPR/健康数据合规与漏洞披露通道

**User Story:** 作为一名欧盟用户或安全研究人员，我希望仓库根目录有清晰的 PRIVACY.md、SECURITY.md、CONTRIBUTING.md、CODE_OF_CONDUCT.md 与医疗免责聚合页，以便我能确认本 add-on 处理我的睡眠数据是否合规、能找到漏洞披露邮箱、能理解贡献流程。

**Bug Condition X:** v2.0.3 状态下，仓库根目录的 `legal_docs_present` 集合为空集；任何包含「health data」「medical」字样的文档没有显著的免责声明聚合页；用户无法在仓库内找到漏洞披露邮箱；GDPR 第 13 条要求的「数据控制者身份」「处理目的」「保留期限」「用户权利」四项信息未公开。

#### Acceptance Criteria

1. THE Add-on_Repository SHALL 在仓库根目录提供 `PRIVACY.md`、`SECURITY.md`、`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`MEDICAL_DISCLAIMER.md` 五个文件，全部使用中文为主、附带英文摘要。
2. THE PRIVACY.md SHALL 至少声明：（a）add-on 处理的数据类型（睡眠分期、温湿度、睡眠质量评分等）；（b）数据存储位置（默认完全本地，`/data/*.json`）；（c）数据保留期限（默认无限期，用户可手动删除 `/data/`）；（d）默认不向任何第三方传输数据；（e）opt-in 遥测的具体内容与用户撤回方式（与 Requirement 6 一致）。
3. THE SECURITY.md SHALL 声明漏洞披露邮箱（项目所有者邮箱或 `security@<域名>`）、期望响应时间（≤ 7 天首次回复）、CVE 申请流程，并禁止在 GitHub Issues 公开报告未修复漏洞。
4. THE CONTRIBUTING.md SHALL 描述 PR 流程、commit message 约定、本地测试命令、`prepare.sh` 同步要求与 `pytest --cov` 覆盖率门槛。
5. THE CODE_OF_CONDUCT.md SHALL 采用 Contributor Covenant v2.1 模板，并填入项目所有者的执行联系方式。
6. THE MEDICAL_DISCLAIMER.md SHALL 聚合所有医疗免责声明，并被 `README.md`、`sleep_classifier/DOCS.md`、Web UI 首页、Onboarding_Wizard 第一步分别链接。
7. WHEN README 或 DOCS 中提到「sleep stage」「呼吸暂停」「睡眠债」等可能被误解为医学诊断的术语，THE 文档 SHALL 在距离该术语 ≤ 1 个段落的位置出现一条指向 MEDICAL_DISCLAIMER.md 的链接。

#### Property-Based Correctness Properties

- **P5.1（链接可达性不变量）**: 对 README、DOCS、Web UI 中所有出现的「medical / 医学 / 诊断 / diagnose」相关词，存在一条距离 ≤ 1 段落的有效链接指向 `MEDICAL_DISCLAIMER.md`；CI 用 markdown 链接检查器验证。
- **P5.2（漏洞披露通道单调性）**: 对仓库历史上任意一次 SECURITY.md 修改，披露邮箱字段不得为空字符串。

---

### Requirement 6: Opt-in Anonymous Telemetry — 让维护者看到用户在哪些版本、哪些时区遇到问题

**User Story:** 作为本项目的维护者，我希望能看到匿名的 `install_count`、`active_count`、`version_distribution` 与崩溃报告，以便决定下个版本优先修哪些问题；同时我必须确保用户清楚知道遥测的存在并能一键关闭。

**Bug Condition X:** v2.0.3 状态下，没有任何遥测代码，维护者只能通过 GitHub stars 与 issues 估计用户量；`telemetry_consent = undefined`；崩溃发生在用户侧时不会被开发者感知。

#### Acceptance Criteria

1. THE Add-on SHALL 实现一个新模块 `src/telemetry_reporter.py`，默认 `telemetry_enabled = false`（opt-in）。
2. WHEN 用户在 Web UI 或 `config.yaml` 中明确设置 `telemetry_enabled = true`，THE Telemetry_Reporter SHALL 每 24 小时向 `https://telemetry.<项目域名>/v1/report` 发送一条 JSON：`{install_id, version, ha_version, arch, locale, days_since_install, active_last_24h}`。
3. THE `install_id` SHALL 是基于 `/data/install_id.uuid` 的本地随机 UUIDv4，首次启动生成，永不携带 HA token、entity_id 列表、用户偏好数据、温湿度数值。
4. WHEN `telemetry_enabled = false`（默认），THE Telemetry_Reporter SHALL 不发起任何外部 HTTP 请求，且不写入 `install_id.uuid`。
5. THE Web UI SHALL 在配置页面显著位置放置一个「Anonymous telemetry」开关，旁边附带一行字：「我们只收集版本与是否活跃，不收集任何 entity ID 或环境数值。点这里查看完整列表」（链接到 PRIVACY.md 的对应小节）。
6. WHERE 用户在 Web UI 关闭遥测开关，THE Telemetry_Reporter SHALL 在 ≤ 30 秒内停止下一次定时任务并删除 `/data/install_id.uuid`。
7. IF 网络请求失败（DNS、TLS、5xx），THEN THE Telemetry_Reporter SHALL 静默退避（指数退避，max 24h）且不阻塞主事件循环；遥测失败永不影响 add-on 主功能。
8. THE Telemetry_Reporter SHALL 始终在独立的 `asyncio.Task`（必要时通过 `asyncio.to_thread` 隔离阻塞型 HTTP 客户端调用）中发起请求，无论主事件循环当前是否处于 inference 高负载或正在执行 SmartEnvironmentController 闭环；IF 主循环负载升高，THEN 不允许通过「跳过遥测」来降级 —— 必须保持「严格隔离 + 独立调度」语义，由 OS 调度而非业务逻辑决定遥测能否上报。
9. THE Add-on SHALL 集成 Sentry 或 Glitchtip（可选 self-hosted）作为崩溃上报，遵循同一 opt-in 开关；上报前用 `sentry_sdk.scrubber` 移除路径中的用户名、entity_id、token 字段。

#### Property-Based Correctness Properties

- **P6.1（默认隐私不变量）**: 对任意首次安装且未修改默认配置的用户状态，`Telemetry_Reporter` 对外的 HTTP 请求计数严格为 0。
- **P6.2（payload 安全不变量）**: 对所有 telemetry payload `p`，`p` 的 JSON 序列化结果不包含正则 `^sensor\\.|^climate\\.|^light\\.|^binary_sensor\\.` 匹配的字符串（即不泄露 entity ID）；用 hypothesis 生成 100+ 配置变体验证。
- **P6.3（撤回幂等性）**: 用户连续 N 次切换 `telemetry_enabled = false`，第 1 次与第 N 次后系统状态完全相同（`install_id.uuid` 不存在、定时任务未注册）。

---

### Requirement 7: Onboarding Wizard — Web UI 首启时自动扫描 + 一键绑定

**User Story:** 作为一名首次安装本 add-on 的用户，我希望 Web UI 第一次打开时弹出一个引导流程，自动扫描我 HA 中的候选睡眠分期实体并让我点击一下就完成绑定，而不是手动到 add-on 配置页填实体 ID。

**Bug Condition X:** v2.0.3 状态下，`onboarding_path = web_ui_form`：用户必须打开 Web UI、从下拉框里挑实体、点保存；没有自动扫描含 `sleep` 关键字的候选实体；没有「我没有睡眠分期硬件」的引导分支；没有 dry_run 安全提示作为引导步骤之一。

#### Acceptance Criteria

1. WHEN Web UI 检测到 `/data/web_ui_overrides.json` 不存在或 `sleep_stage_source` 为空字符串，THE Web_UI SHALL 在首次加载时弹出 Onboarding_Wizard 模态对话框。
2. THE Onboarding_Wizard SHALL 包含至少 4 个步骤：欢迎与免责声明 → 自动扫描候选实体 → 确认环境传感器与执行设备 → dry_run 安全确认与完成。
3. WHEN 进入「自动扫描」步骤，THE Web_UI SHALL 调用 HA REST API `/api/states` 拉取所有实体，过滤出 `domain ∈ {sensor, binary_sensor, input_select}` 且 `entity_id` 或 `friendly_name` 命中关键字 `sleep|睡眠|stage|分期` 的候选项，按相关性评分排序展示。
4. WHEN 用户在候选列表中点选一个实体并点击「使用此实体」，THE Web_UI SHALL 把该 `entity_id` 写入 `/data/web_ui_overrides.json` 的 `sleep_stage_source` 字段（走 `src._io_utils.atomic_write_json`）。
5. WHEN 自动扫描返回 0 个候选实体，THE Web_UI SHALL 在该步骤显示「我没有睡眠分期硬件」CTA，链接到 `docs/HARDWARE.md`（Requirement 3）。
6. WHEN Onboarding_Wizard 进入最后一步，THE Web_UI SHALL 显示当前 `dry_run` 取值并强烈建议保留为 `true` 至少 7 天；用户点击「完成」后 wizard 关闭、不再自动弹出。
7. THE Onboarding_Wizard SHALL 完整支持 i18n（与 Requirement 2 共享 translations key），与配置页文案无重复。
8. IF 用户在 wizard 任意步骤关闭浏览器或刷新，THEN THE Web_UI SHALL 在下次打开时从首步重新开始（基于 `/data/web_ui_overrides.json` 是否存在做判定），不丢失部分填值的副作用。

#### Property-Based Correctness Properties

- **P7.1（幂等性）**: 用户连续完成 wizard N 次（每次都填同样的实体），最终 `/data/web_ui_overrides.json` 内容只与最后一次填值有关。
- **P7.2（候选实体扫描完备性）**: 给定一组合成的 HA `/api/states` 响应（用 hypothesis 生成 entity_id），所有命中关键字的实体必须出现在候选列表中；用 property test 验证扫描函数。
- **P7.3（dry_run 默认安全）**: wizard 完成后写入 `/data/web_ui_overrides.json` 的 `dry_run` 字段不会被设为 `false`，除非用户明确点击「关闭 dry_run」按钮。

---

### Requirement 8: One-Click Lovelace Dashboard Importer

**User Story:** 作为一名希望立刻看到 4-view dashboard 的新用户，我希望在 Web UI 点一下「导入仪表板」按钮就完成创建，而不是手动复制 `examples/lovelace-*.yaml` 到 HA 配置。

**Bug Condition X:** v2.0.3 状态下，`dashboard_setup = copy_paste_yaml`；DOCS.md 中的 4-view dashboard 示例需要用户复制 YAML 到 HA `Settings → Dashboards → Add Dashboard → Edit YAML`；新用户大概率因为不熟悉 Lovelace YAML 而放弃这一步。

#### Acceptance Criteria

1. THE Web_UI SHALL 在主页面提供一个「Import Lovelace Dashboard」按钮（i18n key 同 Requirement 2）。
2. WHEN 用户点击该按钮，THE Web_UI SHALL 调用 HA REST API `/api/lovelace/dashboards`（POST）创建一个新 dashboard `sleep_classifier_dashboard`，并通过 WebSocket `lovelace/config/save` 写入预制的 4-view 配置（与 `examples/lovelace-sleep-dashboard.yaml` 等价）。
3. IF 用户已经存在 url_path 为 `sleep-classifier` 的 dashboard，THEN THE Web_UI SHALL 弹出「已存在，是否覆盖？」确认对话框，默认选项为「取消」；WHEN 用户在该对话框中点击「取消」，THE Web_UI SHALL 静默关闭对话框、不显示错误提示、不发起任何 HA REST API 写入。
3a. WHEN 调用方（前端按钮、未来的 CLI、或经由开发者工具直接 POST）以「确认覆盖」语义抵达 `/api/lovelace/dashboards` 写入路径（即使 UI 对话框被绕过），THE Web_UI SHALL 仍然继续执行覆盖动作 —— 后端不重复做「需先看过对话框」的二次校验，仅以前端 payload 中的 `confirm_overwrite=true` 字段为权威信号。
4. THE Web_UI SHALL 在导入成功后显示一段文本「Dashboard created. Open it in HA → Sidebar → Sleep Classifier」并附带可点击的相对链接（不使用绝对 `/lovelace/...`，遵循 v2.0.3 的 Ingress 路径契约）。
5. IF 调用 `/api/lovelace/dashboards` 返回 4xx/5xx，THEN THE Web_UI SHALL 显示具体错误信息（HTTP code + body 摘要）并提供「手动复制 YAML」回退按钮。
6. THE Add-on_Repository SHALL 把预制 dashboard 配置作为 Python 字符串常量存放在 `sleep_classifier/lovelace_template.py`（避免运行时再读 yaml 文件），引用的 entity_id 全部使用 `sensor.sleep_classifier_*` 前缀。

#### Property-Based Correctness Properties

- **P8.1（实体引用完备性）**: dashboard 模板引用的所有 `sensor.sleep_classifier_*` 实体 ID 必须是 `SleepStatePublisher` 与 `LearningPanelPublisher` 已声明的 20 个实体的子集；用单元测试在导入时静态验证。
- **P8.2（覆盖语义不变量）**: 已存在同名 dashboard 时，未确认覆盖前用户的现有 dashboard 内容必须保持原样不被修改。

---

### Requirement 9: Upgrade Notifier — 用户能在 Web UI 看到「有新版本可用」

**User Story:** 作为一名已经装了 v2.0.3 的用户，我希望在新版本发布后能在 Web UI 或 HA notification 中看到提示，而不是等几个月才偶然在 GitHub 看到 changelog。

**Bug Condition X:** v2.0.3 状态下，`upgrade_notification = none`；用户装了旧版本后没有任何机制感知新版本（HA Supervisor 只在 add-on store 主动刷新时显示更新，但许多用户不会主动看 store）。

#### Acceptance Criteria

1. THE Add-on SHALL 实现一个新模块 `src/upgrade_notifier.py`，每 24 小时调用 GitHub REST API `/repos/<owner>/<repo>/releases/latest` 获取最新 release tag。
2. WHEN 最新 release tag 字符串大于当前运行版本（用 `packaging.version.parse` 比较），THE Upgrade_Notifier SHALL 在 Web UI 主页顶部展示一条非阻塞 banner：「v{latest} is available. Release notes: <link>」。
3. WHERE 用户在 `config.yaml` 中设置 `upgrade_notifications_enabled = false`，THE Upgrade_Notifier SHALL 跳过所有版本检查与 banner 渲染。
4. WHEN Upgrade_Notifier 检测到新版本，THE Add-on SHALL 通过 HA Supervisor proxy 发送一条 `persistent_notification.create` 服务调用（`title="Sleep Classifier 有新版本可用"`），方便用户在 HA 主界面也能看到。
5. IF GitHub API 返回 404、403（rate limit）或网络失败，THEN THE Upgrade_Notifier SHALL 静默指数退避至 max 24 小时，永不阻塞主事件循环。
6. THE Upgrade_Notifier SHALL 不携带任何用户标识（不发 install_id），仅做匿名 GET 请求，符合 Requirement 6 的隐私语义。

#### Property-Based Correctness Properties

- **P9.1（版本比较单调性）**: 对任意 `(current, latest)` 字符串对，若 `current ≥ latest`（按 PEP 440 解析），banner 必定不显示；用 hypothesis 生成 100+ 版本对验证。
- **P9.2（开关一致性）**: `upgrade_notifications_enabled = false` 状态下，所有外部 HTTP 请求计数严格为 0（与 telemetry 共享 P6.1 的隐私契约）。

---

### Requirement 10: Monetization Path — README 增加赞助与未来变现路径

**User Story:** 作为本项目的维护者，我希望 README 顶部和底部有清晰的赞助按钮（GitHub Sponsors、爱发电、买杯咖啡），并在 ROADMAP 中明确未来的付费方向（托管服务、付费技术支持、硬件套件），以便我能逐步走向可持续的开源 + 商业模型。

**Bug Condition X:** v2.0.3 状态下，README 没有任何赞助/付费链接；ROADMAP 中没有写明商业化路径；未来若想引入付费版本，缺乏与现有用户的预期管理。

#### Acceptance Criteria

1. THE README.md SHALL 在顶部 badges 行增加一个 GitHub Sponsors 徽章，链接到 `https://github.com/sponsors/<owner>`。
2. THE README.md SHALL 在底部「Support the project」section 列出至少 2 种赞助方式：GitHub Sponsors 与爱发电（或买杯咖啡 / Patreon）。
3. THE Add-on_Repository SHALL 在 `.github/FUNDING.yml` 文件中配置 GitHub Sponsors 与备选平台，使 GitHub UI 在仓库主页显示「Sponsor」按钮。
4. THE `docs/ROADMAP.md`（如不存在则创建）SHALL 包含一节「Commercial roadmap (post-v2.1.0)」，明确列出至少 3 个潜在变现方向：托管服务、付费技术支持、推荐硬件套件 affiliate 计划。
5. THE README.md 与 ROADMAP.md SHALL 明确承诺：现有 MIT License 下的功能永远不会被移到付费版；付费仅针对增量服务（hosting、support、hardware）。
6. WHERE 推荐硬件涉及 affiliate 链接（与 Requirement 3 相关），THE README.md SHALL 重申 affiliate disclosure 一致性。

---

### Requirement 11: User Evidence — 加 testimonial 与 30 天真实案例图

**User Story:** 作为一名考察本 add-on 的潜在用户，我希望 README 里能看到至少 1 个 30 天真实使用案例（带匿名化数据图），以便我能判断「这个东西在真实卧室里真的能跑起来」。

**Bug Condition X:** v2.0.3 状态下，`testimonials = 0`；README 完全靠功能描述而非真实使用证据；潜在用户难以评估实际效果。

#### Acceptance Criteria

1. THE README.md SHALL 增加「Real-world results」section，至少包含 1 张匿名化的 30 天 Lovelace 截图（可以是项目维护者本人的数据）展示推荐入睡时间、学习到的环境参数、睡眠质量分趋势。
2. THE README.md SHALL 提供「Beta tester program」段落，说明早期用户可以通过提交真实使用截图（匿名）换取在 README 与 testimonials 页的署名（可选 + opt-in）。
3. THE Add-on_Repository SHALL 在 `docs/CASE_STUDIES.md` 创建专门的案例研究索引页，初版至少包含项目维护者自己的 30 天案例。
4. WHERE testimonials 涉及具体用户，THE testimonials 文档 SHALL 取得用户书面同意（issue / PR comment 留痕即可），并允许用户随时撤回。
5. THE README.md 的 testimonials 数据 SHALL 不包含任何具体 entity_id、HA 实例 URL、家庭住址、生物识别信息。

---

### Requirement 12: Device Ecosystem Beyond HA — 明确 v2.2.0+ ROADMAP（本期 deferred）

**User Story:** 作为一名使用 Apple Home / SmartThings / Matter 但不用 HA 的潜在用户，我希望项目 ROADMAP 能告诉我未来是否会支持我的生态，以便我决定是否要切换或等待。

**Bug Condition X:** v2.0.3 状态下，本 add-on 仅支持 HA；项目 ROADMAP 没有明确「device ecosystem 扩展」的路径；潜在用户在评估时无法判断是「永远只支持 HA」还是「短期只支持 HA、长期会扩展」。

#### Acceptance Criteria

1. THE `docs/ROADMAP.md` SHALL 包含一节「Device ecosystem expansion (deferred to v2.2.0+)」，明确写出本期不实施。
2. THE ROADMAP.md SHALL 列出至少 3 个候选扩展方向（Matter sleep tracker integration、SmartThings webhook bridge、Apple Health export）的可行性概览。
3. THE ROADMAP.md SHALL 说明「本 add-on 的核心学习器（preference_learner、sleep_quality_score）是纯 Python 模块，理论上可以从 HA add-on 中抽出复用」，给未来贡献者一个明确的技术起点。
4. THE README.md SHALL 在 FAQ 中增加一条「Why HA only?」并链接到 ROADMAP 的对应小节。
5. WHEN v2.2.0 开始实施时，THE 对应 spec SHALL 在本 spec 的「Out of scope」记录中显式承接，不丢失需求。

---

### Requirement 13: Multi-Resident / Multi-Room — 明确 v2.2.0+ ROADMAP（本期 deferred）

**User Story:** 作为一对夫妻共睡一床、对体感温度需求不同的用户，我希望项目 ROADMAP 能说明「v2.1.0 仍假设单户单房间，但 v2.2.0+ 会支持多用户多房间」，以便我们能决定是凑合用还是等待。

**Bug Condition X:** v2.0.3 状态下，`preference_learner` 用单一 `user_preferences.json` 学习，`smart_environment_controller` 假设一个卧室一组目标；夫妻不同体感、孩子另一房间的场景未支持；ROADMAP 也没有明确这一限制。

#### Acceptance Criteria

1. THE `docs/ROADMAP.md` SHALL 包含一节「Multi-resident / multi-room (deferred to v2.2.0+)」，明确说明本期不实施。
2. THE ROADMAP.md SHALL 列出技术挑战：每用户独立的 preference 文件、设备槽位的「per-room」分组、不同 wake_window 的协同控制。
3. THE README.md 的 FAQ SHALL 增加一条「Two people sharing a bed?」并链接到 ROADMAP。
4. THE `sleep_classifier/DOCS.md` SHALL 在「Limitations」段落显式列出当前的「单户单房间假设」。
5. WHEN v2.2.0 开始实施多用户支持时，THE 对应 spec SHALL 处理 `/data/user_preferences.json` 的迁移路径（保持向后兼容）。

---

### Requirement 14: Medical Advisor Placeholder Section

**User Story:** 作为一名关注本 add-on 是否「靠谱」的用户，我希望 README 里有一节医学顾问 placeholder（即使目前是空的），以便我看到团队对医学背书的态度，而不是单纯「程序员的副业」。

**Bug Condition X:** v2.0.3 状态下，没有任何医学背书 / advisor 章节；潜在用户对睡眠类产品的医学合规性难以建立信任；社区贡献者也不知道项目欢迎医学专业人士加入。

#### Acceptance Criteria

1. THE README.md SHALL 增加一节「Medical advisors」，明确说明「项目目前由开源社区维护，正在寻找具有 sleep medicine、polysomnography、smart-home health 背景的志愿顾问」。
2. THE README.md SHALL 在该 section 提供联系邮箱与申请说明（可复用 SECURITY.md 的邮箱）。
3. WHERE 顾问加入项目，THE README.md SHALL 在 advisors section 列出姓名、机构、贡献内容（取得本人书面同意后）。
4. THE MEDICAL_DISCLAIMER.md SHALL 与 advisors section 互相链接，并在 advisors 介入前明确说明「目前所有医学性陈述均为非临床、非诊断、非医疗建议」。
5. THE README.md SHALL 不出现任何形式的虚假背书或未经同意的人名引用。

---

### Requirement 15: Single Case Study — 30 天数据 blog 发 Reddit / HN

**User Story:** 作为本项目的维护者，我希望发布至少 1 篇基于自己 30 天真实数据的案例研究 blog（含数据图、复盘、教训），并发到 Reddit r/homeautomation、Hacker News 等社区，以便项目能拿到第一批种子用户。

**Bug Condition X:** v2.0.3 状态下，没有任何已发表的案例研究；项目曝光完全依赖 HA 社区论坛偶尔的提及；冷启动困难。

#### Acceptance Criteria

1. THE Add-on_Repository SHALL 在 `docs/CASE_STUDIES.md` 提供至少 1 篇 ≥ 1500 字的案例研究草稿，覆盖：硬件清单、安装时间、第 1/3/7/14/30 天的体验差异、踩过的坑、最终效果数据。
2. THE 案例研究 SHALL 附带至少 3 张 Lovelace 截图（匿名化）作为支撑数据。
3. WHEN 案例研究发布到外部社区（Reddit、HN、HA Community Forum、知乎、少数派），THE README.md SHALL 在「In the press / community」section 列出对应链接；IF 截至 v2.1.0 release 时尚未有任何外部社区发布，THEN README.md 不强制创建该 section（允许在首次外部发布后再补上），避免出现空 placeholder 反而损害可信度。
4. WHERE 外部 blog 平台有 SEO 价值，THE 案例研究 SHALL 在 README.md 与 docs/CASE_STUDIES.md 之间互相链接，避免内容割裂。
5. THE 案例研究内容 SHALL 严格遵守 Requirement 11 的隐私要求：不暴露真实 entity_id、HA URL、家庭住址。
6. THE 案例研究 SHALL 在结尾包含一段「How to reproduce on your own data」指引，引导读者来安装本 add-on。

---

## Preservation Requirements — 不能破坏 v2.0.3 的既有行为

本 spec 的所有改动 MUST 保持向后兼容。以下是 v2.0.3 状态的核心不变量，任何 v2.1.0 改动若违反则 CI 失败。

### PR1: 测试套件兼容性

1. THE v2.1.0 实现 SHALL 保持 `pytest tests/ --timeout=60` 在 Python 3.10/3.11/3.12 上 100% 通过。
2. THE v2.1.0 实现 SHALL 保持 `pytest --cov=src --cov=scripts` 总覆盖率 ≥ 92%（v2.0.3 当前值）。
3. WHEN 新增模块（telemetry_reporter、upgrade_notifier、lovelace_template、onboarding 后端）被引入，THE 对应 `tests/test_<module>.py` SHALL 与之同步提交且覆盖率 ≥ 90%。

### PR2: Sensor 实体契约

1. THE v2.1.0 SHALL 保持 v2.0.3 的全部 20 个 `sensor.sleep_classifier_*` 实体 ID 与 attribute schema 不变。
2. IF 需要新增 sensor，THEN 仅允许在 `SleepStatePublisher` / `LearningPanelPublisher` 中追加，不允许重命名或删除已有实体。

### PR3: 持久化文件向后兼容

1. THE v2.1.0 SHALL 能成功读取 v2.0.3 写入的 `/data/user_preferences.json`、`/data/web_ui_overrides.json`、`/data/effective_config.json`，不抛出 schema 异常。
2. WHERE v2.1.0 新增持久化字段（如 `install_id.uuid`、`last_upgrade_check`），THE 字段 SHALL 全部 optional，缺失时使用安全默认值。
3. THE v2.1.0 SHALL 通过 `src._io_utils.atomic_write_json` 写入所有 `/data/*.json`，禁止使用 `Path.write_text`（与 v2.0.3 steering rule 一致）。

### PR4: 运行时依赖

1. THE v2.1.0 SHALL 不向 `requirements-runtime.txt` 添加除 `aiohttp` 之外的新硬依赖。
2. WHERE Sentry/Glitchtip 集成需要额外包，THE 依赖 SHALL 通过「optional extra」机制（`pip install sleep-classifier[telemetry]`）声明，且默认不安装。
3. THE v2.1.0 SHALL 保持 add-on 镜像体积 ≤ v2.0.3 体积 × 1.10（10% 余量）。

### PR5: Add-on 启动契约

1. THE v2.1.0 SHALL 保持 v2.0.3 的「sleep_stage_source 未绑定时也能发占位 sensor」行为（v2.0.2 修复，prepare 必跑契约）。
2. THE v2.1.0 SHALL 保持 v2.0.3 的 SIGTERM 转发契约（`tini -g` + `wait -n`，10 秒内 flush preferences）。
3. THE v2.1.0 SHALL 保持 v2.0.3 的 Ingress 路径契约（前端用相对路径，aiohttp 同时挂载 `/api/*` 与 `/ingress_entry/api/*`）。
4. THE v2.1.0 SHALL 保持 v2.0.3 的 `startup: application` 与 `homeassistant_api: true` 配置语义。

### PR6: 配置 schema 向后兼容

1. WHERE v2.1.0 在 `sleep_classifier/config.yaml` 新增 `options.*` 字段（如 `telemetry_enabled`、`upgrade_notifications_enabled`），THE 字段 SHALL 全部带默认值且 schema 标记为 `?`（optional）。
2. THE v2.1.0 SHALL 不修改已有 `options.*` 字段的 schema 类型或默认值，避免触发 HA Supervisor 的「configuration migration」警告。

---

## Property-Based Correctness Summary（跨需求）

| Property | 涉及 Requirement | 测试位置 |
|---|---|---|
| 默认隐私不变量（无 opt-in 时零外部请求） | R6, R9 | `tests/test_telemetry_reporter.py`, `tests/test_upgrade_notifier.py` |
| Translations 覆盖率不变量（en/zh-cn/config.yaml key 集合相等） | R2 | `tests/test_translations_coverage.py` + `addon-lint` |
| 版本一致性不变量（pyproject / setup.py / config.yaml 版本相等） | R4 | `.github/workflows/test.yml` + `scripts/sync_version.py --check` |
| Idempotence（onboarding wizard、telemetry opt-out、dashboard import 重复执行结果一致） | R7, R6, R8 | hypothesis `@given` 生成 N=2..10 重复操作 |
| Round-trip（atomic_write_json → read_json → atomic_write_json 等价） | PR3 | 已存在 `tests/test_io_utils.py`（v2.0.3 保留） |
| 链接可达性（README/DOCS 中 medical 关键词附近存在 disclaimer 链接） | R5 | CI markdown link checker |

---

## Out of Scope (deferred to v2.2.0+)

为避免 v2.1.0 范围爆炸，以下项明确 deferred：

1. **多用户 / 多房间架构**（Requirement 13）：需重写 `preference_learner` 与 `smart_environment_controller` 的设备槽位模型。
2. **Device ecosystem 扩展**（Requirement 12）：需要把核心学习器从 add-on 中抽出做成独立 Python 包。
3. **付费托管服务 / 付费技术支持产品化**（Requirement 10 涉及但仅停留在 ROADMAP 与 link 层面，不实施实际付费功能）。
4. **真实自动化生成 demo GIF / 视频**：v2.1.0 仅要求静态截图 + 至少 1 段视频链接；自动化录制延后。

每一项都在对应 ROADMAP 文档中显式标注，确保 v2.2.0 spec 启动时能直接承接，不丢失需求。
