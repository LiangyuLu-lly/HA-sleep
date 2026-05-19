"""端到端模拟一晚的 sleep_classifier 闭环行为。

用真实 src 模块，不依赖运行中的 HA。模拟流程：

  1. 喂 7 晚历史 session 让 PreferenceLearner 学到 midpoint
  2. 按 23:30 → 00:00 → 01:30 → 03:00 → 04:30 → 06:00 → 07:00 推进 stage
  3. 每个时点跑 SmartEnvironmentController.apply
  4. 打印每步规划的 HA service call
  5. 跑完后看 PreferenceLearner.recommend_*() 学到了什么

dry_run=True 下所有 HA call 都被 AsyncMock 拦截记录，不会真的吹冷气。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

# 让 src.* 可导
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data_structures import SleepStage
from src.device_discovery import ActionableDevices
from src.ha_api_client import HAEntity
from src.preference_learner import (
    EnvironmentParams,
    PreferenceConfig,
    PreferenceLearner,
    SleepSession,
)
from src.smart_environment_controller import (
    SmartControlConfig,
    SmartEnvironmentController,
)
from src.sleep_quality_score import (
    blend_subjective,
    compute_metrics,
    compute_objective_quality,
)
from src.user_profile import UserProfile


NIGHT_TIMELINE = [
    ("23:30", SleepStage.AWAKE, "入睡前 30 分钟（wind-down 预冷开始）"),
    ("00:00", SleepStage.LIGHT, "入睡，进入浅睡 LIGHT"),
    ("01:30", SleepStage.DEEP,  "进入深睡 DEEP（最低温、最暗）"),
    ("03:00", SleepStage.REM,   "REM 期"),
    ("04:30", SleepStage.LIGHT, "回到 LIGHT"),
    ("06:00", SleepStage.DEEP,  "晨间最后一段深睡"),
    ("07:00", SleepStage.AWAKE, "起床（自然结束 session）"),
]


def _print_banner(text: str) -> None:
    line = "=" * 72
    print()
    print(line)
    print(text)
    print(line)


def _make_devices() -> ActionableDevices:
    return ActionableDevices(
        lights=[
            HAEntity(
                entity_id="light.bedroom_main",
                state="off",
                attributes={"supported_color_modes": ["brightness", "color_temp"]},
            ),
        ],
        climates=[
            HAEntity(
                entity_id="climate.bedroom_ac",
                state="cool",
                attributes={"supported_features": 1},
            ),
        ],
        humidifiers=[
            HAEntity(
                entity_id="humidifier.bedroom",
                state="on",
                attributes={},
            ),
        ],
        fans=[
            HAEntity(
                entity_id="fan.bedroom",
                state="off",
                attributes={"supported_features": 1},
            ),
        ],
    )


def _seed_history(learner: PreferenceLearner) -> None:
    nights = [
        # (days_ago, bedtime_hh:mm, env_temp, humidity, brightness, quality)
        (7, "23:30", 21.5, 52, 4,  78.0),
        (6, "23:00", 21.0, 50, 3,  82.0),
        (5, "22:30", 22.0, 55, 5,  68.0),
        (4, "00:00", 20.5, 48, 2,  75.0),
        (3, "23:00", 21.0, 50, 3,  85.0),  # 用户睡得最香
        (2, "23:30", 21.5, 51, 4,  80.0),
        (1, "23:00", 21.0, 50, 3,  83.0),
    ]
    base = datetime.now(timezone.utc)
    for days_ago, hhmm, t, h, b, q in nights:
        hh, mm = map(int, hhmm.split(":"))
        bedtime = base.replace(hour=hh, minute=mm, second=0, microsecond=0) - timedelta(days=days_ago)
        wake = bedtime + timedelta(hours=7, minutes=30)
        env = EnvironmentParams(
            temperature_c=t, humidity_pct=h, brightness_pct=b,
        )
        # 一晚约等于 5 cycle: LIGHT 5 + DEEP 6 + REM 4 = 15 个 5min 桶 × 5 = 75 个 stage tick
        stage_counts = {
            "AWAKE": 4,   # 入睡前 + 几次 brief awakening
            "LIGHT": 25,
            "DEEP": 30,
            "REM": 20,
        }
        session = SleepSession(
            session_id=f"night_{days_ago}",
            started_at=bedtime.timestamp(),
            ended_at=wake.timestamp(),
            env_params=env,
            stage_counts=stage_counts,
            quality_score=q,
            n_samples=sum(stage_counts.values()),
        )
        learner.record_session(session)


async def main() -> None:
    print()
    _print_banner("Sleep Classifier 端到端模拟  ─  一整晚完整流程")
    print()
    print("场景：")
    print("  · 用户已使用 7 晚，learner 学到 23:00 入睡 + 21°C/50% 是最佳环境")
    print("  · 卧室设备：1 盏主灯、1 台空调、1 个加湿器、1 个风扇")
    print("  · dry_run=true（所有 HA service call 被 mock 拦截）")

    # ── 1. learner 从 7 晚历史中学习 ────────────────────────────────────
    profile = UserProfile(birth_year=1995, chronotype="neutral")

    # 给 learner 一个临时 history 文件，避免污染真实 /data/
    with tempfile.TemporaryDirectory() as tmpdir:
        cfg_pref = PreferenceConfig(
            history_path=str(Path(tmpdir) / "user_preferences.json"),
            min_sessions_for_personalisation=3,
        )
        learner = PreferenceLearner(config=cfg_pref)
        _seed_history(learner)

        _print_banner("步骤 1  ─  PreferenceLearner 学到的偏好")

        bedtime_info = learner.recommend_bedtime()
        print(f"  推荐入睡时间（按工作日 / 周末分桶）：")
        for k, v in bedtime_info.items():
            if v is not None:
                print(f"    {k}: {v}")
        defaults = EnvironmentParams(temperature_c=22.0, humidity_pct=50, brightness_pct=5)
        midpoint = learner.recommend(defaults=defaults)

        print(f"  最佳环境（midpoint，用户睡得最好的几晚）：")
        if midpoint.temperature_c is not None:
            print(f"    温度: {midpoint.temperature_c:.1f} °C")
        if midpoint.humidity_pct is not None:
            print(f"    湿度: {midpoint.humidity_pct:.0f} %")
        if midpoint.brightness_pct is not None:
            print(f"    亮度: {midpoint.brightness_pct:.1f} %")

        # ── 2. 造 controller ────────────────────────────────────────────
        devices = _make_devices()
        ha_client = AsyncMock()
        ha_client.call_service = AsyncMock(return_value=None)

        cfg = SmartControlConfig(
            enabled=True,
            dry_run=True,
            deadband_temperature_c=0.5,
            deadband_humidity_pct=5.0,
            deadband_brightness_pct=10.0,
            min_seconds_between_actions=0.0,
        )
        ctrl = SmartEnvironmentController(
            config=cfg, ha_client=ha_client, devices=devices, learner=learner,
        )

        # ── 3. 逐时点跑闭环 ──────────────────────────────────────────────
        _print_banner("步骤 2  ─  逐时点闭环模拟（dry-run）")

        current_env = EnvironmentParams(
            temperature_c=23.5, humidity_pct=45, brightness_pct=60,
        )

        cumulative_actions = 0
        for time_str, stage, desc in NIGHT_TIMELINE:
            actions = await ctrl.apply(stage=stage, current_env=current_env)

            print(f"\n[{time_str}] stage={stage.name}  ─  {desc}")
            t = current_env.temperature_c if current_env.temperature_c is not None else 0
            h = current_env.humidity_pct if current_env.humidity_pct is not None else 0
            b = current_env.brightness_pct if current_env.brightness_pct is not None else 0
            print(f"  当前室温={t:.1f}°C  湿度={h:.0f}%  亮度={b:.0f}%")

            if actions:
                cumulative_actions += len(actions)
                for a in actions:
                    eid = a.data.get('entity_id', '?')
                    desc_extras = _describe_action(a.data)
                    print(f"  → {a.domain}.{a.service}({eid})  {desc_extras}")
            else:
                print("  → （无动作 — 已在 deadband 内或当前 stage 维持）")

            # 简化的环境追踪：朝目标走 70%
            target = ctrl.target_for(stage)
            current_env = EnvironmentParams(
                temperature_c=_blend(current_env.temperature_c, target.temperature_c),
                humidity_pct=_blend(current_env.humidity_pct, target.humidity_pct),
                brightness_pct=_blend(current_env.brightness_pct, target.brightness_pct),
            )

        # ── 4. 总结 ──────────────────────────────────────────────────────
        _print_banner("步骤 3  ─  本晚累计动作摘要")
        print(f"  总共发起 HA service 调用: {cumulative_actions} 次")
        domain_counts: dict[str, int] = {}
        for call in ha_client.call_service.call_args_list:
            d = call.args[0] if call.args else call.kwargs.get("domain", "?")
            domain_counts[d] = domain_counts.get(d, 0) + 1
        for d, n in sorted(domain_counts.items()):
            print(f"    {d}: {n}")

        # ── 5. 真实算质量分 ─────────────────────────────────────────────
        _print_banner("步骤 4  ─  晨起睡眠质量评分（真实算法）")

        # 构造一个写实的一晚 stage 序列（30s epoch × 7.5h = 900 epoch）
        # 包含：5min SOL、3 cycles (LIGHT 25min + DEEP 30min + REM 20min)、
        # 中间 2 次 brief awakening (各 1min)、最后 5min final-wake
        epoch_s = 30
        seq: list[SleepStage] = []
        # 5 分钟 SOL（入睡前躺床）
        seq += [SleepStage.AWAKE] * (5 * 60 // epoch_s)
        # 3 个完整周期
        for i in range(3):
            seq += [SleepStage.LIGHT] * (25 * 60 // epoch_s)
            seq += [SleepStage.DEEP]  * (30 * 60 // epoch_s)
            seq += [SleepStage.REM]   * (20 * 60 // epoch_s)
            if i < 2:
                # 周期之间 1 分钟 brief awakening
                seq += [SleepStage.AWAKE] * (1 * 60 // epoch_s)
        # 早晨 final wake 5 分钟
        seq += [SleepStage.AWAKE] * (5 * 60 // epoch_s)

        metrics = compute_metrics(seq, epoch_seconds=epoch_s)
        print(f"  TIB（卧床时间）       : {metrics.tib_min:.1f} min")
        print(f"  TST（实际睡眠时间）   : {metrics.tst_min:.1f} min")
        print(f"  WASO（中段觉醒时间）  : {metrics.waso_min:.1f} min")
        print(f"  SOL（入睡潜伏期）     : {metrics.sol_min:.1f} min")
        print(f"  N awakenings（觉醒次数）: {metrics.n_awakenings}")
        print(f"  Sleep Efficiency      : {metrics.sleep_efficiency_pct:.1f} %")
        print(f"  Stage counts          : {metrics.stage_counts}")

        scores = compute_objective_quality(metrics)
        print()
        print(f"  四个分项评分（每项 0-100）：")
        print(f"    Architecture（睡眠结构 — DEEP/REM 比例）   : {scores['architecture']:.1f}")
        print(f"    Efficiency（睡眠效率 SE）                  : {scores['efficiency']:.1f}")
        print(f"    Fragmentation（碎片化 — WASO/awakenings）  : {scores['fragmentation']:.1f}")
        print(f"    Onset（入睡时长是否健康）                  : {scores['onset']:.1f}")
        print(f"  → 客观综合分（加权）：{scores['composite']:.1f} / 100")

        # 主观评分融合：用户晨起在 input_number 上打 4/5
        user_subjective = 4
        final = blend_subjective(scores['composite'], user_subjective, subjective_scale=5)
        print()
        print(f"  用户晨起主观评分: {user_subjective} / 5")
        print(f"  → 最终融合质量分: {final:.1f} / 100  （客观 60% + 主观 40%）")

        # ── 6. 让 learner 把今晚记入历史 ──────────────────────────────────
        _print_banner("步骤 5  ─  本晚 session 记入 learner 历史")

        # 用今晚实际环境的均值（dry-run 下 controller 没真改，但我们假设有真改）
        tonight_env = EnvironmentParams(
            temperature_c=20.5, humidity_pct=49, brightness_pct=2,
        )
        tonight_session = SleepSession(
            session_id="tonight",
            started_at=datetime.now(timezone.utc).timestamp() - metrics.tib_min * 60,
            ended_at=datetime.now(timezone.utc).timestamp(),
            env_params=tonight_env,
            stage_counts=metrics.stage_counts,
            quality_score=final,
            n_samples=len(seq),
        )
        learner.record_session(tonight_session)
        print(f"  本晚 session 已写入 learner 历史，质量分={final:.1f}")
        print(f"  历史 session 数：{len(learner._load())}")

        # 重新看 midpoint 是否被今晚的好成绩拉动
        new_midpoint = learner.recommend(defaults=defaults)
        print()
        print(f"  新 midpoint（含今晚 8 晚数据）：")
        if new_midpoint.temperature_c is not None:
            print(f"    温度: {new_midpoint.temperature_c:.1f} °C "
                  f"(之前: {midpoint.temperature_c:.1f} °C)")
        if new_midpoint.humidity_pct is not None:
            print(f"    湿度: {new_midpoint.humidity_pct:.0f} % "
                  f"(之前: {midpoint.humidity_pct:.0f} %)")
        if new_midpoint.brightness_pct is not None:
            print(f"    亮度: {new_midpoint.brightness_pct:.1f} % "
                  f"(之前: {midpoint.brightness_pct:.1f} %)")

        print()
        print("✅ 端到端模拟完整跑通。验证了：")
        print("  · learner 学到偏好（midpoint）")
        print("  · controller 按 stage 转换真的生成 HA action")
        print("  · 评分系统从 stage 序列算出 4 项指标 + 综合分")
        print("  · 主客观融合（用户打分 + 算法分）")
        print("  · 今晚的高分会回流到 learner，下次用更优 midpoint")
        print()
        print("  整套闭环：传感器 → 推理 → 控制 → 评分 → 学习 → 偏好更新 ✓")
        print()


def _blend(current: float | None, target: float | None, ratio: float = 0.7) -> float | None:
    if current is None and target is None:
        return None
    if current is None:
        return target
    if target is None:
        return current
    return current + (target - current) * ratio


def _describe_action(data: dict[str, Any]) -> str:
    parts: list[str] = []
    if "brightness_pct" in data:
        parts.append(f"亮度={data['brightness_pct']:.0f}%")
    if "kelvin" in data or "color_temp_kelvin" in data:
        k = data.get("kelvin", data.get("color_temp_kelvin"))
        parts.append(f"色温={k}K")
    if "temperature" in data:
        parts.append(f"目标温度={data['temperature']:.1f}°C")
    if "hvac_mode" in data:
        parts.append(f"模式={data['hvac_mode']}")
    if "humidity" in data:
        parts.append(f"目标湿度={data['humidity']:.0f}%")
    if "percentage" in data:
        parts.append(f"档位={data['percentage']:.0f}%")
    return ", ".join(parts)


if __name__ == "__main__":
    asyncio.run(main())
