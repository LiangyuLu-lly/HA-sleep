# Requirements Document

> Spec: algorithmic-moat-v3.0.0
> Workflow: requirements-first（feature spec）
> 关联：替换 / 升级 v2.1.0 的可替代性较强部分（preference_learner、smart_environment_controller 决策路径），引入 4 个核心算法模块以建立技术护城河。

## Introduction

v2.1.0 之前 sleep_classifier 的核心算法（加权中位数 + k-NN + deadband + per-stage delta）任意工程师 2 周内可复刻，无技术壁垒。v3.0.0 通过 4 个独立但配套的算法方向建立可证明的技术护城河：

1. **Bayesian Active Optimization (BAO)** — 用高斯过程后验 + Thompson Sampling 替代被动加权中位数，让系统主动探索环境最优解，4 周内可证明收敛。
2. **Causal Attribution Engine (CAE)** — 用因果 DAG + 反事实推断回答"今晚为什么没睡好"，提供竞品（Eight Sleep / Whoop）都没有的解释性。
3. **Population Prior (PP)** — 用 MESA + SHHS 公开 PSG 数据集训练 hierarchical Bayesian prior，让新用户第 1 晚就有合理 setpoint，**冷启动从 7 晚压到 1 晚**。
4. **Edge Micro-Stage Transformer (EMST)** — 50KB INT8 量化的端侧 transformer，提前 60 秒预测 stage 转换，让响应慢的设备（空调、电热毯、地暖）能提前动作。

四个方向均为**纯本地实现**，不引入服务器、不收集用户数据、保持 Add-on 自包含特性。本期暂不做真·联邦学习（推迟至 v3.1.0），用 PP 路径桥接冷启动问题。

## Glossary

- **GP（Gaussian Process）**：用核函数描述输入-输出后验分布的非参数贝叶斯模型。
- **Thompson Sampling**：贝叶斯探索-利用策略，每步从后验抽样作为决策。
- **Regret bound**：bandit / 优化算法相对于"已知最优"的累积损失上界，可数学证明。
- **DAG（Directed Acyclic Graph）**：因果关系图，节点是变量，边是因果方向。
- **do-calculus**：Pearl 提出的因果推断符号系统，区分干预 do(X=x) 与观察 X=x。
- **Heckman correction**：处理选择偏倚（selection bias）的两阶段回归方法。
- **Hierarchical Bayesian prior**：分层贝叶斯先验，例如 `年龄段 → 性别 → 个体` 的多层条件结构。
- **MESA**：Multi-Ethnic Study of Atherosclerosis Sleep Study，2056 受试者 PSG，NSRR 提供。
- **SHHS**：Sleep Heart Health Study，6441 受试者 PSG。
- **Sleep-EDF**：PhysioNet 公开的多导睡眠图（PSG）数据集，197 晚。
- **NSRR**：National Sleep Research Resource，公开 PSG 数据集分发平台。
- **ONNX Runtime**：跨平台模型推理引擎，支持 CPU INT8 量化模型。
- **PSG（Polysomnography）**：金标准睡眠监测，含 EEG / EOG / EMG / ECG 多通道。
- **Causal effect / counterfactual**：因果效应；反事实指"若过去某变量取另一值，结果会怎样"。
- **Feature flag**：配置项级别的功能开关，让用户/运维独立开关每个新算法路径。

## Requirements

### Requirement 1: Bayesian 后验建模（GP）

**User Story:** 作为对睡眠优化效果有可证明性需求的用户，我希望系统用高斯过程对"环境 → 睡眠质量"建立后验模型，使其学习曲线和不确定度可视化、可解释、可数学约束。

#### Acceptance Criteria

