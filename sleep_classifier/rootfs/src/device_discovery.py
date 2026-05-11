"""Auto-detect physiological sensors and controllable devices from HA.

Given a snapshot of all entities exposed by Home Assistant
(``HomeAssistantClient.get_states()``), this module classifies them into two
buckets:

1. **Sensor sources** — what we read *from*:

   - heart-rate (Mi Band, Garmin, ECG belts, mmWave radar, ...)
   - movement / activity (accelerometers, mmWave, bed sensors)
   - environment context (temperature, humidity, illuminance)

2. **Actionable devices** — what we write *to*:

   - lights (``light.*``)
   - HVAC / climate (``climate.*``)
   - fans (``fan.*``)
   - humidifiers / dehumidifiers (``humidifier.*``)
   - switches (``switch.*``) and media players (``media_player.*``)

Resolution order (highest priority wins)
----------------------------------------
#. **Slot bindings** — the user explicitly maps an ``entity_id`` to a slot in
   the add-on Configuration UI (e.g. ``heart_rate_source: sensor.mi_band_hr``).
   When a slot has at least one binding, *no* keyword scan runs for that
   bucket; the user's choice is final.
#. **Keyword + device_class + unit match** — vendors name entities very
   differently (``sensor.mi_band_5_heart_rate`` vs ``sensor.xiaomi_pulse``
   vs Chinese ``sensor.shoukuan_xinlu``), so for each remaining unfilled
   bucket we use a **triple-key match**:

   * substring match against keyword lists (English + Chinese defaults),
   * ``device_class`` attribute (HA standardised: ``temperature``,
     ``humidity``, ``illuminance``, ``motion``),
   * ``unit_of_measurement`` (``bpm``, ``°C``, ``%``, ``lx``).

   A sensor is accepted if **any** of the three matches.
#. **Area filter** — when ``area_filter`` is set we *prefer* entities whose
   ``area_id`` matches; if zero candidates pass that filter for a critical
   bucket (heart_rate or movement), the discovery falls back to scanning
   *all* HA entities.  This avoids the common pitfall where the user has
   not yet assigned their sensors to an HA area.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

from src.ha_api_client import HAEntity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class SensorSources:
    """Entities the service reads sensor data from.

    ``breathing`` is a non-physiological-HR alternative often produced by
    mmWave sleep radars (e.g. R60ABD1 ``TS6 呼吸信息``).  The inference
    engine treats it as a **fallback** for ``heart_rate`` when the latter
    is empty — the model was trained on HR but the input is a 1-channel
    physiological signal so a respiratory-rate trace correlates well
    enough for a usable prediction.
    """

    heart_rate: List[HAEntity] = field(default_factory=list)
    movement: List[HAEntity] = field(default_factory=list)
    breathing: List[HAEntity] = field(default_factory=list)
    temperature: List[HAEntity] = field(default_factory=list)
    humidity: List[HAEntity] = field(default_factory=list)
    illuminance: List[HAEntity] = field(default_factory=list)

    def as_summary(self) -> Dict[str, List[str]]:
        return {
            "heart_rate":  [e.entity_id for e in self.heart_rate],
            "movement":    [e.entity_id for e in self.movement],
            "breathing":   [e.entity_id for e in self.breathing],
            "temperature": [e.entity_id for e in self.temperature],
            "humidity":    [e.entity_id for e in self.humidity],
            "illuminance": [e.entity_id for e in self.illuminance],
        }

    def all_subscribed_entity_ids(self) -> List[str]:
        """Flat de-duplicated list of every entity_id we want WS updates for."""
        seen = set()
        ordered: List[str] = []
        for bucket in (
            self.heart_rate, self.movement, self.breathing,
            self.temperature, self.humidity, self.illuminance,
        ):
            for e in bucket:
                if e.entity_id not in seen:
                    seen.add(e.entity_id)
                    ordered.append(e.entity_id)
        return ordered


@dataclass
class ActionableDevices:
    """Entities the service can issue commands to."""

    lights: List[HAEntity] = field(default_factory=list)
    climates: List[HAEntity] = field(default_factory=list)
    fans: List[HAEntity] = field(default_factory=list)
    humidifiers: List[HAEntity] = field(default_factory=list)
    switches: List[HAEntity] = field(default_factory=list)
    media_players: List[HAEntity] = field(default_factory=list)

    def as_summary(self) -> Dict[str, List[str]]:
        return {
            "lights":        [e.entity_id for e in self.lights],
            "climates":      [e.entity_id for e in self.climates],
            "fans":          [e.entity_id for e in self.fans],
            "humidifiers":   [e.entity_id for e in self.humidifiers],
            "switches":      [e.entity_id for e in self.switches],
            "media_players": [e.entity_id for e in self.media_players],
        }


@dataclass
class DiscoveryResult:
    sensors: SensorSources
    devices: ActionableDevices

    def has_minimum_sensors(self) -> bool:
        """Service can still run with only HR (or breathing fallback) or movement."""
        return bool(
            self.sensors.heart_rate
            or self.sensors.movement
            or self.sensors.breathing
        )

    def log_summary(self) -> None:
        logger.info("=" * 60)
        logger.info("Device discovery — sensor sources")
        for key, ids in self.sensors.as_summary().items():
            logger.info("  %-12s → %d entities: %s", key, len(ids), ids or "—")
        logger.info("Device discovery — actionable devices")
        for key, ids in self.devices.as_summary().items():
            logger.info("  %-13s → %d entities: %s", key, len(ids), ids or "—")
        logger.info("=" * 60)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


# Default bilingual keyword lists.  Match logic is case-insensitive substring
# search against both ``entity_id`` and ``friendly_name``, so ESPHome-generated
# IDs like ``sensor.sleepradar_r60abd1_ts5_yundong_zhuangtai`` will hit on
# ``yundong`` (Chinese pinyin) while Mi Band's ``sensor.mi_band_5_heart_rate``
# hits on ``heart_rate``; their Chinese ``friendly_name`` ("运动状态",
# "心率") additionally hits on the Chinese keywords below.
DEFAULT_HEART_RATE_KEYWORDS = (
    # English
    "heart_rate", "heartrate", "hr", "pulse", "bpm", "ecg", "ppg",
    # Chinese (matched against friendly_name)
    "心率", "脉搏", "心跳",
    # Chinese pinyin (sometimes appears in entity_id)
    "xinlu", "xinlv", "maibo",
)
DEFAULT_MOVEMENT_KEYWORDS = (
    # English
    "movement", "motion", "activity", "accel", "presence", "occupancy", "body",
    # Chinese
    "运动", "体动", "身体", "人体", "动作", "存在", "移动",
    # Pinyin
    "yundong", "tidong", "renti",
)
DEFAULT_BREATHING_KEYWORDS = (
    # English
    "breathing", "breath", "respiration", "respiratory", "rr",
    # Chinese
    "呼吸", "呼气", "呼气频率",
    # Pinyin
    "huxi",
)
DEFAULT_TEMPERATURE_KEYWORDS = (
    "temperature", "temp", "温度", "wendu",
)
DEFAULT_HUMIDITY_KEYWORDS = (
    "humidity", "humid", "湿度", "shidu",
)
DEFAULT_ILLUMINANCE_KEYWORDS = (
    "illuminance", "lux", "light_level", "brightness", "光照", "亮度", "照度",
    "guangzhao",
)


@dataclass
class DiscoveryConfig:
    """Keyword lists + filters used to classify HA entities.

    ``slot_bindings`` lets the user pin specific ``entity_id``s to specific
    sensor / device slots from the add-on Configuration UI.  When a slot has
    at least one entry, the keyword scan is **skipped** for that bucket; the
    user's choice is treated as authoritative.  Recognised keys:

    * sensors: ``heart_rate`` / ``movement`` / ``breathing`` / ``temperature``
      / ``humidity`` / ``illuminance``
    * devices: ``lights`` / ``climates`` / ``humidifiers`` / ``fans`` /
      ``switches`` / ``media_players``

    Unknown keys are silently ignored.
    """

    heart_rate_keywords: Sequence[str] = DEFAULT_HEART_RATE_KEYWORDS
    movement_keywords: Sequence[str] = DEFAULT_MOVEMENT_KEYWORDS
    breathing_keywords: Sequence[str] = DEFAULT_BREATHING_KEYWORDS
    temperature_keywords: Sequence[str] = DEFAULT_TEMPERATURE_KEYWORDS
    humidity_keywords: Sequence[str] = DEFAULT_HUMIDITY_KEYWORDS
    illuminance_keywords: Sequence[str] = DEFAULT_ILLUMINANCE_KEYWORDS
    controllable_domains: Sequence[str] = (
        "light", "climate", "fan", "humidifier", "switch", "media_player",
    )
    area_filter: Optional[str] = None
    explicit_includes: Sequence[str] = ()   # always include these entity_ids
    explicit_excludes: Sequence[str] = ()   # never include these entity_ids
    slot_bindings: Mapping[str, Sequence[str]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Dict) -> "DiscoveryConfig":
        valid = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in valid})


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------


def _matches_any_keyword(entity: HAEntity, keywords: Iterable[str]) -> bool:
    """Substring match against entity_id and friendly_name (case-insensitive)."""
    lower_id = entity.entity_id.lower()
    lower_name = entity.friendly_name.lower()
    for kw in keywords:
        kw_l = kw.lower()
        if kw_l in lower_id or kw_l in lower_name:
            return True
    return False


def _device_class_in(entity: HAEntity, expected: Iterable[str]) -> bool:
    dc = (entity.device_class or "").lower()
    return any(dc == x for x in expected)


def _unit_in(entity: HAEntity, expected: Iterable[str]) -> bool:
    unit = (entity.unit_of_measurement or "").lower().strip()
    return any(unit == x.lower() for x in expected)


def _passes_area_filter(entity: HAEntity, area_filter: Optional[str]) -> bool:
    """Accept entity if no filter, otherwise require the filter substring.

    HA does not always expose ``area_id`` on the entity itself (you need the
    device registry for that), so we also accept matches on entity_id /
    friendly_name as a heuristic fallback.
    """
    if not area_filter:
        return True
    needle = area_filter.lower()
    area = (entity.area or "").lower()
    if needle in area:
        return True
    # Fallback: many users embed the room name in the entity id.
    return needle in entity.entity_id.lower() or needle in entity.friendly_name.lower()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class DeviceDiscovery:
    """Classify a snapshot of HA entities into sensors + actionable devices."""

    # Device-class names HA uses for environment sensors.
    _DC_TEMP = ("temperature",)
    _DC_HUM = ("humidity",)
    _DC_ILLU = ("illuminance",)

    # Common units; HA may emit either degree sign so we accept both.
    _UNIT_TEMP = ("°c", "°f", "c", "f")
    _UNIT_HUM = ("%",)
    _UNIT_ILLU = ("lx", "lux")
    _UNIT_HR = ("bpm",)

    def __init__(self, config: DiscoveryConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------ #
    # Sensors                                                            #
    # ------------------------------------------------------------------ #

    def _is_heart_rate(self, entity: HAEntity) -> bool:
        if entity.domain != "sensor":
            return False
        if _matches_any_keyword(entity, self.config.heart_rate_keywords):
            return True
        # ``bpm`` unit is a strong signal
        return _unit_in(entity, self._UNIT_HR)

    def _is_movement(self, entity: HAEntity) -> bool:
        # ``binary_sensor.motion`` is also valid as a coarse activity signal.
        if entity.domain not in {"sensor", "binary_sensor"}:
            return False
        if _matches_any_keyword(entity, self.config.movement_keywords):
            return True
        return _device_class_in(entity, ("motion", "occupancy", "presence"))

    def _is_breathing(self, entity: HAEntity) -> bool:
        # Sleep-grade mmWave radars publish a respiration-rate sensor, e.g.
        # the R60ABD1 ``TS6 呼吸信息`` entity.  We treat this as a
        # *fallback* HR proxy when no real heart-rate source is available.
        if entity.domain != "sensor":
            return False
        return _matches_any_keyword(entity, self.config.breathing_keywords)

    def _is_temperature(self, entity: HAEntity) -> bool:
        if entity.domain != "sensor":
            return False
        if _device_class_in(entity, self._DC_TEMP):
            return True
        if _unit_in(entity, self._UNIT_TEMP):
            # Avoid false positives like outside temperature when an area
            # filter is set: caller already filters areas.
            return True
        return _matches_any_keyword(entity, self.config.temperature_keywords)

    def _is_humidity(self, entity: HAEntity) -> bool:
        if entity.domain != "sensor":
            return False
        if _device_class_in(entity, self._DC_HUM):
            return True
        if _matches_any_keyword(entity, self.config.humidity_keywords):
            return _unit_in(entity, self._UNIT_HUM) or True
        return False

    def _is_illuminance(self, entity: HAEntity) -> bool:
        if entity.domain != "sensor":
            return False
        if _device_class_in(entity, self._DC_ILLU):
            return True
        if _unit_in(entity, self._UNIT_ILLU):
            return True
        return _matches_any_keyword(entity, self.config.illuminance_keywords)

    # ------------------------------------------------------------------ #
    # Actionable devices                                                 #
    # ------------------------------------------------------------------ #

    def _is_actionable(self, entity: HAEntity) -> bool:
        return entity.domain in set(self.config.controllable_domains)

    # ------------------------------------------------------------------ #
    # Main entry point                                                   #
    # ------------------------------------------------------------------ #

    # Slot keys recognised in ``DiscoveryConfig.slot_bindings``.  The bucket
    # attribute name on ``SensorSources`` / ``ActionableDevices`` is the same
    # as the slot key, so we can index uniformly.
    _SENSOR_SLOTS = (
        "heart_rate", "movement", "breathing",
        "temperature", "humidity", "illuminance",
    )
    _DEVICE_SLOTS = (
        "lights", "climates", "fans",
        "humidifiers", "switches", "media_players",
    )

    def discover(self, entities: Iterable[HAEntity]) -> DiscoveryResult:
        """Run classification on a snapshot of HA entities.

        Resolution order:

        1. Apply user-supplied slot bindings (highest priority).  Buckets
           filled this way are *frozen* — the keyword scan will not add
           anything else to them.
        2. Run keyword + device_class + unit_of_measurement scan over the
           area-filtered entity set for any remaining unfilled bucket.
        3. If neither ``heart_rate`` *nor* ``movement`` *nor* ``breathing``
           ended up populated, run the keyword scan a second time over
           **all** entities (ignoring the area filter) so we never silently
           fail just because the user has not assigned their bedroom
           sensors to an HA area yet.

        Args:
            entities: typically ``await ha_client.get_states()``.

        Returns:
            :class:`DiscoveryResult` with sensors and devices populated.
        """
        all_entities = list(entities)
        sensors = SensorSources()
        devices = ActionableDevices()

        # ---- Stage 1: slot bindings (user explicit choices) -------------
        # ``frozen_slots`` records buckets where at least one user-supplied
        # entity_id was *actually resolved* against the live HA state list.
        # A binding to a non-existent entity_id should NOT freeze the bucket
        # — otherwise a typo in Configuration would silently lock the user
        # out of auto-discovery for that role.
        bindings = self.config.slot_bindings or {}
        by_id = {e.entity_id: e for e in all_entities}
        bound_ids: set[str] = set()
        frozen_slots: set[str] = set()
        for slot, eids in bindings.items():
            if not eids:
                continue
            bucket = self._slot_bucket(sensors, devices, slot)
            if bucket is None:
                logger.warning("Unknown slot binding %r — ignored", slot)
                continue
            for eid in eids:
                if not eid:
                    continue
                ent = by_id.get(eid)
                if ent is None:
                    logger.warning(
                        "Slot %s references %s which is not in HA’s state list — "
                        "check the entity_id in Configuration tab.", slot, eid,
                    )
                    continue
                bucket.append(ent)
                bound_ids.add(eid)
                frozen_slots.add(slot)

        # ---- Stage 2: keyword scan within area filter ------------------
        excluded = set(self.config.explicit_excludes)
        includes = set(self.config.explicit_includes)

        def _scan(in_scope: Iterable[HAEntity], area_override: Optional[str]) -> None:
            for entity in in_scope:
                eid = entity.entity_id
                if eid in excluded or eid in bound_ids:
                    continue
                if not (eid in includes or _passes_area_filter(entity, area_override)):
                    continue

                # Sensors — only fill buckets that aren't user-frozen
                if "heart_rate" not in frozen_slots and self._is_heart_rate(entity):
                    sensors.heart_rate.append(entity)
                if "movement" not in frozen_slots and self._is_movement(entity):
                    sensors.movement.append(entity)
                if "breathing" not in frozen_slots and self._is_breathing(entity):
                    sensors.breathing.append(entity)
                if "temperature" not in frozen_slots and self._is_temperature(entity):
                    sensors.temperature.append(entity)
                if "humidity" not in frozen_slots and self._is_humidity(entity):
                    sensors.humidity.append(entity)
                if "illuminance" not in frozen_slots and self._is_illuminance(entity):
                    sensors.illuminance.append(entity)

                # Actionable devices — same frozen-slot gate keyed by the
                # device-bucket name.
                if not self._is_actionable(entity):
                    continue
                if entity.domain == "light" and "lights" not in frozen_slots:
                    devices.lights.append(entity)
                elif entity.domain == "climate" and "climates" not in frozen_slots:
                    devices.climates.append(entity)
                elif entity.domain == "fan" and "fans" not in frozen_slots:
                    devices.fans.append(entity)
                elif entity.domain == "humidifier" and "humidifiers" not in frozen_slots:
                    devices.humidifiers.append(entity)
                elif entity.domain == "switch" and "switches" not in frozen_slots:
                    devices.switches.append(entity)
                elif entity.domain == "media_player" and "media_players" not in frozen_slots:
                    devices.media_players.append(entity)

        area = self.config.area_filter
        _scan(all_entities, area)

        # ---- Stage 3: global rescan if no critical sensor was found -----
        # Without HR/movement/breathing the inference loop has nothing to
        # consume, so as a last resort we ignore the area filter and try
        # again across the whole HA registry.  The user is then told via
        # the warning log to either set an area or use slot bindings.
        critical_filled = bool(
            sensors.heart_rate or sensors.movement or sensors.breathing
        )
        if area and not critical_filled and "heart_rate" not in frozen_slots:
            logger.warning(
                "No HR/movement/breathing sensor matched in area '%s'. "
                "Re-scanning all HA entities (ignoring area filter).", area,
            )
            _scan(all_entities, None)

        return DiscoveryResult(sensors=sensors, devices=devices)

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    def _slot_bucket(
        self,
        sensors: SensorSources,
        devices: ActionableDevices,
        slot: str,
    ) -> Optional[List[HAEntity]]:
        """Resolve a slot key to the actual list it should be appended to."""
        if slot in self._SENSOR_SLOTS:
            return getattr(sensors, slot)
        if slot in self._DEVICE_SLOTS:
            return getattr(devices, slot)
        return None

    @staticmethod
    def suggest_candidates(
        entities: Iterable[HAEntity],
        config: Optional["DiscoveryConfig"] = None,
        *,
        limit_per_bucket: int = 5,
    ) -> Dict[str, List[str]]:
        """Return per-bucket likely candidates regardless of area filter.

        Used by the smart service to print a helpful “did you mean…” list
        when discovery comes up empty so the user can copy the suggested
        ``entity_id``s into the slot fields in the add-on Configuration UI.
        """
        cfg = config or DiscoveryConfig()
        cfg = DiscoveryConfig(
            heart_rate_keywords=cfg.heart_rate_keywords,
            movement_keywords=cfg.movement_keywords,
            breathing_keywords=cfg.breathing_keywords,
            temperature_keywords=cfg.temperature_keywords,
            humidity_keywords=cfg.humidity_keywords,
            illuminance_keywords=cfg.illuminance_keywords,
            controllable_domains=cfg.controllable_domains,
            area_filter=None,                # ignore area when suggesting
        )
        d = DeviceDiscovery(cfg)
        result = d.discover(entities)
        sug: Dict[str, List[str]] = {}
        for k, ids in result.sensors.as_summary().items():
            sug[k] = ids[:limit_per_bucket]
        for k, ids in result.devices.as_summary().items():
            sug[k] = ids[:limit_per_bucket]
        return sug


__all__ = [
    "DiscoveryConfig",
    "DiscoveryResult",
    "SensorSources",
    "ActionableDevices",
    "DeviceDiscovery",
    "DEFAULT_HEART_RATE_KEYWORDS",
    "DEFAULT_MOVEMENT_KEYWORDS",
    "DEFAULT_BREATHING_KEYWORDS",
    "DEFAULT_TEMPERATURE_KEYWORDS",
    "DEFAULT_HUMIDITY_KEYWORDS",
    "DEFAULT_ILLUMINANCE_KEYWORDS",
]
