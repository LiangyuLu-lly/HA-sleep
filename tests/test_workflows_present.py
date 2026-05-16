"""Static validation: three required workflow files exist with required job names.

Validates: Requirements 4.1, 4.2, 4.3, 4.7
"""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest

_WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _load_workflow(name: str) -> dict:
    """Load and parse a workflow YAML file."""
    path = _WORKFLOWS_DIR / name
    assert path.exists(), f"Workflow file {name} does not exist"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class TestWorkflowsPresent:
    """Assert that the three required workflows exist and contain required jobs."""

    def test_test_yml_exists(self):
        assert (_WORKFLOWS_DIR / "test.yml").exists()

    def test_addon_build_yml_exists(self):
        assert (_WORKFLOWS_DIR / "addon-build.yml").exists()

    def test_release_yml_exists(self):
        assert (_WORKFLOWS_DIR / "release.yml").exists()

    def test_test_yml_has_test_job(self):
        wf = _load_workflow("test.yml")
        jobs = wf.get("jobs", {})
        assert "test" in jobs, "test.yml must contain a 'test' job"

    def test_addon_build_yml_has_build_job(self):
        wf = _load_workflow("addon-build.yml")
        jobs = wf.get("jobs", {})
        assert "build" in jobs, "addon-build.yml must contain a 'build' job"

    def test_release_yml_has_release_job(self):
        wf = _load_workflow("release.yml")
        jobs = wf.get("jobs", {})
        assert "release" in jobs, "release.yml must contain a 'release' job"