1. WHERE 用户启用 `bayesian_optimizer_enabled = true`（默认 true），WHEN 系统启动时，THE add-on SHALL 在内存中建立一个 3 维（温度、湿度、亮度）GP 后验模型，使用 RBF kernel + 噪声方差超参数初始化。
2. WHILE 历史 session 数 < 5，THE add-on SHALL 仅用 PP 模块（方向 3）提供的 prior 作为 GP 后验，不调用 GP 推断路径。
3. WHEN 一晚 session 结束并产生 quality_score，THE add-on SHALL 用 (env_params, quality_score) 二元组更新 GP 后验，更新耗时不超过 200 ms（Pi 4B）。
4. WHERE GP 模型出现数值不稳定（cholesky decomp 失败），THE add-on SHALL 回退使用 v2.x `recommend()` 加权中位数路径，并通过 `sensor.sleep_classifier_optimizer_health` 发布 `degraded` 状态。
5. THE add-on SHALL 用 `numpy>=1.24` 与 `scipy>=1.10` 实现 GP，禁止依赖 GPy / GPyTorch / scikit-learn 这些更重的库（镜像体积约束）。
6. THE add-on SHALL 在 `/data/bao_model.pickle` 持久化 GP 训练数据（最多保留 60 个最近 session），通过 `src._io_utils.atomic_write_*` 写入。
7. THE add-on SHALL 在 `sensor.sleep_classifier_optimizer_uncertainty` 暴露当前 GP 后验在推荐点处的标准差（°C / % / lux 各一份），用于 Lovelace 可视化收敛过程。

### Requirement 2: Thompson Sampling 决策

**User Story:** 作为希望系统在长期最优与短期舒适之间智能权衡的用户，我希望每晚的 setpoint 由 Thompson Sampling 决定，平衡探索（找到更好点）与利用（用已知最优）。

#### Acceptance Criteria

1. WHEN GP 后验已有 ≥ 5 个观测样本，THE add-on SHALL 在每次 stage 切换时从 GP 后验抽样得到本次决策的 setpoint，而非直接取均值。
2. WHERE 用户配置 `exploration_rate ∈ [0.0, 0.5]`（默认 0.1），THE add-on SHALL 让该比例的 stage 切换走"高不确定性优先"探索策略，其余走 exploit。
3. WHEN 系统处于 wind-down 阶段（入睡前 30 分钟），THE add-on SHALL 强制走 exploit（不探索），避免影响入睡。
4. THE add-on SHALL 在 `sensor.sleep_classifier_decision_mode` 暴露当前决策模式（`exploit` / `explore-temp` / `explore-humidity` / `explore-brightness` / `prior-only`）。
5. IF 用户在 Web UI 临时锁定某个维度（"今晚不要探索温度"），THEN THE add-on SHALL 在后续 24 小时内只对未锁定维度做 Thompson Sampling，且 `sensor.sleep_classifier_locked_dimensions` 反映状态。
6. THE add-on SHALL 用伪随机种子（基于 install_id + 当晚日期 hash）保证 Thompson Sampling 可重复，便于事后复盘。

### Requirement 3: Bayesian 优化可观测性与收敛保证

**User Story:** 作为想验证算法宣传"4 周收敛"的用户和评审者，我希望系统持续暴露收敛指标，并提供独立验证脚本。

#### Acceptance Criteria

1. THE add-on SHALL 维护一个滚动窗口（最近 14 晚）的 quality_score 序列，并发布 `sensor.sleep_classifier_quality_trend_14d`（数值为窗口斜率，单位 score/day）。
2. WHEN 14 晚滚动窗口斜率 ≥ +0.5 score/day 持续 ≥ 7 晚（含端点：第 7 晚必须满足），THE add-on SHALL 把 `sensor.sleep_classifier_optimizer_status` 状态置为 `converging`；持续 ≥ 14 晚（同样包含端点）则置为 `converged`；阈值满足但持续时间不足时状态保持上一稳定值（默认 `learning`），不允许"瞬时进入 converging"。
3. THE add-on SHALL 提供 `scripts/eval_bayesian_regret.py`，输入 user_preferences.json，输出累积 regret 曲线 + 理论 regret bound（GP-UCB 形式）。
4. THE add-on SHALL 在 `docs/algorithm_evaluation.md` 提供至少一份 holdout 评估报告，对比 v2.x（中位数）与 v3.x（GP+TS）在合成数据集上的 regret，证明 v3.x 在 28 晚后累积 regret 至少低 30%。
5. THE add-on SHALL 在 README 仅声明 "GP-UCB 形式 regret bound 在 RBF kernel + 加性噪声假设下成立"，不夸大临床效果。

