# Requirements Document

> Spec: v3-core-algorithm-moat

## Introduction

v2.1.x 系列已经把 Sleep Classifier add-on 推到了「装得上、留得住、可观测」的商业化基线。但算法层（`src/preference_learner.py` 的加权中位数 + k-NN，`src/smart_environment_controller.py` 的 deadband + per-stage delta）在公开仓库里**门槛过低**：一个有 Python 经验的工程师在 1–2 周内就能复刻同等推荐质量。

v3.0.0 的目标是用**四重正交的算法护城河**把竞品复刻成本拉高一个数量级，同时维持 Add-on 在 Raspberry Pi 4B 上「纯 Python + aiohttp + 单事件循环」的工程基线：

1. **Bayesian Active Optimization**（贝叶斯主动优化）—— 用 Gaussian Process 后验 + Thompson Sampling 把「找用户最佳环境」转化为顺序决策问题，4 周内 SE 提升有可证明的 regret bound。
2. **Causal Counterfactual Attribution**（因果反事实归因）—— 当晚 SE 偏低时给出「03:14 空调跳到 24°C 害你 WASO 翻倍」级别的归因解释，并支持「如果不那样会怎样」的反事实查询。
3. **Population Prior**（公开数据集出厂预训练）—— 基于 MESA + SHHS + Sleep-EDF 8 000+ 临床 PSG 受试者，离线训练 hierarchical Bayesian prior，新用户**第 1 晚**就有冷启动起点。
4. **Edge Micro-Stage Transformer**（端侧微 transformer）—— ≤ 50 KB INT8 量化模型预测下一 60 s stage，让 controller 在 LIGHT→DEEP 切换前就把 setpoint 提前下发。

本 spec 同时形式化 5 条**跨方向工程不变量**（PR1–PR5），覆盖测试、sensor 契约、持久化、镜像体积、降级路径。

**核心约束**：v3.0.0 必须保持向后兼容 v2.1.x 的全部行为契约，特别是：
- v2.1.0 已发布的 20 个 `sensor.sleep_classifier_*` 实体的 `entity_id` 与 attribute 集合**不变**。
- `/data/user_preferences.json`（v2.1.x 的偏好持久化）在 v3.0.0 启动时必须被旧字段集**完整加载**，缺失新字段时回退到安全默认值。
- 运行时镜像新增依赖只允许 `onnxruntime`（纯 wheel，aarch64/amd64 双架构均有官方包）；其余三个新模块（Bayesian、Causal、Population Prior 加载器）必须**纯 stdlib + aiohttp**，不引入 numpy / scipy / GPy / dowhy / causalml。
- 4 个新模块必须各自带 fallback 开关；任意一个抛异常时，调用方降级到 v2.1.x 同语义对应行为，**不得让 add-on crash**。
- 公开数据集预训练流程是**离线**的（开发者侧 CI 执行），结果作为 `.pkl` / `.onnx` 资产打包进 add-on 镜像；运行时只读，不在用户侧重训。

**明确不在范围内**：
- 真正的联邦学习 server（推迟到 v3.1.0）。
- 任何 cloud telemetry 字段增强（v2.1.0 的 opt-in `telemetry_reporter` 不动）。
- HACS 仓库迁移（仍然是 HA add-on，而非 custom_components integration）。
- 多用户 / 多卧室（保持单户单房间假设；v3.1.0+ 再处理）。

---

## Glossary

### 系统组件（System Names，要被 EARS `THE <System> SHALL` 子句引用）

- **V3_Core_Algorithm_Suite**: 本 spec 引入的 4 个新模块的总称（Bayesian_Recommender + Causal_Attributor + Population_Prior_Loader + Edge_Transformer_Predictor）。
- **Bayesian_Recommender**: `src/bayesian_recommender.py`，替代 `PreferenceLearner.recommend()` 的主推荐器。
- **GP_Posterior_Engine**: Bayesian_Recommender 内部的纯函数子组件，给定 history 与 query point 返回 RBF kernel 下的 closed-form GP 后验均值与方差。
- **Thompson_Sampler**: Bayesian_Recommender 内部的 acquisition 子组件，从 GP_Posterior_Engine 的后验分布抽样并按 90% exploit / 10% explore 选择下一夜 setpoint。
- **Causal_Attributor**: `src/causal_attributor.py`，对当晚 SE 与 baseline 的差额做 6 维干扰因子的因果归因。
- **Counterfactual_Engine**: Causal_Attributor 内部子组件，给定一夜的环境时间序列与一个「假设不发生的事件」返回反事实 SE 估计。
- **Population_Prior_Loader**: `src/population_prior.py` 的运行时加载器，从镜像中只读 `models/population_prior.pkl` 并按 `(age_bucket, sex, chronotype, season)` 查询先验。
- **Population_Prior_Trainer**: `scripts/train_population_prior.py` 的离线训练器，开发者侧 CI 执行；运行时镜像**不**包含此文件。
- **Edge_Transformer_Predictor**: `src/edge_stage_predictor.py`，加载 `models/edge_stage_predictor.onnx` 并对下一 60 s stage 做单步预测。
- **ONNX_Runtime_Adapter**: Edge_Transformer_Predictor 内部对 `onnxruntime` 的薄封装，单事件循环下用 `asyncio.to_thread` 调用以避免阻塞。
- **Algorithm_Fallback_Manager**: `src/algorithm_fallback.py`，对 V3_Core_Algorithm_Suite 的每个模块提供 try/except + degrade-to-legacy 装饰器，确保 PR5 不变量。
- **Sensor_Contract_Guard**: 针对 `sensor.sleep_classifier_*` 的 entity_id 与 attribute 不变量的运行时与 CI 双重断言（运行时由 `learning_panel_publisher.py` / `sleep_state_publisher.py` 守卫，CI 由 `tests/test_sensor_contract_v2_1_0.py` 守卫）。
- **Persistence_Migration_Layer**: `src/preference_learner.py` 与新模块共用的 schema migration helper：v3 启动时若读到 v2.1.x schema 的 `/data/user_preferences.json`，按 forward-only 规则加载并补齐新字段。
- **Image_Build_Auditor**: CI 中的镜像体积与依赖白名单检查（`.github/workflows/addon-build.yml` 新增 step），强制最终镜像 ≤ 35 MB。
- **PreferenceLearner_Legacy**: v2.1.x 的 `src/preference_learner.PreferenceLearner` 实例，v3.0.0 起仅作为 fallback 路径，不再是默认主路径。
- **SmartEnvironmentController**: `src/smart_environment_controller.SmartEnvironmentController`，v3 中接受新 learner 接口注入。
- **Add-on_Image**: `sleep_classifier/Dockerfile` 构建出的最终容器镜像，体积上限受 PR4 约束。

### 算法术语

