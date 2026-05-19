# 医疗免责声明（Medical Disclaimer）

> 适用对象：Sleep Classifier Home Assistant Add-on（以下简称「本 Add-on」）
> 维护者：[LiangyuLu-lly](https://github.com/LiangyuLu-lly)
> 关联文档：[`PRIVACY.md`](PRIVACY.md) · [`SECURITY.md`](SECURITY.md) · [`README.md`](README.md) · [`sleep_classifier/DOCS.md`](sleep_classifier/DOCS.md)

---

## English Summary (TL;DR)

Sleep Classifier is **not** a medical device, diagnostic tool, or clinical
decision-support system. All outputs — sleep stages, sleep debt estimates,
quality scores, apnea-related signals, recommended bedtime — are derived from
consumer-grade sensors and heuristic algorithms, with no FDA / NMPA / CE
medical clearance and no validation against polysomnography (PSG) gold
standards. The add-on does **not** diagnose, treat, cure, or prevent any
disease. **Always consult a licensed sleep physician before changing your
sleep routine, medications, or treating suspected sleep disorders.** If you
experience persistent insomnia, severe daytime sleepiness, observed apnea, or
any other sleep-related medical concern, seek professional medical care.

---

## 1. 本 Add-on 不是什么

- **不是医疗器械**：未通过 FDA、NMPA（国家药监局）、CE Medical、TGA、PMDA
  等任何医疗器械监管机构的认证或备案。
- **不是诊断工具**：不能用于诊断或排除任何睡眠障碍，包括但不限于失眠症
  （insomnia）、阻塞性睡眠呼吸暂停（OSA）、中枢性睡眠呼吸暂停（CSA）、
  不宁腿综合征（RLS）、嗜睡症（narcolepsy）、昼夜节律失调
  （circadian rhythm disorders）、REM 睡眠行为障碍（RBD）。
- **不是临床决策支持系统**：不能替代医生的临床判断，输出不应被纳入电子病历
  或处方决策流程。
- **不是治疗设备**：自动调节卧室环境的功能（灯光、空调、加湿器、风扇）属于
  生活方式辅助，不构成对任何疾病的治疗、缓解、预防或康复。
- **未经 PSG 校准**：本 Add-on 订阅用户已有的 HA 睡眠分期实体（手环 / 雷达 /
  手表 / 第三方 add-on），其底层数据均为消费级传感器输出，与多导睡眠图
  （polysomnography, PSG）金标准之间存在系统性偏差，本项目从未做过临床验证。

## 2. 输出的性质

| 输出 | 数据来源 | 局限性 |
|---|---|---|
| 睡眠阶段（AWAKE / LIGHT / DEEP / REM） | 用户已有的消费级硬件 | 不同硬件算法差异 ≥ 20%；不应作为临床分期 |
| 推荐入睡时间 | 加权中位数 + k-NN 邻居池 | 仅反映你历史上「质量分较高的夜晚」的统计模式，不代表医学最优 |
| 睡眠质量分（0–100） | 启发式公式（DEEP/REM 比例 + 碎片化惩罚） | 是项目自定义指标，不与任何临床量表（PSQI、ESS、ISI）等价 |
| 睡眠债（sleep debt） | NSF / AAP 年龄相关推荐时长 - 实际睡眠时长 | 推荐时长本身是流行病学统计，不构成针对个体的医学建议 |
| 唤醒窗口建议 | 浅睡阶段挑选 + dawn-light ramp | 不针对任何医学状况设计 |
| 呼吸暂停 PoC（v1.6.0+） | 纯函数式启发式检测，**未接入主流程** | 误报率与漏报率未做临床验证；任何「检测到的事件」均不构成 OSA 诊断 |

**关键原则**：所有数值输出仅供你个人参考与生活方式调整，**不应**用于
自我诊断、自我用药、调整既有处方、决定是否就医、给他人提供医学建议。

## 3. 何时必须就医

如果出现以下情况，请**立即停止**依赖本 Add-on 的输出做生活方式判断，并寻求
专业医学帮助：

- **持续 ≥ 1 个月的失眠**（入睡困难、夜间频繁觉醒、过早觉醒）。
- **白天严重嗜睡**：开车、工作、看书时反复无法保持清醒，或 Epworth 嗜睡量表
  得分 ≥ 11。
- **被同床者观察到呼吸暂停**：响亮鼾声 + 间歇性呼吸停止 + 喘息惊醒。
- **晨起严重头痛、口干、心跳异常**。
- **REM 睡眠行为障碍**：在梦中出现喊叫、肢体剧烈活动、自伤或伤及他人。
- **猝倒、幻觉、入睡前麻痹**等可能的嗜睡症前兆。
- **任何可能涉及心血管 / 呼吸 / 神经系统**的症状（胸痛、严重心律不齐、抽搐）。

> 紧急情况请立即拨打当地急救电话（中国大陆 120、美国 911、欧盟 112）。

## 4. 推荐的就医路径

- **基层 / 全科医生**：先由家庭医生 / 全科医生评估，必要时转诊。
- **睡眠专科**：综合性医院的睡眠中心、神经内科或呼吸科可提供 PSG 检查。
- **行为治疗**：失眠症的一线推荐是「失眠认知行为疗法」（CBT-I），不是
  本 Add-on 的环境调节。
- **可选远程评估**：部分地区提供居家睡眠呼吸监测（HSAT）作为 PSG 的初筛
  替代，仍需由医生开具与解读。

## 5. 不构成的事项

本 Add-on 不构成、亦不应被理解为：

1. 医生 / 注册护士 / 心理治疗师 / 任何持牌医疗执业者的专业意见。
2. 处方药、非处方药、保健品、医疗器械的使用建议或调整建议。
3. CPAP / BiPAP / 口腔矫正器 / 助眠光疗设备等医疗设备的替代品。
4. CBT-I（失眠认知行为疗法）、刺激控制、睡眠限制等行为疗法的替代品。
5. 心理健康咨询、心理治疗、精神科评估的替代品。
6. 任何形式的远程问诊、在线诊断、AI 辅助诊断服务。
7. 医学研究、临床试验、流行病学研究的数据来源。

## 6. 数据准确性的限制

- **硬件层**：用户选择的睡眠分期硬件（消费级手环 / 雷达 / 手表）的内部
  算法不公开、不一致、可能随固件升级悄悄变更；本 Add-on 仅消费其输出。
- **传感器层**：温湿度计、光照传感器的精度受位置、校准、衰减影响。
- **算法层**：本项目的偏好学习器是启发式 + 加权中位数 + k-NN，**没有**做过
  临床随机对照试验（RCT），没有发表过 peer-reviewed 论文。
- **个体差异**：算法默认的年龄相关睡眠推荐时长来自人群统计，**不代表**
  你个人的最优值；睡眠需求受遗传、激素、用药、季节等多因素影响。
- **误差累积**：长期偏好学习的输出可能放大消费级硬件的系统性偏差。

## 7. 用户责任

使用本 Add-on 即表示你理解并接受：

- 你**自主决定**是否采用本 Add-on 的环境调节建议；维护者不对该决定的后果
  负责。
- 你**自主决定**是否就睡眠问题就医，本 Add-on 的输出不影响该决定的合理性。
- 你**自主选择**接入哪些睡眠分期硬件，硬件的医疗合规性由其制造商承担。
- 你**自主管理**家庭中其他成员（包括儿童、老人、敏感人群）是否暴露于本
  Add-on 调节的环境，并自行评估其安全性。
- 你**不会**把本 Add-on 的输出作为对他人的医学建议、健康教育材料、咨询
  服务的依据。

## 8. 关于「医学顾问」与未来背书

截至 v2.1.0，本项目**没有**任何执业医生、睡眠技师、临床研究者作为正式
顾问。即便未来招募到医学顾问（详见 [`README.md`](README.md) 的
"Medical advisors" 段），其参与也**不会**改变本免责声明的核心内容：

- 顾问加入只表示对项目方向的非临床建议。
- 不构成对任何具体输出 / 算法 / 设备组合的医学背书。
- 不暗示项目获得了任何医学认证。

任何未经书面同意的医学专业人士姓名、机构、文章引用都**禁止**出现在项目
任何文档中。

## 9. 与其它 Add-on / 产品的关系

- 与 Apple Watch、Fitbit、小米手环、Withings、Oura Ring 等消费级睡眠设备
  无任何商业 / 医学合作关系。
- 与 Seeed R60ABD1 等毫米波雷达硬件无任何商业 / 医学合作关系；硬件推荐
  仅出于工程兼容性，详见 [`docs/HARDWARE.md`](docs/HARDWARE.md)。
- 与任何 CPAP / BiPAP 设备厂商、睡眠中心、远程问诊平台无合作。
- 与 HA 社区中其它睡眠相关 add-on（如 sleep_as_android 桥接）的兼容仅是
  数据层面，不构成医学整合。

## 10. 司法管辖与适用法律

- **中国大陆**：本 Add-on 不属于《医疗器械监督管理条例》定义的医疗器械，
  亦不开展互联网医疗服务。其使用应符合《广告法》对健康相关陈述的
  约束（详见 [`docs/HARDWARE.md`](docs/HARDWARE.md) 的 affiliate disclosure
  与 [`README.md`](README.md) 的对应段落）。
- **欧盟**：本 Add-on 不属于 MDR 2017/745 定义的医疗器械；GDPR 相关声明
  详见 [`PRIVACY.md`](PRIVACY.md)。
- **美国**：本 Add-on 不属于 FDA 21 CFR Part 820 监管的医疗器械；任何
  「健康相关」陈述受 FTC 真实广告法律约束，详见
  [`docs/HARDWARE.md`](docs/HARDWARE.md) 的 affiliate disclosure。
- **其它司法管辖**：以本地法律为准；维护者不承担因当地法规差异导致的
  使用风险。

## 11. 本免责声明的更新

- 本免责声明随版本演进更新，重大变更（新增临床功能、移除 PoC 模块、
  纳入医学顾问）会在 [`CHANGELOG.md`](CHANGELOG.md) 以 `[Medical]`
  前缀标记。
- 任何对「医学性陈述」的修改，会同时更新 README、DOCS 与本文档；
  CI 中的 `scripts/check_medical_links.py` 守护「医学关键字附近必有
  本声明链接」（详见 spec
  [`commercial-readiness-v2.1.0/requirements.md`](.kiro/specs/commercial-readiness-v2.1.0/requirements.md)
  的 Requirement 5.6 / 5.7）。

## 因果归因免责（v3.0.0+）

> 适用版本：v3.0.0 起；仅当用户的 `causal_attribution_enabled = true`
> （默认 true，可在 Add-on 配置中关闭）且 v3.0.0 因果归因模块
> **Causal Attribution Engine（CAE）** 处于 `healthy` 时，本节适用。
> 本节扩展自 [Requirement 6.5](.kiro/specs/algorithmic-moat-v3.0.0/requirements.md)
> ：「归因解释为相关性 + 因果模型推断，非临床诊断」。

v3.0.0 起，本 Add-on 在每晚醒来后会通过 CAE 在 Lovelace
（`sensor.sleep_classifier_attribution_*`）暴露形如「今晚最大干扰是
 X，因果效应估计 Y（95% CI `[a, b]`）」的归因解释。**该输出属于统计
推断，不构成临床诊断、不替代医生意见**，具体边界如下：

1. **本质是相关性 + 因果模型推断，不是临床诊断**：CAE 在 6 维干扰
   因子（HRV、噪声、光、温度漂移、湿度漂移、体动）的有向无环图
   （DAG）上做反事实推断，输入仅为消费级传感器读数 + 用户偏好历史，
   **未经 PSG 校准**、**未做临床随机对照试验（RCT）**，亦未发表
   peer-reviewed 论文。任何「这个因子让你今晚睡眠质量下降 X 分」
   的结论，仅在 DAG 假设成立、可观测因子完整、IID（独立同分布）等
   模型前提下有效；DAG 错配 / 混淆变量未观测 / 季节切换 / 重大生
   活变化等情况都会让该结论偏离真实因果（与 §6「数据准确性的限制」
   一致）。
2. **95% CI 是 bootstrap 重采样的统计区间，不是临床置信度**：CAE
   输出的 `[lower, upper]` 区间来自对历史 session 做 ≥ 200 次
   bootstrap 重采样得到的 effect 分布的 2.5 / 97.5 百分位数，反映
   **估计量自身的抽样波动**，**不**反映临床诊断或医疗决策的可靠性。
   当区间跨 0 时，add-on 会自动在 `explanation_zh` 后追加「（统计
   显著性弱）」提示；此时该因子的归因结论尤其不应被用于医学判断。
3. **不构成医学建议**：用户**不应**基于「最大干扰是噪声」「呼吸率
   因子 effect 显著」等 CAE 输出去自我诊断睡眠呼吸障碍、调整既有
   处方、决定是否就医。任何持续性睡眠困扰仍应按 §3「何时必须就医」
   的指引寻求专业医学帮助。
4. **CAE 输出全部本地生成、永不上传**：归因结果落盘于
   `/data/causal_factors.jsonl`，与原始 `install_id` 解耦（仅存
   `sha256(install_id)`，R14.2）；详见 [`PRIVACY.md`](PRIVACY.md)
   的「v3.0.0 算法栈数据流」段落（R14.3）。即便用户主动开启
   `telemetry_enabled = true`，CAE 的因子值、effect、CI 也**不**进入
   遥测 payload。

简而言之：**CAE 是「为什么没睡好」的统计解释器，不是「你是否生病」
的诊断器**。如出现 §3 列举的就医信号，应直接寻求专业医学帮助，与
 CAE 当晚输出无关。

## 12. 联系方式

- 一般问题：GitHub Issues 加 `[medical]` 标签。
- 隐私担忧：见 [`PRIVACY.md`](PRIVACY.md) §11。
- 安全 / 漏洞披露：见 [`SECURITY.md`](SECURITY.md)。
- 行为准则相关：见 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)。
- 维护者邮箱：`liangyulu781@gmail.com`。