### Requirement 4: 因果归因 DAG 与数据收集

**User Story:** 作为想知道"今晚为什么没睡好"的用户，我希望系统建立 6 维干扰因子的因果图，并持续记录每晚的因子值。

#### Acceptance Criteria

1. THE add-on SHALL 在 `src/causal_attribution.py` 定义包含至少 6 个混杂因子节点的 DAG：
   - `temperature_drift`（夜间温度方差）
   - `noise_level`（环境噪声指标，无传感器时取 0）
   - `light_leak`（夜间亮度峰值）
   - `hrv_anomaly`（HRV 偏离基线 σ）
   - `bedtime_offset`（实际 vs 推荐入睡时间差）
   - `prior_night_debt`（上一晚累计 sleep debt 小时）
2. WHEN session 结束，THE add-on SHALL 把上述 6 维因子值与 4 维质量分项（architecture / efficiency / fragmentation / onset）一起持久化到 `/data/causal_factors.jsonl`（每行一晚）。
3. THE add-on SHALL 至少保留 90 晚因子数据，超过则按 FIFO 滚动删除。
4. WHILE 因子文件中数据 < 30 晚，THE add-on SHALL 把 `sensor.sleep_classifier_attribution` 置为 `insufficient_data` 状态，且 attribution 路径不进入推断流程。
5. THE add-on SHALL 用纯 Python 实现 DAG 邻接表存储；禁止引入 networkx / dowhy 等重型因果推断库（依赖治理）。
6. WHERE HRV / 噪声 等传感器缺失，THE add-on SHALL 把对应因子标记为 `unobserved` 而非 0，避免污染因果效应估计。

### Requirement 5: 反事实推断与每晚归因输出

**User Story:** 作为用户，我希望每晚醒来在 Lovelace 上看到"今晚最大干扰是 X，因果效应估计 Y"。

#### Acceptance Criteria

1. WHEN 因子文件累计 ≥ 30 晚 AND 当晚 quality_score < 用户个人 30 天均值 - 5，THE add-on SHALL 用 do-calculus + Heckman correction 估计每个干扰因子对 quality_score 的因果效应（单位：分），保留小数点后 1 位。
2. THE add-on SHALL 在 `sensor.sleep_classifier_attribution` 发布 `top_factor`、`top_effect_pp`、`counterfactual_score`、`explanation_zh` 4 个属性，例如：
   - `top_factor: temperature_drift`
   - `top_effect_pp: 12.4`
   - `counterfactual_score: 87.5`
   - `explanation_zh: "如果今晚卧室温度方差从 1.2°C 降到 0.5°C，估计睡眠质量分会从 75 提到 87.5"`
3. WHEN 当晚 quality_score ≥ 个人 30 天均值，THE add-on SHALL 把 sensor 置为 `nominal` 且不做反事实计算（避免误导）。
4. THE add-on SHALL 限制反事实推断耗时 ≤ 5 秒（Pi 4B），超时则发布 `timeout` 状态并跳过本次。
5. WHERE 用户启用 `causal_attribution_explain_all = true`（默认 false），THE add-on SHALL 把所有 6 个因子的因果效应都发布到 `sensor.sleep_classifier_attribution_full`（attribute 是字典），便于研究使用。
6. THE add-on SHALL 在 estimator 内置最小有效观测数检查（每个因子至少 5 个非缺失值），不满足时该因子 effect 标记为 `nan` 而不是 0。

### Requirement 6: 因果归因质量保证

**User Story:** 作为想避免被虚假因果误导的用户，我希望归因结果有统计置信度披露。

