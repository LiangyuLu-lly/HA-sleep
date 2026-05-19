# Implementation Plan: algorithmic-moat-v3.0.0

> Spec: algorithmic-moat-v3.0.0
> Workflow: requirements-first（feature spec）
> 关联：`.kiro/specs/algorithmic-moat-v3.0.0/requirements.md`、`.kiro/specs/algorithmic-moat-v3.0.0/design.md`

## Overview

本实施计划将 design.md 描述的 4 个算法方向（BAO / CAE / PP / EMST）+ 横切契约（PR1–PR6）转换为可由编码 agent 增量执行的代码任务列表。

执行原则：

- **加层而非改写**：v2.x 既有模块（`preference_learner` / `smart_environment_controller` / `external_stage_subscriber` / `sleep_state_publisher` / `_io_utils` / `config_loader`）的现有公开 API **逐字保留**；新功能仅通过新增方法（如 `add_session_listener`、`set_setpoint_provider`）接入。
- **平铺式 `src/`**：4 个新模块各自独占一个 `src/<module>.py`，对应 `tests/test_<module>.py`，遵循 structure.md 的「一职责一文件」规则。
- **PR3 持久化契约**：所有新文件（`bao_model.pickle`、`causal_factors.jsonl`、`predictor_audit.jsonl`）必须走 `src/_io_utils.py` 中本期新增的 `atomic_write_bytes` / `atomic_append_jsonl`。
- **PR1 dry_run 契约**：4 个新模块**不直接调用** `ha_client.call_service`；提前控制路径全部经由 `SmartEnvironmentController` 既有方法转发，确保 `dry_run=true` 一处守护即可阻断。
- **优雅降级**：任一新模块 import / 加载 / 运行时异常 ≥ 3 次 → 自动停用，主流程继续；4 个 flag 全 false 时字节级等价于 v2.1.0。

## Tasks

- [x] 1. 依赖治理与 `_io_utils` 扩展（PR3 / PR4 / R12 基础设施）
  - [x] 1.1 更新 `requirements-runtime.txt` 与 `requirements.txt`，新增 `requirements-train.txt`
    - 在 `requirements-runtime.txt` 追加 `numpy>=1.24,<2.0`、`scipy>=1.10,<2.0`、`onnxruntime>=1.16,<2.0`，每行附中文注释说明对应模块（BAO / CAE / EMST）
    - 在 `requirements.txt` 追加 `hypothesis>=6.92.0,<7.0`（仅 dev，PBT 用）
    - 创建 `requirements-train.txt`（开发者机器，**不进** add-on 镜像）：`torch / pandas / pyEDFlib / onnx / matplotlib / nsrr-toolkit`，全部带版本范围
    - _Requirements: 12.1, 12.5, 12.6_

  - [x] 1.2 在 `src/_io_utils.py` 新增 `atomic_write_bytes` 与 `atomic_append_jsonl`
    - `atomic_write_bytes(path, data)`：与现有 `atomic_write_json` 同语义（tmpfile + `os.replace`），但接受 `bytes`，用于 BAO 持久化 pickle
    - `atomic_append_jsonl(path, record, *, max_lines=None)`：原子追加一条 JSON 行；`max_lines` 非空时按 FIFO 截断（读全文 + 追加 + atomic_replace），CAE / EMST 用
    - 类型注解齐全，docstring 跟随既有模块的中文注释风格
    - _Requirements: 1.6, 4.2, 4.3, 10.2_

  - [x] 1.3 编写 `_io_utils` 扩展的单元测试与原子性 property
    - `tests/test_v3_atomic_writes.py` 新建
    - **Property 18 (X1): PR3 持久化原子性**
    - **Validates: Requirements 1.6, 4.2, 10.2**
    - 入口函数 `test_property_x1_atomic_writes_survive_interrupt_injection`：用 monkeypatch 在 `os.replace` 之前/之后 raise `OSError`，断言事后磁盘文件要么是上一稳定版本要么是新提交版本，不存在中间损坏状态
    - 同时覆盖 `max_lines` FIFO 截断的算术正确性（example-based）
    - _Requirements: 1.6, 4.2, 10.2_

- [x] 2. PopulationPrior 模块（R7、R8 — 新用户冷启动）
  - [x] 2.1 在 `src/population_prior.py` 实现数据结构与 repository
    - 定义 `AgeBand / Sex / Chronotype / Season / BucketKey` 类型别名
    - 定义 `PriorBucket` / `PriorMetadata` / `PopulationPrior` 三个 frozen slots dataclass（与 design §3.1.1 对齐）
    - 实现 `PopulationPriorRepository`：`load(path)`（含 SHA-256 + 大小 ≤ 8 MB 校验，失败返回 `None`）、`lookup(age_band, sex, chronotype, season)` 返回 `(bucket, fallback_level)`，`fallback_level ∈ {0,1,2,3}`
    - `lookup` 兜底策略：优先返回 `n_samples >= 50` 的桶；不足则按 sex → chronotype → age_band 顺序逐层放宽，记录 fallback_level
    - 提供 `expected_size_bytes()` 给构建期 guard 用
    - 启动期一次性打印 NSRR DUA 摘要 + DOI INFO 日志（R14.1）
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 8.1, 8.6, 14.1_

  - [x] 2.2 PopulationPrior 桶均值生理范围 property
    - `tests/test_population_prior.py` 新建
    - **Property 6: Prior pickle 桶均值在合理生理区间内**
    - **Validates: Requirements 7.2, 7.4**
    - 入口函数 `test_property_p6_all_bucket_means_within_physiological_range`：对所有桶（含 fallback 层）断言 `temperature_mean_c ∈ [16, 28]`、`humidity_mean_pct ∈ [30, 70]`、`brightness_mean_pct ∈ [0, 50]`
    - 使用合成 prior（不依赖真实 MESA / SHHS 数据）
    - _Requirements: 7.2, 7.4_

  - [x] 2.3 PopulationPrior 桶兜底 property
    - 入口函数 `test_property_p7b_lookup_fallback_finds_large_bucket`
    - **Property 15: Prior 桶兜底始终命中大样本桶**
    - **Validates: Requirements 8.6**
    - 用 hypothesis 生成混合大/小样本桶树，断言 `lookup(...)` 返回的桶满足 `n_samples ≥ 50` 或 `fallback_level == 3`（已到根桶）
    - _Requirements: 8.6_

  - [x] 2.4 PopulationPrior 单元测试（metadata 校验、加载失败降级）
    - `test_load_returns_none_on_size_exceed_8mb`
    - `test_load_returns_none_on_sha256_mismatch`
    - `test_load_returns_none_on_missing_file`
    - `test_dua_log_printed_once_at_load`（R14.1）
    - _Requirements: 7.3, 8.1, 14.1_

