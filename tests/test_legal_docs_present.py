"""Legal docs 静态校验测试。

断言五份 legal 文档存在 + 关键段落 grep 命中。

**Validates: Requirements 5.1, 5.3, 5.5**
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

LEGAL_DOCS = [
    ("PRIVACY.md", [r"数据控制者|data controller"]),
    ("SECURITY.md", [r"liangyulu781|security"]),
    ("CONTRIBUTING.md", [r"pytest --cov|覆盖率"]),
    ("CODE_OF_CONDUCT.md", [r"Contributor Covenant"]),
    ("MEDICAL_DISCLAIMER.md", [r"医疗免责|Medical Disclaimer"]),
]


@pytest.mark.parametrize(
    "filename,patterns",
    LEGAL_DOCS,
    ids=[doc[0] for doc in LEGAL_DOCS],
)
class TestLegalDocPresent:
    """验证每份 legal 文档存在且包含关键内容。"""

    def test_file_exists(self, filename: str, patterns: list[str]) -> None:
        path = REPO_ROOT / filename
        assert path.is_file(), f"{filename} not found at repo root"

    def test_key_content_present(self, filename: str, patterns: list[str]) -> None:
        path = REPO_ROOT / filename
        content = path.read_text(encoding="utf-8")
        for pattern in patterns:
            assert re.search(pattern, content, re.IGNORECASE), (
                f"{filename} missing expected content matching: {pattern}"
            )
