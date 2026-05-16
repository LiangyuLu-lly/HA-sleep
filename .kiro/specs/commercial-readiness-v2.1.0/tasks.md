# Implementation Plan: commercial-readiness-v2.1.0

> Spec: commercial-readiness-v2.1.0
> Workflow: requirements-first（feature spec）
> 关联: `.kiro/specs/commercial-readiness-v2.1.0/requirements.md`、`.kiro/specs/commercial-readiness-v2.1.0/design.md`

## Overview

本计划把 design.md 拆成可独立执行的代码任务。优先建立 CI 守护脚本与持久化默认值（早期发现回归），然后逐个交付新模块（`onboarding_scanner` / `telemetry_reporter` / `upgrade_notifier` / `lovelace_template`），最后扩展 `sleep_classifier/web_ui.py` 与 `scripts/run_ha_smart_service.py` 把它们接入主事件循环。所有改动遵守 PR1–PR6 的不变量：

- 新模块都按 `src/<module>.py` ↔ `tests/test_<module>.py` 镜像式命名（structure.md）。
- 运行时依赖**仅** `aiohttp`，不引入 hypothesis / numpy / scipy / ML 框架；correctness properties 用普通 pytest 参数化与显式不变量循环表达（与当前 tech.md 状态一致）。
- 文档使用中文，代码标识符英文（language.md）。
- `/data/*.json` 写入一律走 `src._io_utils.atomic_write_json`，禁止 `Path.write_text`（tech.md / PR3）。
- 所有 HA 交互经 `src/ha_api_client.py`，新模块以独立 `asyncio.Task` 接入主循环。

## Tasks

- [ ] 1. CI 守护脚本与持久化默认值
  - [x] 1.1 扩展 web_ui_overrides 加载契约
    - 在 `sleep_classifier/web_ui.py` 既有 `_load_overrides()` / `_save_overrides()` 顶部增加 v2.1.0 新字段安全默认值：`onboarding_skipped=false`、`telemetry_enabled=false`、`upgrade_notifications_enabled=true`，缺失字段一律 `.get(key, default)`。
    - 新增 `src/_overrides_schema.py`（纯函数模块）集中暴露默认值常量与 `apply_v2_1_0_defaults(data: dict) -> dict`，供 web_ui 与新模块共享。
    - 写入路径仍调用 `src._io_utils.atomic_write_json`，不修改 v2.0.3 既有字段。
    - _Requirements: 6.6, 7.8, 9.3, PR3.1, PR3.2, PR6.1_

  - [x] 1.2 编写持久化字段缺失安全测试（Property 11）
    - 新增 `tests/test_overrides_schema.py`：参数化删除 `{onboarding_skipped, telemetry_enabled, upgrade_notifications_enabled, checked_at, latest, notified}` 任意子集组合，断言 `apply_v2_1_0_defaults` 返回的 dict 满足「最隐私友好」默认值且不抛异常。
    - **Property 11: 持久化字段缺失安全**
    - **Validates: Requirements 6.6, 7.8, 9.3**

  - [x] 1.3 实现 scripts/sync_version.py
    - `read_canonical()` 读 `pyproject.toml [project] version`；`sync(version, *, check_only)` 同步/校验 `setup.py`、`sleep_classifier/config.yaml`、`src/__init__.py`（如存在）；`main()` 接受 `--check` 选项。
    - 仅用 stdlib（`tomllib` for 3.11+；YAML 用 `pyyaml` 已在开发环境；正则替换 + 行扫描足够）。
    - 不一致时返回非零退出码并 print 三处版本字符串。
    - _Requirements: 4.5, 4.6_

  - [x] 1.4 编写 sync_version 一致性测试（Property 1）
    - 新增 `tests/test_sync_version.py`：用 `tmp_path` 构造若干「先行不一致初态」（pyproject 与 config.yaml 版本不同），调用 `sync` 后断言三处版本字符串严格相等；调用 `sync(check_only=True)` 在不一致时返回非零、一致时返回 0。
    - **Property 1: 版本号四处一致**
    - **Validates: Requirements 4.5, 4.6**

  - [x] 1.5 实现 scripts/check_branding.py
    - 解析 PNG header（stdlib `struct`），断言 `sleep_classifier/icon.png` 为 128×128、`sleep_classifier/logo.png` 为 250×100；任意一项不合规返回非零。
    - 不引入 Pillow 运行时依赖；CI 步骤中独立调用即可。
    - _Requirements: 1.1, 1.2, 1.6_

  - [x] 1.6 实现 scripts/check_translations.py
    - 解析 `sleep_classifier/config.yaml` 的 `options:` 与 `sleep_classifier/translations/{en,zh-cn}.yaml` 的 `configuration:` key 集合，三集合不严格相等即返回非零。
    - 缺失的 key 在错误信息中按文件分组列出。
    - _Requirements: 2.1, 2.4, 2.5_

  - [x] 1.7 实现 scripts/check_medical_links.py
    - 扫描 `README.md`、`sleep_classifier/DOCS.md`、`docs/*.md`，对每个段落用正则 `medical|医学|诊断|diagnose|sleep[\s-]apnea|呼吸暂停` 匹配；命中段落（含其前后 ≤ 1 段落窗口）必须存在指向 `MEDICAL_DISCLAIMER.md` 的相对链接。
    - 任一窗口违反即返回非零；纯函数 `check_paragraph_window(paragraphs: list[str], idx: int) -> bool` 便于单测。
    - _Requirements: 5.6, 5.7, 14.4_

  - [x] 1.8 实现 scripts/check_funding.py
    - 解析 `.github/FUNDING.yml`，断言 README sponsor badge 链接的 owner 与 FUNDING 中 `github` 字段一致；不一致返回非零。
    - _Requirements: 10.1, 10.3_

  - [x] 1.9 编写 CI 守护脚本的守护性测试（Property 9 + Property 10）
    - 新增 `tests/test_check_branding.py`：参数化生成 `(width, height)` 偏离用例（含合规 128×128 / 250×100 与多组偏离尺寸），断言对偏离用例返回非零、对合规用例返回 0；用 stdlib 写入最小 PNG 头到 `tmp_path`。
    - 新增 `tests/test_check_translations.py`：参数化「config.yaml 增删改」mutation，断言对应 `en.yaml` / `zh-cn.yaml` 未同步时返回非零；完全合规时返回 0。
    - 新增 `tests/test_check_medical_links.py`：构造若干 mutated 段落（医学关键字命中 / 不命中、disclaimer 链接存在 / 不存在），断言 `check_paragraph_window` 对违反用例严格检测出。
    - **Property 9: 医疗免责链接可达性**
    - **Property 10: CI 资产与翻译合规守护是真的会拦**
    - **Validates: Requirements 1.6, 2.5, 5.6, 5.7**

