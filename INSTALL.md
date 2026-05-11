# 在 Home Assistant OS 上一键安装(树莓派 4B)

如果你的 HA 是装在 HA OS (Pi 4B 完整版),用 **Add-on Repository** 方式安装,
体验和 HACS 完全一致 — **不用 SSH、不用 pip、不用生成 token**。

## 总流程(6 步,~5 分钟)

```text
1. 跑 prepare.bat / prepare.sh 同步 rootfs/
2. Push 本仓库到你自己的 GitHub
3. 在 HA Web UI 添加仓库 URL
4. 安装 "Sleep Classifier" add-on（等 build 完成,~3-5 min）
5. 在 Configuration tab 填入 area 等参数
6. Start → 完成
```

> 💡 **轻量化部署**:add-on 镜像里**不装 TensorFlow**(~30 MB 而非 ~650 MB),
> 推理走纯 numpy 路径。训练用的 Keras 权重通过
> `tests/test_numpy_keras_equivalence.py` 验证与 numpy 推理数值一致
> (max abs diff < 1e-3)。

---

## 1. 先在本机跑 prepare 脚本 ⚠️ 必做

HA Supervisor build add-on 时,**只能看到 add-on 目录内的文件**,
看不到仓库根的 `src/`、`scripts/`、`config/`、`models/`。所以 push 之前要把
这些目录同步到 `addons/sleep_classifier/rootfs/`:

**Windows**:

```cmd
addons\sleep_classifier\prepare.bat
```

**Linux/macOS**:

```bash
chmod +x addons/sleep_classifier/prepare.sh
addons/sleep_classifier/prepare.sh
```

预期输出:

```text
[prepare] mirrored src\
[prepare] mirrored scripts\
[prepare] mirrored config\
[prepare] mirrored models\
[prepare] copied requirements.txt
[prepare] done
```

> 📌 每次修改 `src/`、`scripts/`、`config/`、`models/`、`requirements.txt`
> 之后,**都要重跑一次 prepare**,然后 commit + push。

## 2. 把本仓库 push 到 GitHub

如果还没建仓库:

```bash
# 在本机项目根
cd "C:\Users\28717\Desktop\大创结题睡眠模型"
git init
git add .
git commit -m "Initial commit with HA add-on"

# 在 GitHub 网页上 New Repository, 名字随意,公开或私有都行
git remote add origin https://github.com/<你的GitHub用户名>/<仓库名>.git
git branch -M main
git push -u origin main
```

> ⚠️ **如果是私有仓库**,HA Supervisor 不能直接拉。要么改公开,要么用
> Personal Access Token 在 URL 里:`https://<token>@github.com/...`

## 3. 在 HA Web UI 添加仓库

1. 浏览器打开 HA: `http://homeassistant.local:8123` 或 Pi 的 IP
2. **Settings → Add-ons → ADD-ON STORE**(右下角 + 号)
3. 右上角 **⋮ 三点菜单 → Repositories**
4. 在弹窗里粘贴你 1 步的仓库 URL,例如:
   `https://github.com/<你的用户名>/<仓库名>`
5. 点 **Add**, 然后 **Close**
6. 回到 Add-on Store,**刷新页面** (Ctrl+F5),滚到底应能看到一个新的分类
   **"CNN-BiLSTM Sleep Model Add-ons"**,里面有 **Sleep Classifier**

## 4. 安装 add-on

1. 点 **Sleep Classifier** 卡片 → **INSTALL**
2. **等待**。第一次 build 在 Pi 4B 上大约 **3–5 分钟**(下载 numpy /
   scipy / h5py / aiohttp wheel 约 30 MB,piwheels 直接提供 arm64 预编译包,
   不装 TensorFlow)。可以打开 **Log** tab 看进度。
3. Build 完成后会看到 **START / STOP / RESTART** 按钮可以点了。

## 5. 配置 add-on

### 5.1 推荐:用内置 Web UI 选实体(v1.2.1+)

**先 Start 一次 add-on**(默认 `dry_run: true`,不会乱动设备),然后:

1. **Sleep Classifier** 详情页 → 顶栏 **"OPEN WEB UI"** 按钮
2. 表单里每个槽位都是**下拉框**,选项 = 你 HA 里**真实存在**的实体,
   不用再去 Developer Tools 抄 entity_id
3. 填完点 **"保存 / Save"**
4. 回 add-on 详情页点 **RESTART**

Web UI 写到 `/data/web_ui_overrides.json`,**优先级高于** Configuration tab
的同名字段。要清空某个槽位回到自动发现,选下拉里第一项 `— 留空(自动发现) —`。

