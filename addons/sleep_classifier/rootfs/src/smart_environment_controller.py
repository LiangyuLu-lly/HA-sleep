"""Stage-aware controller that drives Home Assistant devices directly.

Where :class:`src.environment_controller.EnvironmentController` produces
*MQTT command payloads*, :class:`SmartEnvironmentController` goes one step
further: it **picks a target environment**, **diffs it against the current
state** read from HA, and then **calls the matching HA services** through
:class:`src.ha_api_client.HomeAssistantClient`.

Personalised setpoints come from :class:`src.preference_learner.PreferenceLearner`
if it has enough history; otherwise we fall back to the static, sleep-medicine
inspired table below.  A configurable **deadband** prevents the controller
from flapping (e.g. don't bump the HVAC for a 0.1 °C change).

Closing the loop
----------------
The controller exposes :meth:`feedback_score` so the orchestrator (the
service main loop) can report how good the *last* stretch of sleep was; the
controller uses that to decide whether to lean on personalisation (when the
recent feedback is good) or to invite the learner to explore (when bad).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.data_structures import SleepStage
from src.device_discovery import ActionableDevices
from src.ha_api_client import HomeAssistantClient
from src.preference_learner import (
    EnvironmentParams,
    PreferenceLearner,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default targets per stage (used as fallback before any learning has happened)
# ---------------------------------------------------------------------------


_DEFAULT_TARGETS: Dict[SleepStage, EnvironmentParams] = {
    SleepStage.AWAKE: EnvironmentParams(
        temperature_c=23.0, humidity_pct=50.0, brightness_pct=40.0, fan_speed_pct=20.0,
    ),
    SleepStage.LIGHT: EnvironmentParams(
        temperature_c=21.0, humidity_pct=55.0, brightness_pct=8.0,  fan_speed_pct=15.0,
    ),
    SleepStage.DEEP: EnvironmentParams(
        temperature_c=19.0, humidity_pct=55.0, brightness_pct=0.0,  fan_speed_pct=10.0,
    ),
    SleepStage.REM: EnvironmentParams(
        temperature_c=19.5, humidity_pct=55.0, brightness_pct=0.0,  fan_speed_pct=10.0,
    ),
}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class SmartControlConfig:
    enabled: bool = True
    min_seconds_between_actions: float = 120.0
    deadband_temperature_c: float = 0.5
    deadband_humidity_pct: float = 5.0
    deadband_brightness_pct: float = 10.0
    dry_run: bool = False

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "SmartControlConfig":
        valid = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in valid})


@dataclass
class ControlAction:
    """A single planned HA service call (for logging / testing)."""

    domain: str
    service: str
    entity_id: str
    data: Dict[str, Any]
    reason: str = ""

    def describe(self) -> str:
        kvs = ", ".join(f"{k}={v}" for k, v in self.data.items() if k != "entity_id")
        return f"{self.domain}.{self.service}({self.entity_id}{', ' + kvs if kvs else ''})"


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class SmartEnvironmentController:
    """Pull current state from HA, decide target, push commands back."""

    def __init__(
        self,
        config: SmartControlConfig,
        ha_client: HomeAssistantClient,
        devices: ActionableDevices,
        learner: Optional[PreferenceLearner] = None,
    ) -> None:
        self.config = config
        self.ha = ha_client
        self.devices = devices
        self.learner = learner

        # Bookkeeping for rate limiting and feedback signal
        self._last_action_ts: Dict[str, float] = {}
        self._actions_log: List[ControlAction] = []
        self._recent_quality: Optional[float] = None
        self._current_targets: Dict[SleepStage, EnvironmentParams] = dict(
            _DEFAULT_TARGETS
        )

    # ------------------------------------------------------------------ #
    # Target selection                                                   #
    # ------------------------------------------------------------------ #

    def feedback_score(self, score: float) -> None:
        """Report the *most recent* sleep-quality score (0-100).

        Bad scores enable exploration in the learner.
        """
        self._recent_quality = float(score)

    def _should_explore(self) -> bool:
        if self._recent_quality is None:
            return False
        return self._recent_quality < 50.0   # below median → try something new

    def target_for(self, stage: SleepStage) -> EnvironmentParams:
        """Return the environment we want for ``stage``.

        Combines:

        * defaults (sleep-medicine table),
        * learner recommendation (if there is enough history),
        * optional perturbation when recent sleep was poor.
        """
        defaults = _DEFAULT_TARGETS[stage]
        if self.learner is None:
            return defaults
        explore = self._should_explore()
        return self.learner.recommend(defaults, explore=explore)

    # ------------------------------------------------------------------ #
    # Planning                                                           #
    # ------------------------------------------------------------------ #

    def plan_actions(
        self,
        stage: SleepStage,
        current_env: EnvironmentParams,
    ) -> List[ControlAction]:
        """Compute the list of HA service calls to issue for the new stage.

        Args:
            stage: the freshly classified sleep stage.
            current_env: temperature / humidity / brightness as last reported
                by HA.  Fields that are ``None`` are treated as unknown and
                always trigger an action.

        Returns:
            List of :class:`ControlAction` objects.  Empty when no change is
            needed or when the rate-limit is still active.
        """
        if not self.config.enabled:
            return []

        target = self.target_for(stage)
        actions: List[ControlAction] = []
        now = time.time()

        # --- Temperature: climate.set_temperature ------------------------
        if target.temperature_c is not None and self.devices.climates:
            outside_dead = (
                current_env.temperature_c is None
                or abs(current_env.temperature_c - target.temperature_c)
                > self.config.deadband_temperature_c
            )
            if outside_dead and self._allow_action("climate", now):
                for c in self.devices.climates:
                    actions.append(
                        ControlAction(
                            domain="climate",
                            service="set_temperature",
                            entity_id=c.entity_id,
                            data={"temperature": round(target.temperature_c, 1)},
                            reason=f"stage={stage.name} curr={current_env.temperature_c}",
                        )
                    )

        # --- Humidity: humidifier.set_humidity ---------------------------
        if target.humidity_pct is not None and self.devices.humidifiers:
            outside_dead = (
                current_env.humidity_pct is None
                or abs(current_env.humidity_pct - target.humidity_pct)
                > self.config.deadband_humidity_pct
            )
            if outside_dead and self._allow_action("humidifier", now):
                for h in self.devices.humidifiers:
                    actions.append(
                        ControlAction(
                            domain="humidifier",
                            service="set_humidity",
                            entity_id=h.entity_id,
                            data={"humidity": int(round(target.humidity_pct))},
                            reason=f"stage={stage.name} curr={current_env.humidity_pct}",
                        )
                    )

        # --- Brightness: light.turn_on / turn_off ------------------------
        if target.brightness_pct is not None and self.devices.lights:
            outside_dead = (
                current_env.brightness_pct is None
                or abs(current_env.brightness_pct - target.brightness_pct)
                > self.config.deadband_brightness_pct
            )
            if outside_dead and self._allow_action("light", now):
                for light in self.devices.lights:
                    if target.brightness_pct <= 0.5:
                        actions.append(
                            ControlAction(
                                domain="light",
                                service="turn_off",
                                entity_id=light.entity_id,
                                data={},
                                reason=f"stage={stage.name} → lights off",
                            )
                        )
                    else:
                        # Use warm kelvin for sleep stages so the user isn't
                        # blasted with daylight white.
                        data: Dict[str, Any] = {
                            "brightness_pct": int(round(target.brightness_pct)),
                        }
                        if stage in {SleepStage.LIGHT, SleepStage.DEEP, SleepStage.REM}:
                            data["kelvin"] = 2200
                        actions.append(
                            ControlAction(
                                domain="light",
                                service="turn_on",
                                entity_id=light.entity_id,
                                data=data,
                                reason=f"stage={stage.name} target={target.brightness_pct}%",
                            )
                        )

        # --- Fan: fan.set_percentage -------------------------------------
        if target.fan_speed_pct is not None and self.devices.fans:
            if self._allow_action("fan", now):
                for f in self.devices.fans:
                    if target.fan_speed_pct <= 1.0:
                        actions.append(
                            ControlAction(
                                domain="fan",
                                service="turn_off",
                                entity_id=f.entity_id,
                                data={},
                                reason=f"stage={stage.name} → fan off",
                            )
                        )
                    else:
                        actions.append(
                            ControlAction(
                                domain="fan",
                                service="set_percentage",
                                entity_id=f.entity_id,
                                data={"percentage": int(round(target.fan_speed_pct))},
                                reason=f"stage={stage.name} target={target.fan_speed_pct}%",
                            )
                        )

        return actions

    def _allow_action(self, domain: str, now: float) -> bool:
        """Return True if we have waited long enough since the last action."""
        last = self._last_action_ts.get(domain, 0.0)
        return now - last >= self.config.min_seconds_between_actions

    # ------------------------------------------------------------------ #
    # Execution                                                          #
    # ------------------------------------------------------------------ #

    async def apply(
        self,
        stage: SleepStage,
        current_env: EnvironmentParams,
    ) -> List[ControlAction]:
        """Plan + execute the actions for ``stage``.

        Returns the list of actions that were planned (whether actually
        executed depends on ``dry_run``).
        """
        actions = self.plan_actions(stage, current_env)
        if not actions:
            return []

        for action in actions:
            self._actions_log.append(action)
            if self.config.dry_run:
                logger.info("[dry-run] would call %s  // %s",
                            action.describe(), action.reason)
                continue
            try:
                await self.ha.call_service(
                    action.domain,
                    action.service,
                    entity_id=action.entity_id,
                    **action.data,
                )
                self._last_action_ts[action.domain] = time.time()
                logger.info("Executed %s  // %s",
                            action.describe(), action.reason)
            except Exception as exc:    # noqa: BLE001
                logger.error(
                    "Service call failed (%s): %s",
                    action.describe(), exc,
                )

        return actions

    @property
    def action_history(self) -> List[ControlAction]:
        return list(self._actions_log)


__all__ = [
    "SmartControlConfig",
    "SmartEnvironmentController",
    "ControlAction",
]