#### Acceptance Criteria

1. THE add-on SHALL 对每个因果效应估计同步给出 95% bootstrap 置信区间（重采样 ≥ 200 次）。
2. WHEN 95% 置信区间跨 0（即 effect 在统计意义上与 0 不可区分），THE add-on SHALL 在 `explanation_zh` 后追加 "（统计显著性弱）"。
3. THE add-on SHALL 提供 `scripts/eval_causal_synthetic.py`，输入合成数据（已知 ground-truth 因果图）输出 estimator 的偏差与方差。
4. THE add-on SHALL 在 `docs/algorithm_evaluation.md` 报告合成数据上的因果效应回收率 ≥ 70%（在 30 晚样本量下）。
5. THE add-on SHALL 在 `docs/MEDICAL_DISCLAIMER.md` 增补段落明确："归因解释为相关性 + 因果模型推断，非临床诊断"。

### Requirement 7: 公开数据集 Prior 训练

**User Story:** 作为开发者，我需要一个可复现的训练脚本，把 MESA + SHHS 公开数据训练成 hierarchical Bayesian prior 文件，并打包进 add-on 镜像。

#### Acceptance Criteria

1. THE add-on SHALL 在 `scripts/train_population_prior.py` 提供训练脚本，输入 NSRR 提供的 MESA / SHHS CSV / EDF 文件路径，输出 `sleep_classifier/rootfs/training_config/population_prior.pickle`。
2. THE prior 文件 SHALL 序列化以下结构：
   - 按 `(age_band, sex, chronotype, season)` 4 维分桶（age_band ∈ {18-25, 26-35, 36-50, 51-65, 65+}；sex ∈ {M, F, unspecified}；chronotype ∈ {morning, evening, neutral}；season ∈ {spring, summer, autumn, winter}）
   - 每桶包含 (温度均值, 温度方差, 湿度均值, 湿度方差, 亮度均值, 亮度方差) 6 个浮点数
   - 每桶包含 `n_samples`（用于权重计算）
3. THE prior pickle 文件大小 SHALL ≤ 8 MB（实际约 5 MB）；超出此上限即使其它校验全部通过 CI 也 SHALL 拒绝该 build。
4. THE add-on SHALL 在 prior pickle 中嵌入 `metadata` 字段，包含训练数据来源（MESA / SHHS 引用 + DOI）、训练时间戳、git commit hash。
5. THE add-on SHALL 在镜像构建时拒绝 prior 文件不存在 OR 校验失败 OR 大小超过 R7.3 上限 任意一种情况（CI 守护必须三项全部通过才算合格）。
6. THE add-on SHALL 在 `docs/POPULATION_PRIOR.md` 文档化数据来源、引用格式、伦理审查（NSRR DUA 摘要）、桶定义、字段含义。

### Requirement 8: Prior 加载与用户画像融合

**User Story:** 作为新用户，我希望第一晚就有合理 setpoint，而不需要 dry-run 7 晚才学到偏好。

#### Acceptance Criteria

1. WHEN add-on 启动时，THE add-on SHALL 加载 `population_prior.pickle`；加载失败时退化为 v2.x 默认值且发布 `sensor.sleep_classifier_prior_status = unavailable`。
2. THE Web UI SHALL 在 onboarding wizard 第 3 步（slot binding 之后）增加用户画像填写：`age_band`（选择 5 个区间）、`sex`（选 3 项）、`chronotype`（选 3 项），全部可选（默认 unspecified / neutral）。
3. WHEN 用户保存画像，THE add-on SHALL 把对应桶的 prior 复制到内存的 GP 后验初始 mean，作为冷启动 prior。
4. WHILE 用户历史 session 数 < 7，THE GP 后验权重 SHALL 满足 `w_prior >= 0.5`（即 prior 至少占一半）；session 数 ≥ 7 后 prior 权重按指数衰减到 ≤ 0.1。
5. THE add-on SHALL 暴露 `sensor.sleep_classifier_prior_weight`（0~1 浮点）展示当前 prior 在决策中的占比；用户可在 Web UI 把该值手动锁定到 0（彻底关闭 prior 影响），即使 session 数 < 7 也允许，让用户完全控制何时停止使用人群数据。
6. WHERE 用户的 `age_band / sex / chronotype` 组合在 prior 中 `n_samples < 50`（小样本桶硬阈值，不分梯度），THE add-on SHALL 退化到上一层（如 sex unspecified）的桶并在 sensor 中标记 `prior_fallback_level`；任何 `n_samples ∈ [1, 49]` 的桶都按"小样本"处理（不区分 5 或 49）。
7. THE prior 数据 SHALL 永远不上传给任何外部服务（PR3 隐私契约）；用户画像也 SHALL 仅存于本地 `web_ui_overrides.json`。

