"""Bridge between :class:`PreferenceLearner` + :class:`SleepDebtTracker`
and :class:`SleepStatePublisher`.

Why this module exists
----------------------

Previously the orchestrator (`scripts/run_ha_smart_service.py`)
embedded two ~50-line `_publish_*` methods that:

1. Pulled the latest snapshot off the preference learner and the sleep-
   debt tracker, with all the defensive try/except needed to survive a
   corrupted history file.
2. Translated that snapshot into the right publisher call shape (which
   evolved twice: v1.3 added 4 sensors, v1.5 added 1 more).

Embedded in the orchestrator they were untestable in isolation — every
test had to spin up the whole `SmartSleepService` first, including HA
client mocks, just to check that "no learner → no publish".  Splitting
the panel out into ``LearningPanelPublisher`` lets us:

* Unit-test the panel against trivial learner / publisher doubles.
* Drop ~110 lines off ``run_ha_smart_service.py`` (1215 → ~1105),
  pulling that file off the BACKLOG's "biggest file" hotspot.
* Add a 5th, 6th, … learning sensor in v1.7 without growing the
  orchestrator further.

The class is **stateless** with respect to control loops — it
re-fetches everything on each `publish_now()` call.  Caching of
expensive learner methods (e.g. ``recommend_per_stage_deltas``) lives
inside the learner / controller, not here.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence, Tuple

from src.preference_learner import EnvironmentParams, PreferenceLearner
from src.sleep_debt import SleepDebtTracker
from src.sleep_state_publisher import SleepStatePublisher
from src.user_profile import UserProfile

logger = logging.getLogger(__name__)


class LearningPanelPublisher:
    """Pull → translate → push the 6 learning-panel sensors.

    The two public coroutines are :meth:`publish_debt_and_bedtime` and
    :meth:`publish_learning_panel`.  Both no-op silently when the
    underlying learner / publisher is missing — the orchestrator's
    boot path constructs us before any of those exist, so the no-op
    behaviour keeps the call sites simple.
    """

    def __init__(
        self,
        learner: Optional[PreferenceLearner],
        publisher: Optional[SleepStatePublisher],
        profile: UserProfile,
        wake_window_strs: Sequence[str],
        env_provider,
    ) -> None:
        """Args:

        learner: source of bedtime / k-NN / per-stage recommendations.
        publisher: HA sensor writer.
        profile: user preferences (recommended sleep hours).
        wake_window_strs: e.g. ``["07:00", "07:30"]`` — passed through
            to :meth:`SleepDebtTracker.plan_recovery`.
        env_provider: callable returning a fresh ``EnvironmentParams``
            snapshot at call time.  Decouples the panel from the
            orchestrator's mutable ``last_env`` field.
        """
        self.learner = learner
        self.publisher = publisher
        self.profile = profile
        self.wake_window_strs = list(wake_window_strs)
        self._env_provider = env_provider

    # ------------------------------------------------------------------ #
    # Sleep-debt + recommended-bedtime entities                          #
    # ------------------------------------------------------------------ #

    async def publish_debt_and_bedtime(self) -> None:
        """Refresh ``sensor.sleep_classifier_debt_hours`` and the
        recommended-bedtime entity from the latest history.
        """
        if self.learner is None or self.publisher is None:
            return
        try:
            sessions = self.learner.sessions()
            tracker = SleepDebtTracker.from_sessions(self.profile, sessions)
            # ``SleepDebtTracker.plan_recovery`` expects either ``None``
            # 或一个 2-tuple ``("HH:MM", "HH:MM")``。Add-on 配置里
            # wake_window_start / wake_window_end 默认空字符串，
            # 此处会得到空列表 / 含空串的列表 — 任意非法形态都用
            # ``None`` 兜底，避免 ``IndexError``（v3.0.2 修复）。
            ww: Optional[Tuple[str, str]] = None
            if (
                len(self.wake_window_strs) >= 2
                and self.wake_window_strs[0]
                and self.wake_window_strs[1]
            ):
                ww = (
                    str(self.wake_window_strs[0]),
                    str(self.wake_window_strs[1]),
                )
            plan = tracker.plan_recovery(wake_window=ww)
            await self.publisher.publish_debt(
                plan.current_debt_hours,
                severity=plan.severity.value,
                target_hours=self.profile.recommended_total_sleep_hours(),
                nights_to_full_recovery=plan.nights_to_full_recovery,
            )
            await self.publisher.publish_recommended_bedtime(
                plan.tonight_bedtime,
                tonight_target_hours=plan.tonight_target_hours,
                reason=plan.message,
            )
        except Exception as exc:    # noqa: BLE001
            logger.warning("Could not publish debt/bedtime: %s", exc)

    # ------------------------------------------------------------------ #
    # 4× v1.3.0 + 1× v1.5.0 learning entities                            #
    # ------------------------------------------------------------------ #

    async def publish_learning_panel(self) -> None:
        """Refresh the bedtime / environment / explain / per-stage entities.

        Pulls the latest snapshot from :class:`PreferenceLearner` and
        forwards each piece to the corresponding publisher method.  Any
        exception is logged but never re-raised so a bad learner state
        can't interrupt the orchestrator's inference loop.
        """
        if self.learner is None or self.publisher is None:
            return
        try:
            defaults = self._current_env_defaults()
            current_temp_c = defaults.temperature_c

            bedtime = self.learner.recommend_bedtime()
            await self.publisher.publish_learned_bedtime(bedtime)

            knn = self.learner.recommend_knn(
                defaults, current_temp_c=current_temp_c,
            )
            await self.publisher.publish_learned_environment(
                knn["env"].to_dict(),
                confidence=float(knn.get("confidence", 0.0)),
                n_used=int(knn.get("n_used", 0)),
            )

            explanation = self.learner.explain(
                defaults, current_temp_c=current_temp_c,
            )
            await self.publisher.publish_recommendation_explain(explanation)

            # v1.5.0 — per-stage deltas.  Wrapped in its own try so a
            # learner that predates v1.5 (no recommend_per_stage_deltas)
            # still gets the four v1.3 sensors published.
            try:
                deltas = self.learner.recommend_per_stage_deltas()
                await self.publisher.publish_per_stage_deltas(deltas)
            except AttributeError:
                pass
        except Exception as exc:    # noqa: BLE001
            logger.warning("Could not publish learning panel: %s", exc)

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #

    def _current_env_defaults(self) -> EnvironmentParams:
        """Snapshot the orchestrator's `last_env` via the injected provider.

        Done as a method (not a stored field) so we always read the
        *latest* env at publish time, not the env that existed when the
        panel was constructed at boot.
        """
        env: Any = self._env_provider()
        # Defensively coerce to EnvironmentParams in case the provider
        # returns a dict (e.g. a future change to the orchestrator that
        # holds env as a dict).
        if isinstance(env, EnvironmentParams):
            return EnvironmentParams(
                temperature_c=env.temperature_c,
                humidity_pct=env.humidity_pct,
                brightness_pct=env.brightness_pct,
                fan_speed_pct=env.fan_speed_pct,
            )
        return EnvironmentParams.from_dict(env or {})


__all__ = ["LearningPanelPublisher"]
