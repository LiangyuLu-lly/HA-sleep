# 项目结构（Structure）

## 顶层目录
```
.
├── src/                  # 运行时核心模块（18 个文件，被 Add-on 和 scripts 共用）
├── scripts/              # Add-on 入口 + 数据工具
├── tests/                # pytest 测试套件（~419 个测试）
├── training_config/      # 配置加载器 + 默认 JSON（不再做模型训练）
├── sleep_classifier/     # Home Assistant Add-on 打包目录
├── docs/                 # 开发者文档（backlog、手动部署、HACS 迁移）
├── examples/             # Lovelace 仪表板等用户示例
├── models/               # 预留目录（v1.3.0 之后不再存放模型文件）
├── data/                 # 本地开发/调试数据（不进入镜像）
├── pyproject.toml        # PEP 621 元数据 + pytest / coverage 配置
├── requirements.txt      # 开发环境（= runtime + pytest 三件套）
├── requirements-runtime.txt   # Add-on 镜像只装这个
├── setup.py              # 兼容老工具链
├── repository.yaml       # HA Add-on 仓库元数据（必须在仓库根目录）
├── setup_env.{sh,bat}    # 一键搭建开发环境
├── README.md / INSTALL.md / CHANGELOG.md
└── .kiro/                # Kiro specs + steering
```

## `src/` —— 每个模块一个职责
所有模块都是平铺的（不分子包），命名语义化。新增模块时请遵循同样的「一职责一文件」原则。

| 文件 | 职责 |
|---|---|
| `ha_api_client.py` | 与 HA 的**唯一**交互层：REST + WebSocket + 重连 + 错误封装。 |
| `external_stage_subscriber.py` | 订阅任意 HA 睡眠分期实体，做 **stage debouncing**。 |
| `device_discovery.py` / `device_capabilities.py` | 自动发现 + 槽位绑定 + 各设备能力识别。 |
| `smart_environment_controller.py` | 核心闭环：per-stage deltas、per-actuator anticipation、wind-down 预冷、deadband 节流。 |
| `preference_learner.py` | **纯函数**学习器：sessions → midpoint + k-NN + decay + weekday/weekend 分桶。 |
| `sleep_quality_score.py` | 客观质量评分（DEEP/REM 奖励 + 碎片化惩罚） + 与主观反馈融合。 |
| `sleep_debt.py` | 睡眠债累计 + NSF/AAP 年龄相关推荐时长。 |
| `smart_wake.py` | 唤醒窗口内挑选最佳唤醒时刻 + 30 分钟 dawn-light ramp。 |
| `whitenoise_matcher.py` | 各阶段白噪/景观音轨匹配。 |
| `sleep_state_publisher.py` | 发布 5 个状态/诊断 sensor。 |
| `learning_panel_publisher.py` | 发布 4 个偏好学习 sensor（v1.6.0 从 orchestrator 抽出）。 |
| `feedback_input.py` | 订阅 `input_number.*` 接收晨起主观评分。 |
| `user_profile.py` | 出生年、chronotype 等用户画像。 |
| `data_structures.py` | `SleepStage` 枚举 + `SleepSession` dataclass 等核心数据类型。 |
| `apnea_detector.py` | v1.6.0 PoC：纯函数式呼吸暂停/低通气检测（暂未接入主流程）。 |
| `_time_utils.py` | 带下划线前缀的私有辅助：本地时区、时间桶化。 |
| `_io_utils.py` | atomic write helpers (JSON / 文本)，I/O 相关纯函数辅助。 |

## `scripts/`
| 文件 | 说明 |
|---|---|
| `run_ha_smart_service.py` | **主入口**，被 `sleep_classifier/run.sh` 调用；`asyncio.run()` 总调度。 |
| `download_data.py` | 公共数据集下载工具（不属于运行时）。 |

## `tests/` —— 镜像式命名
- 每个 `src/<module>.py` 对应一个 `tests/test_<module>.py`。
- 跨模块的集成测试用 `test_smart_sleep_service_*.py` 命名。
- 保持 ~92% 覆盖率是硬指标；新增模块必须同步加测试。
- 异步测试直接写 `async def test_*`；pytest-asyncio 已在 `pyproject.toml` 配为 `auto`。

## `training_config/`
| 文件 | 说明 |
|---|---|
| `config_loader.py` | 读取 `config.json` / Add-on options / 环境变量并合并成运行时 config。 |
| `config.json` | 默认值；被 Add-on `options` 覆盖。 |

> 即使名字叫 `training_config`，v1.3.0 起**不再用于训练**，保留仅用于配置加载。

## `sleep_classifier/` —— HA Add-on 目录
```
sleep_classifier/
├── config.yaml           # Add-on manifest（options schema、ingress、map）
├── build.yaml            # 构建时变量
├── Dockerfile            # Alpine + Python 3.11，只装 requirements-runtime.txt
├── run.sh                # 容器启动脚本：生成 config → 执行 run_ha_smart_service.py
├── web_ui.py             # 内嵌 aiohttp UI（/data/web_ui_overrides.json）
├── bootstrap_placeholders.py  # 启动期抢发占位 sensor
├── apparmor.txt          # 自定义 AppArmor profile，+1 安全分
├── DOCS.md               # Add-on 用户手册（HA 会渲染到 UI）
├── prepare.sh / prepare.bat   # 镜像 src/ scripts/ training_config/ 到 rootfs/
└── rootfs/               # ⚠️ prepare 生成的产物，不要手改；commit 前跑 prepare
```

**关键不变量**：`Dockerfile` 的 Docker 构建 context 只有 `sleep_classifier/`，看不到外面。所以 `src/` 的任何改动都**必须**先跑 `prepare` 再 commit，否则 Add-on 拉到的是旧代码。

## `docs/`
- `BACKLOG.md` —— 未完成工作清单。
- `MANUAL_DEPLOYMENT.md` —— 非 HA OS 环境的 systemd / Docker 部署。
- `HACS_MIGRATION.md` —— v2.0 迁移到 HACS 的规划。

## 命名与文件放置规则
1. **新的运行时逻辑** → `src/<new_module>.py` + `tests/test_<new_module>.py`，保持平铺。
2. **新的入口脚本** → `scripts/`，不要放在 `src/`。
3. **用户可见文档** → `README.md` / `INSTALL.md` / `sleep_classifier/DOCS.md`。
4. **开发者内部文档** → `docs/`。
5. **新的 HA 传感器** → 放进 `SleepStatePublisher` 或 `LearningPanelPublisher`，用 `sensor.sleep_classifier_*` 前缀。
6. **纯函数工具** → 优先放进 `preference_learner.py` / `sleep_quality_score.py` 等已有纯函数模块；I/O 逻辑不要混进去。
7. **跨模块类型定义** → 放到 `src/data_structures.py`，避免循环依赖。

## 生成/临时目录（不要提交）
- `.pytest_cache/`、`.hypothesis/`、`__pycache__/`、`.coverage`、`sleep_classifier/rootfs/`（理论上应由 `prepare` 重建，但当前仓库选择 commit 以便 HA Supervisor 直接构建）。
- 具体以 `.gitignore` 为准。