### 5.2 备用:直接编辑 Configuration tab

切到 **Configuration** tab。**v1.1.0 默认 `dry_run: true`**,你可以直接
Save + Start 试跑。最少要改的只有 sensor 槽位:

```yaml
# ---- 选填:留空就走自动发现 + 中文/英文双语关键字扫描 ----
area: ""                            # 留空 = 扫全 HA;填了用作软过滤

# ---- 强烈建议:把雷达 / 手环 entity_id 直接钉到槽位 ----
# Developer Tools → States 搜你的设备名,把 entity_id 拷过来
heart_rate_source: sensor.xiaomi_smart_band_9_pro_heart_rate
movement_source: sensor.sleepradar_r60abd1_ts5_yundong_zhuangtai
breathing_source: sensor.sleepradar_r60abd1_ts6_huxi_xinxi

# ---- 你想让 add-on 控的设备 ----
light_targets:
  - light.bedroom_main
  - light.bedroom_bedside
climate_target: climate.bedroom_ac
humidifier_target: humidifier.bedroom_humidifier

dry_run: true                       # 第一晚保持 true!确认无误再关
```

**第一次 `dry_run: true`** 启动后看 **Log** tab:

- 列出识别到的传感器(应有你的手环、雷达、温湿度计…)
- 列出识别到的可控设备(应有卧室灯、空调…)
- 每 30 秒推理一次 + 打印"planned"动作(没真发出)

如果 0 命中,日志会**自动列出候选 entity_id**,直接拷到 Configuration
对应的 `*_source` 槽位就行。

确认无误后改回 `dry_run: false`,RESTART。

## 6. 启动并验证

* **Info tab → START**
* 切到 **Log tab**, 等 30 秒应该看到:

  ```text
  [run.sh] slot bindings active: 5 role(s)
  smart_service | Fetching entity registry from HA …
  smart_service | HA exposes 187 entities
  src.device_discovery | Device discovery — sensor sources
    heart_rate   → 1 entities: ['sensor.xiaomi_smart_band_9_pro_heart_rate']
    movement     → 1 entities: ['sensor.sleepradar_r60abd1_ts5_yundong_zhuangtai']
    breathing    → 1 entities: ['sensor.sleepradar_r60abd1_ts6_huxi_xinxi']
  smart_service | inference_buffer restored (hr=512, mv=512, age=120s)
  smart_service | infer stage=LIGHT conf=0.91  env(T=22.5 H=48.0)
  smart_service |   → 3 HA action(s) planned
  ```

* 在 HA UI 看睡眠数据:**Developer Tools → States → 搜 sleep_classifier**
  应能看到 4 个新实体:

  ```text
  sensor.sleep_classifier_stage              # AWAKE / LIGHT / DEEP / REM
  sensor.sleep_classifier_confidence         # 0..100 %
  sensor.sleep_classifier_quality_score      # 最近一次睡眠质量评分
  sensor.sleep_classifier_session_duration   # 当前会话累计秒数
  ```

  把它们拖到 Lovelace 卡片(参见下文示例)就能在 dashboard 看睡眠分期实时变化。

* 开 **Settings → Devices & Services → Logbook**, 应该看到本 add-on 调用
  `light.turn_on` / `climate.set_temperature` 等的记录。

* 长期运行后,**Settings → Add-ons → Sleep Classifier → Files tab**(需要
  开 SSH add-on 才能直接看),或者在 HA 终端里:

  ```bash
  cat /usr/share/hassio/addons/data/<slug>/user_preferences.json
  ```

  可看到学习器记录的会话历史 + 质量分。

---

## Lovelace 卡片(直接复制粘贴)

在你的 Dashboard 右上角 ⋮ → Edit dashboard → 加卡片 → Manual,
粘贴下面 YAML:

```yaml
type: vertical-stack
cards:
  - type: glance
    title: 睡眠监测
    entities:
      - entity: sensor.sleep_classifier_stage
        name: 当前阶段
      - entity: sensor.sleep_classifier_confidence
        name: 置信度
      - entity: sensor.sleep_classifier_quality_score
        name: 上次评分
      - entity: sensor.sleep_classifier_session_duration
        name: 本次时长
  - type: history-graph
    title: 整夜睡眠分期
    hours_to_show: 12
    entities:
      - sensor.sleep_classifier_stage
      - sensor.sleep_classifier_confidence
```

**进阶**:用 stage 变化触发 automation,例如深睡时静音电视:

```yaml
alias: "深睡静音电视"
trigger:
  - platform: state
    entity_id: sensor.sleep_classifier_stage
    to: "DEEP"
action:
  - service: media_player.volume_mute
    target:
      entity_id: media_player.bedroom_tv
    data:
      is_volume_muted: true
```

## 长期保养

- **自动重启**:Info tab 打开 **Watchdog** 和 **Auto update** 开关。
- **看实时日志**:浏览器留着 Log tab 即可,自动滚动。
- **升级 add-on**:push 新版本 + 改 `addons/sleep_classifier/config.yaml`
  的 `version` 字段,HA 会自动检测并在 Info tab 显示 **UPDATE** 按钮。
- **WS 自动重连**:网络抖动 add-on 自动指数回退重连(1 → 2 → 4 → … → 5 min),
  不会再因网络问题假死退出。
- **重启秒恢复**:推理 buffer 持久化到 `/data/inference_buffer.npz`,
  重启 add-on 不再要等 10 分钟冷启动(只要上次保存 < 6 小时前)。

---

## 卸载

* HA UI → Settings → Add-ons → Sleep Classifier → UNINSTALL
* 偏好历史保存在 `/usr/share/hassio/addons/data/<slug>/user_preferences.json`,
  add-on 卸载后**不会**自动删,可以下次重装继续用。要彻底清掉:

  ```bash
  # 通过 SSH add-on
  rm /data/user_preferences.json
  ```

---

## 故障排查速查

| 现象 | 原因 | 解决 |
|---|---|---|
| 添加仓库报错 "Not a valid repository" | URL 错或仓库私有未公开 | 检查 URL,改公开,或用 token URL |
| Add-on 不出现在 Store | 缓存 | 强制刷新(Ctrl+F5)、重启 supervisor |
| Build 卡在 `installing scipy/h5py` | 网络慢或 piwheels 临时不可用 | 等待,或检查 Pi 网络 |
| Build 失败:`no matching distribution for h5py` | 极少见的非主流 arch | 看 Log 找具体报错,issue 给我 |
| 启动时 `TENSORFLOW not available — using numpy-based ...` 警告 | 正常,这是预期行为 | 忽略,推理走 numpy 路径,与训练时数值等价 |
| Start 后立刻 stop,日志说 `No HR/movement/breathing sensor found` | sensor 没识别到 | 看 Log 末尾的"Suggested entity_ids"列表,把对应 entity_id 拷到 Configuration 的 `*_source` 槽位 |
| 灯/空调没反应 | dry_run=true,或 deadband 卡住 | 检查 Configuration,看 Log 里"planned" vs "Executed";HA Logbook 也会显示真实调用 |
| HA UI 看不到 `sensor.sleep_classifier_*` 实体 | 还没出现首次推理 | 等 ~10 分钟(冷启动)或确认 buffer 已恢复;`dry_run` 也会创建实体 |
| 日志反复 `WebSocket transport error` | HA 或网络抖动 | 自动重连,只要不是 401/403 不用管;持续抖动看 supervisor 日志 |

---

## 拟自然睡眠功能(v1.2.0 新增)

v1.2.0 新增 5 个"拟自然睡眠"模块,全部**可选**,在 Configuration tab
按需开启。

### 1. 睡眠债务(核心专利)

填 `birth_year`(如 `1995`)启用。add-on 从你的历史会话 vs 年龄段推荐时长
(NSF/AAP 2015/2016)算累积债务,Lovelace 看:

```yaml
type: entity
entity: sensor.sleep_classifier_debt_hours
# attributes 里还有: severity / nightly_target_hours / nights_to_full_recovery
```

结合 `wake_window_start` / `wake_window_end`,会同步输出今晚推荐就寝:

```text
sensor.sleep_classifier_recommended_bedtime = "2026-05-12T23:35:00"
  attr: tonight_target_hours = 10.5
  attr: reason = "You're 3.2 h in debt — too large for one night..."
```

恢复策略基于 Van Dongen 2003 + Belenky 2003:小债务(≤ 2h)一晚补足;
大债务多晚分摊(每晚还 50 %),直到 < 0.5 h 视为归零。

### 2. 智能唤醒

给一个**窗口** 而不是一个点,系统在窗口内自动选最优唤醒时刻:

```yaml
wake_window_start: "07:00"
wake_window_end: "07:30"
wake_light_targets:
  - light.bedroom_main
  - light.bedroom_bedside
```

