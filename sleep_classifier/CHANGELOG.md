# Sleep Classifier — Changelog

> 这个文件会显示在 HA 的 Add-on 更新提示对话框里。
> 完整工程日志见 GitHub 上的 [`CHANGELOG.md`](https://github.com/LiangyuLu-lly/HA-sleep/blob/main/CHANGELOG.md)。

## v2.0.2 (2026-05-14) — 首次安装体验修复

- 🩹 **修复 Web UI 点「重新加载实体列表」偶发 502**：容器架构重构 —
  Web UI 现在是前台 PID 1（永远在线），主服务在后台由 bash 循环监管，
  崩溃不再拖垮容器。
- 🔒 **修复未配置 `sleep_stage_source` 时的死循环**：主服务不再直接退出触发
  容器重启，而是等到用户在 Web UI 绑定了睡眠阶段实体再启动。
- 🛡️ **修复用户输入含中文/引号时 `run.sh` 的 heredoc 注入问题**：
  把 Python 配置生成器从 run.sh 的 heredoc 搬到独立文件
  `render_effective_config.py`，通过环境变量传参，彻底消除 shell 字符串
  插值。
- 🧹 镜像构建时自动清理 `__pycache__` 和 `.pyc` 残留。

## v2.0.1 (2026-05-14) — 网络适配

- 🐳 切换基础镜像从 `ghcr.io/home-assistant/aarch64-base:3.19` 到
  Docker Hub 的 `python:3.11-alpine`，在国内网络环境下构建速度从「卡 10+ 分钟」
  变成「2 分钟内完成」。

## v2.0.0 (2026-05-14) — 商业化打磨版

- 🎨 全新 4-view Lovelace 仪表板（覆盖全部 20 个 sensor）
- 🌐 关键日志双语化（中文系统自动切换）
- 📋 11 条常见问题 FAQ
- 🔧 `scripts/diagnostic_export.py` 一键导出诊断信息
- 🎵 白噪音一键降音量按钮
- 🧪 501 个测试全绿

## v1.9.0 (2026-05-13) — 用户反馈 + 边界加固

- 🎛️ 用户可手动覆盖学到的温度（`input_number`）
- 📊 首晚自动输出详细诊断报告
- 🕐 夏令时切换日不再崩溃
- ⏱️ HA 重启后 Add-on 正确延迟发数据
- 🧪 新增 7 天合成数据收敛测试 + 事件风暴压测

## v1.8.0 (2026-05-13) — 可观测性 + 数据保护

- 🚥 新增 `sensor.sleep_classifier_health` 一眼看系统状态
- 📈 质量分拆成 4 个子 sensor（架构/效率/碎片化/入睡）
- 💤 < 60 分钟的 session 自动过滤，不污染学习
- 💾 偏好数据滚动备份，主文件损坏自动恢复
- 🧪 新增 8 小时端到端夜晚集成测试

## v1.7.0 (2026-05-13) — 呼吸暂停趋势监测

- 🫁 新增 `sensor.sleep_classifier_apnea_index`（opt-in）
- ⚠️ 完全不公开 AHI 数字（防止被当作医疗诊断）
- 🔒 撤销同意立即清除基线数据

## v1.6.4 (2026-05-13) — 环境传感器稳健性

- 🌡️ 15 分钟没报告的传感器视为无效
- 🛑 空调无法降温时不再反复发指令

## v1.6.3 (2026-05-13) — Session 生命周期

- ⏰ Session 边界自动检测（onset 5 分钟 / wake 10 分钟）
- 👤 每个 session 独立统计，不再把多晚混算

## v1.6.2 (2026-05-12) — 设备能力感知

- 🧠 自动识别每个设备真正支持的指令
- 🔄 不支持的指令优雅降级（避免 HA 假装成功的坑）

## v1.6.0 (2026-05-12) — 工程抽象

- 🏗️ `LearningPanelPublisher` 模块化抽出
- 🔌 WebSocket 断线自动重连
- ⚡ 60 秒 bedtime 缓存

## v1.5.0 (2026-05-12) — Per-stage 学习

- 🎯 每个阶段的偏移量也从用户数据学习
- 📏 Kish 有效样本量守卫（ESS ≥ 4）

## v1.4.0 (2026-05-12) — 真实世界稳健性

- 🧊 AC 提前 15 分钟预冷（per-actuator 预测）
- 🌙 wind-down 预冷（入睡前 30 分钟开始调温）
- 🎛️ Stage 抖动过滤（30 秒短醒不触发控制）

## v1.3.0 (2026-05-12) — 外部分阶订阅

- 📱 移除本地 CNN-BiLSTM 模型
- 🔗 改为订阅任意 HA 睡眠分阶实体
- 🌏 支持 Apple Watch / 小米手环 / Fitbit / sleep_as_android 等所有设备

---

## v2.0.1 (2026-05-13) — 构建系统切到 Docker Hub

用户 HA 的 Docker 拉 `ghcr.io/home-assistant/*-base:3.19` 时卡死
10+ 分钟（国内网络 ghcr.io 不稳定）。改为 `python:3.11-alpine`
基础镜像，走 Docker Hub（国内镜像加速器有缓存）。

- Dockerfile base image: `ghcr.io/home-assistant/aarch64-base:3.19`
  → `python:3.11-alpine`
- 镜像体积略减（~15 MB → ~12 MB，省去 bashio/s6-overlay）
- 构建时间从卡死 → Pi 4B 上约 90 秒
