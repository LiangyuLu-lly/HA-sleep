# CNN-BiLSTM Sleep Algorithm

A deep learning-based sleep stage classification system for smart home environments, processing dual-sensor data (heart rate and movement) for real-time sleep analysis.

> 📑 **结题报告**: 完整的训练结果、评估指标、混淆矩阵和复现说明见
> [`docs/PROJECT_COMPLETION_REPORT.md`](docs/PROJECT_COMPLETION_REPORT.md)。
>
> 🆕 **v1.2.1 内置 Web UI 实体选择器**(2026-05):add-on 详情页点
> "OPEN WEB UI" → 下拉选 entity_id,不用再手敲。Supervisor Ingress
> 免登录,选完写 `/data/web_ui_overrides.json`,优先级高于 Configuration
> 表单。详见 [`INSTALL.md §5.1`](INSTALL.md#51-推荐用内置-web-ui-选实体v121)。
>
> 🌙 **v1.2.0 拟自然睡眠套件**(2026-05): 6 个新模块
> (用户画像 / 睡眠债务 / 智能唤醒 / SE/WASO/SOL 评分 / 主观反馈 / 白噪音匹配)
> 全部可选,4 个新 Lovelace 实体,12 篇一级文献支撑。
> 详见 [`INSTALL.md`](INSTALL.md#拟自然睡眠功能v120-新增) 或
> [`sleep_classifier/DOCS.md`](sleep_classifier/DOCS.md)。
>
> �� **Home Assistant 部署 — 三种方式**:
>
> 1. ⭐ **HA OS Add-on**(树莓派 4B + 完整 HA OS,**像 HACS 一样一键安装**)
>    → [`INSTALL.md`](INSTALL.md) (5 步,3 分钟操作 + 等 build)
> 2. 智能闭环手动部署(Raspberry Pi OS / NUC 等)→
>    [`docs/HA_SMART_DEPLOYMENT.md`](docs/HA_SMART_DEPLOYMENT.md)
> 3. 轻量 MQTT 集成(用户自己写 automation)→
>    [`docs/HA_DEPLOYMENT.md`](docs/HA_DEPLOYMENT.md)

## 智能闭环模式(树莓派 4B + HA)

服务通过 HA REST/WebSocket API 自动:

1. **发现** HA 中接入的传感器(手环 / 雷达 / 床垫 / 温湿度计)和可控设备
   (灯 / 空调 / 加湿器 / 风扇);
2. **订阅** WebSocket 实时获取传感器数据 → CNN-BiLSTM 推理睡眠分期;
3. **调控** 智能家居 — 直接调用 `light.turn_on` / `climate.set_temperature` 等;
4. **学习** 用户睡眠偏好 — 持续记录"什么环境下睡得最好"并自动应用。

```bash
# 干跑(不联 HA,跑合成数据)
python scripts/run_ha_smart_service.py --dry-run --duration 10

# 真实部署
export HA_TOKEN="eyJ..."  # 在 HA Profile → Long-Lived Access Tokens 生成
python scripts/run_ha_smart_service.py \
    --base-url http://homeassistant.local:8123 \
    --area bedroom --infer-interval 30
```

完整树莓派 4B 部署步骤(token 生成 / systemd / Docker / 故障排查) →
[`docs/HA_SMART_DEPLOYMENT.md`](docs/HA_SMART_DEPLOYMENT.md)。

## 接入 Home Assistant(轻量 MQTT Discovery)

服务把睡眠分期、心率、运动作为传感器实体推送给 HA,HA 内的 automation 负责
真正去控制灯光/空调/加湿器:

```bash
# 干跑(不连接 broker,只打印发布内容)
python scripts/run_ha_service.py --dry-run --speedup 600 --duration 10 --publish-interval 5

# 实战:回放 EDF 数据 → 推送到真实 HA broker
python scripts/run_ha_service.py \
    --broker 192.168.1.100 --username sleep_service --password "..." \
    --source replay --subjects ST7011 --speedup 60

# 生产:订阅手环/雷达 MQTT 数据 → 实时推理 → 推送 HA
python scripts/run_ha_service.py --source mqtt
```

启动后,HA 自动出现一个 **Bedroom Sleep Classifier** 设备 + 6 个实体
(`sensor.sleep_stage`、`sensor.sleep_confidence`、`sensor.heart_rate`、
`sensor.movement_intensity`、`binary_sensor.smoke_alarm`、
`binary_sensor.gas_alarm`)。

完整步骤(broker 安装 / systemd / Docker / Windows 任务计划)见
[`docs/HA_DEPLOYMENT.md`](docs/HA_DEPLOYMENT.md)。

## 一键运行(真实 Sleep-EDF Telemetry 数据)

```bash
# 1. 从 PhysioNet 下载真实数据(默认 2 个受试者,~50 MB)
python scripts/download_data.py

# 2. 训练 CNN-BiLSTM 模型(~2 分钟)
python scripts/train.py --subjects ST7011 ST7022 \
    --max-epochs 30 --batch-size 16 --learning-rate 0.05 \
    --stride 2048 --patience 8 --split-mode time

# 3. 在留出验证集上评估(产出 per-class 指标 + 混淆矩阵)
python scripts/evaluate.py --subjects ST7011 ST7022 \
    --split-mode time --stride 2048

# 4. 端到端演示
python scripts/run_demo.py --mode hypnogram --subjects ST7011  # 整夜睡眠分期回放
python scripts/run_demo.py --mode mqtt      --subjects ST7011  # MQTT mock 实时推理
```

**最新结果**(2 受试者,17 小时真实数据):
- 验证集准确率 **61.67%**(主类基线 60%)
- REM 识别 F1 = **0.60**, LIGHT F1 = **0.74**
- 542 个测试用例全部通过

## Project Structure

```text
.
├── src/                          # 17 个核心模块(数据结构、EDF 解析、CNN/BiLSTM、MQTT…)
├── scripts/                      # 一键运行入口
│   ├── download_data.py          # 从 PhysioNet 下载 Sleep-EDF Telemetry
│   ├── train.py                  # 数据加载 + 训练 + 模型保存
│   ├── evaluate.py               # 评估 + per-class 指标 + 混淆矩阵
│   ├── run_demo.py               # 端到端演示(hypnogram + MQTT 两种模式)
│   └── run_ha_service.py         # Home Assistant 桥接(MQTT Discovery)
├── tests/                        # 25 个测试文件,562 用例
├── config/
│   ├── config.json               # 模型/训练/MQTT/灾难监控/HA 参数
│   └── config_loader.py
├── data/sleep-edf-telemetry/     # 下载的 EDF 数据(默认 2 受试者,~50 MB)
├── models/                       # 训练产出
│   ├── best_model.h5             # CNN+BiLSTM+classifier 完整权重
│   ├── training_history.json     # 训练曲线
│   └── evaluation_report.json    # 验证指标 + 混淆矩阵
├── docs/
│   ├── PROJECT_COMPLETION_REPORT.md   # 结题报告(中文)
│   ├── HA_DEPLOYMENT.md               # Home Assistant 部署指南
│   ├── ha_automations.yaml            # HA automation 模板
│   └── EDF_PARSER_IMPLEMENTATION.md
├── requirements.txt
└── README.md
```

## Setup

### 1. Create Virtual Environment

```bash
python -m venv venv
```

### 2. Activate Virtual Environment

**Windows:**
```bash
venv\Scripts\activate
```

**Linux/Mac:**
```bash
source venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

## Core Data Structures

- **HeartRateData**: Heart rate sensor data (100Hz sampling)
- **MovementData**: Movement/accelerometer data (100Hz sampling)
- **SleepStage**: Sleep stage enumeration (AWAKE, LIGHT, DEEP, REM)
- **SleepStages**: Sleep stage annotation sequence
- **EDFHeader**: EDF file header information
- **TimeFrequencyMatrix**: Time-frequency matrix for CNN input (1024×128×2)
- **Dataset**: Combined dual-sensor data and annotations
- **TrainingSet/TestSet**: Training and test datasets
- **MQTTMessage**: MQTT message structure
- **ModelWeights**: Model weights for persistence
- **PerformanceMetrics**: Performance evaluation metrics

## Configuration

Configuration is managed through `config/config.json` with the following sections:

- **data_processing**: Normalization, wavelet denoising, movement filtering
- **model**: CNN, BiLSTM, and classifier parameters
- **mqtt**: MQTT broker settings and topic mappings
- **disaster_monitoring**: Smoke and gas thresholds
- **training**: Training hyperparameters

## Running Tests

```bash
pytest tests/
```

## Requirements

The project ships **two** dependency layers:

- **`requirements-runtime.txt`** — numpy / scipy / h5py / PyWavelets /
  paho-mqtt / aiohttp.  This is what the Pi 4B add-on actually installs
  (~30 MB).  Inference at runtime is pure-numpy and the
  `tests/test_numpy_keras_equivalence.py` suite guarantees numerical
  parity with the TensorFlow-trained checkpoint.
- **`requirements-train.txt`** — adds TensorFlow / Keras / pytest /
  hypothesis on top.  Use this on a workstation when you (re)train the
  model or run the full test suite.

```bash
pip install -r requirements-runtime.txt   # inference-only, ~30 MB
pip install -r requirements-train.txt     # everything (training + tests)
```

Minimum versions:

- Python >= 3.10
- numpy >= 1.24, scipy >= 1.11, h5py >= 3.9
- PyWavelets >= 1.4.1, paho-mqtt >= 1.6.1, aiohttp >= 3.9
- TensorFlow >= 2.13 *(training only)*
- hypothesis >= 6.82, pytest >= 7.4 *(testing only)*

## License

MIT License
