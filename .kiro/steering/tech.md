# 技术栈与构建（Tech）

## 语言与运行时
- **Python ≥ 3.10**（`pyproject.toml` 硬性要求）；Add-on 镜像运行时为 Alpine + Python 3.11。
- 纯 `asyncio` 单事件循环架构，所有 I/O（HA REST、WebSocket、文件持久化）都走协程。
- 代码风格：类型注解齐全，`from __future__ import annotations`；文档字符串使用 reStructuredText。

## 核心依赖
### 运行时（`requirements-runtime.txt`，会装入 Add-on 镜像）
- `aiohttp >= 3.9.0` —— HA REST + WebSocket 客户端 + 内嵌 Web UI server。

> v1.6.0 之后 `src/` 与 `scripts/` 已不再 `import numpy`：
> 加权中位数、指数衰减、睡眠债推算都用纯 Python（`math.exp`
> + 手写循环）实现，因此 numpy 已从运行时依赖中移除，镜像
> 再瘦约 5 MB。未来若要向量化，请先用 `grep -R "numpy" src/`
> 确认并同步加回 `requirements-runtime.txt`。

### 开发 / 测试（`requirements.txt`）
- `pytest >= 7.4.0`
- `pytest-asyncio >= 0.23.0`（`asyncio_mode = "auto"`，无需给 async 测试加装饰器）
- `pytest-timeout >= 2.2.0`（默认 60 秒超时，防止卡死测试）

> `.hypothesis/` 目录是 CNN-BiLSTM 时代 property-based 测试的
> 历史产物；当前 `tests/` 里已无 `@given` / `strategies` 使用，
> 因此 hypothesis **不在**开发依赖中。

### 明确不再使用
- **不用 TensorFlow / Keras / PyTorch**（v1.3.0 删除了本地模型）。
- **不用 MQTT / paho-mqtt**（走 HA REST + WS API）。
- **不用 scipy / h5py / PyWavelets / numpy**。

## 架构总览
```
HA WebSocket (state_changed)                HA REST (/api/services/...)
       │                                              ▲
       ▼                                              │
ExternalStageSubscriber                               │
       │ SleepStage + debounced transitions          │
       ▼                                              │
SmartEnvironmentController ──► per-stage + per-actuator planner
       │
       ▼
PreferenceLearner ◄── sessions (JSON @ /data/user_preferences.json)
       │
       ▼
SleepStatePublisher / LearningPanelPublisher ──► sensor.sleep_classifier_*
```
`scripts/run_ha_smart_service.py` 是单一入口，串联所有模块到一个 `asyncio.run()` 循环中。

## 构建系统
- **PEP 621 布局**（`pyproject.toml`），但依赖清单仍保留在 `requirements*.txt`（Dockerfile 直接读）。
- **无独立构建步骤**：Home Assistant Supervisor 在用户侧通过 `sleep_classifier/Dockerfile` 构建 Add-on 镜像。
- 每次修改 `src/`、`scripts/`、`training_config/` 或 `requirements-runtime.txt` 后，必须重新运行 `prepare` 脚本，把内容镜像到 `sleep_classifier/rootfs/`。

## 常用命令

### 开发环境准备
```bash
# Windows
setup_env.bat

# Linux / macOS
./setup_env.sh

# 或手动安装
pip install -r requirements.txt
```

### 测试
```bash
# 完整测试套件（~414 个测试，期望 92% 覆盖率）
pytest

# 带覆盖率
pytest --cov=src --cov=scripts

# 跳过慢测
pytest -m "not slow"

# 只跑某个模块
pytest tests/test_preference_learner.py -v
```

### 本地模拟运行（不连真实 HA）
```bash
python scripts/run_ha_smart_service.py --dry-run --duration 60
```

### 真实 HA 部署（非 Add-on 场景）
```bash
HA_TOKEN="..." python scripts/run_ha_smart_service.py \
    --base-url http://homeassistant.local:8123 \
    --area bedroom --infer-interval 30 --session-interval 1800
```

### Add-on 打包（发布流程）
```bash
# 1. 镜像源码到 add-on 目录（Windows: prepare.bat）
bash sleep_classifier/prepare.sh

# 2. 提交并 push 到公开 GitHub
git add . && git commit -m "..." && git push

# 3. 用户在 HA 侧: Settings → Add-ons → Add-on Store → ⋮ → Repositories
```

## 代码约定
- **禁止在关键路径上阻塞事件循环**：文件 I/O 必要时用 `asyncio.to_thread`。
- **任何与 HA 的交互都通过 `src/ha_api_client.py`**；不要直接调用 aiohttp 去命中 HA。
- **HA 实体订阅只在一处**：`src/external_stage_subscriber.py`（带 debounce + 重连）。
- **偏好学习是纯函数**：`src/preference_learner.py` 不做 I/O，便于单元测试与确定性重算。
- **`dry_run=True` 默认开启**：开发与首次安装必须保证不意外下发真实指令。
- **pytest-asyncio 用 `asyncio_mode = "auto"`**：新写的 `async def test_*` 不需要 `@pytest.mark.asyncio`。
- **持久化路径约定**：
  - `/data/user_preferences.json` —— 偏好历史（Add-on 私有）
  - `/data/web_ui_overrides.json` —— Web UI 选中的实体 ID（优先级高于 config.yaml）
  - `/share/*` —— 可供其他 Add-on 读取的调试导出

## 日志
- 使用标准 `logging`，按模块名分 logger。
- `log_level` 通过 Add-on 配置暴露给用户（debug / info / warning / error）。
- 日志里不要打印 HA 长效访问令牌 / Supervisor token。
