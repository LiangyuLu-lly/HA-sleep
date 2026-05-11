# 智能闭环部署指南(树莓派 4B + Home Assistant)

升级版部署 — 让本服务**直接接管 HA 中的智能家居**:自动发现传感器/设备、
学习用户睡眠偏好、实时调控环境。

> 📑 如果只想以 MQTT 传感器方式被动暴露给 HA(用户自己写 automation),
> 请改看 [`HA_DEPLOYMENT.md`](HA_DEPLOYMENT.md)。

## 0. 架构概览

```text
┌──────────────────────── Raspberry Pi 4B ────────────────────────┐
│                                                                  │
│  ┌──────────────────────┐         ┌──────────────────────────┐    │
│  │ Home Assistant       │  REST   │ Sleep Smart Service       │    │
│  │ (port 8123)          │ ◄──────►│ (本项目, asyncio loop)     │    │
│  │                      │  WS     │                           │    │
│  │ - 手环 sensor.*_hr   │         │ 1. HA API Client          │    │
│  │ - 雷达 sensor.*motion│         │ 2. 自动设备发现            │    │
│  │ - 灯 light.*         │         │ 3. CNN-BiLSTM 推理         │    │
│  │ - 空调 climate.*     │         │ 4. 偏好学习器(JSON 持久化)  │   │
│  │ - 加湿 humidifier.*  │         │ 5. 闭环控制(调用 service)  │   │
│  │ - 风扇 fan.*         │         │                           │    │
│  └──────────────────────┘         └──────────────────────────┘    │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

**核心差异**(相比传统 MQTT-only 集成):

| 维度 | MQTT 模式 | **智能闭环模式** |
|---|---|---|
| 与 HA 通信 | 单向(发布传感器) | **双向**(REST + WebSocket) |
| 控制设备 | 用户写 automation | **服务直接调用 light.turn_on 等** |
| 设备发现 | 用户手填 entity_id | **自动扫描 + 关键字匹配** |
| 个性化 | 没有 | **从历史会话学最优环境参数** |
| 反馈学习 | 没有 | **质量分数 → 探索 → 优化** |

---

## 1. 树莓派 4B 先决条件

### 1.1 Pi 4B 配置要求

| 项 | 推荐 |
|---|---|
| 内存 | 4 GB 或 8 GB(2 GB 也能跑但 TF 推理慢) |
| 存储 | 32 GB+ microSD(class 10 / A2) |
| 系统 | HA OS 12+ / Raspberry Pi OS 64-bit Bookworm |
| 网络 | 与手环 / 雷达同一局域网 |

### 1.2 HA 已经在 Pi 上跑了

* **HA OS** 用户:可以,但你需要在 SSH add-on 里跑本服务,或者把本服务装到
  **另一台机器 / 同台 Docker 容器** 中。Pi 上 HA OS 不允许直接 pip install。
* **HA Supervised / HA Container / HA Core on Raspberry Pi OS**:都可以原生跑
  本服务。推荐这种,本指南以此为例。

> ⚠️ 如果你坚持用 HA OS,把本服务装到 Pi 上另一个 LXC 容器或者干脆装到第二台机器
> (NUC / 老笔记本),通过局域网调用 HA REST。**只要能访问 8123 端口都行。**

### 1.3 生成 Long-Lived Access Token

1. HA Web UI → 点左下角你的头像 → **Security** 标签 → 滚到底部 **Long-Lived
   Access Tokens** → **Create Token**
2. 起个名字 `sleep_smart_service`,**保存生成的 token 字符串**(只显示一次!)

---

## 2. 安装项目到 Pi

### 2.1 SSH 进 Pi,装 Python 3.10+ 和依赖

```bash
ssh pi@<pi-ip>

# 系统包(Pi OS Bookworm 已自带 Python 3.11)
sudo apt update
sudo apt install -y python3-pip python3-venv git libhdf5-dev pkg-config

# 拉项目
cd ~
git clone <你的仓库 URL> sleep_smart
cd sleep_smart

# 虚拟环境 + 装依赖(TF 在 arm64 上要装 tensorflow-aarch64)
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt
```

**关于 TF on Pi**:`pip install tensorflow` 在 arm64 上现在可以直接装(2024+
版本官方支持 aarch64),但**编译/装包要 10 分钟+**。如果想避免,可以:

```bash
pip install --extra-index-url https://www.piwheels.org/simple/ \
    tensorflow keras h5py