- [x] 3. BayesianOptimizer 模块（R1、R2、R3 — GP + Thompson Sampling）
  - [x] 3.1 在 `src/bayesian_optimizer.py` 实现 GP 后验 + Thompson Sampling
    - 定义 `GPHyperparams` / `GPObservation` / `GPRecommendation` / `BAOPersistedState` 四个 dataclass（与 design §3.2.1 + §4.1.1 对齐）
    - 实现 `BayesianOptimizer.__init__`、`load_or_init`、`observe`、`recommend`、`posterior_uncertainty`、`persist`、`export_hyperparams_json`
    - GP 用 `numpy + scipy.linalg.cho_factor / cho_solve` 实现 RBF kernel 后验，cholesky 失败 raise `GPNumericalError`（caller 捕获后回退 v2.x 路径，error_count +1）
    - 决策路径：N < 5 时仅用 prior（`mode="prior-only"`）；N ≥ 5 时按 `exploration_rate` 概率走 explore（取 σ 最大点）或 exploit（Thompson Sample）
    - `prior_weight(N) = max(0.1, exp(-N / 14))`，N=0 时为 1.0；用户在 Web UI 锁定 `prior_weight_lock` 时直接使用锁定值（R8.5）
    - `wind_down=True` 或维度被锁定时强制 exploit（R2.3 / R2.5）
    - 决策伪随机种子 = `hash(install_id + ISO-date)`（R2.6），保证可重复
    - FIFO 滚动保留最近 60 个 observation（R1.6）
    - _Requirements: 1.1, 1.2, 1.3, 1.5, 1.6, 1.7, 2.1, 2.2, 2.3, 2.5, 2.6, 8.4, 8.5_

  - [x] 3.2 在 `src/smart_environment_controller.py` 增加 `set_setpoint_provider` hook
    - 新增方法签名 `set_setpoint_provider(provider: Callable | None)`，默认 None（v2.x 行为）
    - 决策路径：provider 非 None 时优先调用；返回 `None` 或抛异常时回退 `_compute_target_via_learner`（既有 v2.x 路径）+ error_count +1
    - 不修改任何现有方法签名（PR2 不变量）
    - _Requirements: 1.4, 11.3, 11.4_

  - [x] 3.3 持久化 BAO 状态到 `/data/bao_model.pickle`
    - 启动期 `load_or_init` 走 `Path.read_bytes` + `pickle.loads`；失败时初始化空状态 + log WARN
    - `persist()` 走 `_io_utils.atomic_write_bytes`（PR3）
    - 周期持久化：每次 `observe` 后通过 `asyncio.create_task` fire-and-forget 调用 `persist`，避免阻塞主循环
    - 持久化 task 在 `_v3_tasks` 注册（PR5 优雅退出依赖此清单）
    - _Requirements: 1.6_

  - [x] 3.4 BAO P1 后验单调性 property
    - `tests/test_bayesian_optimizer.py` 新建
    - **Property 1: GP 后验更新单调性**
    - **Validates: Requirements 1.3, 1.6**
    - 入口函数 `test_property_p1_observe_does_not_increase_local_variance`
    - 用 hypothesis 生成任意已观测状态 + 任意新 observation，断言 `posterior_uncertainty(at=obs.x)` 在 `observe(obs)` 之后不大于之前
    - _Requirements: 1.3, 1.6_

  - [x] 3.5 BAO P2 探索率收敛 property
    - 入口函数 `test_property_p2_exploration_rate_converges_to_config`
    - **Property 2: Thompson Sampling 探索率长期收敛**
    - **Validates: Requirements 2.2, 2.3, 2.5**
    - 用 hypothesis 在 `exploration_rate ∈ [0, 0.5]` 上抽样，跑 ≥ 100 次决策，断言实际 explore 比例与配置值偏差 ≤ 0.02
    - _Requirements: 2.2, 2.3, 2.5_

  - [x] 3.6 BAO P13 wind-down 与维度锁定强制 exploit property
    - 入口函数 `test_property_p2b_wind_down_or_locked_forces_exploit`
    - **Property 13: wind-down 与维度锁定强制 exploit**
    - **Validates: Requirements 2.3, 2.5**
    - 断言 `in_wind_down=True` OR 锁定集合非空时 `mode == "exploit"`，锁定维度的 setpoint 等于 GP 后验均值（不抽样）
    - _Requirements: 2.3, 2.5_

  - [x] 3.7 BAO P7 prior_weight 衰减 property
    - 入口函数 `test_property_p7_prior_weight_decay_curve`
    - **Property 7: Prior 权重在 N=0 时 = 1.0；N=14 时 ≤ 0.1（指数衰减）**
    - **Validates: Requirements 8.4, 8.5**
    - 断言 `prior_weight(0) ≈ 1.0`、`prior_weight(14) ≤ 0.1`、关于 N 单调不增；用户锁定为 0 时实际生效值 = 0
    - _Requirements: 8.4, 8.5_

  - [x] 3.8 BAO 单元测试与性能 budget
    - `test_observe_within_200ms_budget`：60 个 observation 状态下 `observe()` ≤ 200 ms（CI 容忍 ×1.5 = 300 ms）
    - `test_cholesky_failure_raises_gp_numerical_error_then_fallback`：注入奇异矩阵，断言 raise + caller 回退路径
    - `test_persist_uses_atomic_write_bytes`：mock `_io_utils.atomic_write_bytes` 验证被调用
    - `test_export_hyperparams_json_returns_plain_dict`（forward-compat 钩子，仅基础类型）
    - _Requirements: 1.3, 1.4, 1.6_

