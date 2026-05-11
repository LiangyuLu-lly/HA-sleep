# Home Assistant 部署指南

把训练好的 CNN-BiLSTM 睡眠分类器部署成 Home Assistant 设备的完整步骤。

## 0. 架构概览

```
┌──────────────────────┐                  ┌──────────────────────┐
│  本项目 Python 服务   │   MQTT JSON     │   Home Assistant     │
│  scripts/run_ha_     │ ───────────────► │   (自动发现 6 个实体) │
│  service.py          │   Discovery+    │                      │
│                      │   State topics  │   Automation 触发     │
│  - EDF 回放 / 实时    │                  │   ├─ light.bedroom   │
│    MQTT 订阅          │                  │   ├─ climate.ac      │
│  - CNN-BiLSTM 推理    │                  │   └─ humidifier.*    │
│  - 每 30 秒发布状态    │                  │                      │
└──────────┬───────────┘                  └──────────▲───────────┘
           │                                         │
           ▼                                         │
   ┌───────────────────────────────────────────────────────┐
   │  MQTT Broker (Mosquitto add-on / 独立 broker)          │
   │  Broker 地址: 通常等于 HA 主机 IP, 端口 1883            │
   └───────────────────────────────────────────────────────┘
```

**核心思路**:本服务不直接调用 `light.turn_on`,而是把睡眠分期/心率/运动作为
**普通传感器实体** 发布给 HA。**控制逻辑用 HA 的 automation 编写**(见
[`ha_automations.yaml`](ha_automations.yaml))——这样用户可以在 HA UI 里
直接修改控制策略,无需重启本服务。

---

## 1. 先决条件

| 组件 | 说明 | 推荐版本 |
|---|---|---|
| Home Assistant | OS / Container / Supervised 任一形态 | 2024.x 以上 |
| MQTT Broker | HA 官方 add-on **Mosquitto broker** 即可 | 6.x |
| HA MQTT Integration | Settings → Devices & Services → Add → MQTT | 自动 |
| 本项目 Python 环境 | Python 3.10 + `pip install -r requirements-runtime.txt`(推理只需 ~30 MB,不装 TF) | 见 `requirements-runtime.txt` / `requirements-train.txt` |

### 1.1 安装 Mosquitto add-on (HA OS)

1. **Settings → Add-ons → Add-on Store**
2. 搜索并安装 **Mosquitto broker**
3. 启动 add-on 并打开"Show in sidebar"
4. 在 **Configuration** 标签中添加一个用户:
   ```yaml
   logins:
     - username: sleep_service
       password: "请改成强密码"
   ```
5. **Restart** add-on
6. 在 HA UI 左边栏点 **Mosquitto broker** 应该能看到运行中

### 1.2 添加 MQTT integration

1. **Settings → Devices & Services → Add Integration**
2. 搜索 **MQTT**, 选择 **MQTT Broker**
3. **Broker**: `core-mosquitto` (HA OS 内置)或 `localhost`
4. **Username/Password**: 上一步创建的账号
5. Submit, 应看到 "Connected to MQTT" 提示

> ⚠️ HA 默认监听端口是 `1883`。如果你的部署用了 8883 (TLS),需要在本服务
> 配置 `--port 8883` 并提供证书(本项目当前不内置 TLS,可用 stunnel 或 nginx 反代)。

---

## 2. 配置本项目

### 2.1 修改 `config/config.json`

```jsonc
{
  "mqtt": {
    "broker_address": "192.168.1.100",   // ← HA 主机 IP
    "broker_port": 1883,
    "username": "sleep_service",          // ← 1.1 步骤创建的账号
    "password": "请改成强密码",
    "topics": { ... }
  },
  "home_assistant": {
    "enabled": true,
    "discovery_prefix": "homeassistant",   // 默认前缀,与 HA 默认一致
    "device_id": "sleep_classifier_bedroom",
    "device_name": "Bedroom Sleep Classifier",
    "publish_interval_seconds": 30,        // 30 秒推一次状态
    "expire_after_seconds": 120            // 2 分钟未更新视为不可用
  }
}
```

> 💡 **如果你在 HA 中改了 discovery prefix**,这里也要保持一致。绝大多数
> 用户不需要改,保持 `homeassistant` 即可。

### 2.2 验证模型已训练