- **SE**: Sleep Efficiency = TST / TIB，由 `src/sleep_quality_score.py` 计算的 0–100 数值。
- **Baseline_SE**: 用户最近 14 晚 SE 的 14 天半衰期指数加权均值（v2.1.x 已存在，v3 复用，不重新定义）。
- **Regret_Bound**: 在线学习中的累计遗憾界。本 spec 取「累计遗憾 R(N) = Σ (SE_optimal − SE_actual_n)」并要求其在 N → 4 周内增长慢于 sublinear。
- **Closed-form_GP_Posterior**: 给定先验 + 训练对 (X, y) 与查询点 x*，由 `μ* = k*ᵀ (K + σ²I)⁻¹ y`、`σ²* = k(x*, x*) − k*ᵀ (K + σ²I)⁻¹ k*` 计算，无需迭代优化。
- **RBF_Kernel**: `k(x, x') = exp(−||x − x'||² / (2 ℓ²))`，长度尺度 ℓ 按各维度独立。
- **Acquisition_Function**: 决定下一次试探点的函数；本 spec 使用 Thompson Sampling 而非 UCB / EI。
- **Confounder_Set**: Causal_Attributor 处理的 6 维干扰因子，固定为 {temperature_drift, noise_level, light_leakage, hrv_anomaly, sleep_onset_offset, prior_night_debt}。
- **DAG**: Directed Acyclic Graph，描述 Confounder_Set ↔ SE 的因果结构。
- **Do_Calculus**: Pearl 的 do(·) 算子；本 spec 用 backdoor adjustment 的闭式表达。
- **Heckman_Correction**: 处理选择偏倚的两阶段估计；本 spec 用 inverse-Mills 比作为额外回归项以矫正「干预日志只在异常夜被记录」的偏倚。
- **Cold_Start_Window**: Causal_Attributor 在用户累计睡眠夜数 < 30 时进入的状态，此期间 sensor 显示 `data_warming` 而非真实归因。
- **Population_Prior**: 公开数据集训练得到的 hierarchical Bayesian prior `p(env_setpoint_optimal | age_bucket, sex, chronotype, season)`，量化为 `(age_bucket × sex × chronotype × season) → EnvironmentParams + uncertainty` 查表 + 平滑残差。
- **Age_Bucket**: 离散化年龄分组 `{≤18, 19–25, 26–35, 36–50, 51–65, ≥66}`，与 NSF 推荐睡眠时长分桶对齐。
- **Season**: 离散化季节分桶 `{spring, summer, autumn, winter}`，由 `now_local()` 的月份按北/南半球判断（v3.0.0 默认北半球，南半球留 config 开关 `southern_hemisphere`，默认 false）。
- **Hit_Rate**: Edge_Transformer_Predictor 对下一 60 s stage 的预测命中率，定义为「过去 N 次预测中预测 stage == 实际 stage」的比例。
- **INT8_Quantization**: ONNX `QuantType.QInt8` 静态量化，模型权重以 8-bit 整数存储。
- **Fallback_Path**: 某新模块抛异常或 disabled 时，Algorithm_Fallback_Manager 切回的 v2.1.x 等价行为路径。

### 文档与文件

- **Add-on_Manifest**: `sleep_classifier/config.yaml`。
- **Persistence_File**: `/data/user_preferences.json`，v2.1.x 与 v3 共用。
- **Population_Prior_Asset**: 镜像内 `/app/models/population_prior.pkl`，由 Population_Prior_Trainer 离线生成。
- **Edge_Model_Asset**: 镜像内 `/app/models/edge_stage_predictor.onnx`，离线量化得到。
- **Training_Recipe**: `docs/V3_TRAINING_RECIPE.md`，描述 Population_Prior_Trainer 与 Edge transformer 的可复现训练流程（数据来源、预处理、超参、随机种子）。

---

## Bug Condition C(X) 的形式化定义（algorithm-moat 视角）

虽然本 spec 是 feature 而非 bugfix，但「v2.1.x 算法层缺乏护城河」可以被形式化为状态不变量。沿用 `commercial-readiness-v2.1.0/requirements.md` 与 `post-v2.0.2-full-pipeline-audit/bugfix.md` 的 Pascal 风格记号：

```pascal
// X: 任意"v2.1.x 仓库 + 一名候选用户 + 一段 N 晚睡眠历史"复合状态
TYPE AlgorithmMoatState = RECORD
  recommender_kind: {weighted_median, kNN, gp_thompson}
  recommender_uses_acquisition: bool
  recommender_provable_regret: bool
  attribution_kind: {none, correlation, causal_dag}
  attribution_supports_counterfactual: bool
  attribution_cold_start_guard_nights: int
  cold_start_strategy: {wait_7_nights, public_data_prior}
  cold_start_first_night_recommendation_quality: {default_constants, prior_aware}
  stage_prediction_horizon_seconds: int   // 0 = 仅观测当前 stage
  stage_prediction_hit_rate: float         // 命中率，范围 [0, 1]
  stage_prediction_model_size_bytes: int
  stage_prediction_inference_ms: int
  test_line_coverage_new_modules_pct: float
  has_pbt_property_per_new_module: bool
  sensor_contract_v2_1_0_preserved: bool
  persistence_v2_1_x_loadable: bool
  image_size_mb: float
  fallback_path_exists_per_new_module: bool
  runtime_dependencies: Set<PackageName>
END

FUNCTION isAlgorithmMoatGap(X: AlgorithmMoatState): bool
  // 任意一条不满足，C(X) 即成立。
  RETURN NOT all_of(
    M1_recommender_is_gp_thompson(X),
    M2_recommender_has_provable_regret_bound(X),
    M3_attribution_is_causal_with_counterfactual(X),
    M4_attribution_guards_cold_start_min_30_nights(X),
    M5_cold_start_uses_public_data_prior(X),
    M6_first_night_quality_is_prior_aware(X),
    M7_stage_prediction_horizon_at_least_60s(X),
    M8_stage_prediction_hit_rate_at_least_0_8(X),
    M9_stage_prediction_model_at_most_50KB(X),
    M10_stage_prediction_inference_under_50ms(X),
    PR1_coverage_per_new_module_at_least_90_pct(X),
    PR1_pbt_property_per_new_module(X),
    PR2_sensor_contract_v2_1_0_preserved(X),
    PR3_persistence_v2_1_x_loadable(X),
    PR4_image_size_at_most_35_MB(X),
    PR5_fallback_path_exists_per_new_module(X),
    DEP_runtime_only_aiohttp_and_onnxruntime(X)
  )
END FUNCTION
```

每条 user story 给出一个 X 使得 v2.1.x 状态下 C(X) 成立、v3.0.0 完成后 C(X) 不再成立。

---

## Requirements


### Requirement 1: Bayesian_Recommender 替代加权中位数成为推荐主路径

**User Story:** 作为一名希望 add-on 「越用越懂我」的用户，我希望 v3 用顺序决策意义下的贝叶斯主动优化替代 v2.1.x 的加权中位数 + k-NN，以便每一晚的设定点都能从历史与当前状态中提取更多信息，并且每次推荐都附带不确定度。

**Bug Condition X:** v2.1.x 状态下，`AlgorithmMoatState.recommender_kind == weighted_median`、`recommender_uses_acquisition == false`：`PreferenceLearner.recommend()` 用加权中位数 + 当前夜 k-NN 给出 `EnvironmentParams`，无后验、无不确定度、无 acquisition。

#### Acceptance Criteria

1. THE Bayesian_Recommender SHALL 暴露与 `PreferenceLearner.recommend(now: datetime, current_temperature_c: Optional[float], current_bedtime_hour: Optional[float]) -> EnvironmentParams` **签名兼容**的 `recommend(...)` 方法（参数名与返回类型保持完全一致）。
2. WHEN `Bayesian_Recommender.recommend()` 被调用，THE Bayesian_Recommender SHALL 调用 GP_Posterior_Engine 计算每个被建模维度（temperature_c、humidity_pct、brightness_pct、fan_speed_pct）的后验均值与方差，再调用 Thompson_Sampler 选择最终 setpoint。
3. WHEN `Bayesian_Recommender.recommend()` 返回，THE Bayesian_Recommender SHALL 将本次后验均值与方差写入新的诊断 attribute `posterior_mean` 与 `posterior_variance` 到 `sensor.sleep_classifier_recommendation_explain` 的 attributes 字典；既有 attribute 不删除、不重命名。
4. WHERE Add-on_Manifest 的 `bayesian_enabled` 选项为 `true`（默认值），THE SmartEnvironmentController SHALL 把注入的 learner 实例从 PreferenceLearner_Legacy 替换为 Bayesian_Recommender。
5. WHERE Add-on_Manifest 的 `bayesian_enabled` 选项为 `false`，THE SmartEnvironmentController SHALL 继续使用 PreferenceLearner_Legacy 作为推荐源，不实例化 Bayesian_Recommender。
6. IF 历史 session 数量小于 4 且 `bayesian_enabled == true`，THEN THE Bayesian_Recommender SHALL 委托 Population_Prior_Loader 给出初始建议，而不是直接调用 GP_Posterior_Engine（GP 在 < 4 个数据点时方差过大）。