- [x] 4. CausalAttribution 模块（R4、R5、R6 — 因果归因）
  - [x] 4.1 在 `src/causal_attribution.py` 实现 DAG + 反事实 estimator
    - 定义模块级常量 `CAUSAL_DAG`（6 因子邻接表，R4.1）、`ALL_FACTORS` tuple
    - 定义 `CausalFactorRecord` / `CausalEffect` / `AttributionResult` dataclass
    - 实现 `CausalAttributionEngine.__init__`、`on_session`、`attribute`、`n_records`、`export_dag_json`
    - `on_session` 走 `_io_utils.atomic_append_jsonl(jsonl_path, record, max_lines=90)`（PR3 + R4.3 FIFO 滚动）
    - `attribute` 内部用 `asyncio.wait_for(asyncio.to_thread(self._run_estimator, ...), timeout=5.0)`，超时返回 `status="timeout"` 不抛异常
    - estimator：do-calculus 调整 + Heckman 两阶段回归 + ≥ 200 次 bootstrap 95% CI；纯 Python + numpy（不引入 networkx / dowhy / statsmodels）
    - 因子文件 < 30 晚 → `status="insufficient_data"`；当晚 quality ≥ 个人 30 天均值 → `status="nominal"` 不计算（R5.3）
    - 每个因子非缺失观测 < 5 时 `effect_pp = NaN`、`is_significant=False`（R5.6）
    - 95% CI 跨 0 时 `explanation_zh` 末尾追加「（统计显著性弱）」（R6.2）
    - 生成 `explanation_zh` 时按 R5.2 模板拼接 `top_factor` / `top_effect_pp` / `counterfactual_score`
    - `install_id_hash = sha256(install_id)` 永远不存原 install_id（R14.2）
    - _Requirements: 4.1, 4.2, 4.3, 4.5, 4.6, 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 6.1, 6.2, 14.2_

  - [x] 4.2 在 `src/preference_learner.py` 增加 `add_session_listener` hook
    - 新增 `add_session_listener(listener: Callable[[SleepSession], Awaitable[None]])`，默认 listener 列表为空
    - 在 `record_session` 持久化成功后通过 `asyncio.create_task` fire-and-forget 调用所有 listener
    - listener 抛异常仅 log + 计数，不传播；不修改 `record_session` 既有签名（PR2）
    - 注册的 task 加入 `_v3_tasks` 用于 PR5 优雅退出
    - _Requirements: 4.2, 11.3_

  - [x] 4.3 CAE P14 CausalEffect CI 一致性 property
    - `tests/test_causal_attribution.py` 新建
    - **Property 14: CausalEffect CI 一致性与最小观测数**
    - **Validates: Requirements 5.6, 6.1, 6.2**
    - 入口函数 `test_property_p4b_effect_within_ci_bounds`：对 hypothesis 生成的合成数据断言 `ci_low ≤ effect_pp ≤ ci_high`；非缺失观测数 < 5 时 `effect_pp` 为 NaN 且 `is_significant=False`
    - _Requirements: 5.6, 6.1, 6.2_

  - [x] 4.4 CAE P5 attribute 性能 property
    - 入口函数 `test_property_p5_attribute_within_5s_on_synthetic_30_to_90_nights`（marked `@pytest.mark.slow`）
    - **Property 5: 反事实推断耗时 ≤ 5 秒**
    - **Validates: Requirements 5.4**
    - 在合成 30/60/90 晚样本上断言耗时 ≤ 5 秒（CI 放宽 ×1.5 = 7.5 秒）
    - 配套 `test_property_p5b_estimator_timeout_returns_timeout_status`：mock 阻塞 6 秒的 estimator，验证返回 `status="timeout"` 且不污染既有状态
    - _Requirements: 5.4_

  - [x] 4.5 CAE 单元测试
    - `test_status_insufficient_data_when_records_lt_30`
    - `test_status_nominal_when_quality_above_personal_mean`
    - `test_explanation_zh_appends_significance_warning_when_ci_crosses_zero`
    - `test_install_id_never_stored_raw`（R14.2）
    - `test_export_dag_json_schema_v1`（forward-compat）
    - _Requirements: 4.4, 5.2, 5.3, 6.2, 14.2_

  - [x] 4.6 CAE P4 null 因子 CI 覆盖率 property（slow）
    - `tests/test_causal_attribution_synthetic.py` 新建（marked `@pytest.mark.slow`）
    - **Property 4: 因果效应估计在已知 null 因子上 95% CI 覆盖率 ≥ 92%**
    - **Validates: Requirements 4.6, 6.1**
    - 入口函数 `test_property_p4_null_factor_ci_coverage_at_least_92pct`：合成 ground-truth DAG（含至少 1 个真实效应为 0 的因子），重复 200 次 bootstrap，断言 null 因子 95% CI 覆盖 0 的比例 ≥ 92%
    - _Requirements: 4.6, 6.1_

