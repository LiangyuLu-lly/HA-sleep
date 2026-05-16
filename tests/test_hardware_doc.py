"""Static validation tests for docs/HARDWARE.md structure.

Validates: Requirements 3.1
"""
from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HARDWARE_MD = _REPO_ROOT / "docs" / "HARDWARE.md"


@pytest.fixture()
def hardware_content() -> str:
    assert _HARDWARE_MD.is_file(), f"{_HARDWARE_MD} does not exist"
    return _HARDWARE_MD.read_text(encoding="utf-8")


def test_hardware_doc_exists():
    assert _HARDWARE_MD.is_file(), "docs/HARDWARE.md must exist"


def test_key_section_titles_present(hardware_content: str):
    """Assert key hardware category section titles exist."""
    required_keywords = ["毫米波雷达", "智能手环", "智能手表"]
    for keyword in required_keywords:
        assert keyword in hardware_content, (
            f"docs/HARDWARE.md must mention hardware category '{keyword}'"
        )


def test_compatibility_table_exists(hardware_content: str):
    """Assert a compatibility matrix table exists (markdown table with | delimiters)."""
    lines = hardware_content.splitlines()
    table_lines = [ln for ln in lines if ln.strip().startswith("|") and "---" not in ln]
    # At least header + 5 data rows (≥5 hardware entries)
    assert len(table_lines) >= 6, (
        f"Expected ≥6 table rows (header + 5 hardware), got {len(table_lines)}"
    )


def test_affiliate_disclosure_present(hardware_content: str):
    """Assert affiliate disclosure section is present."""
    assert "affiliate" in hardware_content.lower() or "Affiliate" in hardware_content, (
        "docs/HARDWARE.md must contain an affiliate disclosure section"
    )