---

### Requirement 2: GP_Posterior_Engine 用纯 Python 闭式表达计算 RBF kernel 后验

**User Story:** 作为 add-on 维护者，我需要 GP 后验计算**不引入** numpy / scipy / GPy 等重型依赖，以便镜像维持 stdlib + aiohttp + onnxruntime 的轻量基线，并且在 Pi 4B 上单次推荐计算可以在 200 ms 内完成。

**Bug Condition X:** v2.1.x 状态下不存在 GP 实现；任何外部贡献者若直接 `pip install scipy` 引入闭式 GP 后验，会让 `runtime_dependencies` 集合超出 `{aiohttp, onnxruntime}` 白名单，违反 DEP 不变量。

#### Acceptance Criteria

1. THE GP_Posterior_Engine SHALL 仅依赖 Python 标准库（`math`、`random`、`statistics`、`typing`），不得 `import numpy`、`import scipy`、`import gpy`、`import sklearn`。
2. THE GP_Posterior_Engine SHALL 用闭式表达计算 RBF kernel 下的后验均值与方差：μ\*= k\*ᵀ (K + σ²I)⁻¹ y、σ²\* = k(x\*, x\*) − k\*ᵀ (K + σ²I)⁻¹ k\*。
3. THE GP_Posterior_Engine SHALL 用纯 Python（基于 LU 分解或 Cholesky 分解的列表实现）求解 (K + σ²I)⁻¹ y 这一步，不调用任何外部线性代数库。
4. WHEN `Bayesian_Recommender.recommend()` 在历史 session 数量 ≤ 200 的工况下被调用，THE Bayesian_Recommender SHALL 在 Raspberry Pi 4B 等价硬件（CI 中以 `pytest-benchmark` 在 amd64 上 200 ms × 1.5 = 300 ms 作为代理上限）上 200 ms 内返回结果。
5. IF 历史 session 数量超过 200，THEN THE GP_Posterior_Engine SHALL 仅取最近 200 条参与 kernel 矩阵构建（滑动窗口），不得让矩阵规模无限增长。
6. IF 矩阵 (K + σ²I) 的对角元出现 NaN / 负数 / 数值奇异（行列式 < 1e-12），THEN THE GP_Posterior_Engine SHALL 抛出 `GPNumericalError`，由 Algorithm_Fallback_Manager 捕获并降级到 PreferenceLearner_Legacy。

#### Property-Based Correctness Properties

- **P2.1（推理形状不变量）**: 对任意被建模维度集合 D ⊆ {temperature_c, humidity_pct, brightness_pct, fan_speed_pct} 与任意训练集 (X, y)，`GP_Posterior_Engine.predict(x_query)` 返回的 `(mean, variance)` 字典键集恰好等于 D，且每个 variance ≥ 0。
- **P2.2（确定性）**: 给定相同的 history 与 query point，`GP_Posterior_Engine.predict(...)` 两次连续调用返回完全相等的均值与方差（无浮点漂移、无随机性）。

---

### Requirement 3: Thompson_Sampler 90/10 exploit/explore acquisition

**User Story:** 作为算法层维护者，我希望推荐器在 90% 的夜晚抽样后验最优点（exploit）、10% 的夜晚抽样后验更高方差点（explore），以便在「逼近用户最佳环境」与「主动收集边缘信息」之间形成可量化的平衡，并保证 Regret_Bound 成立。

**Bug Condition X:** v2.1.x 的 `SmartEnvironmentController` 有一个 `exploration_rate=0.1` 配置项，但仅作用于在中点附近做小幅扰动，**不基于后验方差**也**不构成 acquisition function**：在 `AlgorithmMoatState.recommender_uses_acquisition == false` 时不能据此论证 regret bound。

#### Acceptance Criteria

1. THE Thompson_Sampler SHALL 暴露 `sample_next_setpoint(posterior: dict, exploit_probability: float = 0.9) -> EnvironmentParams` 函数，其中 `posterior` 来自 GP_Posterior_Engine。
2. WHEN `sample_next_setpoint` 被调用，THE Thompson_Sampler SHALL 以概率 `exploit_probability` 选择后验均值（exploit）、以概率 `1 − exploit_probability` 从后验高斯分布抽样一个候选点（explore）。
3. THE Thompson_Sampler SHALL 用 `random.Random` 实例（构造时由调用方注入种子或注入 `random.SystemRandom()`），不得直接使用全局 `random` 模块状态。
4. THE Thompson_Sampler SHALL 把每一夜本次抽样究竟走了 exploit 还是 explore 路径写入 attribute `acquisition_branch ∈ {"exploit", "explore"}`，附加在 `sensor.sleep_classifier_recommendation_explain` 上。
5. IF 调用方传入 `exploit_probability` 不在闭区间 [0, 1]，THEN THE Thompson_Sampler SHALL 抛出 `ValueError`，由 Algorithm_Fallback_Manager 捕获并降级。
6. WHILE 用户在 Add-on_Manifest 中将 `bayesian_exploit_probability` 设为非默认值，THE Bayesian_Recommender SHALL 把该值传给 Thompson_Sampler 而非硬编码 0.9。

---

### Requirement 4: Regret_Bound 单调收敛保证 4 周内 SE 提升

**User Story:** 作为对算法承诺持怀疑态度的用户，我希望 v3 在 4 周内显示出**可证明**的 SE 提升（≥ 5%），而不是「我们觉得新算法更好」的口头承诺；这种提升应该来自 Bayesian 在线学习的 sublinear regret bound，而不是数据 cherry-picking。

**Bug Condition X:** v2.1.x 状态下 `recommender_provable_regret == false`：加权中位数没有任何 regret bound，且 28 晚平均 SE 在用户文档中没有明确承诺。

#### Acceptance Criteria

1. THE Training_Recipe 文档 SHALL 包含 Bayesian_Recommender 在「平稳奖励 + ≤ 4 维 setpoint 空间」假设下的 sublinear regret bound 推导（公式 + 引用 Srinivas et al. 2010 GP-UCB 类的结果）。
2. WHEN add-on 已运行至少 28 晚（用户首次安装 + 28 个 session 已写入 Persistence_File），THE Sensor_Contract_Guard SHALL 让 `sensor.sleep_classifier_v3_se_uplift_pct` 反映「最近 28 晚平均 SE 减去最早 28 晚平均 SE」的差值百分比。
3. WHEN Persistence_File 中 session 数量小于 28，THE `sensor.sleep_classifier_v3_se_uplift_pct` SHALL 报告字符串 `"data_warming"`（由 SleepStatePublisher 写为 state，attribute `nights_remaining` 给出剩余夜数）。
4. THE Bayesian_Recommender SHALL 维护内部 attribute `cumulative_regret`，在每次 `recommend()` 后用「best-so-far SE 与本次预期 SE 的差」累加；该值通过 `sensor.sleep_classifier_recommendation_explain.attributes["cumulative_regret"]` 暴露。
5. THE Add-on_Image SHALL 在 CI 的 `tests/test_bayesian_regret_property.py` 中验证：在合成 stationary reward 仿真下，跑 100 次模拟、每次 56 晚，`cumulative_regret(N) / N` 的均值在 N = 56 时**严格小于** N = 14 时（即 regret 增速次线性）。

#### Property-Based Correctness Properties

- **P4.1（Regret 不退步）**: 对任意 stationary reward 仿真种子集合（≥ 100 个），`cumulative_regret(N=56) / 56 ≤ cumulative_regret(N=14) / 14`（per-step regret 单调不增）。
- **P4.2（端到端 SE 不退步）**: 对任意 ≥ 100 次仿真，最近 28 晚的平均 SE − 最早 28 晚的平均 SE ≥ 0（v3 推荐的累计 SE 不会比初始期差）。


