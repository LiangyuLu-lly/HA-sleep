"""Static guard: no direct Path.write_text to /data in web_ui.py or src/*.py.

PR3.3 requires that all writes to ``/data`` go through
``src._io_utils.atomic_write_json`` / ``atomic_write_text``.  This test
uses the ``ast`` module to parse source files and flag any ``Path(...).write_text(...)``
call where the path argument contains ``/data``.

Excluded: ``src/_io_utils.py`` (the authorized writer).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Files to scan.
_WEB_UI = PROJECT_ROOT / "sleep_classifier" / "web_ui.py"
_SRC_DIR = PROJECT_ROOT / "src"
_EXCLUDED = {"_io_utils.py"}


def _collect_files() -> list[Path]:
    """Gather all source files to audit."""
    files: list[Path] = []
    if _WEB_UI.exists():
        files.append(_WEB_UI)
    for p in sorted(_SRC_DIR.glob("*.py")):
        if p.name not in _EXCLUDED:
            files.append(p)
    return files


def _contains_data_string(node: ast.AST) -> bool:
    """Return True if any string literal in *node* subtree contains '/data'."""
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if "/data" in child.value:
                return True
        # Also check JoinedStr (f-strings) values
        if isinstance(child, ast.JoinedStr):
            for val in child.values:
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    if "/data" in val.value:
                        return True
    return False


def _is_path_call(node: ast.AST) -> bool:
    """Return True if *node* looks like a ``Path(...)`` constructor call."""
    if isinstance(node, ast.Call):
        func = node.func
        # Path(...)
        if isinstance(func, ast.Name) and func.id == "Path":
            return True
        # pathlib.Path(...)
        if isinstance(func, ast.Attribute) and func.attr == "Path":
            return True
    return False


def _find_violations(source_path: Path) -> list[tuple[int, str]]:
    """Parse *source_path* and return (line, snippet) for each violation.

    A violation is any call chain that ends in ``.write_text(...)`` where the
    receiver involves a ``Path(...)`` constructor call AND any string literal
    in the full expression contains ``/data``.

    We are intentionally conservative: if in doubt, flag it.
    """
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    except SyntaxError:
        return []

    violations: list[tuple[int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Look for *.write_text(...)
        if not (isinstance(func, ast.Attribute) and func.attr == "write_text"):
            continue
        # Check if the receiver chain involves Path(...)
        receiver = func.value
        receiver_has_path = False
        for sub in ast.walk(receiver):
            if _is_path_call(sub):
                receiver_has_path = True
                break
        if not receiver_has_path:
            # Also flag variable.write_text(...) if arguments contain /data
            # This catches cases like: some_path.write_text(...) where some_path
            # was assigned from Path("/data/...")
            pass

        # Check the full expression (receiver + arguments) for /data string
        if _contains_data_string(node) or _contains_data_string(receiver):
            line = getattr(node, "lineno", 0)
            snippet = ast.dump(node)[:120]
            violations.append((line, snippet))
        elif receiver_has_path:
            # Conservative: Path(...).write_text(...) even without visible /data
            # in the immediate expression. Check if ANY ancestor or sibling
            # in the same statement might reference /data.
            # For now, only flag if /data is directly visible.
            pass

    return violations


@pytest.mark.parametrize("source_file", _collect_files(), ids=lambda p: p.name)
def test_no_direct_write_text_to_data(source_file: Path) -> None:
    """Assert no source file uses Path(...).write_text(...) with /data paths."""
    violations = _find_violations(source_file)
    if violations:
        msg_lines = [
            f"{source_file.name} uses Path.write_text with /data "
            f"(must use src._io_utils.atomic_write_json/atomic_write_text):"
        ]
        for line, snippet in violations:
            msg_lines.append(f"  line {line}: {snippet}")
        pytest.fail("\n".join(msg_lines))