- [ ] 2. Branding / Translations / Legal / Docs 资产
  - [x] 2.1 提交 branding 资产与 README 截图墙
    - 提交 `sleep_classifier/icon.png`（128×128 PNG）与 `sleep_classifier/logo.png`（250×100 PNG）；与 HA 主色 `#03a9f4` 协调的「半月 + 床」简笔。
    - 新建 `assets/screenshots/`，至少 1 张 ≥ 1200 px 宽 Lovelace 4-view 截图。
    - GitHub 仓库 topics（`home-assistant`、`addon`、`sleep-tracking`、`smart-home`）由维护者在 GitHub UI 配置；在 `docs/ROADMAP.md` 顶部 maintenance checklist 中登记此手工步骤。
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_

  - [x] 2.2 创建 Translations pack（en + zh-cn）
    - 新建 `sleep_classifier/translations/en.yaml` 与 `sleep_classifier/translations/zh-cn.yaml`，`configuration:` 块覆盖 `config.yaml` 当前 `options:` 全部 ≥ 30 个 key（含本期新增 `telemetry_enabled` / `upgrade_notifications_enabled`），每 key 提供 `name` 与 `description`。
    - 新增 `onboarding:` 命名空间承载 wizard 文案（`step1_title`、`step1_disclaimer`、`step2_title`、`no_hardware_cta`、`step4_dry_run_warning` 等），中英对齐。
    - 不翻译 entity_id / 服务名 / Python 标识符 / CLI 命令。
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.6, 7.7_

  - [x] 2.3 创建 Legal document set
    - 在仓库根目录新建 `PRIVACY.md`、`SECURITY.md`、`CONTRIBUTING.md`、`CODE_OF_CONDUCT.md`、`MEDICAL_DISCLAIMER.md` 五份文档，中文为主 + 英文摘要。
    - PRIVACY.md 至少声明：处理的数据类型、本地存储位置、保留期限、默认不向第三方传输、opt-in 遥测内容与撤回方式（与 R6 一致）。
    - SECURITY.md 写明披露邮箱、≤ 7 天首响、CVE 流程、禁止公开 issue。
    - CONTRIBUTING.md 写明 PR 流程、commit message 约定、`pytest --cov` 门槛、`prepare.sh` 同步要求。
    - CODE_OF_CONDUCT.md 采用 Contributor Covenant v2.1 + 维护者执行联系方式。
    - MEDICAL_DISCLAIMER.md 聚合所有医疗免责声明。
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6_

  - [~] 2.4 编写 Legal docs 静态校验测试
    - 新增 `tests/test_legal_docs_present.py`：断言五份 legal 文档存在 + 关键段落（披露邮箱、Contributor Covenant 标识、医疗免责标题）grep 命中。
    - **Validates: Requirements 5.1, 5.3, 5.5**

  - [x] 2.5 新建 docs/HARDWARE.md
    - 至少 3 类（毫米波雷达、智能手环、智能手表）共 ≥ 5 款硬件；每款列出型号、价格区间、HA 接入路径、典型 entity_id、4 阶段（AWAKE/LIGHT/DEEP/REM）支持度、是否经 ≥ 7 天 dry_run 真实验证。
    - 顶部加 affiliate disclosure 段（FTC + 中国《广告法》合规）。
    - _Requirements: 3.1, 3.2, 3.4, 3.6_

  - [x] 2.6 新建 docs/ROADMAP.md
    - 包含 v2.1.0（current）章节链接 15 条 requirements。
    - 包含 v2.2.0+（deferred）— `Device ecosystem expansion` 列出至少 3 个候选方向（Matter / SmartThings / Apple Health）+ 「核心学习器是纯 Python 模块可抽离」技术起点说明。
    - 包含 v2.2.0+（deferred）— `Multi-resident / multi-room` 列出技术挑战 + `/data/user_preferences.json` 迁移路径。
    - 包含 `Commercial roadmap (post-v2.1.0)` 列出至少 3 个变现方向（托管服务、付费技术支持、推荐硬件套件）+ MIT 功能不会被移到付费版的承诺。
    - _Requirements: 10.4, 10.5, 12.1, 12.2, 12.3, 13.1, 13.2_

  - [~] 2.7 新建 docs/CASE_STUDIES.md
    - 索引页 + 至少 1 篇 ≥ 1500 字案例研究（项目维护者本人 30 天数据）。
    - ≥ 3 张匿名化 Lovelace 截图引用。
    - 严格遵守 R11 隐私要求（不暴露真实 entity_id / HA URL / 家庭住址）。
    - 结尾包含「How to reproduce on your own data」指引。
    - _Requirements: 11.1, 11.5, 15.1, 15.2, 15.5, 15.6_

  - [x] 2.8 新建 .github/FUNDING.yml
    - 配置 `github: <owner>` 与至少一个备选平台（爱发电 / Patreon / 买杯咖啡）。
    - _Requirements: 10.3_

  - [~] 2.9 重写 README.md 顶部与商业化 sections继续检查当前剩余任务，全量完成
  
    - 在顶部 badges 行加 GitHub Sponsors 徽章（`https://github.com/sponsors/<owner>`）。
    - 在「30 天你会看到什么」表格之前插入「Hardware Required」section（链接 `docs/HARDWARE.md`，标注 `No sleep-stage sensor? Start here.` 中英双语）+ demo GIF / 视频外链段落引用 `assets/screenshots/`。
    - 表格之后增加「Real-world results」section（引用 `assets/screenshots/` 与 `docs/CASE_STUDIES.md`）+「Beta tester program」段。
    - 增加「Medical advisors」section（招募中状态、联系邮箱复用 SECURITY.md 的）+ 显式声明「目前所有医学性陈述均为非临床、非诊断、非医疗建议」并链接 `MEDICAL_DISCLAIMER.md`。
    - 增加 FAQ 条目「Why HA only?」（链接 ROADMAP §Device ecosystem）+「Two people sharing a bed?」（链接 ROADMAP §Multi-resident）。
    - 在底部增加「Support the project」section 列出至少 2 种赞助方式。
    - 在底部「In the press / community」section 保留 placeholder 注释说明 R15.3（首次外部发布前可缺省）。
    - 显式承诺现有 MIT License 功能永远不会被移到付费版（与 ROADMAP 一致）。
    - _Requirements: 1.5, 3.3, 10.1, 10.2, 10.5, 11.1, 11.2, 12.4, 13.3, 14.1, 14.2, 14.4, 14.5, 15.3, 15.4_

  - [~] 2.10 在 sleep_classifier/DOCS.md 增加 Limitations 与 disclaimer 链接
    - 在 DOCS.md「Limitations」段落显式列出当前的「单户单房间假设」并链接 ROADMAP §Multi-resident。
    - 在 DOCS.md 首页与各「medical / 呼吸暂停」邻近段落补充 `MEDICAL_DISCLAIMER.md` 相对链接。
    - 在 DOCS.md 描述 telemetry 开关的位置写明「默认关闭、不上报 entity_id、可一键撤回」并链接 PRIVACY.md。
    - _Requirements: 5.6, 5.7, 6.5, 13.4_

  - [~] 2.11 静态校验 docs / README 结构
    - 新增 `tests/test_hardware_doc.py` / `tests/test_roadmap_doc.py` / `tests/test_case_studies_doc.py` / `tests/test_funding_yml.py`：解析对应文件断言关键段落标题、表格存在、字数下限（CASE_STUDIES.md ≥ 1500 字）。
    - 新增 `tests/test_readme_sections.py`：grep README 必含 sections（Hardware Required / Real-world results / Medical advisors / Support the project / FAQ-Why HA only / FAQ-Two people sharing a bed）。
    - **Validates: Requirements 3.1, 10.1, 10.4, 11.1, 12.1, 13.1, 14.1, 15.1**

