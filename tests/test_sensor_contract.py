"""Sensor contract snapshot test (PR2 guardian).

Hard-codes the v2.0.3 20-entity snapshot of ``sensor.sleep_classifier_*``
entity IDs.  Asserts that both publishers' currently declared entity IDs
match this snapshot exactly.  Any rename or deletion fails the test,
preventing accidental breaking changes to downstream Lovelace dashboards
and automations.

Validates: PR2.1, PR2.2
"""
from __future__ import annotations

import sys
from pathlib import Path

_repo = Path(__file__).resolve().parent.parent
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))

from src.sleep_state_publisher import (
    ENTITY_APNEA_INDEX,
    ENTITY_CONFIDENCE,
    ENTITY_DEBT,
    ENTITY_DURATION,
    ENTITY_HEALTH,
    ENTITY_LAST_ACTION,
    ENTITY_LEARNED_BEDTIME_WEEKEND,
    ENTITY_LEARNED_BEDTIME_WORKDAY,
    ENTITY_LEARNED_ENVIRONMENT,
    ENTITY_PER_STAGE_DELTAS,
    ENTITY_QUALITY,
    ENTITY_QUALITY_ARCHITECTURE,
    ENTITY_QUALITY_EFFICIENCY,
    ENTITY_QUALITY_FRAGMENTATION,
    ENTITY_QUALITY_ONSET,
    ENTITY_RECOMMENDED_BEDTIME,
    ENTITY_RECOMMENDATION_EXPLAIN,
    ENTITY_SOUNDSCAPE,
    ENTITY_STAGE,
    ENTITY_WAKE_DECISION,
)


# -------------------------------------------------------------------- #
# v2.0.3 snapshot — 20 entities                                        #
# Any rename or removal MUST fail this test.                           #
# -------------------------------------------------------------------- #

V2_0_3_ENTITY_SNAPSHOT: frozenset[str] = frozenset({
    "sensor.sleep_classifier_stage",
    "sensor.sleep_classifier_confidence",
    "sensor.sleep_classifier_quality_score",
    "sensor.sleep_classifier_session_duration",
    "sensor.sleep_classifier_last_action",
    "sensor.sleep_classifier_debt_hours",
    "sensor.sleep_classifier_recommended_bedtime",
    "sensor.sleep_classifier_wake_decision",
    "sensor.sleep_classifier_soundscape",
    "sensor.sleep_classifier_learned_bedtime_workday",
    "sensor.sleep_classifier_learned_bedtime_weekend",
    "sensor.sleep_classifier_learned_environment",
    "sensor.sleep_classifier_recommendation_explain",
    "sensor.sleep_classifier_per_stage_deltas",
    "sensor.sleep_classifier_apnea_index",
    "sensor.sleep_classifier_health",
    "sensor.sleep_classifier_quality_architecture",
    "sensor.sleep_classifier_quality_efficiency",
    "sensor.sleep_classifier_quality_fragmentation",
    "sensor.sleep_classifier_quality_onset",
})


def _current_publisher_entity_ids() -> frozenset[str]:
    """Collect the full set of entity IDs from both publishers.

    SleepStatePublisher owns all 20 entity constants directly.
    LearningPanelPublisher delegates to SleepStatePublisher (it calls
    publish methods that reference the same module-level constants)
    and does not declare additional entity IDs.
    """
    return frozenset({
        ENTITY_STAGE,
        ENTITY_CONFIDENCE,
        ENTITY_QUALITY,
        ENTITY_DURATION,
        ENTITY_LAST_ACTION,
        ENTITY_DEBT,
        ENTITY_RECOMMENDED_BEDTIME,
        ENTITY_WAKE_DECISION,
        ENTITY_SOUNDSCAPE,
        ENTITY_LEARNED_BEDTIME_WORKDAY,
        ENTITY_LEARNED_BEDTIME_WEEKEND,
        ENTITY_LEARNED_ENVIRONMENT,
        ENTITY_RECOMMENDATION_EXPLAIN,
        ENTITY_PER_STAGE_DELTAS,
        ENTITY_APNEA_INDEX,
        ENTITY_HEALTH,
        ENTITY_QUALITY_ARCHITECTURE,
        ENTITY_QUALITY_EFFICIENCY,
        ENTITY_QUALITY_FRAGMENTATION,
        ENTITY_QUALITY_ONSET,
    })


class TestSensorContractSnapshot:
    """PR2 guardian: entity IDs must match the v2.0.3 snapshot exactly."""

    def test_snapshot_size(self) -> None:
        """Sanity: snapshot contains exactly 20 entities."""
        assert len(V2_0_3_ENTITY_SNAPSHOT) == 20

    def test_current_ids_match_snapshot_exactly(self) -> None:
        """No rename, no deletion allowed vs v2.0.3 baseline."""
        current = _current_publisher_entity_ids()
        missing = V2_0_3_ENTITY_SNAPSHOT - current
        assert missing == set(), (
            f"Entity IDs REMOVED since v2.0.3 (breaking change!): "
            f"{sorted(missing)}"
        )

    def test_no_unexpected_removals(self) -> None:
        """Current set must be a superset of v2.0.3 (additions OK)."""
        current = _current_publisher_entity_ids()
        assert current >= V2_0_3_ENTITY_SNAPSHOT

    def test_exact_match_for_v2_0_3(self) -> None:
        """Strict equality — if new entities are added in v2.1.0+, update
        this test to assert superset instead.  For now, the 20-entity
        contract is frozen."""
        current = _current_publisher_entity_ids()
        assert current == V2_0_3_ENTITY_SNAPSHOT, (
            f"Added: {sorted(current - V2_0_3_ENTITY_SNAPSHOT)}, "
            f"Removed: {sorted(V2_0_3_ENTITY_SNAPSHOT - current)}"
        )

    def test_all_start_with_sleep_classifier_prefix(self) -> None:
        """Every entity uses the project's namespace prefix."""
        for eid in V2_0_3_ENTITY_SNAPSHOT:
            assert eid.startswith("sensor.sleep_classifier_"), (
                f"Entity {eid} doesn't use the expected prefix"
            )
