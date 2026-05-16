"""Static validation tests for docs/ROADMAP.md structure.

Validates: Requirements 10.4, 12.1, 13.1
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ROADMAP_MD = _REPO_ROOT / "docs" / "ROADMAP.md"


@pytest.fixture()
def roadmap_content() -> str:
    assert _ROADMAP_MD.is_file(), f"{_ROADMAP_MD} does not exist"
    return _ROADMAP_MD.read_text(encoding="utf-8")


def test_roadmap_doc_exists():
    assert _ROADMAP_MD.is_file(), "docs/ROADMAP.md must exist"


def test_v2_1_0_section_present(roadmap_content: str):
    """Assert v2.1.0 section exists."""
    assert "v2.1.0" in roadmap_content, (
        "docs/ROADMAP.md must contain a v2.1.0 section"
    )


def test_v2_2_0_deferred_section_present(roadmap_content: str):
    """Assert v2.2.0+ deferred section exists."""
    assert "v2.2.0" in roadmap_content, (
        "docs/ROADMAP.md must contain a v2.2.0+ deferred section"
    )


def test_commercial_roadmap_section_present(roadmap_content: str):
    """Assert Commercial roadmap section exists."""
    assert "Commercial roadmap" in roadmap_content, (
        "docs/ROADMAP.md must contain a 'Commercial roadmap' section"
    )