- [~] 3. Checkpoint - 基础守护层就绪
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 4. 新增 src/ 模块（onboarding_scanner / telemetry_reporter / upgrade_notifier / lovelace_template）
  - [x] 4.1 在 src/data_structures.py 增加 CandidateEntity dataclass
    - 在 `src/data_structures.py` 末尾追加：
      ```python
      @dataclass(frozen=True, slots=True)
      class CandidateEntity:
          entity_id: str
          friendly_name: str
          score: int
      ```
    - 不修改既有 `SleepStage` / `SleepSession` 类型（PR2 守护）。
    - _Requirements: 7.3_

  - [x] 4.2 实现 src/onboarding_scanner.py
    - `score_candidate(entity_id, friendly_name) -> int`：根据关键字命中度返回 0–100 评分；关键字 `sleep|睡眠|stage|分期`，`entity_id` 命中 +60、`friendly_name` 命中 +40，重复命中按权重叠加但不超过 100。
    - `filter_candidates(states, *, domains=frozenset({"sensor","binary_sensor","input_select"}), keyword_pattern=SLEEP_STAGE_KEYWORD_PATTERN) -> list[CandidateEntity]`：过滤 + 评分 + 严格按 score 降序（同分按 entity_id 升序保证确定性）。
    - 暴露 `SLEEP_STAGE_KEYWORD_PATTERN: re.Pattern[str]` 常量便于复用。
    - 模块为纯函数，不做 I/O，不依赖 aiohttp。
    - _Requirements: 7.3, 7.5_

  - [~] 4.3 编写 onboarding_scanner 测试（Property 2）
    - 新增 `tests/test_onboarding_scanner.py`：
      - 参数化生成「entity_id / friendly_name 含或不含关键字」组合，断言（a）所有命中关键字的实体出现在结果中（无漏召）、（b）所有未命中的不出现（无误召）、（c）结果按 `score` 严格降序（同分按 entity_id 升序）。
      - 0 候选时返回空列表（与 wizard CTA 联动验证）。
      - domain 不在白名单的实体即使 friendly_name 命中也排除。
    - **Property 2: 候选实体扫描完备性 + 评分单调性**
    - **Validates: Requirements 7.3**

  - [~] 4.4 在 src/ha_api_client.py 增加 Lovelace WebSocket 方法
    - 增加三个 async 方法（复用现有 WS 重连基础设施）：
      - `lovelace_dashboards() -> list[dict[str, Any]]`：发送 `lovelace/dashboards/list`。
      - `lovelace_create_dashboard(*, url_path, title, icon) -> dict[str, Any]`：发送 `lovelace/dashboards/create`。
      - `lovelace_save_config(*, url_path, config) -> None`：发送 `lovelace/config/save`。
    - 异常处理顺序保持 `HAAuthError` → `HAAPIError`（PR5），失败时由调用方决定回滚。
    - _Requirements: 8.2, 8.5_

  - [~] 4.5 实现 src/telemetry_reporter.py
    - 实现 design §3.6 的 `TelemetryReporter` 类：
      - 构造参数：`enabled`、`endpoint`、`version`、`ha_version`、`arch`、`locale`、`data_dir=Path("/data")`、`clock=time.time`、`interval_seconds=86_400.0`。
      - `async run()`：`enabled=False` 立即 return（且不创建 install_id）；`enabled=True` 时 24h 周期上报，aiohttp 出站；`try/except Exception` 包裹 tick，永不冒泡。
      - `async disable()`：取消内部 task + `Path("/data/install_id.uuid").unlink(missing_ok=True)`，幂等。
      - `enable()` 与 `run()` 之间通过内部 `_task: asyncio.Task | None` 状态机管理。
    - `@staticmethod build_payload(...) -> dict[str, Any]`：构造 `TelemetryPayload`，序列化后 self-check 不匹配 `_ENTITY_ID_PATTERN = re.compile(r"^sensor\.|^climate\.|^light\.|^binary_sensor\.", re.MULTILINE)`，匹配则 `raise RuntimeError`。
    - install_id 生成：仅在 `enabled=true` 且 `/data/install_id.uuid` 不存在时 `uuid.uuid4()` 后通过 `src._io_utils.atomic_write_text` 写入。
    - 网络失败 / 4xx / 5xx 静默指数退避 max 24h，记 WARNING。
    - 可选 `sentry_sdk` 集成：`try: import sentry_sdk` 成功时 `sentry_sdk.scrubber` 移除 `entity_id` / `token` / `username`；不强制依赖。
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.6, 6.7, 6.8, 6.9_

  - [~] 4.6 编写 build_payload 不泄露 entity_id 测试（Property 3）
    - 新增 `tests/test_telemetry_payload.py`：参数化 `install_id`、`version`、`ha_version`、`arch`、`locale`、`days_since_install`、`active_last_24h`（含极端值，例如版本号字符串含 `sensor.` 字面量），调用 `build_payload(...)` 后 `json.dumps(sort_keys=True)` 必不匹配 `_ENTITY_ID_PATTERN`；故意构造伪装载荷验证 self-check 抛 `RuntimeError`。
    - **Property 3: Telemetry payload 不泄露 entity_id**
    - **Validates: Requirements 6.3**

  - [~] 4.7 编写 telemetry 默认零外部出站测试（Property 4 — telemetry 部分）
    - 新增 `tests/test_telemetry_default_privacy.py`：用 aiohttp mock 拦截所有出站请求，断言 `enabled=false` 状态下 `TelemetryReporter.run()` 立即返回且 HTTP 请求计数严格为 0；并断言 `Path("/data/install_id.uuid")` 不存在（用 `tmp_path` 作为 `data_dir`）。
    - **Property 4（telemetry 部分）: 默认与禁用状态下零外部出站请求**
    - **Validates: Requirements 6.4**

  - [~] 4.8 编写 telemetry enable/disable 幂等测试（Property 6）
    - 新增 `tests/test_telemetry_disable_idempotent.py`：参数化生成 N ∈ [1, 10] 的 `enable()` / `disable()` 操作序列，最终调用 `disable()` 后断言 `_task is None`、install_id 文件不存在；与单次 `disable()` 调用之后状态等价。
    - **Property 6: Telemetry 撤回幂等**
    - **Validates: Requirements 6.6**

  - [~] 4.9 实现 src/upgrade_notifier.py
    - 实现 design §3.9 的 `UpgradeNotifier` 类：
      - 构造参数：`enabled`、`current_version`、`owner`、`repo`、`ha_client: HAAPIClient`、`data_dir=Path("/data")`、`interval_seconds=86_400.0`。
      - `async run()`：`enabled=False` 立即 return；`enabled=True` 时 24h 周期 GET `https://api.github.com/repos/{owner}/{repo}/releases/latest`，User-Agent: `sleep-classifier/{version}`；不发 install_id。
      - 检测到新版本时调用 `ha_client.call_service("persistent_notification", "create", ...)` 固定 `notification_id="sleep_classifier_upgrade"`。
      - 持久化 `/data/last_upgrade_check.json`（含 `checked_at` / `latest` / `notified`），通过 `atomic_write_json`。
      - 网络失败 / 403 / 404 / 5xx 静默指数退避 max 24h。
      - `try/except Exception` 包裹 tick，永不冒泡。
    - `@staticmethod is_newer(current, latest) -> bool`：用 `packaging.version.parse` 比较；`current >= latest` 返回 False；非 PEP 440 字符串返回 False（保守）。
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6_

  - [~] 4.10 编写 is_newer 版本比较测试（Property 5）
    - 新增 `tests/test_upgrade_notifier_is_newer.py`：参数化生成 `(current, latest)` PEP 440 字符串对（含预发布、后置版本号、不规范字符串），断言：
      - 反对称：`is_newer(a, b)=True ⇒ is_newer(b, a)=False`。
      - 一致性：`current==latest ⇒ False`。
      - 传递性：在 ≤ 50 个枚举三元组上验证 `is_newer(a,b) ∧ is_newer(b,c) ⇒ is_newer(a,c)`。
    - **Property 5: GitHub release 版本比较语义正确**
    - **Validates: Requirements 9.2**

  - [~] 4.11 编写 upgrade_notifier 默认零外部出站测试（Property 4 — upgrade 部分）
    - 新增 `tests/test_upgrade_notifier_disabled.py`：mock aiohttp 出站，断言 `enabled=false` 时 `run()` 立即返回且 HTTP 请求计数严格为 0；`/data/last_upgrade_check.json` 不被写入。
    - **Property 4（upgrade 部分）: 默认与禁用状态下零外部出站请求**
    - **Validates: Requirements 9.3**

  - [~] 4.12 编写 upgrade_notifier 行为示例测试
    - 新增 `tests/test_upgrade_notifier.py`：mock GitHub API 返回新版本 → 断言 `ha_client.call_service` 被以 `notification_id="sleep_classifier_upgrade"` 调用一次；GitHub 403 / 404 / 5xx → 不调用 `call_service` 且静默退避；headers 不含 `install_id` / `Authorization`。
    - **Validates: Requirements 9.1, 9.4, 9.5, 9.6**

  - [~] 4.13 实现 sleep_classifier/lovelace_template.py
    - 新建 `sleep_classifier/lovelace_template.py`：
      - 常量 `DASHBOARD_TITLE = "Sleep Classifier"`、`DASHBOARD_URL_PATH = "sleep-classifier"`、`DASHBOARD_ICON = "mdi:bed-clock"`。
      - 常量 `REFERENCED_ENTITIES: frozenset[str]`，覆盖 4-view 用到的全部 `sensor.sleep_classifier_*` 实体（≤ 20 个）。
      - 纯函数 `build_dashboard_config() -> dict[str, Any]`：返回 4-view 字典结构（Tonight / Stage / Learning / Diagnostics），与 `examples/lovelace-sleep-dashboard.yaml` 等价。
    - 不读外部文件，常量内嵌；不引入 yaml 运行时依赖。
    - _Requirements: 8.6_

  - [~] 4.14 静态测试 lovelace_template 引用完备性（P8.1）
    - 新增 `tests/test_lovelace_template.py`：
      - 断言 `REFERENCED_ENTITIES ⊆ SleepStatePublisher.ENTITY_IDS ∪ LearningPanelPublisher.ENTITY_IDS`（从两个 publisher 模块导入声明的 entity_id 集合）。
      - 断言 `build_dashboard_config()["views"]` 长度等于 4，title 与 url_path 正确。
    - **Validates: Requirements 8.6, P8.1**

  - [~] 4.15 编写 SleepStatePublisher / LearningPanelPublisher sensor 契约 snapshot 测试（PR2）
    - 新增 `tests/test_sensor_contract.py`：硬编码 v2.0.3 的 20 个 `sensor.sleep_classifier_*` entity_id 与 attribute schema 作为 snapshot；断言两个 publisher 当前声明严格 == snapshot；任何重命名 / 删除即 fail。
    - **Validates: PR2.1, PR2.2**