- [x] 5. StagePredictor 模块（R9、R10 — EMST 提前预测）
  - [x] 5.1 在 `src/stage_predictor.py` 实现 ONNX 推理 + 命中率审计
    - 定义 `PredictorInput` / `PredictorOutput` / `HitRecord` dataclass
    - 实现 `StagePredictor.__init__`、`try_load`（onnxruntime 缺失或模型 > 80 KB 或文件缺失返回 `None`）、`predict`、`maybe_anticipate`、`record_hit`、`hit_rate_7d`
    - `predict` 校验：`PredictorInput.is_complete_enough`（每通道非 None ≥ 50%，R9.6）；推理 > 50 ms 计 1 次 error，连续 3 次后 `disabled_until = now + 3600s`
    - `PredictorOutput.is_valid`：4 概率 ∈ `[0, 1]` 且 `|sum - 1| ≤ 0.01`（R9.5），含 NaN 直接 invalid
    - `maybe_anticipate` 触发条件：`current=LIGHT AND predicted=DEEP AND confidence ≥ 0.6 AND is_valid AND device_class ∈ {"climate", "humidifier"}`（R10.1，电热毯归 climate）
    - 提前控制路径**不直接调用** `ha_client.call_service`，而是通过传入的 `controller.dispatch_with_lookahead(...)` 转发，PR1 / dry_run 一处守护
    - `record_hit` 走 `_io_utils.atomic_append_jsonl(audit_jsonl, record, max_lines=None)` + 按时间戳 prune > 7 天（R10.2）
    - `hit_rate_7d` 缓存 1 小时（R10.3）；连续 3 晚 < 70% 时把 `predictor_status` 置 `auto_disabled`（R10.4）
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6, 10.1, 10.2, 10.3, 10.4_

  - [x] 5.2 在 `src/external_stage_subscriber.py` 增加 `add_pre_transition_hook` hook
    - 新增 `add_pre_transition_hook(hook: Callable[[SleepStage, SleepStage], Awaitable[None]])`
    - 在 debouncer 即将 `_emit_transition` 前 `await asyncio.wait_for(hook(...), timeout=0.1)`（100 ms budget）
    - timeout 仅 skip + 计数；hook 抛异常仅 log + 计数，不传播；不修改既有 `_emit_transition` 签名（PR2）
    - _Requirements: 9.1, 10.1, 11.3_

  - [x] 5.3 在 `src/smart_environment_controller.py` 暴露 `dispatch_with_lookahead`
    - 新增方法 `dispatch_with_lookahead(stage: SleepStage, lead_seconds: int)`：复用既有 per-actuator anticipation 路径，但允许调用方传入额外 lead time
    - 仅对 `device_class ∈ {"climate", "humidifier"}` 生效（与 EMST `slow_devices_only` 一致）
    - `dry_run=true` 时只 log 不下发（与既有 `_apply_setpoint` 一致），保持 PR1 / R11.5 不变量
    - 不修改任何现有方法签名
    - _Requirements: 10.1, 11.5_

  - [x] 5.4 EMST P16 maybe_anticipate 触发条件 property
    - `tests/test_stage_predictor.py` 新建
    - **Property 16: maybe_anticipate 触发条件等价**
    - **Validates: Requirements 9.3, 9.5, 9.6, 10.1**
    - 入口函数 `test_property_p8b_maybe_anticipate_triggers_iff_all_conditions`：用 hypothesis 生成 `(current_stage, predicted, confidence, is_valid, device_class)` 五元组，断言提前控制触发的充要条件
    - 同时覆盖 `is_valid` 等价定义（4 概率 ∈ `[0,1]` AND `|sum-1| ≤ 0.01`）
    - _Requirements: 9.3, 9.5, 9.6, 10.1_

  - [x] 5.5 EMST P8 推理性能 property（slow）
    - 入口函数 `test_property_p8_predict_p95_within_50ms`（marked `@pytest.mark.slow`、`@pytest.mark.integration`）
    - **Property 8: Stage 预测推理 ≤ 50 ms**
    - **Validates: Requirements 9.4**
    - 用合成 stub ONNX（输入 (1,3,300) → 输出 (1,4)）跑 100 次 `predict()`，断言 p95 ≤ 50 ms（CI 放宽 ×1.5 = 75 ms）
    - 注意：测试不依赖真实 `stage_predictor.onnx` 文件存在，应能在 CI 上无 artifact 时跳过（pytest.skip + log）
    - _Requirements: 9.4_

  - [x] 5.6 EMST 单元测试（缺失通道、ONNX 加载降级）
    - `test_predict_returns_none_when_channel_missing_50pct`
    - `test_predict_returns_none_when_inference_timeout_3_consecutive`
    - `test_try_load_returns_none_when_onnxruntime_missing`（用 sys.modules monkeypatch）
    - `test_try_load_returns_none_when_model_exceeds_80kb`
    - `test_dispatch_with_lookahead_respects_dry_run`（PR1）
    - _Requirements: 9.2, 9.4, 9.5, 9.6, 11.5_

  - [x] 5.7 EMST P9 命中率算术 + P9b 自动停用 property
    - `tests/test_stage_predictor_audit.py` 新建
    - **Property 9: 7 晚滚动命中率统计正确**
    - **Validates: Requirements 10.2, 10.3, 10.4**
    - 入口函数 `test_property_p9_hit_rate_matches_arithmetic`：合成 N 条 `(predicted, actual_after_60s)` 序列，断言 `hit_rate_7d()` 等于算术值（含 N < 7 晚返回 None 的边界）
    - 配套 `test_property_p9b_auto_disable_after_3_consecutive_below_70pct`：连续 3 晚命中率 < 70% 后断言 `predictor_status == "auto_disabled"`
    - _Requirements: 10.2, 10.3, 10.4_

