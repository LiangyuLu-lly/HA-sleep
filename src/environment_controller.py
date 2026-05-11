"""Environment Controller for sleep-stage-based smart home automation.

Generates lighting, temperature, and humidity control commands based on the
current sleep stage, and optionally publishes them via MQTT.

Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6
"""

import time
from typing import Dict, Optional

from src.data_structures import SleepStage

# ---------------------------------------------------------------------------
# Control strategy tables
# ---------------------------------------------------------------------------

# Lighting: brightness percentage (0–100)
_LIGHTING: Dict[SleepStage, Dict] = {
    SleepStage.DEEP:  {"target_value": 0,  "min": 0,  "max": 0,   "priority": 1},
    SleepStage.REM:   {"target_value": 0,  "min": 0,  "max": 0,   "priority": 1},
    SleepStage.LIGHT: {"target_value": 10, "min": 5,  "max": 15,  "priority": 2},
    SleepStage.AWAKE: {"target_value": 50, "min": 30, "max": 100, "priority": 3},
}

# Temperature: degrees Celsius
_TEMPERATURE: Dict[SleepStage, Dict] = {
    SleepStage.DEEP:  {"target_value": 19, "min": 18, "max": 20, "priority": 1},
    SleepStage.REM:   {"target_value": 19, "min": 18, "max": 20, "priority": 1},
    SleepStage.LIGHT: {"target_value": 21, "min": 20, "max": 22, "priority": 2},
    SleepStage.AWAKE: {"target_value": 23, "min": 22, "max": 24, "priority": 3},
}

# Humidity: relative humidity percentage
_HUMIDITY: Dict[SleepStage, Dict] = {
    SleepStage.DEEP:  {"target_value": 55, "min": 50, "max": 60, "priority": 1},
    SleepStage.REM:   {"target_value": 55, "min": 50, "max": 60, "priority": 1},
    SleepStage.LIGHT: {"target_value": 55, "min": 50, "max": 60, "priority": 2},
    SleepStage.AWAKE: {"target_value": 50, "min": 40, "max": 60, "priority": 3},
}


class EnvironmentController:
    """Generates environment control commands based on sleep stage.

    Each ``generate_*`` method returns a dict with at minimum:
    - ``control_type``  – ``"lighting"``, ``"temperature"``, or ``"humidity"``
    - ``target_value``  – numeric target for the controlled parameter
    - ``priority``      – integer command priority (1 = highest)
    - ``timestamp``     – Unix timestamp of command generation
    - ``min_value``     – lower bound of the acceptable range
    - ``max_value``     – upper bound of the acceptable range
    - ``sleep_stage``   – name of the sleep stage that triggered the command

    Optionally, an :class:`~src.mqtt_publisher.MQTTPublisher` instance can be
    supplied so that commands are published automatically.
    """

    def __init__(self, mqtt_publisher=None) -> None:
        """Initialise the controller.

        Args:
            mqtt_publisher: Optional :class:`~src.mqtt_publisher.MQTTPublisher`
                instance.  When provided, every ``generate_*`` call also
                publishes the command via MQTT.
        """
        self._publisher = mqtt_publisher

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate_lighting_control(self, sleep_stage: SleepStage) -> Dict:
        """Generate a lighting control command for the given sleep stage.

        Args:
            sleep_stage: Current sleep stage.

        Returns:
            Dict with ``control_type``, ``target_value``, ``priority``,
            ``timestamp``, ``min_value``, ``max_value``, and ``sleep_stage``.
        """
        spec = _LIGHTING[sleep_stage]
        command = self._build_command("lighting", spec, sleep_stage)
        if self._publisher is not None:
            self._publisher.publish_environment_control(
                control_type="lighting",
                target_value=command["target_value"],
                priority=command["priority"],
            )
        return command

    def generate_temperature_control(self, sleep_stage: SleepStage) -> Dict:
        """Generate a temperature control command for the given sleep stage.

        Args:
            sleep_stage: Current sleep stage.

        Returns:
            Dict with ``control_type``, ``target_value``, ``priority``,
            ``timestamp``, ``min_value``, ``max_value``, and ``sleep_stage``.
        """
        spec = _TEMPERATURE[sleep_stage]
        command = self._build_command("temperature", spec, sleep_stage)
        if self._publisher is not None:
            self._publisher.publish_environment_control(
                control_type="temperature",
                target_value=command["target_value"],
                priority=command["priority"],
            )
        return command

    def generate_humidity_control(self, sleep_stage: SleepStage) -> Dict:
        """Generate a humidity control command for the given sleep stage.

        Args:
            sleep_stage: Current sleep stage.

        Returns:
            Dict with ``control_type``, ``target_value``, ``priority``,
            ``timestamp``, ``min_value``, ``max_value``, and ``sleep_stage``.
        """
        spec = _HUMIDITY[sleep_stage]
        command = self._build_command("humidity", spec, sleep_stage)
        if self._publisher is not None:
            self._publisher.publish_environment_control(
                control_type="humidity",
                target_value=command["target_value"],
                priority=command["priority"],
            )
        return command

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_command(control_type: str, spec: Dict, sleep_stage: SleepStage) -> Dict:
        return {
            "control_type": control_type,
            "target_value": spec["target_value"],
            "priority": spec["priority"],
            "timestamp": time.time(),
            "min_value": spec["min"],
            "max_value": spec["max"],
            "sleep_stage": sleep_stage.name,
        }
