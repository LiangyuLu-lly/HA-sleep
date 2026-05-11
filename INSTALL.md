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
   scipy / h5py wheel 约 30 MB,piwheels 直接提供 arm64 预编译包)。
   可以打开 **Log** tab 看进度。
3. Build 完成后会看到 **START / STOP / RESTART** 按钮可以点了。

## 5. 配置 add-on

切到 **Configuration** tab。每个字段都有说明,可改可不改。最重要的是:

```yaml
area: bedroom                       # 改成你的房间名(area_id)
infer_interval: 30                  # 30 秒推一次,默认即可
dry_run: false                      # 第一次先设 true 跑测试!
controllable_domains:
  - light
  - climate
  - humidifier
  # 删掉 fan / switch / media_player 如果你不想让它碰这些
```

**强烈建议第一次先 `dry_run: true`**,启动后看 **Log** tab:

- 它列出了识别到的传感器(应该有你的手环、温湿度计…)
- 它列出了识别到的可控设备(应该有你卧室的灯、空调…)
- 每 30 秒推理一次 + 打印"如果不是 dry_run 我会调用 xxx"

确认无误后改回 `dry_run: false`,RESTART。

## 6. 启动并验证

* **Info tab → START**
* 切到 **Log tab**, 等 30 秒应该看到:

  ```text
  smart_service | Fetching entity registry from HA …
  smart_service | HA exposes 187 entities
  src.device_discovery | Device discovery — sensor sources
    heart_rate   → 1 entities: ['sensor.mi_band_5_heart_rate']
    movement     → 1 entities: ['sensor.bedroom_mmwave_motion']
  ...
  smart_service | infer stage=LIGHT conf=0.91  env(T=22.5 H=48.0)
  smart_service |   → 3 HA action(s) planned
  ```

* 开 **Settings → Devices & Services → Logbook**, 应该看到本 add-on 调用
  `light.turn_on` / `climate.set_temperature` 等的记录。

* 长期运行后,**Settings → Add-ons → Sleep Classifier → Files tab**(需要
  开 SSH add-on 才能直接看),或者在 HA 终端里:

  ```bash
  cat /usr/share/hassio/addons/data/<slug>/user_preferences.json
  ```

  可看到学习器记录的会话历史 + 质量分。

---

## 长期保养

* **自动重启**: 在 Info tab 打开 **Watchdog** 和 **Auto update** 开关。
* **看实时日志**: 浏览器留着 Log tab 即可,自动滚动。
* **升级 add-on**: 当你 push 新版本到 GitHub 并改 `addons/sleep_classifier/config.yaml`
  的 `version` 字段后,HA 会自动检测更新并在 Info tab 显示 "UPDATE" 按钮。

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
| Start 后立刻 stop | Log 里会有原因,常见是 `No HR/movement sensors` | 加 `explicit_includes` 把你的实体 ID 强制塞进去 |
| 灯/空调没反应 | dry_run=true,或 deadband 卡住 | 检查 Configuration,看 Log 里"planned" vs "Executed" |

---

## 不想用 Add-on?

如果你坚持手动跑(比如 Pi 不在 HA OS 而是 Raspberry Pi OS),改看:

* [`docs/HA_SMART_DEPLOYMENT.md`](docs/HA_SMART_DEPLOYMENT.md) — 手动 pip + systemd 方案
* [`docs/HA_DEPLOYMENT.md`](docs/HA_DEPLOYMENT.md) — 轻量 MQTT 集成