- [x] 6. Sensor 发布与配置兼容（PR2 + PR6）
  - [x] 6.1 在 `src/sleep_state_publisher.py` 追加 14 个 v3 sensor
    - 新增私有方法 `_publish_v3_sensors()`，在既有 `_publish_all` 末尾 `if v3_modules_loaded:` 后调用
    - 14 个 sensor 与 design §3.5 表逐一对齐：`optimizer_health` / `optimizer_status` / `optimizer_uncertainty` / `decision_mode` / `locked_dimensions` / `quality_trend_14d` / `attribution` / `attribution_full` / `prior_status` / `prior_weight` / `predictor_health` / `predictor_status` / `predictor_hit_rate_7d` / `v3_health_summary`
    - 模块停用时仍发布 sensor 但 state = `disabled`，保证 Lovelace 一致渲染
    - state 长度 ≤ 255 字符（HA Core 限制），超长内容仅放 attribute
    - 既有 20 个 sensor 的 entity_id + attribute schema **逐字保留**（PR2 不变量）
    - _Requirements: 1.4, 1.7, 2.4, 2.5, 3.1, 3.2, 5.2, 5.5, 6.1, 8.1, 8.5, 9.4, 10.3, 10.4, 11.6_

  - [x] 6.2 在 `training_config/config_loader.py` 接入 4 个 flag + 用户画像
    - 新增字段读取：`bayesian_optimizer_enabled` / `causal_attribution_enabled` / `population_prior_enabled` / `stage_predictor_enabled`（默认全 true）、`causal_attribution_explain_all`（默认 false）、`user_profile_age_band` / `user_profile_sex` / `user_profile_chronotype`（默认空字符串 → 视为 unspecified / neutral）
    - 缺失字段时回退默认值并打印一行 INFO 日志（不 WARN，避免老用户升级刷屏）
    - 校验 `user_profile_*` 取值在合法枚举内，非法值 → 回退默认 + INFO 日志
    - _Requirements: 8.2, 11.1, 11.2, 14.2_

  - [x] 6.3 在 `sleep_classifier/config.yaml` 追加 v3 字段
    - 在 `options:` 末尾追加 8 个新字段（4 个 flag + 1 个 explain_all + 3 个 user_profile）
    - 在 `schema:` 同步追加，全部用 `?` 后缀（`bool?` / `match(...)?`），保证 v2.1.0 旧 config 升级时不被拒（PR6）
    - `user_profile_*` 用 regex match 限制取值
    - _Requirements: 11.1, 11.2_

  - [x] 6.4 PR2 sensor schema 不变量测试
    - `tests/test_sensor_schema_invariant.py` 新建
    - 锁定 v2.1.0 已有 20 个 sensor 的 entity_id 集合 + attribute key 集合（用 fixture 嵌入 baseline）
    - 跑一晚合成数据后断言这 20 个 sensor 的 entity_id + attribute key 与 baseline 完全一致
    - _Requirements: 11.6_

  - [x] 6.5 PR6 配置兼容性测试
    - `tests/test_v3_config_compat.py` 新建
    - 用 v2.1.0 旧 config（不含任何 v3 字段）验证 `load_config()` 不抛异常并应用所有默认值
    - 验证非法 `user_profile_*` 取值回退默认 + INFO 日志
    - _Requirements: 11.1, 11.2_

- [x] 7. Web UI onboarding 第 3 步：用户画像
  - [x] 7.1 在 `sleep_classifier/web_ui.py` 增加用户画像 onboarding
    - 在 onboarding wizard 第 3 步（slot binding 之后）追加表单：`age_band`（5 选 1）、`sex`（3 选 1）、`chronotype`（3 选 1），全部可选
    - 同时增加「锁定 prior_weight 到 0」的开关（R8.5）
    - 提交后写入 `/data/web_ui_overrides.json` 的 `v3_user_profile` sub-dict（走 `_io_utils.atomic_write_json`，PR3）
    - 顶部 sticky 区显示当前 4 个算法的健康状态（绿/琥珀/红/disabled，R11.6），数据来源 `sensor.sleep_classifier_v3_health_summary`
    - _Requirements: 8.2, 8.3, 8.5, 8.7, 11.6, 14.2_

  - [x] 7.2 Web UI onboarding 单元测试
    - `tests/test_web_ui_v3_onboarding.py` 新建
    - 测试合法画像保存到 `v3_user_profile` 子字段
    - 测试缺失字段 → unspecified / neutral 兜底
    - 测试 `prior_weight_lock=0` 持久化字段
    - 测试 v2.x 老 web_ui_overrides.json 加载时未引用字段被忽略（PR6）
    - _Requirements: 8.2, 8.3, 8.5, 8.7_

