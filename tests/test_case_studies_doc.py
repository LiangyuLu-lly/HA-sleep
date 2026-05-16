"""Static validation tests for docs/CASE_STUDIES.md structure.

Validates: Requirements 11.1, 15.1, 15.2, 15.6
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CASE_STUDIES_MD = _REPO_ROOT / "docs" / "CASE_STUDIES.md"


@pytest.fixture()
def case_studies_content() -> str:
    assert _CASE_STUDIES_MD.is_file(), f"{_CASE_STUDIES_MD} does not exist"
    return _CASE_STUDIES_MD.read_text(encoding="utf-8")


def test_case_studies_doc_exists():
    assert _CASE_STUDIES_MD.is_file(), "docs/CASE_STUDIES.md must exist"


def test_minimum_length(case_studies_content: str):
    """Assert case study content is ≥1500 characters (Chinese text is dense)."""
    assert len(case_studies_content) >= 1500, (
        f"docs/CASE_STUDIES.md must be ≥1500 chars, got {len(case_studies_content)}"
    )


def test_how_to_reproduce_section(case_studies_content: str):
    """Assert 'How to reproduce' section is present."""
    assert "how to reproduce" in case_studies_content.lower(), (
        "docs/CASE_STUDIES.md must contain a 'How to reproduce' section"
    )


def test_at_least_3_screenshot_references(case_studies_content: str):
    """Assert ≥3 screenshot/image references exist."""
    # Match markdown image references or screenshot file references
    screenshot_pattern = re.compile(
        r"(!\[.*?\]\(.*?\)|screenshots/|\.png|\.jpg|\.gif)", re.IGNORECASE
    )
    matches = screenshot_pattern.findall(case_studies_content)
    assert len(matches) >= 3, (
        f"docs/CASE_STUDIES.md must reference ≥3 screenshots, found {len(matches)}"
    )