逻辑:窗口开始前 30 min 开始光线渐进 ramp(Phipps-Nelson 2003);窗口内
优先在 **LIGHT / post-REM 边界**触发(Hilditch & McHill 2019);若到
窗口末还在 DEEP,60 秒安全边际强制唤醒(不会迟到)。

实时决策看 `sensor.sleep_classifier_wake_decision`:
`hold → pre_ramp → open_window → fire_now`。

### 3. 白噪音匹配

把一个 `media_player` 绑到 `whitenoise_target`,系统按阶段自动切音乐:

```text
AWAKE (入睡前) → 雨声 30 %
LIGHT          → 粉红噪声 22 %
DEEP           → 棕色噪声 18 %  (Papalambros 2017)
REM            → 静音       (Massar 2024: 噪音碎片化梦境)
PRE-WAKE       → 晨鸟声 35 %   (Geerdink 2016)
```

Lovelace:`sensor.sleep_classifier_soundscape` enum 实体实时显示当前音轨。

### 4. 主观反馈作权重

在 **Settings → Devices & Services → Helpers** 建一个 `input_number`
(范围 1-5),填到 `feedback_entity`:

```yaml
feedback_entity: input_number.sleep_rating
feedback_scale: 5
```

起床后给自己打分,下一个会话结算时会把 1-5 映射成 0-100,与客观分
(架构 + SE + WASO + SOL,AASM + Ohayon 2017)加权 60/40 混合;
同时喂给偏好学习器 + UserProfile 的贝叶斯后验更新,让系统学到
"你的好觉"。

### 5. 用户画像

`birth_year` + `chronotype: morning/evening/neutral` 驱动。**9 个年龄
段**(新生儿/婴儿/幼儿/学龄前/学龄/青少年/青年/成年/老年)按 NSF/AAP
论文查表得推荐时长,再经 7 晚 pseudo-count 贝叶斯先验 × 你的实际高质量
夜晚更新个人后验(clamped 在年龄段 low/high 区间)。文件:
`/data/user_profile.json`。

### 示例 Lovelace 卡片 v2(替换上面 v1.1.0 的)

```yaml
type: vertical-stack
cards:
  - type: glance
    title: 睡眠监测
    entities:
      - entity: sensor.sleep_classifier_stage
        name: 当前阶段
      - entity: sensor.sleep_classifier_debt_hours
        name: 睡眠债
      - entity: sensor.sleep_classifier_recommended_bedtime
        name: 今晚就寝
      - entity: sensor.sleep_classifier_wake_decision
        name: 闹钟状态
      - entity: sensor.sleep_classifier_soundscape
        name: 当前音轨
      - entity: sensor.sleep_classifier_quality_score
        name: 上次评分
  - type: history-graph
    title: 整夜睡眠分期 + 质量趋势
    hours_to_show: 24
    entities:
      - sensor.sleep_classifier_stage
      - sensor.sleep_classifier_quality_score
      - sensor.sleep_classifier_debt_hours
```

## 多房间 / 多床位(夫妻房 + 客房)

HA Supervisor 不允许同一 slug 的 add-on 装两份,所以如果你想在两个
房间同时跑模型,需要在仓库里**复制一份 add-on 目录**:

```bash
cp -r addons/sleep_classifier addons/sleep_classifier_guest
# 编辑 addons/sleep_classifier_guest/config.yaml,只改两处:
#   slug: sleep_classifier_guest
#   name: Sleep Classifier (Guest)
# 重跑 prepare:
addons/sleep_classifier_guest/prepare.sh   # 或 .bat
git add -A; git commit -m "feat: guest-room instance"; git push
```

刷新 Add-on Store,你会看到两个 Sleep Classifier 卡片。每个独立 build、
独立 Configuration tab,独立 `*_source` 槽位 → 互不干扰。

> ⚠️ 两个实例发布的 HA 实体 ID 默认相同
> (`sensor.sleep_classifier_stage`),会互相覆盖。复制后请编辑
> `addons/sleep_classifier_guest/rootfs/src/sleep_state_publisher.py`
> 把 `ENTITY_*` 常量加上 `_guest` 后缀,然后 prepare 一遍。

## 不想用 Add-on?

如果你坚持手动跑(比如 Pi 不在 HA OS 而是 Raspberry Pi OS),改看:

* [`docs/HA_SMART_DEPLOYMENT.md`](docs/HA_SMART_DEPLOYMENT.md) — 手动 pip + systemd 方案
* [`docs/HA_DEPLOYMENT.md`](docs/HA_DEPLOYMENT.md) — 轻量 MQTT 集成