- [x] 8. 主入口启动序列与运行时编排
  - [x] 8.1 在 `scripts/run_ha_smart_service.py` 接入 4 个新模块
    - 启动序列按 design §2.5 顺序图：load config → PP load → BAO init（注入 PP）→ EMST try_load → CAE init
    - 任一新模块的 import / 加载失败 → 仅 log INFO + 对应 sensor 置 `disabled` / `unavailable`，主流程继续
    - 注册 hook：`SEC.set_setpoint_provider(BAO.recommend)`、`PL.add_session_listener(CAE.on_session)`、`ESS.add_pre_transition_hook(EMST.maybe_anticipate)`
    - `*_enabled = false` 时**不 import** 对应模块（lazy import in `if flag:`），实现 R11.4 字节级等价回退
    - 维护 `_v3_tasks: list[asyncio.Task]` 收集所有后台 task；SIGTERM 时先 `set` `asyncio.Event` 让 task 主动退出，再 `await asyncio.wait_for(asyncio.gather(*_v3_tasks, return_exceptions=True), timeout=10)`，超时则 `cancel()`（PR5）
    - 启动期一次性打印 design §6.2 中的 v3 status INFO 日志
    - _Requirements: 1.1, 1.2, 4.1, 7.1, 8.1, 8.3, 9.1, 11.3, 11.4, 11.5, 11.6_

  - [x] 8.2 错误计数 → 自动降级状态机
    - 4 个新模块统一暴露 `error_count` 属性 + `should_disable` 判定（≥ 3 次异常）
    - 主入口在每次调用对应 hook 后检查；触发降级时设 internal flag = False 并通过 `SleepStatePublisher` 把 `*_health` 置 `degraded`
    - _Requirements: 1.4, 11.3, 11.6_

  - [x] 8.3 P10 全关回退到 v2.1.0 端到端测试
    - `tests/test_v3_feature_flags_full_disable.py` 新建
    - **Property 10: 4 个 feature flag 独立关闭时 add-on 主流程仍可启动 + 跑完一晚 dry_run**
    - **Validates: Requirements 11.4, 11.5**
    - 入口函数 `test_property_p10_full_disable_equivalent_to_v2_1_0`：4 个 flag 全 false + `dry_run=true`，跑一晚合成数据；断言 `ha_client.call_service` 调用次数 = 0、20 个 v2.x sensor 与 baseline 完全一致、4 个 v3 健康 sensor 状态 ∈ `{disabled, healthy}`
    - _Requirements: 11.4, 11.5_

  - [x] 8.4 P17 dry_run 阻断 call_service 端到端测试
    - `tests/test_v3_dry_run_safety.py` 新建
    - **Property 17: dry_run=true 阻断所有 call_service**
    - **Validates: Requirements 11.5**
    - 入口函数 `test_property_p10b_dry_run_blocks_all_call_service`：用 hypothesis 在 4 flag × {true,false} = 16 种组合上跑 + `dry_run=true`，断言每种组合下 `call_service` 调用次数 = 0
    - _Requirements: 11.5_

  - [x] 8.5 P19 SIGTERM 优雅退出端到端测试
    - `tests/test_v3_graceful_shutdown.py` 新建
    - **Property 19 (X2): PR5 优雅退出契约**
    - **Validates: Requirements 11.3, 11.6**
    - 入口函数 `test_property_x2_sigterm_drains_all_v3_tasks_within_10s`：在 4 模块任意子集 ∈ {空, 单, 任意, 全开} 启动后发出 SIGTERM，断言 `_v3_tasks` 全部 done/cancelled 且总耗时 ≤ 10 秒
    - _Requirements: 11.3, 11.6_

  - [x] 8.6 P20 错误计数 → 自动降级端到端测试
    - `tests/test_v3_health_degradation.py` 新建
    - **Property 20 (X3): 错误计数 → 自动降级状态机**
    - **Validates: Requirements 1.4, 11.3, 11.6**
    - 入口函数 `test_property_x3_three_strikes_disables_module`：对 4 模块各注入 3 次运行时异常，断言对应 `*_health` sensor = `degraded` 且 internal flag = False
    - _Requirements: 1.4, 11.3, 11.6_

- [x] 9. Checkpoint 1 — 算法核心与运行时集成完成
  - 跑 `pytest -m "not slow"` 全套 fast 测试
  - 跑 `python scripts/run_ha_smart_service.py --dry-run --duration 60` 验证 4 个新模块均可启动且不下发真实指令
  - Ensure all tests pass, ask the user if questions arise.