### Requirement 9: 端侧 Stage 预测推理

**User Story:** 作为有空调 / 电热毯 / 地暖等慢响应设备的用户，我希望系统能提前 60 秒预测 stage 转换，让设备来得及调温。

#### Acceptance Criteria

1. THE add-on SHALL 在 `src/stage_predictor.py` 加载 `sleep_classifier/rootfs/training_config/stage_predictor.onnx`（INT8 量化），通过 `onnxruntime>=1.16` CPU provider 运行。
2. THE ONNX 模型大小 SHALL ≤ 80 KB（INT8 量化后 50 KB 目标）。
3. WHEN 推理调用，THE add-on SHALL 接收 5 分钟时间窗口内的 (HRV, 体动, 呼吸率) 三通道时序，输出未来 60 秒最可能 stage 的概率向量（4 维：AWAKE / LIGHT / DEEP / REM）。
4. THE 单次推理耗时 SHALL ≤ 50 ms（Pi 4B）；超出则发布 `sensor.sleep_classifier_predictor_health = degraded` 并停用预测路径 1 小时。
5. WHEN 预测概率最大值 < 0.6（低置信）OR 模型输出无效（任一概率 < 0、概率和不在 [1.0±0.01] 区间内、或包含 NaN），THE add-on SHALL 不触发提前控制；模型无效输出与低置信走相同的安全路径，避免误动作。
6. WHERE HRV / 体动 / 呼吸率 任一传感器在过去 5 分钟内有 ≥ 50% 缺失，THE add-on SHALL 跳过本次预测；不会用零值填充。

### Requirement 10: 提前控制与命中率监控

**User Story:** 作为运维方，我需要看到 stage 预测对实际控制行为的影响，并能验证命中率。

#### Acceptance Criteria

1. WHEN 预测下个 stage = DEEP 且当前 stage = LIGHT 且置信 ≥ 0.6，THE smart_environment_controller SHALL 提前 60 秒按 DEEP 的 setpoint 启动空调 / 电热毯 / 地暖（仅这三类设备走"提前路径"）。
2. THE add-on SHALL 持续跟踪每次提前预测的命中情况（60 秒后实际 stage 是否匹配），并在 `/data/predictor_audit.jsonl` 记录。
3. THE add-on SHALL 在 `sensor.sleep_classifier_predictor_hit_rate_7d` 暴露最近 7 晚的命中率（百分比），刷新周期 1 小时。
4. WHEN 7 晚命中率 < 70% 持续 ≥ 3 晚，THE add-on SHALL 自动停用预测路径并发布 `sensor.sleep_classifier_predictor_status = auto_disabled`，需要用户手动重启或重训。
5. THE add-on SHALL 提供 `scripts/train_stage_predictor.py`，输入 Sleep-EDF 数据，输出 `.onnx` 文件 + 评估报告。
6. THE add-on SHALL 在 README / DOCS.md 标注："60 秒提前控制对快速响应设备（LED / 风扇 / 智能灯）无明显收益，仅对慢响应设备（空调 / 电热毯 / 地暖）有意义"。

### Requirement 11: Feature flags 与算法降级

