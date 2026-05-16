"""Static validation: addon-build.yml uses docker buildx with multi-arch.

Validates: Requirements 4.3, 4.7
"""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest

_WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _load_addon_build_workflow() -> dict:
    path = _WORKFLOWS_DIR / "addon-build.yml"
    assert path.exists(), "addon-build.yml not found"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _flatten_steps(workflow: dict) -> list[dict]:
    """Extract all steps from all jobs in a workflow."""
    steps = []
    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            steps.append(step)
    return steps


class TestWorkflowsBuildx:
    """Assert addon-build.yml calls setup-buildx-action and builds multi-arch."""

    def test_uses_setup_buildx_action(self):
        wf = _load_addon_build_workflow()
        steps = _flatten_steps(wf)
        uses_buildx = any(
            "docker/setup-buildx-action" in str(step.get("uses", ""))
            for step in steps
        )
        assert uses_buildx, (
            "addon-build.yml must use docker/setup-buildx-action"
        )

    def test_builds_linux_arm64_and_amd64(self):
        wf = _load_addon_build_workflow()
        steps = _flatten_steps(wf)

        # Look for --platform linux/arm64,linux/amd64 in any step's run command
        platform_found = False
        for step in steps:
            run_cmd = step.get("run", "")
            if "linux/arm64" in run_cmd and "linux/amd64" in run_cmd:
                platform_found = True
                break

        assert platform_found, (
            "addon-build.yml must build with --platform linux/arm64,linux/amd64"
        )
