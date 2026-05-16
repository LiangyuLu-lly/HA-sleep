# 贡献指南（Contributing Guide）

> 适用对象：Sleep Classifier Home Assistant Add-on
> 维护者：[LiangyuLu-lly](https://github.com/LiangyuLu-lly)
> 关联文档：[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) · [`SECURITY.md`](SECURITY.md) · [`PRIVACY.md`](PRIVACY.md) · [`MEDICAL_DISCLAIMER.md`](MEDICAL_DISCLAIMER.md)

---

## English Summary (TL;DR)

Thanks for contributing! Workflow: fork → branch off `main` → small focused PR.
Run `pytest --cov=src --cov=scripts` (≥ 92% coverage gate) and
`bash sleep_classifier/prepare.sh` before pushing — both are enforced in CI.
Commit messages follow Conventional Commits (`feat:`, `fix:`, `docs:`,
`refactor:`, `test:`, `ci:`, `chore:`). All contributors are expected to follow
the [Code of Conduct](CODE_OF_CONDUCT.md). Security issues go through
[`SECURITY.md`](SECURITY.md), not public issues.

---

## 0. 在动手之前

- 阅读 [`README.md`](README.md) 与 [`sleep_classifier/DOCS.md`](sleep_classifier/DOCS.md) 理解产品定位。
- 阅读 [`.kiro/steering/tech.md`](.kiro/steering/tech.md)、[`structure.md`](.kiro/steering/structure.md)、
  [`product.md`](.kiro/steering/product.md)、[`language.md`](.kiro/steering/language.md)
  四份 steering 文档；它们是**硬约束**（PR 评审会按此 reject）。
- 不接受未经讨论的破坏性改动（删除模块、重命名 sensor、引入新运行时依赖）。
  请先开 issue 或 draft PR 讨论。

## 1. PR 工作流

1. **Fork + 分支**：从 `main` 切新分支，命名 `feat/<short>`、`fix/<short>`、
   `docs/<short>` 等；不要直接在 fork 的 main 上提交。
2. **小步提交**：单 PR 聚焦一个目标，行数控制在 ≤ 400（含测试）；超出请拆分。
3. **本地自检**：参见 §3 的「最低本地门槛」。
4. **推送 + 开 PR**：base 分支 `LiangyuLu-lly/HA-sleep:main`，PR 标题遵守
   §2 的 commit 约定（首行 ≤ 70 字）。
5. **回应评审**：维护者通常 7 天内首次回复；若超时请在 PR 中 `@LiangyuLu-lly`。
6. **CI 必须全绿**：详见 §4，任意 job 红即不合并。
7. **合并方式**：由维护者使用 `Squash and merge`，commit message 取 PR 标题 +
   PR 描述的 summary 段。

> **不要**修改其他 PR 作者的提交（`--force-push` 别人的分支）；如需协作，
> 请直接在该 PR 下评论或新开一个 PR。

## 2. Commit message 约定（Conventional Commits）

```
<type>(<scope>): <subject>

<body, 可选>

<footer, 可选>
```

- `<type>` ∈ `feat | fix | docs | refactor | test | ci | chore | perf | style`。
- `<scope>` 推荐写模块名（`preference_learner`、`web_ui`、`addon-build`）。
- `<subject>` 一句话祈使句，首字母小写，结尾不加句号。
- 多行 body 用空行隔开，描述「为什么这么做」而非「做了什么」。
- 关闭 issue：在 footer 写 `Closes #123`。
- 引用 spec：在 footer 写 `Spec: commercial-readiness-v2.1.0` 等。

示例：

```
feat(telemetry_reporter): add 24h opt-in payload publisher

Implements design §3.6 for v2.1.0 commercial-readiness.
Default off; payload self-checks for entity_id leakage.

Spec: commercial-readiness-v2.1.0
Closes #42
```

> CI 暂未强制 lint commit message，但维护者在 squash 合并时会重写为合规
> 格式；请尽量自己写好以保留原意。

## 3. 本地最低门槛

在推送前**必须**通过以下检查：

```bash
# 1. 单元 + 属性测试 + 覆盖率门槛（≥ 92%）
pytest --cov=src --cov=scripts --cov-fail-under=92

# 2. 同步 Add-on rootfs（任何 src/ scripts/ training_config/ 改动后必跑）
bash sleep_classifier/prepare.sh        # Linux / macOS
sleep_classifier\prepare.bat            # Windows

# 3. 静态守护脚本（CI 也会跑，本地先过避免反复 push）
python scripts/sync_version.py --check
python scripts/check_branding.py
python scripts/check_translations.py
python scripts/check_medical_links.py
python scripts/check_funding.py
```

如果任一步失败，**修复后再推送**；CI 不接受「先开 PR 等绿了再说」式的反复
触发。

### 3.1 关于 `prepare.sh`

`sleep_classifier/Dockerfile` 的 docker context 仅限于 `sleep_classifier/`，看不
到外面。所以 `src/`、`scripts/`、`training_config/` 的任何改动都**必须**先跑
`prepare` 把内容镜像到 `sleep_classifier/rootfs/`，否则 HA Supervisor 拉到的是
旧代码。CI 在 `addon-build.yml` 中用 `git diff --exit-code` 守护此契约：rootfs
脏即 fail。

### 3.2 关于覆盖率

- 全仓覆盖率门槛 ≥ 92%（`pyproject.toml: [tool.coverage.report] fail_under = 92`）。
- 新增模块单文件覆盖率 ≥ 90%。
- 新增功能必须配镜像式 `tests/test_<module>.py`；既有测试不删不改，仅追加或修复。
- 异步测试用 `async def test_*` 即可，`pytest-asyncio` 已配置 `asyncio_mode = "auto"`。

## 4. CI 流水线（你需要全绿）

`.github/workflows/` 下三个工作流，PR 命中 `main` 时全部触发：

| 工作流 | 关键 job |
|---|---|
| `test.yml` | Python 3.10 / 3.11 / 3.12 矩阵跑 `pytest --cov`，外加 `sync_version.py --check`、`check_branding.py`、`check_translations.py`、`check_medical_links.py`、`check_funding.py`、markdown link checker |
| `addon-build.yml` | `prepare.sh` → `git diff` 守护 → `docker buildx build --platform linux/arm64,linux/amd64`；并校验镜像体积 ≤ 基线 × 1.10 |
| `release.yml` | 仅 tag `v*.*.*` 触发，自动同步版本号、抽 CHANGELOG 段落、创建 GitHub Release |

CI 默认 `fail-fast: false`，所以一次推送你能一次性看到所有版本的失败信息，
不必反复 push。

## 5. 设计 / 测试纪律

- **代码风格**：类型注解齐全，`from __future__ import annotations`，docstring
  使用 reStructuredText（已有模块的风格不要混改）。
- **禁止阻塞主事件循环**：文件 I/O 必要时用 `asyncio.to_thread`。
- **HA 交互只走 `src/ha_api_client.py`**：不要直接 `aiohttp.request` 命中 HA。
- **`/data/*.json` 必须走 `src/_io_utils.atomic_write_json`**：禁止 `Path.write_text`。
- **HA 异常 catch 顺序**：`HAAuthError` 子类在前、`HAAPIError` 父类在后。
- **`dry_run=True` 默认开启**：不要在新代码中默认 `False`。
- **新增 sensor**：仅追加到 `SleepStatePublisher` / `LearningPanelPublisher`，
  不重命名、不删除现有 20 个实体（PR2 不变量）。
- **持久化新字段**：必须 optional，缺失时回退安全默认值。
- **新增运行时依赖**：原则上只允许 `aiohttp`；其它依赖走 `[project.optional-dependencies]`
  的 extras 机制，不进 `requirements-runtime.txt`。

## 6. 文档与语言

- 文档语言：中文为主，重要文档（README / DOCS / spec / steering / legal）
  按 [`language.md`](.kiro/steering/language.md) 执行。
- 代码标识符（变量、函数、类、模块、entity_id、CLI 命令、环境变量）保持英文。
- 同一文件内不要混用中英文注释风格；跟随既有约定。
- 用户可见错误消息用英文（HA UI 的国际化由 translations pack 处理）。

## 7. Spec 驱动开发

本仓库使用 [Kiro spec workflow](.kiro/specs/)：requirements → design → tasks。
重大功能 / 重构请先在 `.kiro/specs/<feature>/` 下提交三份文档，再开 PR
实施。维护者会在 PR 评审中检查是否与 design 一致；偏离需要在 PR 描述中说明
理由。

## 8. 报告问题

| 问题类型 | 通道 |
|---|---|
| Bug / 期望行为 / 文档错别字 | GitHub Issues |
| 安全漏洞 | [`SECURITY.md`](SECURITY.md) 私下披露邮箱，**不要开公开 issue** |
| 隐私担忧 | [`PRIVACY.md`](PRIVACY.md) §11 投诉与申诉流程 |
| 行为问题（骚扰 / 歧视） | [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) 中的执行联系方式 |
| 医学准确性疑问 | [`MEDICAL_DISCLAIMER.md`](MEDICAL_DISCLAIMER.md) + GitHub Issues `[medical]` 标签 |

## 9. 行为准则

所有贡献者（包括维护者）受 [Contributor Covenant v2.1](CODE_OF_CONDUCT.md)
约束。简单说：**对人友善，对代码严格**。

## 10. 许可证

提交 PR 即表示你同意你的贡献按 [MIT License](LICENSE)（如仓库存在 LICENSE
文件）许可。维护者承诺现有 MIT License 下的功能永远不会被移到付费版（详见
[`docs/ROADMAP.md`](docs/ROADMAP.md) 的 Commercial roadmap 段）。