```

`piwheels` 是 Pi 社区预编译 wheel 镜像,大幅加速安装。

### 2.2 拷贝训练好的模型

```bash
# 从开发机用 scp 拷过去
scp models/best_model.h5 pi@<pi-ip>:~/sleep_smart/models/
```

或在 Pi 上直接训练(慢,但可行):

```bash
python scripts/download_data.py
python scripts/train.py --subjects ST7011 ST7022 ...
```

---

## 3. 配置

### 3.1 编辑 `config/config.json`

只需要改 `home_assistant.api` 段:

```jsonc
"home_assistant": {
  ...
  "api": {
    "base_url": "http://localhost:8123",
    "access_token": "在第 1.3 步保存的 token",
    "verify_ssl": false,
    "area_filter": "bedroom",
    "controllable_domains": [
      "light", "climate", "fan", "humidifier"
    ]
  },
  ...
}
```

> 🔐 **安全**:不要把 token 直接提交到 git。优先使用环境变量:
>
> ```bash
> export HA_TOKEN="eyJ..."
> ```
>
> 然后启动 service 时不要写 token 到命令行/config。

### 3.2 确认能调用 HA API

```bash
curl -s -H "Authorization: Bearer $HA_TOKEN" \
    http://localhost:8123/api/ | python3 -m json.tool
```

应返回 `{"message": "API running."}`。

### 3.3 看到自己的传感器/设备

```bash
curl -s -H "Authorization: Bearer $HA_TOKEN" \
    http://localhost:8123/api/states | \
    python3 -c 'import json,sys; [print(e["entity_id"]) for e in json.load(sys.stdin)]' | sort
```

你应该看到诸如:

* `sensor.mi_band_5_heart_rate` 或 `sensor.huawei_band_pulse` …
* `light.bedroom_main`、`climate.bedroom_ac`、`humidifier.bedroom` …

**如果没有手环**:本服务支持任何能发出心率的设备。常见接入方案:

| 设备 | HA 集成 |
|---|---|
| 小米手环 4/5/6/7 | [Gadgetbridge](https://gadgetbridge.org/) (HA via MQTT) |
| 华为手环 | Health 同步 → MQTT 桥接 |
| Apple Watch | HealthKit → [HAHealthBridge](https://github.com/) |
| Garmin Forerunner | [garminconnect](https://github.com/) custom_component |
| 毫米波雷达 | ESPHome + LD2410B / R60ABD1 模块 |
| 智能床垫 | Withings / Beddit / Eight Sleep HA integrations |

---

## 4. 启动服务

### 4.1 干跑(不调用真实 HA)

```bash
source venv/bin/activate
python scripts/run_ha_smart_service.py --dry-run --duration 5 --infer-interval 1
```

应该看到:

```
smart_service | Dry-run without token — discovery and live HA calls are skipped.
smart_service | Running offline synthetic loop for 5.0s
smart_service | stage=AWAKE conf=1.00
```

### 4.2 真实联调

```bash
export HA_TOKEN="eyJ..."
python scripts/run_ha_smart_service.py \
    --base-url http://localhost:8123 \
    --area bedroom \
    --infer-interval 30 \
    --session-interval 1800 \
    --duration 600
```

输出应类似:

```
smart_service | Fetching entity registry from HA …
smart_service | HA exposes 187 entities
src.device_discovery | Device discovery — sensor sources
src.device_discovery |   heart_rate   → 1 entities: ['sensor.mi_band_5_heart_rate']
src.device_discovery |   movement     → 1 entities: ['sensor.bedroom_mmwave_motion']
src.device_discovery |   temperature  → 1 entities: ['sensor.bedroom_temperature']
src.device_discovery |   humidity     → 1 entities: ['sensor.bedroom_humidity']
src.device_discovery | Device discovery — actionable devices
src.device_discovery |   lights        → 2 entities: ['light.bedroom_main', 'light.bedroom_lamp']
src.device_discovery |   climates      → 1 entities: ['climate.bedroom_ac']
src.device_discovery |   humidifiers   → 1 entities: ['humidifier.bedroom']
smart_service | Initial environment: T=22.5°C  H=48.0%  bright=3.0
smart_service | infer stage=LIGHT conf=0.91  env(T=22.5 H=48.0)
smart_service |   → 3 HA action(s) planned
src.smart_environment_controller | Executed climate.set_temperature(climate.bedroom_ac, temperature=21.0)
src.smart_environment_controller | Executed humidifier.set_humidity(humidifier.bedroom, humidity=55)
src.smart_environment_controller | Executed light.turn_on(light.bedroom_main, brightness_pct=8, kelvin=2200)
```

在 HA UI 里能看到灯被自动调暗、空调温度被改、加湿器开始工作。

### 4.3 控制策略说明

| 阶段 | 灯 | 温度 | 湿度 | 风扇 |
|---|---|---|---|---|
| AWAKE | 40 % 亮(4000 K) | 23 °C | 50 % | 20 % |
| LIGHT | 8 % 暖光(2200 K) | 21 °C | 55 % | 15 % |
| DEEP  | 关 | 19 °C | 55 % | 10 % |
| REM   | 关 | 19.5 °C | 55 % | 10 % |

这是默认值。**学到至少 3 个会话后**,系统会用 quality top 30 % 的会话
中位数取代这张表 — 即"在你睡得最好的 N 个晚上,平均环境是这样,我们今晚就
照这个来"。

`deadband_temperature_c=0.5` 等参数确保不会因为 0.1°C 的小波动反复调控。

---

## 5. 后台常驻 — systemd 服务

`/etc/systemd/system/sleep-smart.service`:

```ini
[Unit]
Description=CNN-BiLSTM Sleep Smart HA Service
After=network-online.target home-assistant.service
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/sleep_smart
Environment="HA_TOKEN=eyJ..."
ExecStart=/home/pi/sleep_smart/venv/bin/python \
    /home/pi/sleep_smart/scripts/run_ha_smart_service.py \
    --base-url http://localhost:8123 \
    --area bedroom \
    --infer-interval 30 \
    --session-interval 1800
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
sudo systemctl enable --now sleep-smart
sudo journalctl -fu sleep-smart
```

---

## 6. Docker 部署(可选)

`Dockerfile.smart`:

```dockerfile
FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    libhdf5-dev pkg-config && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV PYTHONUNBUFFERED=1
CMD ["python", "scripts/run_ha_smart_service.py", \
     "--base-url", "http://host.docker.internal:8123", \
     "--area", "bedroom"]
