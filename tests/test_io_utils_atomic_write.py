"""Tests for src/_io_utils atomic write helpers.

Covers:
  (a) Normal write + read back equals original data.
  (b) Mid-write exception doesn't corrupt existing main file.
  (c) Exception path cleans up tmp file.
  (d) Parent directory auto-creation.
  (e) UTF-8 Chinese content written correctly.

Validates: Requirements 1.7 / Design: §6.4
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src._io_utils import atomic_write_json, atomic_write_text


def test_normal_write_read_back_equals(tmp_path: Path) -> None:
    """(a) Normal write + read back equals original data."""
    target = tmp_path / "output.json"
    data = '{"key": "value", "number": 42}'
    atomic_write_text(target, data)
    assert target.read_text(encoding="utf-8") == data


def test_mid_write_exception_preserves_main_file(tmp_path: Path) -> None:
    """(b) Mid-write exception doesn't corrupt existing main file."""
    target = tmp_path / "config.json"
    original = '{"status": "original"}'
    target.write_text(original, encoding="utf-8")

    # Simulate an exception during write by patching os.fsync to raise.
    with patch("src._io_utils.os.fsync", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            atomic_write_text(target, '{"status": "corrupted incomplete data"}')

    # Main file must still contain the original content.
    assert target.read_text(encoding="utf-8") == original


def test_exception_path_cleans_up_tmp_file(tmp_path: Path) -> None:
    """(c) Exception path cleans up tmp file."""
    target = tmp_path / "data.json"

    with patch("src._io_utils.os.fsync", side_effect=OSError("I/O error")):
        with pytest.raises(OSError):
            atomic_write_text(target, "some data")

    # No .tmp files should remain in the directory.
    tmp_files = list(tmp_path.glob("*.tmp.*"))
    assert tmp_files == [], f"Leftover tmp files: {tmp_files}"


def test_parent_directory_auto_creation(tmp_path: Path) -> None:
    """(d) Parent directory auto-creation."""
    target = tmp_path / "nested" / "deep" / "file.json"
    data = '{"nested": true}'
    atomic_write_text(target, data)
    assert target.exists()
    assert target.read_text(encoding="utf-8") == data


def test_utf8_chinese_content(tmp_path: Path) -> None:
    """(e) UTF-8 Chinese content written correctly."""
    target = tmp_path / "chinese.json"
    payload = {"名称": "睡眠分类器", "描述": "智能卧室环境调节"}
    atomic_write_json(target, payload)

    result = json.loads(target.read_text(encoding="utf-8"))
    assert result == payload
    # Verify Chinese characters are NOT escaped (ensure_ascii=False).
    raw = target.read_text(encoding="utf-8")
    assert "睡眠分类器" in raw
    assert "\\u" not in raw