**User Story:** 作为运维方或谨慎用户，我希望每个新算法可以独立开关，加载失败时优雅降级到 v2.x 行为。

#### Acceptance Criteria

1. THE config.yaml SHALL 增加 4 个独立 bool 开关：`bayesian_optimizer_enabled`（默认 true）、`causal_attribution_enabled`（默认 true）、`population_prior_enabled`（默认 true）、`stage_predictor_enabled`（默认 true）。
2. THE config.yaml schema SHALL 用 `"bool?"` 形式让 v2.x 用户从旧 config 升级时不被 schema 校验拒绝（PR3 持久化兼容契约）。
3. WHEN 任一新算法模块 import 失败 OR 文件加载失败 OR 运行时异常 ≥ 3 次，THE add-on SHALL 自动停用该模块（设置 internal feature flag = False）并发布 `sensor.sleep_classifier_<module>_health = degraded`，但 add-on 主流程 SHALL NOT 退出。
4. WHERE 全部 4 个新算法均停用，THE add-on SHALL 在功能上完全等价于 v2.1.0（学习算法走加权中位数 + k-NN，无因果归因，无 prior，无端侧预测）。
5. THE dry_run 安全契约（任何 HA service call 在 dry_run=true 时只打印不执行）SHALL 在所有新算法路径上保持成立。
6. THE add-on SHALL 在 Web UI 顶部 sticky 区显示当前 4 个算法的健康状态（healthy / degraded / disabled）。

### Requirement 12: 镜像与依赖治理

**User Story:** 作为镜像维护者，我希望 v3.0.0 的依赖增加是有据可查、可控的，且不破坏现有 PR4 镜像体积守护。

#### Acceptance Criteria

1. THE add-on SHALL 在 `requirements-runtime.txt` 增加 `numpy>=1.24,<2.0`、`scipy>=1.10,<2.0`、`onnxruntime>=1.16,<2.0`，固定大版本以避免破坏性升级。
2. THE add-on SHALL 更新 `.kiro/steering/tech.md`，把上述 3 个新依赖加入"运行时依赖"清单，并新增段落"v3.0.0 破例理由"，说明每个依赖对应哪个算法方向。
3. THE 镜像体积基线 SHALL 从 v2.1.0 的 ~15 MB 提升到 v3.0.0 的 80 MB（在 `.github/baseline_image_size.txt` 标注新基线）；超过 96 MB（基线 ×1.20）即 CI fail。
4. THE add-on SHALL 在 `addon-build.yml` workflow 增加 step 检查 `numpy / scipy / onnxruntime` 是否被实际 import（防止"装了不用"造成无谓体积膨胀）。
5. THE 训练时依赖（`torch / pandas / nsrr-toolkit`）SHALL 仅放在 `requirements-train.txt`（开发者手动安装），不进入 `requirements-runtime.txt`，不进入 add-on 镜像。
6. THE add-on SHALL 保留 v2.1.0 已有的 `aiohttp` 版本不变。

### Requirement 13: 商业化文案与路线图

**User Story:** 作为产品方，我需要 README / DOCS / ROADMAP 准确反映 v3.0.0 的 4 个算法亮点和未来 v3.1.0 联邦扩展方向。

#### Acceptance Criteria

1. THE README.md SHALL 在顶部"为什么不一样"段落新增"4 个算法护城河"小节，每个方向配一句话价值主张 + 数学保证（regret bound / causal effect / 5 分钟提前预测）。
2. THE README.md SHALL 增加"出厂带 8000+ 受试者 PSG 训练 prior"宣传点，并链接 `docs/POPULATION_PRIOR.md` 数据来源说明。
3. THE docs/ROADMAP.md SHALL 把 v3.1.0 的 federated learning 段落更新为"基于 v3.0.0 prior 模块的真·联邦扩展"，明确依赖关系。
4. THE docs/ROADMAP.md SHALL 把 "Commercial roadmap" 段落更新，把"算法订阅服务"作为 v3.0.0 之后的一个潜在变现方向（GP 后验 / 因果归因为 enterprise feature）。
5. THE add-on SHALL 在 sleep_classifier/DOCS.md 增加"算法可解释性"段落，配一张 ASCII 流程图说明 PSG → prior → GP → Thompson → action 的链路。
6. THE README / DOCS SHALL 全程不夸大临床效果，所有数学保证仅写"假设 X 成立时收敛"。

