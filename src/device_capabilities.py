"""HA capability inspection — what can each entity *actually* do?

Why this module exists (v1.6.1)
-------------------------------

Before v1.6.1 the controller assumed that:

* every entity in the ``climate`` domain accepts ``set_temperature``,
* every entity in the ``light`` domain accepts ``brightness_pct`` +
  ``kelvin``,
* every entity in the ``fan`` domain accepts ``set_percentage``,
* every entity in the ``humidifier`` domain accepts ``set_humidity``.

In a real HA install **none of this is true**:

* A Mihome single-zone AC is exposed as ``climate.*`` but only
  supports ``hvac_modes`` switching — no ``set_temperature``.
* A Yeelight single-colour bulb is ``light.*`` but doesn't
  support ``color_temp_kelvin`` (only ``brightness``).
* A Sonoff iFan04 is ``fan.*`` but only ``preset_modes``
  (``low/medium/high``), no ``set_percentage``.
* A Tuya humidifier is ``humidifier.*`` but only on/off,
  no ``set_humidity``.

When the controller blindly fires the unsupported service HA returns
``200 OK`` (because the *service* exists, it just no-ops on this
entity), the user sees a green "Executed" line in the
`last_action` sensor — but the device never moves.  This is the
single biggest "looks-correct-but-doesn't-actually-work" hazard in
the project before v1.6.1.

Sources of truth
----------------

For each domain HA documents the bitmask of supported features in
``homeassistant.components.<domain>.const``.  We hard-code the
relevant bits here so we don't take a runtime dep on
``homeassistant`` — this module ships in the add-on container
which doesn't have the HA Python package.

The values below come from the HA source (verified at v2024.10):

* ``climate.const.ClimateEntityFeature.TARGET_TEMPERATURE``     = 1
* ``climate.const.ClimateEntityFeature.TARGET_TEMPERATURE_RANGE`` = 2
* ``climate.const.ClimateEntityFeature.TARGET_HUMIDITY``        = 4
* ``climate.const.ClimateEntityFeature.FAN_MODE``               = 8
* ``climate.const.ClimateEntityFeature.PRESET_MODE``            = 16
* ``light.const.LightEntityFeature``                            (we
  prefer the modern ``supported_color_modes`` attribute instead)
* ``fan.const.FanEntityFeature.SET_SPEED``                      = 1
* ``fan.const.FanEntityFeature.OSCILLATE``                      = 2
* ``fan.const.FanEntityFeature.DIRECTION``                      = 4
* ``fan.const.FanEntityFeature.PRESET_MODE``                    = 8
* ``fan.const.FanEntityFeature.TURN_ON``                        = 16
* ``humidifier.const.HumidifierEntityFeature.MODES``            = 1

The constants are also intentionally narrow: we don't try to
enumerate every HA capability, only the ones the Sleep Classifier
controller actually plans actions against.

These bits are stable across HA versions per HA's deprecation policy —
new bits are added at high indices, existing bit values don't shift.
"""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Set

if TYPE_CHECKING:    # pragma: no cover
    from src.ha_api_client import HAEntity


# ---------------------------------------------------------------------------
# Bitmask values copied from HA source
# ---------------------------------------------------------------------------


# climate.ClimateEntityFeature
_CLIMATE_TARGET_TEMPERATURE: int = 1
_CLIMATE_TARGET_TEMPERATURE_RANGE: int = 2
_CLIMATE_TARGET_HUMIDITY: int = 4
_CLIMATE_FAN_MODE: int = 8
_CLIMATE_PRESET_MODE: int = 16

# fan.FanEntityFeature
_FAN_SET_SPEED: int = 1
_FAN_PRESET_MODE: int = 8
_FAN_TURN_ON: int = 16

# humidifier.HumidifierEntityFeature
_HUMIDIFIER_MODES: int = 1


# ---------------------------------------------------------------------------
# Public capability tags
# ---------------------------------------------------------------------------


class Capability(str, Enum):
    """Coarse-grained capability tags the controller can plan against.

    Why an Enum of *strings* rather than the raw HA bitmask:

    * The controller doesn't care whether a fan supports
      ``SET_SPEED`` (HA bit) vs. ``preset_modes=[low, high]`` (HA
      attribute) — both translate to "I can adjust speed in some
      way".  The enum hides that.
    * Tests / logs become readable — ``Capability.SET_TEMPERATURE``
      reads as English, not ``17`` (a bitmask AND result).
    * Future expansion: HA may add a 7th climate bit and we'd want
      to add a tag without re-numbering.
    """

    # Climate
    SET_TEMPERATURE = "set_temperature"
    SET_HUMIDITY_VIA_CLIMATE = "climate.set_humidity"
    SET_HVAC_MODE = "set_hvac_mode"      # switching between off / cool / heat
    # Humidifier
    SET_HUMIDITY = "set_humidity"
    # Light
    SET_BRIGHTNESS = "set_brightness"
    SET_COLOR_TEMP = "set_color_temp"    # for warm-K bedtime ramp
    # Fan
    SET_SPEED_PCT = "set_speed_pct"      # accepts set_percentage
    SET_PRESET_MODE = "set_preset_mode"  # accepts low/medium/high preset
    # Universal
    TURN_ON_OFF = "turn_on_off"


# ---------------------------------------------------------------------------
# Inspection helpers — pure, no I/O
# ---------------------------------------------------------------------------