- [x] 10. 训练与评估脚本（R7、R10.5、R15、R6.3）
  - [x] 10.1 实现 `scripts/train_population_prior.py`
    - CLI：`--mesa-dir / --shhs-dir / --out / --seed`（默认 20260518）
    - 按 `(age_band, sex, chronotype, season)` 4 维分桶，输出 hierarchical Bayesian prior
    - 输出 `<out>` pickle（≤ 8 MB）+ `<out>.meta.json`（每桶 n_samples）+ `<out>.report.md`（数据集大小、桶覆盖率）
    - 嵌入 `PriorMetadata`（schema_version=1、sources、trained_at、git_commit、n_subject_nights、sha256）
    - 在 stdout 打印 NSRR DUA 摘要 + DOI（与 `docs/POPULATION_PRIOR.md` 一致）
    - 退出码：0 OK / 1 数据 schema 不符 / 2 输出超 8 MB
    - 训练时依赖（torch / pandas / pyEDFlib / nsrr-toolkit）仅来自 `requirements-train.txt`
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.6, 12.5, 14.1, 15.5_

  - [x] 10.2 实现 `scripts/train_stage_predictor.py`
    - CLI：`--edf-dir / --out / --quantize / --seed`
    - 输入 (1,3,300) float32（HRV / motion / breathing），输出 (1,4) softmax，INT8 量化后 ≤ 80 KB
    - 训练完成后自动验证：`onnxruntime.InferenceSession` 加载 + 单次推理 ≤ 50 ms
    - 输出 `<out>` ONNX + `<out>.report.md`（4-fold CV hit rate by stage）
    - 退出码：0 / 1（> 80 KB）/ 2（onnx 加载失败）/ 3（推理 > 50 ms）
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 10.5, 12.5, 15.5_

  - [x] 10.3 实现 `scripts/eval_bayesian_regret.py`
    - CLI：`--user-prefs / --baseline {v2.x|random|optimal_oracle} / --nights / --seed`
    - 输出 `<prefix>_regret_curve_<sha7>.png` + `<prefix>_regret_summary_<sha7>.md`，对比 v2.x 中位数 vs v3.x GP+TS 累积 regret
    - 报告中包含 GP-UCB 理论上界，表述统一加「在 RBF kernel + 加性噪声假设下成立」
    - 文件名后缀强制带 git commit hash（`<base>_<sha7>.<ext>`，R15.5）
    - _Requirements: 3.3, 3.5, 15.2, 15.5_

  - [x] 10.4 实现 `scripts/eval_causal_synthetic.py`
    - CLI：`--n-nights / --n-trials / --seed`
    - 合成 ground-truth DAG → 跑 estimator → 输出 `<prefix>_causal_summary_<sha7>.md`
    - 报告 6 因子的 bias / variance / 95% CI 覆盖率；null 因子覆盖率 < 92% 时退出码 = 1
    - _Requirements: 6.3, 15.2, 15.5_

  - [x] 10.5 实现 `scripts/eval_population_prior_rmse.py` 与 `scripts/eval_stage_predictor_hitrate.py`
    - 两个脚本独立实现，与 design §3.8.5 / §3.8.6 接口对齐
    - 输出 markdown 表格 + matplotlib 图表（仅训练 / 评估环境用，不进 runtime）
    - _Requirements: 15.1, 15.2, 15.5_

  - [x] 10.6 实现 `scripts/sanitize_user_data.py`（R14.5）
    - 读 `/data/*.json` / `*.jsonl`，把 entity_id 替换为 sha256 hash、时间戳秒位归零、`age_band/sex/chronotype` 替换为 `"redacted"`
    - 写到指定 `--out` 路径（不覆盖原文件）
    - _Requirements: 14.4, 14.5_

  - [x] 10.7 P12 sanitize 脱敏 property
    - `tests/test_sanitize_user_data.py` 新建
    - **Property 12: sanitize_user_data.py 输出文件中不包含原始 entity_id / 完整时间戳**
    - **Validates: Requirements 14.5**
    - 入口函数 `test_property_p12_sanitize_removes_entity_ids_and_seconds`：用 hypothesis 生成合法 user_preferences.json / causal_factors.jsonl，断言输出不含原 entity_id 字面值、时间戳秒位 = "00"、画像字段 = `"redacted"`
    - _Requirements: 14.5_

  - [x] 10.8 P3 BAO 28 晚 regret holdout 评估（slow）
    - `tests/test_v3_bayesian_regret_holdout.py` 新建（marked `@pytest.mark.slow`）
    - **Property 3: 28 晚累积 regret 比 v2.x 中位数低 ≥ 30%**
    - **Validates: Requirements 3.4**
    - 入口函数 `test_property_p3_regret_at_least_30pct_lower_than_v2`：合成 RBF 形状真值函数 + 高斯噪声，多 100 次种子均值，断言 v3 ≤ v2 × 0.7
    - _Requirements: 3.4_

  - [x] 10.9 训练 / 评估脚本 CLI 单元测试
    - 6 个脚本各写一个 `test_<script>_cli_arg_parsing` smoke 测试（不跑真实训练，仅验证 argparse + 退出码 + 默认 seed = 20260518）
    - _Requirements: 15.5_

- [x] 11. CI / 构建 / 镜像治理（R7.5、R12、PR4）
  - [x] 11.1 实现 `scripts/check_artifacts.py`
    - 校验 `sleep_classifier/rootfs/training_config/population_prior.pickle` 大小 ≤ 8 MB + SHA-256 与 metadata 内嵌值一致
    - 校验 `sleep_classifier/rootfs/training_config/stage_predictor.onnx` ≤ 80 KB
    - `--strict` flag：缺失文件 → 退出码 1（CI 用）；非 strict → 仅 WARN（本地 prepare 用）
    - _Requirements: 7.3, 7.5, 9.2_

  - [x] 11.2 更新 `sleep_classifier/Dockerfile` 与 `prepare.sh` / `prepare.bat`
    - Dockerfile `pip install` 行追加 `--only-binary=:all:`，强制 wheel 安装（避免 Alpine 源码构建）
    - `prepare.sh` / `prepare.bat` 末尾追加 `python scripts/check_artifacts.py`（非 strict，仅 WARN）
    - prepare 脚本镜像 `population_prior.pickle` 与 `stage_predictor.onnx`（如存在）到 `sleep_classifier/rootfs/training_config/`
    - _Requirements: 7.5, 12.1_

  - [x] 11.3 更新 CI workflow
    - `.github/workflows/addon-build.yml` 追加 step：`check_artifacts.py --strict`、`numpy/scipy/onnxruntime` import 静态扫描、multi-arch buildx、image size guard（基线 80 MB × 1.20 = 96 MB 上限）、musllinux wheel 存在性检查（`pip download --only-binary=:all: --platform musllinux_1_2_aarch64 --platform musllinux_1_2_x86_64`）
    - `.github/workflows/test.yml` 增加 `slow` 矩阵分支：`pytest -m 'slow' --timeout=600`
    - 更新 `.github/baseline_image_size.txt` 从 `15M` 到 `80M`
    - _Requirements: 7.5, 12.3, 12.4_

  - [x] 11.4 P11 runtime 依赖 import 覆盖率 property
    - `tests/test_runtime_dependency_coverage.py` 新建
    - **Property 11: 镜像内 numpy / scipy / onnxruntime 必须有 import 路径覆盖率**
    - **Validates: Requirements 12.1, 12.4**
    - 入口函数 `test_property_p11_runtime_deps_actually_imported`：用 grep_search / ast 静态扫描 `src/`，断言 `numpy / scipy / onnxruntime` 各有 ≥ 1 处 import
    - _Requirements: 12.1, 12.4_