- [~] 5. Checkpoint - 新增 src/ 模块完成
  - Ensure all tests pass, ask the user if questions arise.

- [ ] 6. Web UI 集成与 config schema
  - [~] 6.1 在 sleep_classifier/web_ui.py 增加 onboarding wizard 路由
    - 增加：
      - `GET /onboarding`：返回 wizard SPA HTML（4 步 step state 在同一页内），文案从 `translations/{locale}.yaml` 的 `onboarding.*` 命名空间读取，`Accept-Language` 决定 locale。
      - `GET /api/onboarding/candidates`：调用 `ha_api_client._fetch_states()` + `onboarding_scanner.filter_candidates`，返回 JSON 候选列表；缓存 60s。
      - `POST /api/onboarding/save`：写 `web_ui_overrides.json` 的 `sleep_stage_source` + 设备槽位 + `onboarding_skipped=true`；走 `atomic_write_json`。
      - 在 `index` handler 顶部增加重定向：`web_ui_overrides.json` 不存在 / `sleep_stage_source` 为空 → `web.HTTPFound("onboarding")` 相对路径（保持 ingress 契约）。
    - HA `/api/states` 不可达时 wizard step 2 显示「HA 未就绪」+「跳过 wizard 直接进 picker」按钮。
    - 0 候选时 step 2 显示 `docs/HARDWARE.md` CTA。
    - 最后一步显式 dry_run 安全提示；前端发出 `confirm_disable_dry_run=true` 时后端才写 `dry_run=false`，否则保持 `true`。
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8_

  - [~] 6.2 编写 onboarding wizard 步骤渲染与状态恢复测试
    - 新增 `tests/test_onboarding_steps.py`：用 `aiohttp.test_utils` 启动 web_ui，断言：
      - step 1–4 HTML 渲染包含对应文案 key 命中。
      - `Accept-Language: zh-cn` 渲染中文文案；`Accept-Language: fr` 回退英文。
      - `web_ui_overrides.json` 不存在时 `GET /` 重定向到 `/onboarding`（302）；存在且包含 `sleep_stage_source` 时不重定向。
      - HA states 不可达（mock 抛 `HAAPIError`）时 step 2 渲染降级文案 + 跳过按钮。
    - **Validates: Requirements 7.1, 7.2, 7.5, 7.7, 7.8**

  - [~] 6.3 编写 onboarding wizard 完成幂等与 dry_run 安全测试（Property 7 + Property 8）
    - 新增 `tests/test_onboarding_idempotent.py`：参数化用户连续完成 wizard N ∈ [1, 10] 次，每次任意选择不同 `sleep_stage_source` 与槽位组合，断言最终 `web_ui_overrides.json` 内容只与最后一次输入一致。
    - 新增 `tests/test_onboarding_dry_run_safety.py`：参数化 wizard 输入序列（含中途刷新、跳步、随机选择），断言 `dry_run` 字段始终为 `true`，仅当 payload 含 `confirm_disable_dry_run=true` 时才写 `false`。
    - **Property 7: Onboarding wizard 完成幂等**
    - **Property 8: dry_run 默认安全**
    - **Validates: Requirements 7.6, 7.8**

  - [~] 6.4 在 sleep_classifier/web_ui.py 增加 Lovelace dashboard importer 路由
    - 增加 `POST /api/dashboard/import`：
      - body 形如 `{"confirm_overwrite": false}`。
      - 通过共享的 `HAAPIClient` 实例调用 `lovelace_dashboards()` → 检测同名 `url_path == "sleep-classifier"`：
        - 已存在且未 `confirm_overwrite` → 409 `{"existing": true}`。
        - 已存在且 `confirm_overwrite=true` → 调用 `lovelace_create_dashboard`（如不存在）+ `lovelace_save_config(build_dashboard_config())`。
      - 半成功补偿：`save_config` 失败时 `lovelace_dashboards/delete` 回滚已新建的 dashboard，再返回 502。
      - 4xx/5xx 响应体包含具体错误摘要 + 「手动复制 YAML」回退 hint。
      - 成功响应体包含相对路径链接（不使用绝对 `/lovelace/...`）。
    - 后端不重复做「需先看过 UI 对话框」二次校验，仅以 `confirm_overwrite=true` 为权威信号（R8.3a）。
    - _Requirements: 8.1, 8.2, 8.3, 8.3a, 8.4, 8.5_

  - [~] 6.5 编写 dashboard importer 4 种组合 + 回滚测试
    - 新增 `tests/test_dashboard_import.py`：mock `HAAPIClient`，参数化 `(existing, confirm_overwrite) ∈ {True, False}²`：
      - `(False, *)` → 201 + `lovelace_create_dashboard` 被调用。
      - `(True, False)` → 409 + 不调用 `save_config`；mock 的 dashboard 内容保持原样（P8.2 断言）。
      - `(True, True)` → 201 + 调用 `save_config`。
      - 半成功（save_config 抛异常）→ 502 + `delete` 被调用回滚；返回体含错误摘要。
      - 成功响应体的链接为相对路径（regex `^[^/]` 起始或不以 `/lovelace` 开头）。
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5, P8.2**

  - [~] 6.6 在 sleep_classifier/web_ui.py 增加 telemetry 开关与 upgrade banner 路由
    - 增加：
      - `GET /api/telemetry/status` / `POST /api/telemetry/toggle`：读写 `web_ui_overrides.json` 的 `telemetry_enabled`；切到 false 时调用注入的 `TelemetryReporter.disable()`（≤ 30 秒内停 task + 删 install_id）。
      - `GET /api/upgrade/status`：从 `last_upgrade_check.json` 读 `{available, latest, url}` 返回；无文件时返回 `{"available": false}`。
      - 在 index 模板顶部 sticky 区渲染 telemetry 开关（旁边一行字 + PRIVACY.md 链接）与 upgrade banner（`available=true` 时显示）。
    - 路由全部走 ingress 相对路径；Web UI 不直接发起 HTTP 出站，由 `TelemetryReporter` / `UpgradeNotifier` 后台 task 负责。
    - _Requirements: 6.5, 6.6, 9.2, 9.3_

  - [~] 6.7 静态守护 web_ui.py 不直接 Path.write_text 写 /data（PR3）
    - 新增 `tests/test_no_direct_write_text.py`：用 `ast` 解析 `sleep_classifier/web_ui.py` 与 `src/*.py`（排除 `_io_utils.py`），断言不存在「`Path(...).write_text(...)` 且参数路径前缀含 `/data`」的调用；任意命中即 fail。
    - **Validates: PR3.3**

  - [~] 6.8 更新 sleep_classifier/config.yaml schema
    - 在 `options:` 块追加：
      ```yaml
      telemetry_enabled: false
      upgrade_notifications_enabled: true
      ```
    - 在 `schema:` 块追加：
      ```yaml
      telemetry_enabled: "bool?"
      upgrade_notifications_enabled: "bool?"
      ```
    - 不修改既有字段类型 / 默认值（PR6.2）。
    - _Requirements: 6.1, 9.3, PR6.1, PR6.2_

  - [~] 6.9 编写 v2.0.3 持久化向后兼容测试（PR3）
    - 新增 `tests/fixtures/v2.0.3/web_ui_overrides.json` 与 `tests/fixtures/v2.0.3/user_preferences.json`（拷贝 v2.0.3 真实结构样例）。
    - 新增 `tests/test_persistence_backcompat.py`：加载 v2.0.3 fixture，断言不抛 schema 异常；新字段经 `apply_v2_1_0_defaults` 后取「最隐私友好」默认值。
    - **Validates: PR3.1, PR3.2**

