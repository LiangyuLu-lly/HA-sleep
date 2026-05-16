"""Static validation tests for README.md required sections.

Validates: Requirements 3.3, 10.1, 11.1, 12.4, 13.3, 14.1
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_README_MD = _REPO_ROOT / "README.md"


@pytest.fixture()
def readme_content() -> str:
    assert _README_MD.is_file(), f"{_README_MD} does not exist"
    return _README_MD.read_text(encoding="utf-8")


def test_readme_exists():
    assert _README_MD.is_file(), "README.md must exist"


@pytest.mark.parametrize(
    "section_text",
    [
        "Hardware Required",
        "Real-world",  # "Real-world results" or "Real-world Results"
        "Medical Advisor",  # "Medical Advisors" or "Medical advisors"
        "Support the",  # "Support the project" or "Support the Project"
        "Why HA only",
        "Two people sharing a bed",
    ],
    ids=[
        "Hardware Required",
        "Real-world results",
        "Medical advisors",
        "Support the project",
        "Why HA only",
        "Two people sharing a bed",
    ],
)
def test_required_section_present(readme_content: str, section_text: str):
    """Assert that required sections are present in README.md."""
    assert section_text.lower() in readme_content.lower(), (
        f"README.md must contain a section with '{section_text}'"
    )
