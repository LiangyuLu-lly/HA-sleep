"""Add-on 运行时配置加载器。

模块名依旧叫 ``training_config`` 是 v1.0.x 的历史包袱（当年需要
训练本地 CNN-BiLSTM 模型）；v1.3.0 起模型已经删掉，这里只负责
把 ``training_config/config.json`` 里的默认值与 Add-on
``/data/options.json`` 映射过来的字段合并成运行时 config。

两条合并路径：

1. **开发 / 手动部署**：:func:`load_config` 读 JSON 文件，失败兜底
   到 :func:`get_default_config`。
2. **Add-on**：``sleep_classifier/run.sh`` 先把 Configuration 表单
   + Web UI 选择结果合成 ``/data/effective_config.json``，再传给
   ``scripts/run_ha_smart_service.py --config``。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """配置结构或取值不合法时抛出。"""


def get_default_config() -> Dict[str, Any]:
    """返回内置默认配置。

    结构刻意压平——只有 ``home_assistant`` 一个顶层键，匹配
    :class:`src.smart_environment_controller.SmartControlConfig` /
    :class:`src.preference_learner.PreferenceConfig` /
    :class:`src.device_discovery.DiscoveryConfig` 的 ``from_dict``
    接受的形状。v1.3.0 之前这里还塞着 ``model`` / ``mqtt`` /
    ``training`` / ``disaster_monitoring``，全部已经是死键。
    """
    return {
        "home_assistant": {
            "api": {
                "base_url": "http://homeassistant.local:8123",
                "access_token": "",
                "verify_ssl": True,
                "area_filter": "bedroom",
                "controllable_domains": [
                    "light", "climate", "fan", "humidifier",
                    "switch", "media_player",
                ],
                # 保留双语关键词默认值——DiscoveryConfig.from_dict
                # 只在字段存在时覆写，缺失就退回模块常量。
                "heart_rate_keywords": [
                    "heart_rate", "hr", "heartrate", "pulse", "心率",
                ],
                "movement_keywords": [
                    "movement", "motion", "activity", "accel", "运动",
                ],
                "temperature_keywords": [
                    "temperature", "temp", "气温", "室温",
                ],
                "humidity_keywords": ["humidity", "湿度"],
                "illuminance_keywords": [
                    "illuminance", "lux", "光照", "亮度",
                ],
                "sleep_stage_source": "",
            },
            "preference_learner": {
                "enabled": True,
                "history_path": "data/user_preferences.json",
                "min_sessions_for_personalisation": 3,
                "quality_quantile": 0.7,
                "max_sessions_kept": 60,
                "exploration_rate": 0.1,
            },
            "smart_control": {
                "enabled": True,
                "min_seconds_between_actions": 120,
                "deadband_temperature_c": 0.5,
                "deadband_humidity_pct": 5,
                "deadband_brightness_pct": 10,
                "dry_run": True,
                "wind_down_minutes": 30,
                "min_stage_dwell_seconds": 60,
            },
            "natural_sleep": {
                "user_id": "default",
                "chronotype": "neutral",
            },
        },
    }


def validate_config(config: Dict[str, Any]) -> None:
    """对配置做最小化结构校验，不合规抛 :class:`ConfigurationError`。

    v1.3.0 之前这里校验 CNN 滤波器数量、MQTT 端口、disaster 阈值
    等等——全部已经不存在于运行时。现在只保留四条能直接坑到
    Add-on 启动流程的硬约束：

    * 必须有 ``home_assistant`` 段；
    * ``smart_control`` 的 deadband 不能是负数；
    * ``preference_learner.quality_quantile`` 必须在 ``[0, 1]``；
    * ``preference_learner.min_sessions_for_personalisation`` 必须 ≥ 1。

    其它字段交给各自 dataclass 的 ``from_dict`` 用 ``valid = {f for f
    in dataclass_fields}`` 静默过滤——未知键不会让服务起不来。
    """
    if "home_assistant" not in config:
        raise ConfigurationError(
            "配置缺少 home_assistant 段；请从 get_default_config() "
            "起步或重新运行 sleep_classifier/run.sh",
        )

    sc = config.get("home_assistant", {}).get("smart_control", {})
    for key in (
        "deadband_temperature_c",
        "deadband_humidity_pct",
        "deadband_brightness_pct",
        "min_seconds_between_actions",
    ):
        if key in sc and float(sc[key]) < 0:
            raise ConfigurationError(f"smart_control.{key} 必须 ≥ 0")

    pl = config.get("home_assistant", {}).get("preference_learner", {})
    if "quality_quantile" in pl:
        q = float(pl["quality_quantile"])
        if not 0.0 <= q <= 1.0:
            raise ConfigurationError(
                "preference_learner.quality_quantile 必须在 [0, 1]",
            )
    if "min_sessions_for_personalisation" in pl:
        n = int(pl["min_sessions_for_personalisation"])
        if n < 1:
            raise ConfigurationError(
                "preference_learner.min_sessions_for_personalisation 必须 ≥ 1",
            )


def load_config(config_path: str = "training_config/config.json") -> Dict[str, Any]:
    """从 JSON 文件加载配置；文件缺失 / 解析失败 / 校验失败都兜底到默认。

    设计取舍：宁可退回默认也不要让 Add-on 卡在启动阶段——坏
    配置在日志里是可见的，默认 ``dry_run=True`` 保证不会误操作
    用户设备。
    """
    try:
        if not os.path.exists(config_path):
            logger.warning(
                "配置文件 %s 不存在，使用默认配置。", config_path,
            )
            return get_default_config()

        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        validate_config(config)
        logger.info("已从 %s 载入配置。", config_path)
        return config

    except json.JSONDecodeError as exc:
        logger.error("配置文件 JSON 解析失败，使用默认配置：%s", exc)
        return get_default_config()

    except ConfigurationError as exc:
        logger.error("配置校验未通过，使用默认配置：%s", exc)
        return get_default_config()

    except Exception as exc:    # noqa: BLE001
        logger.error("配置加载发生未预期错误，使用默认配置：%s", exc)
        return get_default_config()