- [ ] 7. 主入口接入与 CI 流水线
  - [~] 7.1 集成 telemetry / upgrade task 到 scripts/run_ha_smart_service.py
    - 在启动序列中注册：
      ```python
      telemetry_task = asyncio.create_task(telemetry_reporter.run(), name="telemetry_reporter")
      upgrade_task = asyncio.create_task(upgrade_notifier.run(), name="upgrade_notifier")
      ```
    - 在主进程 SIGTERM 处理路径（既有 `_shutdown` / 退出钩子）中 `task.cancel()` + `await asyncio.gather(*tasks, return_exceptions=True)`，确保 ≤ 10 秒内退出（PR5.2）。
    - 从 `web_ui_overrides.json` 读 `telemetry_enabled` / `upgrade_notifications_enabled` 决定模块构造参数。
    - 不修改既有 `tini -g` + `wait -n` 链路；不修改 `run.sh`。
    - _Requirements: 6.8, 9.1, PR5.1, PR5.2_

  - [~] 7.2 编写 run_ha_smart_service 启动 / 停机集成测试
    - 在 `tests/test_smart_sleep_service_telemetry_integration.py` 新增（命名遵循「跨模块集成测试」约定）：mock HA + mock telemetry endpoint + mock GitHub API，跑 `--dry-run --duration 2`，断言：
      - 两个新 task 被 `asyncio.create_task` 注册（按 name 检查）。
      - SIGTERM（用 `task.cancel()` 模拟）≤ 10 秒内主进程退出，新 task 也被 cancel。
      - `enabled=false` 状态下两个 task 立即 return，无 HTTP 出站（与 Property 4 相互印证）。
    - **Validates: Requirements 6.8, 9.1, PR5.2**

  - [x] 7.3 调整 pyproject.toml（optional extras + coverage gate）
    - 增加：
      ```toml
      [project.optional-dependencies]
      telemetry = ["sentry-sdk>=2.0.0"]
      ```
    - 在 `[tool.coverage.report]` 增加 `fail_under = 92`（PR1.2）。
    - 不修改 `requirements-runtime.txt`，不改既有 `[project] dependencies`（PR4.1）。
    - 不引入 hypothesis 至 `requirements.txt`（与本 spec 既有 tech.md 状态一致；本期 correctness properties 用普通 pytest 表达）。
    - _Requirements: PR1.2, PR4.1, PR4.2_

  - [~] 7.4 扩展 .github/workflows/test.yml
    - 矩阵确保 Python `[3.10, 3.11, 3.12]`，`fail-fast: false`。
    - 在 `pytest` 步骤前后增加独立 step 调用：
      - `python scripts/sync_version.py --check`
      - `python scripts/check_branding.py`
      - `python scripts/check_translations.py`
      - `python scripts/check_medical_links.py`
      - `python scripts/check_funding.py`
    - 在 `pytest` 步骤启用 `--cov=src --cov=scripts --cov-fail-under=92`。
    - 集成 `lycheeverse/lychee-action` 跑 markdown link 检查（README、DOCS、`docs/*.md`），与 R5 守护互补。
    - _Requirements: 4.1, 4.2, 4.6, PR1.1, PR1.2_

  - [~] 7.5 新增 .github/workflows/addon-build.yml
    - 触发：push 到 `main`、PR 到 `main`。
    - 步骤：`actions/checkout` → `bash sleep_classifier/prepare.sh` → `git diff --exit-code sleep_classifier/rootfs/`（dirty 即 fail）→ `docker/setup-buildx-action` → `docker buildx build --platform linux/arm64,linux/amd64 sleep_classifier/`（不 push）。
    - 镜像体积守护 step：`docker images --format '{{.Size}}' sleep_classifier:test` 与 `.github/baseline_image_size.txt` 对比，超过基线 × 1.10 即 fail（PR4.3）。如基线文件不存在则首次 PR 创建之。
    - _Requirements: 4.3, 4.7, PR4.3_

  - [~] 7.6 新增 .github/workflows/release.yml
    - 触发：push tag 形如 `v[0-9]+.[0-9]+.[0-9]+`。
    - 步骤：
      - `python scripts/sync_version.py`（写入 `setup.py` / `config.yaml` / `__init__.py`）+ `git diff --exit-code`（如有 diff 自动 commit `chore: sync version`）。
      - 抽 CHANGELOG 段落：`sed -n '/^## \['"$VERSION"'\]/,/^## \[/p' CHANGELOG.md | sed '$d'`；找不到对应版本即 fail。
      - 创建 GitHub Release（`actions/create-release` 或 `softprops/action-gh-release`）+ 上传 `sleep_classifier/` zip 作为 release asset。
    - _Requirements: 4.4, 4.5, 4.6_

  - [~] 7.7 静态校验 workflows 文件结构
    - 新增 `tests/test_workflows_present.py`：解析 `.github/workflows/*.yml`，断言三个工作流存在且包含必需的 `jobs.*` 名称。
    - 新增 `tests/test_workflows_matrix.py`：断言 `test.yml` 矩阵覆盖 `[3.10, 3.11, 3.12]` 且 `fail-fast: false`。
    - 新增 `tests/test_workflows_buildx.py`：断言 `addon-build.yml` 调用 `docker/setup-buildx-action` + `--platform linux/arm64,linux/amd64`。
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.7**

  - [~] 7.8 跑 prepare 同步 src/ scripts/ 到 sleep_classifier/rootfs/
    - 执行 `bash sleep_classifier/prepare.sh`（Windows 上 `prepare.bat`），确保 `sleep_classifier/rootfs/` 包含本期所有新增 / 修改的 `src/*.py` 与 `scripts/*.py`，避免 Add-on 容器构建时拉到旧代码（structure.md 关键不变量）。
    - `git status` 检查 `rootfs/` 与 `src/`、`scripts/` 一致；不一致即重跑 prepare。
    - _Requirements: 4.7, PR5.1_