### Requirement 5: Causal_Attributor 替代相关性归因，提供 6 维 DAG 因果归因

**User Story:** 作为一名早晨醒来想知道「为什么昨晚我睡不好」的用户，我希望在 HA Lovelace 看到一个 sensor，告诉我类似「03:14 空调跳到 24°C 害你 WASO 翻倍 17 个百分点」这种带因果方向的解释，而不是「跟温度相关 0.62」这种统计相关性。

**Bug Condition X:** v2.1.x 状态下 `attribution_kind == none`：没有任何 sensor 解释当晚 SE 偏低的原因。即便用户自行写 HA 模板，也只能拿到 Pearson 相关。

#### Acceptance Criteria

1. THE Causal_Attributor SHALL 暴露 `attribute(night_record: NightRecord) -> List[AttributionItem]` 方法，其中 `NightRecord` 包含当晚的环境时间序列、stage 时间序列、SE 与 baseline_se。
2. THE Causal_Attributor SHALL 用包含 6 个 confounder 的固定 DAG（temperature_drift、noise_level、light_leakage、hrv_anomaly、sleep_onset_offset、prior_night_debt → SE）做 backdoor adjustment。
3. WHEN 当晚 SE < `baseline_se − 5` 个百分点，THE Causal_Attributor SHALL 至少返回 1 条 `AttributionItem`，且每条包含 `confounder_name`、`event_time_local`、`estimated_effect_pct`、`confidence_level ∈ {low, medium, high}` 四个字段。
4. WHEN 当晚 SE ≥ baseline_se − 5 个百分点，THE Causal_Attributor SHALL 返回空 list（不报告噪声归因）。
5. THE Causal_Attributor SHALL 把 `attribute(...)` 返回的归因结果序列化进 `sensor.sleep_classifier_recommendation_explain` 的新 attribute `attribution_items`（保留 v2.1.x 既有 attributes 不变）。
6. WHERE Add-on_Manifest 的 `causal_enabled` 选项为 `false`，THE LearningPanelPublisher SHALL 让 `attribution_items` 始终为空 list 且不调用 Causal_Attributor。
7. IF Causal_Attributor 抛出任何异常，THEN THE Algorithm_Fallback_Manager SHALL 把 `attribution_items` 写为空 list 并把异常类型与 traceback 摘要写入诊断 sensor `sensor.sleep_classifier_health` 的 `last_attribution_error` attribute，**不得**让 `recommend()` 主路径失败。

---

### Requirement 6: Heckman_Correction 处理选择偏倚

**User Story:** 作为算法层维护者，我希望 Causal_Attributor 在归因时校正「干预日志只在用户主动调温的夜晚才被记录」造成的选择偏倚，以便 v3 的归因不会在 controller 干预少的夜晚系统性偏向「温度无影响」。

**Bug Condition X:** v2.1.x 没有归因层，因此也没有任何 Heckman / IPW 之类的偏倚校正机制。`AlgorithmMoatState.attribution_kind == none` 时此项无意义；v3 引入归因后必须正面处理。

#### Acceptance Criteria

1. THE Causal_Attributor SHALL 在估计每个 confounder 的因果效应前，先以 Heckman 两阶段法估计 inverse-Mills 比 λ，并将 λ 作为额外回归项加入第二阶段回归。
2. THE Causal_Attributor SHALL 用 Persistence_File 中所有可用历史夜（含被 controller 干预与未干预两类）作为校正样本，不限于「当晚」。
3. WHEN 历史夜数量 < 30，THE Causal_Attributor SHALL 跳过 Heckman 步骤并把 `confidence_level` 一律设为 `low`。
4. THE Causal_Attributor SHALL 把每条 `AttributionItem` 是否经过 Heckman 校正记录在 attribute `selection_bias_corrected ∈ {true, false}`。
5. IF Heckman 第一阶段的 selection equation 收敛失败（似然不上升或 Hessian 奇异），THEN THE Causal_Attributor SHALL fallback 到无校正估计，并把 `confidence_level` 降一档（high → medium、medium → low、low 保持）。

---

### Requirement 7: Counterfactual_Engine 支持「如果不那样会怎样」查询

**User Story:** 作为一名怀疑「03:14 空调跳到 24°C 真的害我 WASO 翻倍吗」的用户，我希望在 Lovelace 上点一个按钮，看到「反事实如果空调没跳到 24°C，估计 SE 是多少」的具体数字，以便我能判断这个归因是否值得我去调整 HA automation。

**Bug Condition X:** v2.1.x 状态下 `attribution_supports_counterfactual == false`：归因层本身不存在，反事实查询更不存在。

#### Acceptance Criteria

1. THE Counterfactual_Engine SHALL 暴露 `counterfactual(night_record: NightRecord, intervention: ConfounderIntervention) -> CounterfactualEstimate` 方法，其中 `ConfounderIntervention` 指定一个 6 维 confounder 的干预（如「temperature_drift = 0」）。
2. THE Counterfactual_Engine SHALL 返回 `CounterfactualEstimate(se_estimate: float, se_lower_bound: float, se_upper_bound: float, intervention_supported: bool)`，置信区间宽度反映 GP 后验方差与 confounder 估计的联合不确定度。
3. WHEN 用户从 HA 调用 service `sleep_classifier.counterfactual_query`（在 v3 新增），THE Add-on SHALL 把请求路由到 Counterfactual_Engine 并把结果以 HA notification 的 markdown 形式回写。
4. IF 用户请求的 `ConfounderIntervention` 在当晚的 confounder 历史中**未出现**（例如夜里温度从未跳过 ≥ 2°C），THEN THE Counterfactual_Engine SHALL 把 `intervention_supported` 置 `false` 并提供 `reason: "intervention_out_of_support"`。
5. THE Counterfactual_Engine 单次查询计算耗时 SHALL 在 amd64 CI 环境中不超过 500 ms（在 `tests/test_counterfactual_perf.py` 用 `pytest-timeout` 与基准计时验证）。

#### Property-Based Correctness Properties

- **P7.1（归因解释一致性）**: 对任意 `night_record`，`Causal_Attributor.attribute(night_record)` 两次连续调用返回**完全相等**的 `AttributionItem` 列表（顺序、字段一一对应）。
- **P7.2（反事实单调性）**: 对任意 `night_record` 与任意 confounder c，若 `intervention1.value` 比 `intervention2.value` 更接近基线值且 c 对 SE 已被估计为单调影响，则 `counterfactual(night_record, intervention1).se_estimate ≥ counterfactual(night_record, intervention2).se_estimate`（在 c 对 SE 单调正向时；负向时反之）。

---

### Requirement 8: Cold_Start_Window 至少 30 晚才出归因

**User Story:** 作为一名刚装 add-on 的新用户，我不希望在前几晚看到信息不足却言之凿凿的归因（这会摧毁我对系统的信任）；我希望明确看到「数据预热中，再睡 X 晚」。

**Bug Condition X:** v2.1.x 状态下 `attribution_cold_start_guard_nights == 0`：因为根本没有归因层。但 v3 不能因为有归因层就允许在样本极少的早期夜次显示具体归因。

#### Acceptance Criteria

1. WHEN Persistence_File 中 session 数量 < 30，THE Causal_Attributor SHALL 不对当晚进行任何具体归因，且 `sensor.sleep_classifier_recommendation_explain.attributes["attribution_items"]` SHALL 为空 list。
2. WHILE Persistence_File 中 session 数量 < 30，THE LearningPanelPublisher SHALL 把 `sensor.sleep_classifier_recommendation_explain.state` 设为 `"data_warming"` 且 attribute `cold_start_nights_remaining` 给出 `30 - session_count`。
3. WHEN Persistence_File 中 session 数量 ≥ 30，THE LearningPanelPublisher SHALL 把 `state` 切回正常解释字符串，与 v2.1.x 行为一致。
4. THE Cold_Start_Window 阈值 30 SHALL 通过 Add-on_Manifest 的 `causal_cold_start_nights` 选项可调（int(7, 90)，默认 30）。