def _supported_features(entity: "HAEntity") -> int:
    """``attributes.supported_features`` coerced to int (0 if missing).

    Some integrations write the field as a string ("17"), some as a
    list (rare bug), and some omit it entirely on devices that
    don't declare any features — be defensive.
    """
    raw = entity.attributes.get("supported_features", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def capabilities_of(entity: "HAEntity") -> Set[Capability]:
    """Return the set of :class:`Capability` tags this entity supports.

    This is the authoritative function the discovery layer + the
    controller both consult.  When a device is missing a tag it
    *won't be selected* for the corresponding bucket (discovery), and
    if it is selected through manual config bindings the controller
    will refuse to plan that specific action against it (planner).
    """
    domain = entity.domain
    features = _supported_features(entity)
    caps: Set[Capability] = set()

    if domain == "climate":
        caps.add(Capability.SET_HVAC_MODE)   # always supported on a climate entity
        caps.add(Capability.TURN_ON_OFF)
        if features & _CLIMATE_TARGET_TEMPERATURE:
            caps.add(Capability.SET_TEMPERATURE)
        if features & _CLIMATE_TARGET_TEMPERATURE_RANGE:
            # Some integrations only expose set_temperature as a range
            # (low/high pair).  We treat that as ``SET_TEMPERATURE``
            # for control purposes — the controller passes a single
            # value and HA's coercion fills in the mid-point.  This
            # is a pragmatic shortcut, refine if a user reports
            # range-only AC ignoring our setpoint.
            caps.add(Capability.SET_TEMPERATURE)
        if features & _CLIMATE_TARGET_HUMIDITY:
            caps.add(Capability.SET_HUMIDITY_VIA_CLIMATE)
        # v2.1.0 — Zigbee2MQTT / Matter / custom integrations often
        # don't bother populating supported_features (value 0) but
        # still expose functional attributes.  If the bitmask is
        # empty, infer capability from observable attributes.
        if features == 0:
            attrs = entity.attributes or {}
            # "temperature" is the conventional target-temp attr name
            # in climate entities; "current_temperature" confirms it's
            # a real HA climate entity rather than a free-form state.
            if "temperature" in attrs and "current_temperature" in attrs:
                caps.add(Capability.SET_TEMPERATURE)

    elif domain == "humidifier":
        caps.add(Capability.SET_HUMIDITY)
        caps.add(Capability.TURN_ON_OFF)
        # HumidifierEntityFeature.MODES = 1 → ``set_mode`` available.
        # We don't currently use it but tag it for completeness so a
        # future "set humidifier to ECO mode at night" feature can
        # check this without re-reading attributes.
        if features & _HUMIDIFIER_MODES:
            caps.add(Capability.SET_PRESET_MODE)

    elif domain == "light":
        caps.add(Capability.TURN_ON_OFF)
        # ``brightness`` support: the modern way is to inspect
        # ``supported_color_modes``; pre-2022 HA used a feature bit.
        # We accept either signal.
        modes = entity.attributes.get("supported_color_modes") or []
        if isinstance(modes, str):
            modes = [modes]
        modes_set = {str(m).lower() for m in modes}
        # Any mode that carries an intensity → brightness controllable.
        # ``onoff`` and ``unknown`` modes are explicit no-go.
        if modes_set and modes_set != {"onoff"} and modes_set != {"unknown"}:
            caps.add(Capability.SET_BRIGHTNESS)
        # Color-temp control: any of the kelvin-capable modes.
        if modes_set & {"color_temp", "color_temp_kelvin"}:
            caps.add(Capability.SET_COLOR_TEMP)
        # v2.1.0 — Zigbee2MQTT fallback: ``brightness`` attribute
        # present with numeric value indicates brightness support.
        if Capability.SET_BRIGHTNESS not in caps:
            if entity.attributes.get("brightness") is not None:
                caps.add(Capability.SET_BRIGHTNESS)

    elif domain == "fan":
        caps.add(Capability.TURN_ON_OFF)
        if features & _FAN_SET_SPEED:
            caps.add(Capability.SET_SPEED_PCT)
        if features & _FAN_PRESET_MODE:
            caps.add(Capability.SET_PRESET_MODE)
        # v2.1.0 — Zigbee2MQTT fallback.  A fan that exposes a
        # numeric ``percentage`` attribute clearly supports speed
        # control even if supported_features is 0.  Same for
        # ``preset_modes`` as a non-empty list.
        if features == 0:
            if entity.attributes.get("percentage") is not None:
                caps.add(Capability.SET_SPEED_PCT)
            if entity.attributes.get("preset_modes"):
                caps.add(Capability.SET_PRESET_MODE)

    elif domain == "switch":
        caps.add(Capability.TURN_ON_OFF)

    elif domain == "media_player":
        caps.add(Capability.TURN_ON_OFF)
        # We don't currently plan volume/media actions from the
        # controller — soundscape control lives in the orchestrator.

    return caps


def is_available(entity: "HAEntity") -> bool:
    """Return ``True`` if the entity is in a state HA considers usable.

    HA marks dead devices with state ``"unavailable"`` and freshly-
    added but unread devices as ``"unknown"``.  Calling a service
    against such an entity returns 200 (the service exists) but
    nothing happens.  This guard is the last line of defence the
    controller checks at execution time.
    """
    if not entity:
        return False
    bad = {"unavailable", "unknown", ""}
    return str(entity.state).lower() not in bad


__all__ = [
    "Capability",
    "capabilities_of",
    "is_available",
]