### Requirement 14: 数据合规与伦理

**User Story:** 作为关注隐私的用户和审计方，我希望 v3.0.0 的所有数据流（prior 训练、用户画像、归因数据）都有合规说明。

#### Acceptance Criteria

1. THE docs/POPULATION_PRIOR.md SHALL 包含 NSRR / PhysioNet 的 DUA（Data Use Agreement）摘要，并在 add-on 启动时打印一行 INFO 日志说明 prior 来源（仅启动时一次）。
2. THE add-on SHALL 永远不上传 user_preferences.json / web_ui_overrides.json / causal_factors.jsonl / 用户画像（age_band / sex / chronotype）等**原始**或**未脱敏**数据到任何外部服务；但**衍生分析或聚合洞察**（例如累计睡眠债 / 14 天质量趋势这类已脱敏的标量）若用户启用 telemetry_enabled 则可上传，前提是不可还原个体原始数据。
3. THE add-on SHALL 在 PRIVACY.md 新增段落"v3.0.0 算法栈数据流"，列举 4 个新模块各处理哪些数据、写到哪些文件、是否离开本地（答案均为否）。
4. WHERE 用户上报 bug 时附带 user_preferences.json，THE 维护者文档 SHALL 提示其先用 `scripts/sanitize_user_data.py` 脱敏（去除个人画像 + 时间戳模糊化）。
5. THE add-on SHALL 提供 `scripts/sanitize_user_data.py` 工具，把 `/data/*.json/jsonl` 里的 entity_id 替换成 hash、时间戳保留小时但去秒，便于用户分享调试样本。

### Requirement 15: 算法评估与论文级材料

**User Story:** 作为产品方 / 学术合作者，我需要可重复的评估材料和基线对比脚本，让 4 个算法的优势可独立验证。

#### Acceptance Criteria

1. THE docs/algorithm_evaluation.md SHALL 包含 4 个章节，每个方向一份评估报告：
   - **方向 1**：合成 GP 数据 + 真实 user_preferences.json holdout 上 v2.x 中位数 vs v3.x GP+TS 的累积 regret 曲线
   - **方向 2**：合成 DAG（已知 ground-truth）下 estimator 的偏差 / 方差 / 95% CI 覆盖率
   - **方向 3**：MESA holdout 集上 prior 分桶预测 vs 个体 baseline 的 RMSE
   - **方向 4**：Sleep-EDF 测试集上 stage 预测 60 秒前 hit rate（按 stage 分类报告）
2. THE 每份报告 SHALL 提供独立可执行脚本（位于 `scripts/eval_*.py`），输入数据路径，输出 markdown 表格 + 图表（matplotlib 仅训练/评估环境用，不进 runtime）。
3. THE add-on SHALL 在 `docs/algorithm_evaluation.md` 顶部包含一段"局限性"声明：v3.0.0 算法在 IID 假设下成立，季节切换 / 设备故障 / 重大生活变化下可能性能退化。
4. THE 维护者 SHALL 在 v3.0.0 发布后 4 周内投一篇 short paper（建议 BHI 或 EMBC），把方向 1 + 方向 2 作为主要贡献；方向 3 + 方向 4 作为系统集成支持。
5. THE 评估脚本 SHALL 设置随机种子（默认 20260518）保证结果可重复；任何脚本输出文件名需带 git commit hash 后缀，便于追溯。

## Cross-cutting Properties (PR)

下列约束跨越所有上述 user story，必须在 design 与 tasks 中体现：