### Requirement 9: Population_Prior_Trainer 离线在 8 000+ 受试者数据上训练 hierarchical prior

**User Story:** 作为算法层维护者，我希望基于 MESA Sleep + SHHS + Sleep-EDF 三个公开数据集的 ≥ 8 000 名临床 PSG 受试者训练一个 hierarchical Bayesian prior，并把该过程开源、可复现，以便 v3 的「出厂预训练」是可被同行检验的算法资产，而不是黑盒。

**Bug Condition X:** v2.1.x 状态下 `cold_start_strategy == wait_7_nights`：新用户至少要积累 7 晚才能拿到 k-NN 推荐，前 7 晚只能用硬编码默认值。

#### Acceptance Criteria

1. THE Population_Prior_Trainer SHALL 实现于 `scripts/train_population_prior.py`，仅在开发者侧 / CI 执行，**不**包含在 Add-on_Image 中。
2. THE Population_Prior_Trainer SHALL 从以下三个数据源拉取（或读取本地缓存）样本：MESA Sleep（NSRR）、SHHS（NSRR）、Sleep-EDF（PhysioNet）。`download_data.py` 已存在，可扩展。
3. THE Training_Recipe 文档 SHALL 明确记录：被纳入训练的最终样本数 ≥ 8 000、每个数据源的版本号 / 访问日期、preprocessing pipeline、随机种子、超参。
4. THE Population_Prior_Trainer SHALL 输出 `models/population_prior.pkl`，序列化的对象内容为 `(age_bucket × sex × chronotype × season) → {env_setpoint_mean: EnvironmentParams, env_setpoint_variance: EnvironmentParams, sample_count: int}`。
5. THE Population_Prior_Trainer SHALL 用 hierarchical Bayesian 结构（个体级别的 setpoint ~ 高斯，组级别均值 ~ 高斯先验），不是简单按桶取均值；至少要使用层级收缩（partial pooling）以避免小样本桶的过拟合。
6. THE 最终 `models/population_prior.pkl` 文件大小 SHALL ≤ 5 MB（PR4 镜像体积约束的子约束）。
7. THE Population_Prior_Trainer SHALL 在固定随机种子下输出**比特级一致**的 `population_prior.pkl`（即 `sha256(file)` 在两次执行间不变），以便用户可以独立复现。

---

### Requirement 10: Population_Prior_Loader 运行时只读加载并按用户特征查询

**User Story:** 作为一名第 1 晚就装 add-on 的新用户，我希望 v3 在我第 1 晚就给出基于「与我同年龄段、同 chronotype、同季节」的临床受试者经验的 setpoint，而不是给我一个对所有人都相同的默认值。

**Bug Condition X:** v2.1.x 状态下 `cold_start_first_night_recommendation_quality == default_constants`：第 1 晚拿到的 `EnvironmentParams` 来自 `_DEFAULT_TARGETS` 表，与用户画像无关。

#### Acceptance Criteria

1. THE Population_Prior_Loader SHALL 实现于 `src/population_prior.py`，启动时从镜像内 `/app/models/population_prior.pkl` **只读**加载（用 `pickle.load` + 显式白名单类，禁止任意类反序列化）。
2. THE Population_Prior_Loader SHALL 暴露 `query(age_bucket, sex, chronotype, season) -> tuple[EnvironmentParams, EnvironmentParams]` 方法，返回 `(mean, variance)` 两份 `EnvironmentParams`。
3. WHEN add-on 启动且 Persistence_File 中 session 数量为 0 且 `population_prior_enabled == true`，THE SmartEnvironmentController SHALL 用 Population_Prior_Loader.query(...) 的均值作为第 1 晚 setpoint，而不是 `_DEFAULT_TARGETS`。
4. WHEN Persistence_File 中累计 session 数量增加到 ≥ 7，THE Bayesian_Recommender SHALL 把 Population_Prior 作为 GP 的先验均值函数 m(x)，而不是固定为 0；GP 后验仍由用户实际数据更新。
5. WHERE `user_profile.birth_year == 0`（用户未填年龄），THE Population_Prior_Loader SHALL 默认使用 `age_bucket = 26–35`（add-on 用户中位数桶）；同时把 `sensor.sleep_classifier_health` 上的 `prior_age_bucket_inferred` 标为 `true`。
6. WHERE Add-on_Manifest 的 `population_prior_enabled` 选项为 `false`，THE SmartEnvironmentController SHALL 在 cold start 阶段回退到 v2.1.x 的 `_DEFAULT_TARGETS` 路径。
7. IF `population_prior.pkl` 缺失或反序列化失败，THEN THE Algorithm_Fallback_Manager SHALL 让 Population_Prior_Loader 进入 disabled 状态、`sensor.sleep_classifier_health.prior_status = "load_failed"`，并让 SmartEnvironmentController 降级到 `_DEFAULT_TARGETS`。
8. THE Population_Prior_Loader SHALL 是**只读**的：运行时不写回 `population_prior.pkl`、不修改加载到内存的对象（dataclass `frozen=True`）。

#### Property-Based Correctness Properties

- **P10.1（不同 bucket 返回不同 prior）**: 对任意 `(age_bucket_1, sex, chronotype, season) ≠ (age_bucket_2, sex, chronotype, season)`（仅年龄段不同），返回的 `EnvironmentParams.temperature_c` 之差的绝对值在两个**相邻**桶之间应在 [0.1°C, 3.0°C]、跨度 ≥ 2 桶时在 [0.5°C, 4.0°C]。
- **P10.2（chronotype 单调性）**: 在固定 `(age_bucket, sex, season)` 下，`chronotype = morning` 的推荐入睡前 brightness ramp 起点时间应早于 `chronotype = evening` 至少 30 分钟（hierarchical prior 的产物，用合成校验数据 fixture 验证）。
- **P10.3（不可变性）**: `Population_Prior_Loader.query(...)` 返回的 `EnvironmentParams` 实例在调用方修改字段时抛 `FrozenInstanceError` 或 `dataclasses.FrozenInstanceError`（`frozen=True`）。

---

### Requirement 11: Edge_Transformer_Predictor 加载 ≤ 50 KB INT8 ONNX 模型预测下一 60 s stage

**User Story:** 作为对响应延迟敏感的用户，我希望 controller 不要等到 stage 已经从 LIGHT 跳到 DEEP 才开始降温（那时已经过去 30 s 了），而是能在 stage 切换前 60 s 提前开始；这要求 add-on 自己有一个本地 stage 预测模型。

**Bug Condition X:** v2.1.x 状态下 `stage_prediction_horizon_seconds == 0`：controller 只能根据可穿戴报上来的当前 stage 反应，因此每次 stage 切换天然滞后 30–60 s（取决于可穿戴粒度）。

#### Acceptance Criteria

