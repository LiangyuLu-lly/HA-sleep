"""Static validation tests for .github/FUNDING.yml structure.

Validates: Requirements 10.3
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FUNDING_YML = _REPO_ROOT / ".github" / "FUNDING.yml"


@pytest.fixture()
def funding_content() -> str:
    assert _FUNDING_YML.is_file(), f"{_FUNDING_YML} does not exist"
    return _FUNDING_YML.read_text(encoding="utf-8")


def test_funding_yml_exists():
    assert _FUNDING_YML.is_file(), ".github/FUNDING.yml must exist"


def test_github_field_present(funding_content: str):
    """Assert 'github:' field is present in FUNDING.yml."""
    assert "github:" in funding_content, (
        ".github/FUNDING.yml must contain a 'github:' field"
    )