- **PR1**：`dry_run=true` 时所有新算法路径 SHALL NOT 调用 `ha_client.call_service`（保留 v2.x 安全契约）。
- **PR2**：现有 20 个 `sensor.sleep_classifier_*` SHALL 保留 entity_id + attribute schema，新 sensor 仅追加。
- **PR3**：`/data/*.json` 持久化 SHALL 走 `src._io_utils.atomic_write_*`；任何新算法新增文件（bao_model.pickle、causal_factors.jsonl、predictor_audit.jsonl）也必须走原子写入。
- **PR4**：镜像体积基线从 15 MB → 80 MB；CI 守护超过 96 MB 即 fail（见 R12.3）。
- **PR5**：所有新算法的后台 task 在主进程 SIGTERM 时 SHALL 在 ≤ 10 秒内退出（与 v2.1.0 telemetry / upgrade 行为一致）。
- **PR6**：所有新 config.yaml 字段类型 SHALL 用 `"bool?"` / `"int?"` / `"str?"` 形式，确保 v2.1.0 旧 config 升级时不被拒绝。

## Correctness Properties

下列 12 条 properties 在 design / tasks 中必须有对应可执行测试：

| # | 名称 | 覆盖的 user story |
|---|---|---|
| P1 | GP 后验更新单调性：增加观测后预测均方差不增大 | R1 |
| P2 | Thompson Sampling 探索率长期收敛到配置值 ± 0.02 | R2 |
| P3 | 在合成已知最优场景下 28 晚累积 regret 比 v2.x 中位数低 ≥ 30% | R3 |
| P4 | 因果效应估计 95% CI 在已知 null 因子上覆盖率 ≥ 92% | R4, R6 |
| P5 | 反事实推断耗时 ≤ 5 秒（Pi 4B 模拟环境，小样本 30 晚） | R5 |
| P6 | Prior pickle 文件每个桶的均值在合理生理区间内（温度 [16, 28]°C，湿度 [30, 70]%，亮度 [0, 50]%） | R7 |
| P7 | Prior 权重在 N=0 时 = 1.0；N=14 时 ≤ 0.1（指数衰减） | R8 |
| P8 | Stage 预测推理 ≤ 50 ms（Pi 4B） | R9 |
| P9 | 7 晚滚动命中率监控统计正确（unit test 用合成序列验证） | R10 |
| P10 | 4 个 feature flag 独立关闭时 add-on 主流程仍可启动 + 跑完一晚 dry_run | R11 |
| P11 | 镜像内 numpy / scipy / onnxruntime 必须有 `import` 路径覆盖率（CI 静态扫描） | R12 |
| P12 | sanitize_user_data.py 输出文件中不包含原始 entity_id / 完整时间戳 | R14 |

## Out of Scope（v3.0.0 明确不做）

- 真·联邦学习（差分隐私聚合、跨用户 DP-SGD、secure aggregation）→ 推迟到 **v3.1.0**
- 多用户 / 多床（同一户多人）支持 → 推迟到 **v3.2.0+**
- 训练时 GPU 加速 / 分布式训练 → 单机 CPU 训练即可，不在本期目标
- Apnea / hypopnea 的因果归因 → 暂仍走 v2.x apnea_detector 的纯函数路径
- 用户画像跨应用同步（Health Connect / Apple Health 同步年龄性别）→ v3.2.0+
- 多语言扩展（fr / es / ja）→ 仅保留 zh-cn + en（v2.1.0 已有）

## Out of v3.0.0 但 v3.1.0 必须接住的钩子

- v3.0.0 prior pickle 的格式 SHALL 是 v3.1.0 联邦聚合的 wire format（forward-compat），见 design §"prior schema 演进路径"。
- v3.0.0 GP 模型的 hyperparameter（kernel + length scale）SHALL 可被 v3.1.0 联邦平均（FedAvg）兼容。
- v3.0.0 因果 DAG 的节点 / 边 SHALL 序列化到 JSON，便于 v3.1.0 跨用户因果效应聚合。
