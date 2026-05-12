# 产品概述（Product）

## 项目名称
**Sleep Classifier** —— 一个 Home Assistant（HA）Add-on，用于实现「学习用户个人睡眠偏好 + 闭环调节卧室环境」的智能睡眠助手。

## 核心定位
- 这是一个 **HA Add-on**（不是独立应用），部署在 Home Assistant OS（Raspberry Pi 4B / amd64）上，通过 Supervisor 构建和管理。
- **不训练、不运行本地睡眠分期模型**。自 v1.3.0 起，移除了本地 CNN-BiLSTM 模型，改为订阅 HA 中**任意已有的睡眠分期实体**（Apple Watch、小米手环、Fitbit、sleep_as_android、毫米波雷达等）。
- 镜像体积约 15 MB，运行时依赖只有 `aiohttp`（v1.6.0 已移除 numpy，所有数值计算走纯 Python）。

## 核心价值
1. **学习用户偏好**：根据历史睡眠数据（温度、湿度、亮度、风扇），结合 0–100 的睡眠质量分，找出用户「睡得最好」时的环境中点（midpoint）。
2. **闭环调节**：在入睡前、各睡眠阶段（AWAKE/LIGHT/DEEP/REM）、唤醒窗口内，分别下发设定点到灯、空调、加湿器、风扇。
3. **可解释性**：所有学到的东西都以 HA sensor 的形式暴露，Lovelace 面板可直接查看「为什么今晚是这个设定点」。

## 四个时间尺度的个性化
1. **夜内（per-stage）**：按睡眠阶段调节（AWAKE 偏暖偏亮、DEEP/REM 偏冷偏暗）。
2. **周内（workday vs weekend）**：按 wake-day 分桶，工作日和周末分别给出推荐入睡时间。
3. **月内（recency decay）**：指数衰减（默认 14 天半衰期），季节变化在约一个月内融入模型。
4. **当晚（current-context k-NN）**：按今晚的入睡时间 + 环境温度，用加权中位数在历史邻居中选 k 个最相似的夜晚。

## v1.5.0+ 关键特性
- **学习型 per-stage deltas**：不仅学习中点，连阶段间的温差/亮度差也由用户自己的数据学习，带 Kish 有效样本量（ESS ≥ 4）保护。
- **真实世界稳健性（v1.4.0）**：per-actuator anticipation（按设备响应时间提前吹冷风）、wind-down 预冷、stage debouncing（过滤 30 秒可穿戴抖动）。
- **v1.6.0 工程化**：新增独立的 `LearningPanelPublisher`、WebSocket 断线重连集成测试、60 秒 bedtime cache、`apnea_detector` 纯函数模块（未接入主流程）。

## 非目标（Non-goals）
- 不做医学诊断、不替代医疗建议。
- 不训练深度学习模型；若用户需要复现旧版 CNN-BiLSTM 学术结果，请使用 Git tag `v1.2.3`。
- 不支持私有仓库安装（HA Supervisor 不认证 GitHub）。

## 许可
MIT License。
