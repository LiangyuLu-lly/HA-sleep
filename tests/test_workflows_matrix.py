"""Static validation: test.yml matrix covers Python 3.10/3.11/3.12 with fail-fast: false.

Validates: Requirements 4.1, 4.2
"""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest

_WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _load_test_workflow() -> dict:
    path = _WORKFLOWS_DIR / "test.yml"
    assert path.exists(), "test.yml not found"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestWorkflowsMatrix:
    """Assert test.yml matrix configuration meets CI requirements."""

    def test_matrix_covers_python_versions(self):
        wf = _load_test_workflow()
        test_job = wf["jobs"]["test"]
        strategy = test_job.get("strategy", {})
        matrix = strategy.get("matrix", {})
        python_versions = matrix.get("python-version", [])

        # Convert to strings for comparison (YAML may parse 3.10 as float)
        version_strs = [str(v) for v in python_versions]
        required_versions = ["3.10", "3.11", "3.12"]
        for ver in required_versions:
            assert ver in version_strs, (
                f"Python {ver} missing from test.yml matrix; "
                f"found: {version_strs}"
            )

    def test_fail_fast_is_false(self):
        wf = _load_test_workflow()
        test_job = wf["jobs"]["test"]
        strategy = test_job.get("strategy", {})
        fail_fast = strategy.get("fail-fast", True)
        assert fail_fast is False, (
            "test.yml strategy.fail-fast must be false to show all failures"
        )
