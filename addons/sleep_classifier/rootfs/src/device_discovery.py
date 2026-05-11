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

Matching strategy
-----------------
Vendors name their entities very differently — ``sensor.mi_band_5_heart_rate``
vs ``sensor.xiaomi_mi_band_hr`` vs ``sensor.zepp_pulse``.  We therefore use a
**triple-key match**:

* substring match against keyword lists (configurable per category),
* ``device_class`` attribute (HA standardised: ``temperature``, ``humidity``,
  ``illuminance``),
* ``unit_of_measurement`` (``bpm``, ``°C``, ``%``, ``lx``).

A sensor is accepted if **any** of the three matches.  An optional area
filter (``area_filter="bedroom"``) narrows discovery to one room.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from src.ha_api_client import HAEntity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class SensorSources:
    """Entities the service reads sensor data from."""

    heart_rate: List[HAEntity] = field(default_factory=list)
    movement: List[HAEntity] = field(default_factory=list)
    temperature: List[HAEntity] = field(default_factory=list)
    humidity: List[HAEntity] = field(default_factory=list)
    illuminance: List[HAEntity] = field(default_factory=list)

    def as_summary(self) -> Dict[str, List[str]]:
        return {
            "heart_rate":  [e.entity_id for e in self.heart_rate],
            "movement":    [e.entity_id for e in self.movement],
            "temperature": [e.entity_id for e in self.temperature],
            "humidity":    [e.entity_id for e in self.humidity],
            "illuminance": [e.entity_id for e in self.illuminance],
        }

    def all_subscribed_entity_ids(self) -> List[str]:
        """Flat de-duplicated list of every entity_id we want WS updates for."""
        seen = set()
        ordered: List[str] = []
        for bucket in (
            self.heart_rate, self.movement,
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
        """Service can still run with only HR or only movement."""
        return bool(self.sensors.heart_rate or self.sensors.movement)

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


@dataclass
class DiscoveryConfig:
    """Keyword lists + filters used to classify HA entities."""

    heart_rate_keywords: Sequence[str] = ("heart_rate", "hr", "heartrate", "pulse")
    movement_keywords: Sequence[str] = ("movement", "motion", "activity", "accel")
    temperature_keywords: Sequence[str] = ("temperature", "temp")
    humidity_keywords: Sequence[str] = ("humidity",)
    illuminance_keywords: Sequence[str] = ("illuminance", "lux", "light_level")
    controllable_domains: Sequence[str] = (
        "light", "climate", "fan", "humidifier", "switch", "media_player",
    )
    area_filter: Optional[str] = None
    explicit_includes: Sequence[str] = ()   # always include these entity_ids
    explicit_excludes: Sequence[str] = ()   # never include these entity_ids

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
        return _device_class_in(entity, ("motion", "occupancy"))

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

    def discover(self, entities: Iterable[HAEntity]) -> DiscoveryResult:
        """Run classification on a snapshot of HA entities.

        Args:
            entities: typically ``await ha_client.get_states()``.

        Returns:
            :class:`DiscoveryResult` with sensors and devices populated.
        """
        sensors = SensorSources()
        devices = ActionableDevices()

        explicit_includes = set(self.config.explicit_includes)
        explicit_excludes = set(self.config.explicit_excludes)
        area = self.config.area_filter

        for entity in entities:
            eid = entity.entity_id

            if eid in explicit_excludes:
                continue

            allowed_by_area = (
                eid in explicit_includes
                or _passes_area_filter(entity, area)
            )

            # ---- Sensors -------------------------------------------------
            if allowed_by_area:
                if self._is_heart_rate(entity):
                    sensors.heart_rate.append(entity)
                if self._is_movement(entity):
                    sensors.movement.append(entity)
                if self._is_temperature(entity):
                    sensors.temperature.append(entity)
                if self._is_humidity(entity):
                    sensors.humidity.append(entity)
                if self._is_illuminance(entity):
                    sensors.illuminance.append(entity)

            # ---- Actionable devices -------------------------------------
            # Devices generally pass area filter the same way as sensors.
            if allowed_by_area and self._is_actionable(entity):
                if entity.domain == "light":
                    devices.lights.append(entity)
                elif entity.domain == "climate":
                    devices.climates.append(entity)
                elif entity.domain == "fan":
                    devices.fans.append(entity)
                elif entity.domain == "humidifier":
                    devices.humidifiers.append(entity)
                elif entity.domain == "switch":
                    devices.switches.append(entity)
                elif entity.domain == "media_player":
                    devices.media_players.append(entity)

        return DiscoveryResult(sensors=sensors, devices=devices)


__all__ = [
    "DiscoveryConfig",
    "DiscoveryResult",
    "SensorSources",
    "ActionableDevices",
    "DeviceDiscovery",
]