- [x] 12. 文档与商业化文案（R7.6、R10.6、R13、R14.3）
  - [x] 12.1 创建 `docs/POPULATION_PRIOR.md`
    - 数据来源（MESA + SHHS DOI）、引用格式、伦理审查（NSRR DUA 摘要）、桶定义、字段含义
    - 与 `scripts/train_population_prior.py` stdout 打印的 DUA 摘要一致
    - _Requirements: 7.6, 14.1_

  - [x] 12.2 创建 `docs/algorithm_evaluation.md`
    - 4 个章节对齐 R15.1 四方向评估报告
    - 顶部包含「局限性」声明：v3.0.0 算法在 IID 假设下成立，季节切换 / 设备故障 / 重大生活变化下可能性能退化
    - 引用 4 个 `eval_*.py` 脚本输出，给出 holdout 数据上的具体数字（v3.x 比 v2.x 累积 regret 低 ≥ 30% 等）
    - _Requirements: 3.4, 15.1, 15.2, 15.3_

  - [x] 12.3 更新 `docs/MEDICAL_DISCLAIMER.md` 与新建 / 更新 `docs/PRIVACY.md`
    - MEDICAL_DISCLAIMER.md 增补段落：「归因解释为相关性 + 因果模型推断，非临床诊断」
    - PRIVACY.md 增加「v3.0.0 算法栈数据流」段落，列举 4 个新模块各处理哪些数据、写到哪些文件、是否离开本地（答案均为否）
    - _Requirements: 6.5, 14.3_

  - [x] 12.4 更新 `README.md` 与 `sleep_classifier/DOCS.md`
    - README 顶部「为什么不一样」段落新增「4 个算法护城河」小节，每个方向一句话价值主张 + 数学保证（带「在 X 假设下成立」前缀，禁止夸大）
    - README 增加「出厂带 8000+ 受试者 PSG 训练 prior」宣传点 + 链接 `docs/POPULATION_PRIOR.md`
    - DOCS.md 增加「算法可解释性」段落 + ASCII 流程图（PSG → prior → GP → Thompson → action）
    - DOCS.md / README 标注：「60 秒提前控制对快速响应设备无明显收益，仅对慢响应设备有意义」
    - _Requirements: 10.6, 13.1, 13.2, 13.5, 13.6_

  - [x] 12.5 更新 `docs/ROADMAP.md`
    - v3.1.0 联邦学习段落更新为「基于 v3.0.0 prior 模块的真·联邦扩展」，明确依赖关系
    - Commercial roadmap 段落更新，把「算法订阅服务」作为 v3.0.0 之后潜在变现方向（GP 后验 / 因果归因为 enterprise feature）
    - _Requirements: 13.3, 13.4_

  - [x] 12.6 更新 `.kiro/steering/tech.md`
    - 在「运行时依赖」清单加入 numpy / scipy / onnxruntime
    - 新增段落「v3.0.0 破例理由」，说明每个依赖对应哪个算法方向
    - _Requirements: 12.2_

- [x] 13. Checkpoint 2 — 全套测试与镜像构建验证
  - 跑 `pytest --cov=src --cov=scripts`，新增 4 个模块单文件覆盖率 ≥ 95%、整体 ≥ 90%
  - 跑 `pytest -m slow --timeout=600`，验证 P3 / P4 / P5 / P8 三类性能与统计 property
  - 本地跑 `bash sleep_classifier/prepare.sh`（或 `prepare.bat`）+ `docker buildx build --platform linux/arm64,linux/amd64 sleep_classifier/`，确认镜像 ≤ 96 MB
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- 标记 `*` 的子任务为可选（测试），可在 MVP 冲刺阶段跳过；核心实现任务（不带 `*`）必须执行。
- 每个 property 子任务独立列出，明确标注 property 编号、`**Validates: Requirements ...**`、入口函数名，便于 PBT 框架消费。
- Checkpoint 任务（9、13）不修改代码，仅触发跑测 + 用户确认。
- 文件冲突已通过下方 Task Dependency Graph 拓扑排序：写同一文件的任务在不同波次，独立测试 / 文档任务可并行。
- 所有任务遵循平铺式 `src/` 命名（一职责一文件）+ 镜像式 `tests/test_<module>.py` 命名（structure.md）。
- 持久化路径全部走 `_io_utils.atomic_write_*`（PR3）；HA 交互走 `ha_api_client`（tech.md 硬规则）；不引入 numpy / scipy / onnxruntime 之外的科学计算依赖（R12 治理）。

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["1.3", "2.1", "4.1", "5.1", "6.3", "11.1", "12.1", "12.6"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "3.1", "4.2", "5.2", "5.3", "6.2", "10.1", "10.2", "10.6", "11.2", "12.3", "12.5"] },
    { "id": 3, "tasks": ["3.2", "3.3", "4.3", "4.4", "4.5", "4.6", "5.4", "5.6", "5.7", "6.1", "6.5", "10.3", "10.4", "10.5", "10.7", "10.9", "11.3", "12.2", "12.4"] },
    { "id": 4, "tasks": ["3.4", "3.5", "3.6", "3.7", "3.8", "5.5", "6.4", "7.1", "8.1", "10.8", "11.4"] },
    { "id": 5, "tasks": ["7.2", "8.2"] },
    { "id": 6, "tasks": ["8.3", "8.4", "8.5", "8.6"] }
  ]
}
```