- [~] 8. Final checkpoint - 整合验证
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- 标记 `*` 的子任务为可选测试任务，可按 MVP 节奏跳过；核心实现任务（不带 `*`）必须完成。
- 每条 sub-task 在 `_Requirements:` 行标注其覆盖的具体 acceptance criteria 编号（granular，非 user story 级别），便于 PR 追溯。
- 11 条 correctness properties 按设计文档编号显式分布在以下任务中，保证每条 property 至少有一个对应 pytest 测试文件：
  - Property 1 → 1.4
  - Property 2 → 4.3
  - Property 3 → 4.6
  - Property 4 → 4.7（telemetry）+ 4.11（upgrade）
  - Property 5 → 4.10
  - Property 6 → 4.8
  - Property 7 → 6.3
  - Property 8 → 6.3
  - Property 9 → 1.9
  - Property 10 → 1.9
  - Property 11 → 1.2
- 由于本仓库 `tests/` 当前已无 hypothesis 使用且本期不引入新依赖，property tests 改用「`pytest.mark.parametrize` 大规模穷举 + 显式不变量循环」表达，与 design 中「PBT」语义等价但不依赖第三方库。
- 多个 sub-task 都会修改 `sleep_classifier/web_ui.py`（6.1 / 6.4 / 6.6）与 `README.md`（2.9 直接、2.1 / 2.2 间接）；下方 dependency graph 已把这些任务放进不同 wave 避免冲突。
- 所有出站任务都在 `try/except Exception` 包裹下跑无限循环；禁止异常冒泡到 `asyncio.gather`（design §5.1 关键纪律）。

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.3", "1.5", "1.6", "1.7", "1.8", "2.1", "2.8", "4.1", "7.3"] },
    { "id": 1, "tasks": ["1.2", "1.4", "1.9", "2.2", "2.3", "2.5", "2.6", "2.7", "4.2", "4.4", "4.13"] },
    { "id": 2, "tasks": ["2.4", "2.9", "4.3", "4.5", "4.9", "4.14", "4.15"] },
    { "id": 3, "tasks": ["2.10", "4.6", "4.7", "4.8", "4.10", "4.11", "4.12"] },
    { "id": 4, "tasks": ["2.11", "6.1", "6.4", "6.8"] },
    { "id": 5, "tasks": ["6.2", "6.3", "6.5", "6.6", "6.9"] },
    { "id": 6, "tasks": ["6.7", "7.1", "7.4", "7.5", "7.6"] },
    { "id": 7, "tasks": ["7.2", "7.7", "7.8"] }
  ]
}
```