1. THE Edge_Transformer_Predictor SHALL 实现于 `src/edge_stage_predictor.py`，加载 `/app/models/edge_stage_predictor.onnx`，模型文件大小 ≤ 50 KB。
2. THE Edge_Transformer_Predictor SHALL 使用 `onnxruntime`（INT8 量化推理），运行时不引入除 `onnxruntime` 之外的其它机器学习依赖。
3. THE Edge_Transformer_Predictor SHALL 暴露 `predict_next_stage(window: List[PhysioFeatureRow]) -> StagePrediction` 方法，输入是过去 5 分钟的 (HRV, 体动, 呼吸率, 当前 stage) 序列，输出 `StagePrediction(next_stage_probabilities: Dict[SleepStage, float], horizon_seconds: int)`。
4. THE Edge_Transformer_Predictor SHALL 把 `horizon_seconds` 固定为 60 s。
5. WHEN `predict_next_stage` 被调用，THE Edge_Transformer_Predictor SHALL 在 amd64 CI 环境（作为 Pi 4B 的 1.5× 上限代理）下 75 ms 内返回结果（即 Pi 4B 上的 50 ms 目标）。
6. THE ONNX_Runtime_Adapter SHALL 把 ONNX 推理调用通过 `await asyncio.to_thread(...)` 派发到线程池，**不得**在主事件循环上同步阻塞。
7. WHERE Add-on_Manifest 的 `edge_transformer_enabled` 选项为 `false`，THE SmartEnvironmentController SHALL 不实例化 Edge_Transformer_Predictor 也不加载 onnx 文件。
8. WHEN Edge_Transformer_Predictor 在过去 50 次预测中的 Hit_Rate ≥ 0.80，THE SmartEnvironmentController SHALL 在每次预测点把 setpoint anticipation 提前 60 s 应用（即「预测下一 60 s 是 DEEP 时立刻按 DEEP setpoint 调温」）。
9. WHEN Edge_Transformer_Predictor 的 Hit_Rate < 0.80，THE SmartEnvironmentController SHALL 不应用 60 s 提前下发；当前夜的 anticipation 退化到 v2.1.x 的 per-actuator anticipation。
10. IF onnx 模型文件缺失或 `onnxruntime` 加载失败，THEN THE Algorithm_Fallback_Manager SHALL 让 Edge_Transformer_Predictor 进入 disabled 状态，并写 `sensor.sleep_classifier_health.edge_predictor_status = "load_failed"`。
11. THE Edge_Transformer_Predictor SHALL 是**无状态**的：同一实例可被并发调用（不持有可变内部 buffer），便于 `asyncio.to_thread` 重入。

#### Property-Based Correctness Properties

- **P11.1（输出形状不变量）**: 对任意符合 schema 的 `window` 输入，`predict_next_stage(window).next_stage_probabilities` 的键集恰好等于 `{SleepStage.AWAKE, SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM}`。
- **P11.2（概率和为 1）**: 对任意有效输入，`sum(predict_next_stage(window).next_stage_probabilities.values())` 在 [0.999, 1.001] 之内（容忍 INT8 量化与 float32 转换误差）。
- **P11.3（无副作用）**: 同一 Edge_Transformer_Predictor 实例对相同 `window` 的两次预测返回**比特级一致**的概率分布（推理是确定性的）。

---

### Requirement 12: Edge_Transformer_Predictor 训练流程可复现

**User Story:** 作为算法可信性的把关者，我希望 edge transformer 的训练数据来源、网络结构、量化流程都被 `docs/V3_TRAINING_RECIPE.md` 完整记录，以便审计者能在不同硬件上重新跑出同等大小、同等命中率的模型。

**Bug Condition X:** v2.1.x 状态下不存在 edge transformer。任何引入 ML 模型的尝试如果只 commit `.onnx` 二进制而不附训练 recipe，会导致后继维护无法追溯（属于 ML supply-chain risk）。

#### Acceptance Criteria

1. THE Training_Recipe 文档 SHALL 包含 edge transformer 的输入特征定义、序列长度、网络结构（层数、隐藏维度、注意力头数）、训练数据来源（Sleep-EDF 约 200 晚 PSG 含完整生理信号）、随机种子、量化流程（fp32 → INT8 静态量化）。
2. THE Training_Recipe SHALL 记录用于声明 Hit_Rate ≥ 0.80 的 held-out 测试集划分方法（按受试者切分，避免同一人横跨 train/test）。
3. THE 训练脚本 SHALL 实现于 `scripts/train_edge_stage_predictor.py`，**不**进入 Add-on_Image。
4. THE 训练脚本 SHALL 输出固定文件名 `models/edge_stage_predictor.onnx` 与同名 `.json` metadata（包含 sha256、训练日期、Sleep-EDF 数据版本、命中率）。
5. THE CI_Pipeline 的 release workflow SHALL 在镜像构建前校验 `models/edge_stage_predictor.onnx` 的 sha256 与 `.json` metadata 中的值一致；不一致则失败。


### Requirement 13: PR1 测试覆盖 — 4 个新模块各 ≥ 90% 行覆盖且各带 ≥ 1 条 correctness property

**User Story:** 作为代码评审者与未来维护者，我希望 4 个新模块每一个都有自己单独的高覆盖率测试 + 至少 1 条 property-based correctness 测试，以便后续重构时回归风险被自动捕获，并维持仓库整体「~92% 覆盖率」的工程基线不下降。

**Bug Condition X:** v2.1.x 状态下 `test_line_coverage_new_modules_pct == 0.0`、`has_pbt_property_per_new_module == false`：因为这 4 个模块尚不存在。v3 引入后若不上同等强度的测试，会让仓库整体覆盖率下降。

#### Acceptance Criteria

1. THE Add-on_Repository SHALL 在 `tests/` 下新增以下 4 个测试文件，每个对应一个新模块且行覆盖率 ≥ 90%（`pytest --cov=src.bayesian_recommender,src.causal_attributor,src.population_prior,src.edge_stage_predictor` 报告必须满足）：
   - `tests/test_bayesian_recommender.py`
   - `tests/test_causal_attributor.py`
   - `tests/test_population_prior.py`
   - `tests/test_edge_stage_predictor.py`
2. THE 上述 4 个测试文件 SHALL 各自包含至少 1 条**手写 property-based** 测试（参数化或多次随机种子循环，**不**引入 hypothesis 库 —— 与 tech.md 一致），分别对应 Requirement 2–11 中标注的 P*.* 属性。
3. THE 仓库整体行覆盖率（`pytest --cov=src --cov=scripts`）SHALL ≥ v2.1.x 发布版本的覆盖率减 0.5 个百分点（即 ≥ 91.5%），实际目标维持在 ~92%。
4. THE CI 的 `test.yml` workflow SHALL 在 PR 中跑 `pytest --cov` 并在覆盖率不达标时返回非零退出码。

---

### Requirement 14: PR2 现有 sensor 契约稳定 — 20 个 v2.1.0 sensor 不改 entity_id / attributes

**User Story:** 作为已经在 Lovelace 配置好仪表板的 v2.1.x 用户，我升级到 v3.0.0 后不希望任何现有 sensor 失效或字段消失，所有 v2.1.x 仪表板模板 / automation 在 v3.0.0 下应零修改可用。

**Bug Condition X:** v2.1.x 状态下 `sensor_contract_v2_1_0_preserved` 默认为 `true`。任意 v3 改动若**新增 attribute** 之外还**重命名**或**删除** v2.1.x 已有的 entity_id 或 attribute key，都会让该不变量翻为 `false`，破坏用户仪表板。

#### Acceptance Criteria

1. THE Sensor_Contract_Guard SHALL 在 `tests/test_sensor_contract_v2_1_0.py` 中维护一份「v2.1.0 GA 时点的 20 个 `sensor.sleep_classifier_*` entity_id + 每个 sensor 的核心 attribute key 集合」的 frozen snapshot（JSON fixture）。
2. THE Sensor_Contract_Guard SHALL 在 CI 中对每次 PR 跑 snapshot 比对：v3 实际发布的 sensor entity_id 集合**必须**是 snapshot 的超集；每个 v2.1.x sensor 的 attribute key 集合也**必须**是 snapshot 中对应集合的超集。
3. THE V3_Core_Algorithm_Suite 引入的新 attribute（如 `posterior_mean`、`posterior_variance`、`acquisition_branch`、`cumulative_regret`、`attribution_items`、`prior_age_bucket_inferred`、`edge_predictor_status`）SHALL **只新增**到现有 sensor 的 attribute 字典，不删除、不重命名既有 key。
4. WHERE 新增信息更适合独立成 sensor，THE LearningPanelPublisher SHALL 通过 `sensor.sleep_classifier_v3_*` 命名前缀表达（如 `sensor.sleep_classifier_v3_se_uplift_pct`），不污染既有 20 个 sensor 的语义。
5. IF 任何 PR 要求**删除或重命名** v2.1.0 sensor 的 entity_id 或 attribute key，THEN THE PR SHALL 必须显式更新 snapshot fixture 并在 PR description 中标注 BREAKING CHANGE，由人工审批方可合并（CI 会拒绝默认的 silent 修改）。

