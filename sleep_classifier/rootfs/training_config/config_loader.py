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

v3.0.0 在 ``home_assistant.v3`` 子树下新增 4 个算法 flag + 1 个
``causal_attribution_explain_all`` + 3 个用户画像字段（task 6.2）。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Mapping, Union

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """配置结构或取值不合法时抛出。"""


# ---------------------------------------------------------------------------
# v3.0.0 algorithmic moat — 默认值与合法枚举（task 6.2 / PR6）
# ---------------------------------------------------------------------------

#: 4 个算法模块的总开关；缺失时全部默认 True。
_V3_FLAG_DEFAULTS: Dict[str, bool] = {
    "bayesian_optimizer_enabled": True,
    "causal_attribution_enabled": True,
    "population_prior_enabled": True,
    "stage_predictor_enabled": True,
}

#: ``causal_attribution`` attribute 是否暴露全部 6 个因子；默认 False。
_V3_BOOL_DEFAULTS: Dict[str, bool] = {
    "causal_attribution_explain_all": False,
}

#: 用户画像字段；默认空字符串，下游解读为 unspecified / neutral（R8.2）。
_V3_PROFILE_FIELDS: tuple[str, ...] = (
    "user_profile_age_band",
    "user_profile_sex",
    "user_profile_chronotype",
)

#: 用户画像合法枚举（含 ``""`` 表示未指定）。
#: 与 ``sleep_classifier/config.yaml`` 的 schema regex 严格一致。
_V3_PROFILE_ENUMS: Dict[str, frozenset[str]] = {
    "user_profile_age_band": frozenset({
        "", "18-25", "26-35", "36-50", "51-65", "65+",
    }),
    "user_profile_sex": frozenset({"", "M", "F", "unspecified"}),
    "user_profile_chronotype": frozenset({"", "morning", "evening", "neutral"}),
}


def _v3_section_defaults() -> Dict[str, Any]:
    """返回 ``home_assistant.v3`` 子树的全套默认值。"""
    out: Dict[str, Any] = {}
    out.update(_V3_FLAG_DEFAULTS)
    out.update(_V3_BOOL_DEFAULTS)
    for k in _V3_PROFILE_FIELDS:
        out[k] = ""
    return out


def get_default_config() -> Dict[str, Any]:
    """返回内置默认配置。

    结构刻意压平——只有 ``home_assistant`` 一个顶层键，匹配
    :class:`src.smart_environment_controller.SmartControlConfig` /
    :class:`src.preference_learner.PreferenceConfig` /
    :class:`src.device_discovery.DiscoveryConfig` 的 ``from_dict``
    接受的形状。v1.3.0 之前这里还塞着 ``model`` / ``mqtt`` /
    ``training`` / ``disaster_monitoring``，全部已经是死键。

    v3.0.0 起在 ``home_assistant.v3`` 子树下追加 8 个新字段（task 6.2）。
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
            "v3": _v3_section_defaults(),
        },
    }


def _apply_v3_defaults(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """把 v3.0.0 字段并入 ``cfg['home_assistant']['v3']``，原地修改并返回。

    行为契约（task 6.2 / PR6）：

    * 缺失字段 → 回退默认值；累计后**一次性**打 INFO 日志（不 WARN，
      避免老用户从 v2.1.0 升级时刷屏）。
    * ``user_profile_*`` 值不在合法枚举内 → 回退默认（``""``） + INFO
      日志；非法值不会让校验失败，因为用户画像本身就是「可选 +
      容错」语义。
    * ``home_assistant`` 段不存在时跳过（让 :func:`validate_config`
      去抛 :class:`ConfigurationError`，下游会兜底默认值）。
    * 已有合法值原封保留（empty string 视为合法值，下游自行解读为
      unspecified / neutral，不在这里翻译）。
    """
    ha = cfg.get("home_assistant")
    if not isinstance(ha, dict):
        return cfg

    v3 = ha.get("v3")
    if not isinstance(v3, dict):
        v3 = {}
        ha["v3"] = v3

    defaults = _v3_section_defaults()
    missing_keys: list[str] = []
    invalid_profile: list[tuple[str, Any]] = []

    for key, default in defaults.items():
        if key not in v3:
            v3[key] = default
            missing_keys.append(key)
            continue

        # 用户画像字段做枚举校验；非法值回退默认 + 记录。
        if key in _V3_PROFILE_FIELDS:
            allowed = _V3_PROFILE_ENUMS[key]
            value = v3[key]
            if not isinstance(value, str) or value not in allowed:
                invalid_profile.append((key, value))
                v3[key] = default

    if missing_keys:
        # 一行 INFO，列出缺失字段；老用户升级路径上不刷屏。
        logger.info(
            "v3.0.0 字段缺失，已应用默认值: %s",
            ", ".join(sorted(missing_keys)),
        )

    if invalid_profile:
        # 同样一行 INFO；列出 (key, 实际值) 便于排障。
        details = ", ".join(
            f"{k}={v!r}" for k, v in sorted(invalid_profile, key=lambda x: x[0])
        )
        logger.info(
            "v3.0.0 user_profile_* 非法取值，已回退默认: %s",
            details,
        )

    return cfg


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

    v3.0.0 字段（``home_assistant.v3.*``）**不**在这里硬校验：缺失
    或非法值都由 :func:`_apply_v3_defaults` 在 :func:`load_config`
    流程内静默兜底（PR6 老 config 升级兼容）。
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


def load_config(
    config_path: Union[str, "os.PathLike[str]", Mapping[str, Any]] = (
        "training_config/config.json"
    ),
) -> Dict[str, Any]:
    """从 JSON 文件加载配置；文件缺失 / 解析失败 / 校验失败都兜底到默认。

    设计取舍：宁可退回默认也不要让 Add-on 卡在启动阶段——坏
    配置在日志里是可见的，默认 ``dry_run=True`` 保证不会误操作
    用户设备。

    v3.0.0 起额外接受一个**已加载好的字典**作为入参——单测和
    `python -c "from training_config.config_loader import load_config;
    print(load_config({}))"` 这种 smoke 校验都靠这个分支：传 ``{}``
    会触发缺 home_assistant 校验失败，回退到包含全套 v3 默认值
    的默认配置。
    """
    # 1) 已经是 dict（或 Mapping）：直接走校验 + v3 默认值兜底。
    if isinstance(config_path, Mapping):
        cfg: Dict[str, Any] = dict(config_path)
        try:
            validate_config(cfg)
            return _apply_v3_defaults(cfg)
        except ConfigurationError as exc:
            logger.error("配置校验未通过，使用默认配置：%s", exc)
            return _apply_v3_defaults(get_default_config())

    # 2) 路径分支：原有行为（兼容 str / PathLike）。
    path_str = str(config_path)
    try:
        if not os.path.exists(path_str):
            logger.warning(
                "配置文件 %s 不存在，使用默认配置。", path_str,
            )
            return _apply_v3_defaults(get_default_config())

        with open(path_str, "r", encoding="utf-8") as f:
            config = json.load(f)

        validate_config(config)
        logger.info("已从 %s 载入配置。", path_str)
        return _apply_v3_defaults(config)

    except json.JSONDecodeError as exc:
        logger.error("配置文件 JSON 解析失败，使用默认配置：%s", exc)
        return _apply_v3_defaults(get_default_config())

    except ConfigurationError as exc:
        logger.error("配置校验未通过，使用默认配置：%s", exc)
        return _apply_v3_defaults(get_default_config())

    except Exception as exc:    # noqa: BLE001
        logger.error("配置加载发生未预期错误，使用默认配置：%s", exc)
        return _apply_v3_defaults(get_default_config())
