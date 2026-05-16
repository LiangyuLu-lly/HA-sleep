# 安全披露政策（Security Policy）

> 适用对象：Sleep Classifier Home Assistant Add-on（以下简称「本 Add-on」）
> 维护者：[LiangyuLu-lly](https://github.com/LiangyuLu-lly)
> 关联文档：[`PRIVACY.md`](PRIVACY.md) · [`CONTRIBUTING.md`](CONTRIBUTING.md) · [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)

---

## English Summary (TL;DR)

If you discover a security vulnerability in this add-on, please **do not open a
public GitHub issue**. Email `liangyulu781+security@gmail.com` with a clear
reproduction, the affected version, and any logs you can share. We commit to a
first response within 7 calendar days. Coordinated disclosure timeline is up to
90 days; CVE assignment is requested via GitHub's private advisory flow when
applicable. Out-of-scope items are listed in §6 below.

---

## 1. 我们承诺什么

- **首次响应 ≤ 7 个自然日**：从你发出私下披露邮件起算，我们会在 7 天内确认收到、
  给出严重等级初判与下一步沟通节奏。若 7 天内未收到回复，请通过 GitHub 私信
  [@LiangyuLu-lly](https://github.com/LiangyuLu-lly) 跟进（仍**不要**在公开
  issue 中提及漏洞细节）。
- **协调披露窗口 ≤ 90 天**：从我们确认漏洞之日起，默认在 90 天内发布修复版本。
  若漏洞已被野外利用或正在造成持续伤害，会缩短窗口；若涉及上游依赖、HA
  Supervisor 或硬件厂商，可能需要协调延长，我们会与披露者实时同步进度。
- **致谢**：除非披露者明确要求匿名，我们会在 [`CHANGELOG.md`](CHANGELOG.md)
  对应版本与 GitHub Release notes 的「Security」段中署名致谢。
- **不起诉善意研究**：我们采用类「safe harbor」立场，对**遵守本政策**的安全
  研究人员不会发起任何法律追诉，包括但不限于 DMCA / CFAA 类条款。

## 2. 私下披露邮箱

> **请只通过此通道报告未公开的安全问题。**

- 邮箱：`liangyulu781+security@gmail.com`
- 主题前缀建议：`[security][sleep-classifier] <一句话摘要>`
- 邮件正文请包含：
  1. 受影响版本（`config.yaml: version` / `pyproject.toml: [project] version`，
     或 git commit hash）。
  2. 受影响组件（如 `web_ui.py`、`telemetry_reporter.py`、Add-on 容器边界等）。
  3. 重现步骤（指令、HTTP 请求示例、配置片段）。
  4. 影响评估（机密性 / 完整性 / 可用性 / 隐私 / 横向移动）。
  5. 是否愿意公开署名。
- 如需端到端加密，可在邮件中索取 PGP 公钥指纹；目前未默认提供 PGP，
  Gmail TLS 已经覆盖大多数场景。

## 3. 严重等级（粗略对齐 CVSS 3.1）

| 等级 | 示例 | 默认响应窗口 |
|---|---|---|
| Critical (9.0–10.0) | 容器逃逸、远程未授权代码执行、HA 长效令牌泄漏 | 修复 ≤ 14 天 |
| High (7.0–8.9) | 越权读写 `/data/*.json`、ingress 鉴权绕过 | 修复 ≤ 30 天 |
| Medium (4.0–6.9) | 拒绝服务（崩溃主循环）、敏感日志泄漏 | 修复 ≤ 60 天 |
| Low (0.1–3.9) | 不严重的信息泄漏、配置默认值不安全 | 下一发版周期内 |

最终等级由维护者与披露者协商确认，不强制以 CVSS 数字为准。

## 4. CVE 申请流程

1. 披露者私下报告 → 维护者确认问题真实存在且影响 v2.0.0 之后任意版本。
2. 维护者通过 GitHub 仓库的 **Security → Advisories → New draft advisory**
   创建私有 advisory。
3. 在 advisory 中点击 **Request CVE**，由 GitHub 作为 CNA 分配 CVE ID。
4. 修复版本发布后，advisory 公开，CVE 详情与受影响版本范围一并发布。
5. CHANGELOG 与 Release notes 链接到该 advisory。

如披露者希望从 MITRE 直接申请 CVE，我们也会配合提供 reference 与修复 commit。

## 5. 禁止公开 issue 报告

**未修复**的安全漏洞**禁止**通过以下渠道讨论：

- GitHub Issues / Discussions / Pull Requests。
- HA 社区论坛、Reddit、HN、X / Twitter、知乎、微博等公开平台。
- 仓库的任何公开聊天室。

若你不慎已在公开渠道发布，请：

1. 立即编辑或删除原帖。
2. 通过 `liangyulu781+security@gmail.com` 通知维护者。
3. 我们会在内部加速排期，并在修复发布后保留你的署名（前提是你要求公开）。

公开报告不会被忽略，但会被视为**降级处理**：我们会请求你转移到私下通道
继续协作，否则修复时间表不再受 §1 的承诺约束。

## 6. 范围（Scope）

### 在范围内
- 本 Add-on 自身代码（`src/`、`scripts/`、`sleep_classifier/`、`tests/`）。
- 默认 Add-on 镜像（基于 `python:3.11-alpine`）的构建产物。
- 默认 `requirements-runtime.txt` / `requirements.txt` 锁定的依赖（`aiohttp`
  及其 transitive deps）。
- 与 Home Assistant Supervisor 之间的 ingress / token / API 边界。

### 不在范围内
- HA Core 自身、HA Supervisor、HA OS（请向 [Home Assistant
  Security](https://www.home-assistant.io/security/) 报告）。
- 用户自行选择的睡眠分期硬件 / 第三方 HA 集成（如毫米波雷达 add-on）。
- 用户自托管的 Sentry / Glitchtip / 反向代理等运行环境。
- 社会工程学攻击（钓鱼维护者、伪造 PR 等）。
- DDoS / 流量级攻击（本 Add-on 默认无对外服务端口）。

## 7. 已知不会修复的「by design」

为避免重复披露，以下行为是**有意设计**而非漏洞，欢迎讨论但不会作为安全修复
处理：

- HA 长效令牌存储在 `SUPERVISOR_TOKEN` 环境变量中（Supervisor 强制约定）。
- `/data/web_ui_overrides.json` 在 add-on 私有 volume 中以 0644 写入（Supervisor
  挂载层已隔离同主机其它 add-on）。
- `dry_run = false` 状态下控制器会真实下发 HA 服务调用（用户自主选择）。

## 8. 漏洞修复后的发布节奏

- 中 / 高严重等级修复将以 patch 版本（如 `v2.1.1`）单独发布，并附带迁移说明。
- Critical 修复会同时回填到当前 main 与最近一个稳定 minor 分支。
- 所有安全修复在 [`CHANGELOG.md`](CHANGELOG.md) 中以 `### Security` 段落
  独立记录，并在 GitHub Release notes 顶部高亮。

## 9. 历史 advisories

- v2.1.0 之前：尚无已发布的安全 advisory。
- 后续记录见仓库 [Security Advisories](https://github.com/LiangyuLu-lly/HA-sleep/security/advisories)
  页面。

## 10. 参考资料

- [GitHub Security Advisory 文档](https://docs.github.com/en/code-security/security-advisories)
- [Home Assistant Security Policy](https://www.home-assistant.io/security/)
- [OWASP Vulnerability Disclosure Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Vulnerability_Disclosure_Cheat_Sheet.html)