---

### Requirement 15: PR3 向后兼容 — v2.1.x 的 user_preferences.json 必须能加载

**User Story:** 作为已经在 v2.1.x 累计了 60 晚睡眠数据的用户，我升级到 v3.0.0 后不希望丢失任何历史数据；新模块需要的额外字段如果旧文件没有，应该用安全默认值补齐。

**Bug Condition X:** v2.1.x 状态下 `persistence_v2_1_x_loadable == true`。任意 v3 schema 变更若没有 forward-compatible loader，都会让旧 `/data/user_preferences.json` 在 v3 下加载失败、用户被迫从 0 开始。

#### Acceptance Criteria

1. THE Persistence_Migration_Layer SHALL 在 `PreferenceLearner` 与 `Bayesian_Recommender` 共用的 `load_from_disk()` 路径中接受 v2.1.x 旧 schema 的 JSON 文件并成功加载。
2. WHEN 旧 schema 中缺失 v3 新增字段（如 `posterior_state`、`hit_rate_window`、`attribution_history`），THE Persistence_Migration_Layer SHALL 用 schema 各字段定义的 safe-default 值补齐，且写入日志 `migrating /data/user_preferences.json from v2.1.x → v3.0.0 (added N missing fields)`。
3. THE Persistence_Migration_Layer SHALL 在加载完成后立即调用 `src._io_utils.atomic_write_json` 把补齐后的字典写回 `/data/user_preferences.json`，确保下次启动时已是 v3 schema。
4. WHEN add-on 第一次升级到 v3.0.0 启动，THE Persistence_Migration_Layer SHALL 把原文件备份为 `/data/user_preferences.v2_backup.json` 一次（如果不存在），以便用户能手工回滚到 v2.1.x。
5. IF JSON 文件损坏或不能解析为 dict，THEN THE Persistence_Migration_Layer SHALL 重命名为 `/data/user_preferences.corrupted.<timestamp>.json` 并以空 history 启动；不得让 add-on 启动失败。
6. THE 仓库 SHALL 在 `tests/test_persistence_migration.py` 中包含至少 3 个 v2.1.x 旧 schema 的 fixture JSON 文件（empty history、典型 14 晚 history、边界 schema），并断言全部能成功加载。

---

### Requirement 16: PR4 镜像体积 — 加上 4 个新模块 + Population Prior + onnxruntime 不超过 35 MB

**User Story:** 作为对 Pi 4B 内存与 SD 卡空间敏感的用户，我希望 v3.0.0 镜像不超过 35 MB（v2.1.0 是 15 MB 上下），即便包含 ONNX Runtime + 5 MB Population Prior 资产。

**Bug Condition X:** v2.1.x 状态下 `image_size_mb` ≈ 15。引入 onnxruntime（aarch64 wheel ~ 12 MB）+ population_prior.pkl（≤ 5 MB）+ edge_stage_predictor.onnx（≤ 50 KB）+ 4 个新 src 模块若不做控制，可能超过 40 MB。

#### Acceptance Criteria

1. THE Image_Build_Auditor SHALL 在 `.github/workflows/addon-build.yml` 新增一个步骤，对构建出的镜像执行 `docker images --format '{{.Size}}'` 并断言 ≤ 35 MB。
2. THE Add-on_Image SHALL 仅在 `requirements-runtime.txt` 中添加 **一行** 新依赖：`onnxruntime==<pinned_version>`，其它新模块完全用 stdlib。
3. THE `requirements-runtime.txt` SHALL 把 `onnxruntime` 版本号 pin 到具体小版本（不允许 `>=`），并在 v3.0.0 发布时通过 CI 验证 aarch64 与 amd64 wheel 都存在。
4. THE Image_Build_Auditor SHALL 在 CI 输出镜像内 top-10 大文件清单（`du -sh` 排序），便于人工审查体积来源。
5. IF 任何 PR 让镜像体积超过 35 MB，THEN THE CI SHALL 失败并在 PR comment 中给出体积差与 top-10 大文件 diff。

---

### Requirement 17: PR5 可降级 — 4 个新模块异常时优雅 fallback 到 v2.1.x 行为

**User Story:** 作为运行 add-on 的用户，我不希望 v3 任何一个新算法层出 bug 就让整个 add-on crash；任意一个模块抛异常应该自动降级到 v2.1.x 同语义对应行为，且我能在诊断 sensor 上看到「哪个模块 fallback 了」。

**Bug Condition X:** v2.1.x 状态下 `fallback_path_exists_per_new_module == false`：因为 4 个新模块都不存在。v3 引入后若没有 fallback 包装，任意算法层异常都会冒泡到 `scripts/run_ha_smart_service.py` 的事件循环，触发 add-on 重启循环。

#### Acceptance Criteria

1. THE Algorithm_Fallback_Manager SHALL 暴露 `with_fallback(component_name: str, primary: Callable, legacy: Callable)` 装饰器/上下文管理器，对 4 个新模块的入口函数做 try/except 包装。
2. WHEN Bayesian_Recommender 在 `recommend()` 中抛任何异常，THE Algorithm_Fallback_Manager SHALL 捕获并调用 PreferenceLearner_Legacy.recommend() 返回结果；同时把 `sensor.sleep_classifier_health.bayesian_status = "fallback_active"`。
3. WHEN Causal_Attributor 抛异常，THE Algorithm_Fallback_Manager SHALL 把 `attribution_items` 写为空 list，`sensor.sleep_classifier_health.causal_status = "fallback_active"`，主流程 `recommend()` 不受影响。
4. WHEN Population_Prior_Loader 加载失败或 query 抛异常，THE Algorithm_Fallback_Manager SHALL 让 SmartEnvironmentController 降级到 `_DEFAULT_TARGETS`，`sensor.sleep_classifier_health.prior_status = "load_failed" | "query_failed"`。
5. WHEN Edge_Transformer_Predictor 抛异常或 Hit_Rate < 0.80，THE Algorithm_Fallback_Manager SHALL 让 SmartEnvironmentController 退到 v2.1.x 的 per-actuator anticipation，`sensor.sleep_classifier_health.edge_predictor_status = "fallback_active" | "below_hit_rate_threshold" | "load_failed"`。
6. THE Algorithm_Fallback_Manager SHALL 在每次 fallback 触发时通过 `logger.warning` 记录组件名、异常类型、traceback 摘要；**不得**记录任何 HA 长效令牌或 Supervisor token（与 tech.md 一致）。
7. WHEN 同一模块在 24 小时内连续触发 ≥ 5 次 fallback，THE Algorithm_Fallback_Manager SHALL 把对应模块标记为 `disabled_until_restart` 并停止再尝试 primary 路径，避免反复抛异常拖慢主循环。

---

### Requirement 18: 跨模块 end-to-end 单晚动作数不超过 v2.1.x baseline 的 1.5×

**User Story:** 作为对 HA 设备状态历史敏感的用户（用 InfluxDB 或 Recorder 的人），我担心 v3 的 4 个新算法层叠加后会让 controller 在一晚内频繁下发指令，污染历史时间线。我希望 v3 在合理仿真下，单晚 service call 总数不超过 v2.1.x baseline 的 1.5 倍。

**Bug Condition X:** v2.1.x 状态下 baseline 单晚 service call 数大致已知（在 `tests/test_smart_environment_controller_e2e.py` 中量化）。v3 4 个模块叠加可能在 explore 分支 + edge transformer 提前 + counterfactual 触发等情况下超调。

#### Acceptance Criteria

