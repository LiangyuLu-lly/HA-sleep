"""Static tests for ``sleep_classifier/lovelace_template.py`` (P8.1).

Validates: Requirements 8.6, P8.1
"""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure both ``src/`` and ``sleep_classifier/`` are importable.
_repo = Path(__file__).resolve().parent.parent
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))

from sleep_classifier.lovelace_template import (
    DASHBOARD_TITLE,
    DASHBOARD_URL_PATH,
    REFERENCED_ENTITIES,
    build_dashboard_config,
)
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


# Build the union of entity IDs declared by both publishers.
# SleepStatePublisher owns all 20 entity constants directly.
# LearningPanelPublisher delegates to SleepStatePublisher and does not
# declare its own entity IDs — it calls publish methods that use the same
# constants.  The "LearningPanelPublisher ENTITY_IDS" is the subset it
# touches: bedtime workday/weekend, learned_environment,
# recommendation_explain, per_stage_deltas, debt_hours,
# recommended_bedtime.
_SLEEP_STATE_PUBLISHER_ENTITY_IDS: frozenset[str] = frozenset({
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

_LEARNING_PANEL_PUBLISHER_ENTITY_IDS: frozenset[str] = frozenset({
    ENTITY_LEARNED_BEDTIME_WORKDAY,
    ENTITY_LEARNED_BEDTIME_WEEKEND,
    ENTITY_LEARNED_ENVIRONMENT,
    ENTITY_RECOMMENDATION_EXPLAIN,
    ENTITY_PER_STAGE_DELTAS,
    ENTITY_DEBT,
    ENTITY_RECOMMENDED_BEDTIME,
})

_ALL_PUBLISHER_ENTITY_IDS = (
    _SLEEP_STATE_PUBLISHER_ENTITY_IDS | _LEARNING_PANEL_PUBLISHER_ENTITY_IDS
)


class TestReferencedEntitiesSubset:
    """P8.1: REFERENCED_ENTITIES ⊆ publisher declared entity IDs."""

    def test_referenced_entities_is_subset_of_publishers(self) -> None:
        """Every entity referenced by the dashboard template must be
        declared in one of the two publishers."""
        extra = REFERENCED_ENTITIES - _ALL_PUBLISHER_ENTITY_IDS
        assert extra == set(), (
            f"REFERENCED_ENTITIES contains entities not declared by any "
            f"publisher: {sorted(extra)}"
        )


class TestBuildDashboardConfig:
    """Structural assertions on the generated dashboard config."""

    def test_views_count_is_4(self) -> None:
        config = build_dashboard_config()
        assert len(config["views"]) == 4

    def test_title_correct(self) -> None:
        config = build_dashboard_config()
        assert config["title"] == DASHBOARD_TITLE
        assert config["title"] == "Sleep Classifier"

    def test_url_path_constant(self) -> None:
        assert DASHBOARD_URL_PATH == "sleep-classifier"

    def test_view_titles(self) -> None:
        config = build_dashboard_config()
        view_titles = [v["title"] for v in config["views"]]
        assert view_titles == ["Tonight", "Stage", "Learning", "Diagnostics"]