```bash
python scripts/train.py --subjects ST7011 ST7022 \
    --max-epochs 30 --batch-size 16 --learning-rate 0.05 \
    --stride 2048 --patience 8 --split-mode time
```

成功后,`models/best_model.h5` 存在(详见 `PROJECT_COMPLETION_REPORT.md`)。

---

## 3. 启动服务

### 3.1 干跑 (Dry-run) 验证

不连接 broker,只打印将要发布的内容:

```bash
python scripts/run_ha_service.py --dry-run --speedup 600 --duration 10 --publish-interval 5
```

应该看到:

```
ha_service | Dry-run mode — no MQTT connection will be opened.
src.ha_integration | Published Discovery for 6 HA entities
ha_service | t=  5.0s  HR= 82.0  MV= 0.18  true=AWAKE  pred=LIGHT  conf=0.25
...
```

### 3.2 实战:回放模式(用于演示)

```bash
python scripts/run_ha_service.py \
    --source replay \
    --subjects ST7011 \
    --speedup 60                # 1 分钟实际睡眠 → 1 秒墙钟时间
```

约 10 分钟跑完一整夜。期间打开 HA Web UI → **Settings → Devices & Services → MQTT**,
应该能看到自动发现的设备 **Bedroom Sleep Classifier** 含 6 个实体。

### 3.3 实战:订阅真实传感器(生产模式)

需要先让真实设备(如智能手环、毫米波雷达、ESPHome 设备)发布到本项目监听的 topic:

| Topic | Payload 示例 | 说明 |
|---|---|---|
| `sensors/heart_rate` | `{"value": 72, "timestamp": 1700000000}` | bpm |
| `sensors/movement`   | `{"value": 0.3, "timestamp": 1700000000}` | 归一化 0-1 |
| `sensors/smoke`      | `{"concentration": 5, "timestamp": ...}`  | ppm |
| `sensors/gas`        | `{"concentration": 2, "timestamp": ...}`  | %LEL |

启动:

```bash
python scripts/run_ha_service.py --source mqtt
```

---

## 4. 在 HA 中验证

### 4.1 找到设备

1. **Settings → Devices & Services**
2. 在 **MQTT** integration 下点 **devices**
3. 应看到 **Bedroom Sleep Classifier**
4. 点进去看到 6 个实体:
   - `sensor.sleep_classifier_bedroom_sleep_stage` (AWAKE/LIGHT/DEEP/REM)
   - `sensor.sleep_classifier_bedroom_sleep_confidence` (%)
   - `sensor.sleep_classifier_bedroom_heart_rate` (bpm)
   - `sensor.sleep_classifier_bedroom_movement_intensity`
   - `binary_sensor.sleep_classifier_bedroom_smoke_alarm`
   - `binary_sensor.sleep_classifier_bedroom_gas_alarm`

### 4.2 在 Dashboard 添加卡片

**Overview → ⋮ → Edit dashboard → ADD CARD → Entities**, 选择上述 6 个实体。

或者直接 YAML:

```yaml
type: entities
title: 睡眠监测
entities:
  - entity: sensor.sleep_classifier_bedroom_sleep_stage
    name: 当前阶段
  - entity: sensor.sleep_classifier_bedroom_sleep_confidence
    name: 置信度
  - entity: sensor.sleep_classifier_bedroom_heart_rate
    name: 心率
  - entity: sensor.sleep_classifier_bedroom_movement_intensity
    name: 运动强度
  - entity: binary_sensor.sleep_classifier_bedroom_smoke_alarm
    name: 烟雾
  - entity: binary_sensor.sleep_classifier_bedroom_gas_alarm
    name: 燃气
```

### 4.3 加载控制 automation

把 [`ha_automations.yaml`](ha_automations.yaml) 的内容复制到 HA 的
`configuration.yaml`(在最外层加 `automation:` 节)或者放到独立文件:

1. **File editor / Studio Code Server** add-on 打开 `/config/automations.yaml`
2. 粘贴文件内容,**修改其中的 `light.bedroom` / `climate.bedroom_ac` 等
   为你实际拥有的实体 ID**
3. **Developer Tools → YAML → Reload Automations**
4. **Settings → Automations & Scenes** 应看到 6 条新规则

---

## 5. 后台常驻运行

### 5.1 Linux / systemd (推荐生产)

`/etc/systemd/system/sleep-classifier.service`:

```ini
[Unit]
Description=CNN-BiLSTM Sleep Classifier HA Bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/sleep_model
ExecStart=/home/pi/sleep_model/venv/bin/python scripts/run_ha_service.py \
          --source mqtt --publish-interval 30
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

启用:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sleep-classifier
sudo journalctl -fu sleep-classifier
```

### 5.2 Docker

`Dockerfile`(项目根):

```dockerfile
FROM python:3.10-slim
WORKDIR /app
# Inference-only deps (no TensorFlow) keep the image at ~80 MB.
# Use requirements-train.txt instead if you intend to (re)train inside
# the container.  Numerical parity between numpy and Keras paths is
# guaranteed by tests/test_numpy_keras_equivalence.py.
COPY requirements-runtime.txt .
RUN pip install --no-cache-dir -r requirements-runtime.txt
COPY . .
ENV PYTHONUNBUFFERED=1
CMD ["python", "scripts/run_ha_service.py", "--source", "mqtt"]
```

```bash
docker build -t sleep-classifier .
docker run -d --restart=unless-stopped \
    --name sleep-classifier \
    -v $(pwd)/models:/app/models:ro \
    -v $(pwd)/config:/app/config:ro \
    sleep-classifier
```

### 5.3 Windows (开发期)

新建 `run_service.bat`:

```bat
@echo off
cd /d C:\Users\28717\Desktop\大创结题睡眠模型
E:\anaconda\envs\usleep\python.exe scripts\run_ha_service.py --source mqtt
pause
```

用 **任务计划程序** 设置开机自启即可。

---

## 6. 卸载 / 清理

### 6.1 删除 HA 中的实体

```bash
python scripts/run_ha_service.py --remove-discovery
```

下次打开 HA UI,**Bedroom Sleep Classifier** 设备会消失。

### 6.2 清理 retained MQTT 消息

如果 broker 留着旧的 retained discovery 消息(很少见),用 `mosquitto_pub` 手动清:

```bash
mosquitto_pub -h <broker> -u <user> -P <pass> -t \
    "homeassistant/sensor/sleep_classifier_bedroom/sleep_stage/config" \
    -n -r
```

`-n` = 空 payload,`-r` = retained,组合等于"删除"。

---

## 7. 常见问题

### Q1: HA 看不到设备?

1. 用 `mosquitto_sub -h <broker> -t '#' -v` 看 Discovery topic 是否真的发布了
2. 检查 HA Settings → System → Logs 有无 MQTT 报错
3. 确认 HA MQTT integration 的 broker 跟本服务用的是同一个

### Q2: 实体显示 unknown?

- 检查本服务是否在跑(`journalctl -fu sleep-classifier`)
- `expire_after` 默认 120 秒,超过没有 state 更新就会显示 unknown
- 调小 `--publish-interval` 或者调大 config 的 `expire_after_seconds`

### Q3: 报错 `Failed to connect to broker`?

- broker IP/端口是否正确?(`telnet <broker> 1883` 测试)
- Mosquitto add-on 是否启动?(HA Settings → Add-ons)
- 用户名密码对吗?(`mosquitto_pub` 命令行测试)

### Q4: 心率/运动数据从哪里来?

本项目当前作为"算法服务",自己不采集生理信号。两种方案:

1. **演示用** — 用 `--source replay` 把 EDF 文件作为虚拟传感器(适合答辩演示)
2. **生产用** — 让外部设备(手环 / 雷达 / 床垫传感器)通过 MQTT 发布心率
   和运动信号,本服务订阅它们。当前接入方案:
   - **Mi 手环**: 用 [gadgetbridge-mqtt](https://gadgetbridge.org/)
   - **毫米波雷达**: ESPHome + mmWave 模块,直接发布到 MQTT
   - **ECG 设备**: Polar / Garmin 通过 [Heart-Rate-Monitor-MQTT](https://github.com/) 之类的桥接

### Q5: 怎么改控制策略?

**不要改本服务代码**!直接改 `automations.yaml` 然后在 HA UI **Developer
Tools → Reload Automations**。本服务只负责"输出睡眠状态",HA 负责"做事"。

---

## 8. 进一步阅读

- HA MQTT Discovery 官方文档: https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery
- 项目结题报告: [`PROJECT_COMPLETION_REPORT.md`](PROJECT_COMPLETION_REPORT.md)
- 模型训练流程: 项目根目录 [`README.md`](../README.md)
- automation 模板: [`ha_automations.yaml`](ha_automations.yaml)