```

```bash
docker build -t sleep-smart -f Dockerfile.smart .
docker run -d --restart=unless-stopped \
    --name sleep-smart \
    --add-host=host.docker.internal:host-gateway \
    -e HA_TOKEN="eyJ..." \
    -v $(pwd)/models:/app/models:ro \
    -v $(pwd)/data:/app/data \
    -v $(pwd)/config:/app/config:ro \
    sleep-smart
```

---

## 7. 偏好学习数据

每 30 分钟(`--session-interval`)生成一次会话快照,持久化到:

```
data/user_preferences.json
```

格式:

```json
{
  "version": 1,
  "updated_at": 1717000000.0,
  "sessions": [
    {
      "session_id": "a4f3b2e1",
      "started_at": 1716999000.0,
      "ended_at": 1717000800.0,
      "env_params": {
        "temperature_c": 19.5,
        "humidity_pct": 55.0,
        "brightness_pct": 0.0,
        "fan_speed_pct": 10.0
      },
      "stage_counts": {"AWAKE": 4, "LIGHT": 35, "DEEP": 12, "REM": 9},
      "quality_score": 78.4,
      "n_samples": 60,
      "notes": "auto checkpoint"
    }
  ]
}
```

**质量分公式**(`compute_quality_score`):

```
score = 50
      + 100 * (P(DEEP) - 0.10)         # 深睡奖励
      +  60 * (P(REM)  - 0.18)         # REM 奖励
      +  10 * (P(LIGHT) - 0.50)        # 浅睡微奖励
      - 150 * max(0, P(AWAKE) - 0.05)  # 碎片化惩罚
```

clip 到 [0, 100]。

---

## 8. 故障排查

### Q1: 服务报 `HA REST ping failed`

* 用 `curl` 验证 token + URL(见 3.2)
* 检查 HA 是否监听 `0.0.0.0` 而不是 `127.0.0.1`(本机访问没问题,Docker 需要)
* 检查 Pi 防火墙

### Q2: 找不到任何心率/运动传感器

* 列出所有 sensor 实体并搜索关键字:
  ```bash
  curl -s -H "Authorization: Bearer $HA_TOKEN" \
      http://localhost:8123/api/states | grep -i "heart\|motion\|pulse"
  ```
* 如果实体名不含 `heart_rate` / `hr` / `pulse`,在 `config.json` 的
  `heart_rate_keywords` 加你设备特有的关键字
* 或者用 `explicit_includes` 强制指定 entity_id

### Q3: 服务调用了 HA 但设备没反应

* HA Web UI → **Developer Tools** → **Services** → 手动调用同样的服务,
  看是否成功。如果手动也失败,问题在 HA 端(设备脱机 / 集成报错)
* 在 service 日志找 `Service call failed` 错误信息
* 设备类型不支持某个服务(比如某些灯不支持 `brightness_pct`,需要 `brightness`)
  — 当前实现用通用参数,绝大多数 HA-supported 设备都支持

### Q4: 灯被反复开关 / 温度跳来跳去

* 调大 `min_seconds_between_actions`(默认 120 秒)
* 调大 `deadband_*`(默认 0.5°C / 5% / 10%)
* 关闭学习器的 exploration(`exploration_rate: 0`)直到稳定

### Q5: 想暂时禁用智能控制,只看推理

在 config.json 或 CLI 设置:

```bash
python scripts/run_ha_smart_service.py --dry-run ...
```

依然连 HA / 订阅传感器 / 跑推理,只是不发送 service 调用。

---

## 9. 卸载

```bash
sudo systemctl disable --now sleep-smart
sudo rm /etc/systemd/system/sleep-smart.service
# 或 Docker
docker rm -f sleep-smart
```

历史 `data/user_preferences.json` 可保留供以后参考。

---

## 10. 进阶:多房间 / 多人

* 每个房间起一个 service 实例,各自带 `--area bedroom_a`、`--area bedroom_b`、
  `device_id sleep_classifier_bedroom_a`、`history_path data/prefs_a.json`
* HA 端的实体会被分组到不同 device(因为 `device_id` 不同)
* 偏好数据独立持久化,互不干扰