1. THE 仓库 SHALL 在 `tests/test_v3_e2e_action_budget.py` 用合成 8 小时夜次 fixture 跑 v2.1.x 与 v3 两个配置的 SmartEnvironmentController，分别统计 `ha_api_client.call_service` 调用次数。
2. THE 测试 SHALL 断言：跨任意 ≥ 30 个 fixture 种子下，`v3_action_count(seed) ≤ v2_1_x_action_count(seed) × 1.5`。
3. WHEN 该比率被违反，THE 测试 SHALL 失败并打印超调来源（`acquisition_branch == "explore"` 触发的 explore 调用次数、edge anticipation 触发的提前调用次数）。
4. THE V3_Core_Algorithm_Suite SHALL 通过 `min_seconds_between_actions`（v2.1.x 已有，默认 120s）以及 deadband 在 explore 分支同样生效，避免 explore 引发频繁微调。

#### Property-Based Correctness Properties

- **P18.1（动作预算不变量）**: 对任意合成 8 小时夜次 fixture（≥ 30 种子），`v3_action_count(seed) / v2_1_x_action_count(seed) ≤ 1.5`。
- **P18.2（v3 不会让 SE 退步）**: 对同一组 fixture 与同一组合成 reward function，v3 配置下的「LIGHT/DEEP/REM 时间分配偏离理想分布的 L1 距离」不大于 v2.1.x 配置下的同距离 + 5%。

---

### Requirement 19: Add-on_Manifest 扩展 4 个开关，全部默认 true

**User Story:** 作为给 v3 早期 beta 用户提供「灰度回滚」能力的产品维护者，我希望 4 个新模块每个都对应一个独立 Add-on 配置开关，用户可以单独关闭其中任意一个，剩下三个继续工作。

**Bug Condition X:** v2.1.x 状态下 Add-on_Manifest 没有 4 个新开关。v3 上线后若没有粒度化开关，用户碰到任意一个模块的边界 case 都得整个回滚到 v2.1.x。

#### Acceptance Criteria

1. THE Add-on_Manifest 的 `options` 块 SHALL 新增 4 个布尔开关，默认值全部为 `true`：
   - `bayesian_enabled: true`
   - `causal_enabled: true`
   - `population_prior_enabled: true`
   - `edge_transformer_enabled: true`
2. THE Add-on_Manifest 的 `schema` 块 SHALL 同步新增对应 4 项 `bool?` 类型校验（与既有 `telemetry_enabled` 等行字段一致的可选 bool）。
3. THE `training_config/config_loader.py` SHALL 把 4 个开关从 add-on options / 环境变量 / `config.json` 中合并到运行时 config 字典，缺失时按上述默认值补齐。
4. WHERE 任意一个开关被设为 `false`，THE 对应模块 SHALL 不被实例化、不加载任何资产文件、不消耗 Pi 4B 内存。
5. THE Add-on_Manifest 的 `options` 块 SHALL 同步新增 `bayesian_exploit_probability: float(0,1) = 0.9` 与 `causal_cold_start_nights: int(7,90) = 30` 两个可调标量。
6. THE `sleep_classifier/DOCS.md` SHALL 在 v3 章节用一个 markdown 表格列出全部 6 个新选项（4 个 bool + 2 个标量）的语义、默认值、灰度回滚指引。

---

### Requirement 20: 运行时依赖白名单 — 新模块只允许 stdlib + aiohttp + onnxruntime

**User Story:** 作为对 add-on 镜像供应链安全敏感的运维者，我希望 v3 引入算法层后，运行时依赖只比 v2.1.x 多 1 项（`onnxruntime`），不得「悄悄 pip install」其它科学计算库。

**Bug Condition X:** v2.1.x 状态下 `runtime_dependencies == {aiohttp}`。任意 PR 若引入 numpy / scipy / GPy / dowhy / causalml / sklearn 中的任何一个，会让 `DEP_runtime_only_aiohttp_and_onnxruntime` 不变量翻为 false。

#### Acceptance Criteria

1. THE `requirements-runtime.txt` SHALL 仅包含：`aiohttp >= 3.9.0` 与 `onnxruntime==<pinned_version>` 两行；不得新增其它非 stdlib 包。
2. THE CI_Pipeline SHALL 新增一个 step 跑 `pip-compile --dry-run` 或等价的依赖图检查，确认运行时依赖闭包不含 numpy / scipy / GPy / dowhy / causalml / sklearn。
3. THE `src/bayesian_recommender.py`、`src/causal_attributor.py`、`src/population_prior.py` SHALL 在 import 段不出现 `numpy` / `scipy` / `gpy` / `dowhy` / `causalml` / `sklearn`（CI 用 `grep` 检查）。
4. THE `src/edge_stage_predictor.py` SHALL 仅 import `onnxruntime`、stdlib 与项目内部模块；不得 import 其它 ML 框架。
5. IF 未来某天必须引入 numpy（例如向量化优化），THEN PR SHALL 在 description 中显式声明，并先更新 `tech.md` 的「明确不再使用」清单与 v3-core-algorithm-moat 本 spec 的 Requirement 20，由人工评审，CI 不会自动放行。

---

### Requirement 21: 文档 — `docs/V3_TRAINING_RECIPE.md` 与 README v3 章节

**User Story:** 作为打算复现 v3 算法承诺的同行评审者或贡献者，我希望仓库提供一份「拿到这份 recipe 就能在自己机器上跑出比特级一致的 population_prior.pkl 与等价 hit-rate 的 edge_stage_predictor.onnx」的训练 recipe；并希望 README 顶部有 v3 算法护城河的高层叙述。

**Bug Condition X:** v2.1.x 状态下不存在 `docs/V3_TRAINING_RECIPE.md`，README 也没有提到任何贝叶斯 / 因果 / population prior / edge transformer。即便代码完成，外部读者依然无法识别 v3 的算法差异化。

#### Acceptance Criteria

1. THE 仓库 SHALL 在 `docs/V3_TRAINING_RECIPE.md` 提供一份自包含训练 recipe，覆盖：MESA + SHHS + Sleep-EDF 数据获取与许可说明、preprocessing pipeline、Population_Prior_Trainer 超参与种子、Edge transformer 网络结构与量化流程、复现验证（sha256 比对、hit-rate 比对）。
2. THE README.md SHALL 在「v3.0.0」章节用 ≤ 6 段文字 + 1 张架构图 简述 4 大算法方向与 5 条 PR 工程不变量，并链接到 `docs/V3_TRAINING_RECIPE.md`。
3. THE `sleep_classifier/DOCS.md` SHALL 增加「v3 升级路径」章节，明确告诉 v2.1.x 用户：升级到 v3.0.0 是 in-place（重启 add-on 即可），首次启动会自动完成 Persistence_Migration_Layer 迁移与备份。
4. THE `CHANGELOG.md` SHALL 在 v3.0.0 段落中按 4 大方向 + 5 条 PR 不变量分小节列出新增能力与不变量保证；BREAKING CHANGES 小节明确为空（v3 设计上不破坏 v2.1.x 行为契约）。

---

## Iteration and Feedback Rules

- 本 spec 是 v3.0.0 的起点；如评审过程中发现某条 user story 触碰到 PR1–PR5 之外的隐藏不变量（如 v2.1.0 telemetry 字段、apnea detector 接入、smart_wake 与 edge transformer 的耦合），应在本文档增补并同步刷新 Glossary 与 Bug Condition C(X)。
- 每条 user story 引用的「v2.1.x 旧行为」凡涉及具体常量（28 晚、30 晚、200 ms、500 ms、50 ms、35 MB、5 MB、50 KB、1.5×、0.8 hit rate、0.9 exploit probability），均允许在 design 阶段微调，但本 requirements 文档将作为该次微调的唯一基准锚点。
- 4 个新模块的内部实现细节（GP kernel 类型、Heckman 第一阶段是 probit 还是 logit、edge transformer 的注意力头数）属于 design.md 决策范围，本文档刻意不固定。
