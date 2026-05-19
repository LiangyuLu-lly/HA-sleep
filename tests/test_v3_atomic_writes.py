"""v3.0.0 atomic-write 扩展测试 —— ``atomic_write_bytes`` 与 ``atomic_append_jsonl``。

覆盖：
  (a) Property 18 (X1)：PR3 持久化原子性 —— 在 ``os.replace`` 之前 /
      期间注入 ``OSError`` 后，磁盘文件要么是上一稳定版本要么是新提
      交版本，绝不出现中间损坏状态；同时不留 ``.tmp.*`` 残留。
  (b) ``max_lines`` FIFO 截断的算术正确性（example-based）。
  (c) 基础往返：bytes 与 jsonl 单元行为、Unicode 编码、目录自动创建。

**Validates: Requirements 1.6, 4.2, 10.2**

设计说明
--------
v2.x 已通过 ``tests/test_io_utils_atomic_write.py`` 覆盖了
``atomic_write_text`` / ``atomic_write_json`` 的原子性；v3.0.0 新增
两个 helper 走同样的 tmpfile + ``fsync`` + ``os.replace`` 序列，所以
property 测试用 hypothesis 在两个新 helper 上枚举：

  * 失败点 ∈ {pre_replace, at_replace}
    - ``pre_replace`` 用 ``unittest.mock.patch`` 让 ``src._io_utils.os.fsync``
      抛 ``OSError``，模拟数据落盘前断电 / 磁盘写满；
    - ``at_replace`` 让 ``src._io_utils.os.replace`` 直接抛 ``OSError``，
      模拟 rename 系统调用失败（VFS 跨设备、被并发 unlink 等）。
  * 文件类型 ∈ {bytes, jsonl}：覆盖两个新 helper 的所有 dispatch 分支。
  * 已存在内容 ∈ {缺失, 任意 payload}：验证「存在 → 不被破坏」与
    「不存在 → 不被意外创建」两条不变量。

每个 hypothesis 例子使用独立的 ``tempfile.TemporaryDirectory``，避免跨
样本污染；所有 patch 都用 context manager 应用 / 撤销，pytest fixture
作用域无侧影响。
"""
from __future__ import annotations

import json
import string
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src._io_utils import atomic_append_jsonl, atomic_write_bytes


# ---------------------------------------------------------------------------
# Property 18 (X1): PR3 持久化原子性
# ---------------------------------------------------------------------------

# 限制 key 字母表与值的范围，避免 hypothesis 在 JSON 转义代理对 / 控制字符
# 上花费大量时间 —— 这里只关心原子性，键值具体内容不影响 property。
_JSON_KEY = st.text(min_size=1, max_size=8, alphabet=string.ascii_letters + "_")
_JSON_VAL = st.one_of(
    st.integers(min_value=-1_000, max_value=1_000),
    st.floats(min_value=-1_000.0, max_value=1_000.0, allow_nan=False, allow_infinity=False),
    st.booleans(),
    st.text(min_size=0, max_size=16, alphabet=string.ascii_letters + string.digits + " "),
)
_JSON_RECORD = st.dictionaries(_JSON_KEY, _JSON_VAL, min_size=1, max_size=4)


@st.composite
def _io_scenario(draw: st.DrawFn) -> tuple[str, Any, Any, str]:
    """生成 (kind, existing, new, failure_point) 四元组。"""
    kind = draw(st.sampled_from(["bytes", "jsonl"]))
    failure_point = draw(st.sampled_from(["pre_replace", "at_replace"]))

    if kind == "bytes":
        existing = draw(st.one_of(st.none(), st.binary(min_size=0, max_size=256)))
        new = draw(st.binary(min_size=0, max_size=256))
    else:  # jsonl
        existing = draw(
            st.one_of(
                st.none(),
                st.lists(_JSON_RECORD, min_size=0, max_size=10),
            )
        )
        new = draw(_JSON_RECORD)

    return kind, existing, new, failure_point


def _seed_jsonl(target: Path, records: list[Mapping[str, Any]]) -> None:
    """把已有 jsonl 记录写到目标文件（绕过 atomic 写入，纯 setup 用）。"""
    if not records:
        target.write_text("", encoding="utf-8")
        return
    target.write_text(
        "\n".join(
            json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in records
        )
        + "\n",
        encoding="utf-8",
    )


@given(scenario=_io_scenario())
@settings(
    max_examples=50,
    deadline=None,
    # tempfile.TemporaryDirectory 在 Windows 上偶尔触发 function-scoped
    # fixture warning（其实这里用的是上下文管理器，不是 fixture），关掉。
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property_x1_atomic_writes_survive_interrupt_injection(
    scenario: tuple[str, Any, Any, str],
) -> None:
    """Property 18 (X1)：PR3 持久化原子性。

    **Validates: Requirements 1.6, 4.2, 10.2**

    断言：在 ``os.replace`` 之前（fsync 阶段）或之中（rename 阶段）任意
    位置注入 ``OSError`` 时，**事后磁盘上**：

      1. 若目标文件先前存在 → 内容必须等于先前快照；
      2. 若目标文件先前不存在 → 必须仍然不存在；
      3. 同目录下不留 ``*.tmp.*`` 残留（保证 ``/data`` 不积累垃圾）。
    """
    kind, existing, new, failure_point = scenario

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        target = td_path / ("blob.bin" if kind == "bytes" else "records.jsonl")

        # ---- Setup：写入预先存在的内容（如果有） -----------------------
        if existing is not None:
            if kind == "bytes":
                target.write_bytes(existing)
            else:
                _seed_jsonl(target, existing)

        pre_existed = target.exists()
        pre_snapshot = target.read_bytes() if pre_existed else None

        # ---- 注入失败 -------------------------------------------------
        if failure_point == "pre_replace":
            patch_target = "src._io_utils.os.fsync"
        else:  # at_replace
            patch_target = "src._io_utils.os.replace"

        with patch(patch_target, side_effect=OSError("simulated interrupt")):
            with pytest.raises(OSError, match="simulated interrupt"):
                if kind == "bytes":
                    atomic_write_bytes(target, new)
                else:
                    atomic_append_jsonl(target, new)

        # ---- 不变量 1 / 2：目标文件状态 -------------------------------
        if pre_existed:
            assert target.exists(), "原已存在的目标文件被意外删除"
            assert target.read_bytes() == pre_snapshot, (
                f"原文件被破坏：kind={kind} failure_point={failure_point}"
            )
        else:
            assert not target.exists(), (
                f"目标文件本不应存在却被创建：kind={kind} "
                f"failure_point={failure_point}"
            )

        # ---- 不变量 3：无 .tmp.* 残留 ---------------------------------
        leftover_tmps = list(td_path.glob("*.tmp.*"))
        assert leftover_tmps == [], (
            f"残留 tmp 文件：{[p.name for p in leftover_tmps]}"
        )


# ---------------------------------------------------------------------------
# atomic_write_bytes 单元测试
# ---------------------------------------------------------------------------


def test_atomic_write_bytes_round_trip(tmp_path: Path) -> None:
    """正常写入后读回应等于原始字节。"""
    target = tmp_path / "blob.bin"
    payload = b"\x00\x01\xfe\xff" + bytes(range(256))
    atomic_write_bytes(target, payload)
    assert target.read_bytes() == payload


def test_atomic_write_bytes_overwrites_existing(tmp_path: Path) -> None:
    """对已有文件再写一次时按整体替换语义工作。"""
    target = tmp_path / "blob.bin"
    target.write_bytes(b"old payload" * 10)
    atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"new"


def test_atomic_write_bytes_creates_parent_dir(tmp_path: Path) -> None:
    """父目录缺失时自动创建（与 atomic_write_text 一致）。"""
    target = tmp_path / "nested" / "deep" / "blob.bin"
    atomic_write_bytes(target, b"hello")
    assert target.exists()
    assert target.read_bytes() == b"hello"


def test_atomic_write_bytes_empty_payload(tmp_path: Path) -> None:
    """空字节也是合法 payload（pickle 极少见但 helper 不应禁止）。"""
    target = tmp_path / "empty.bin"
    atomic_write_bytes(target, b"")
    assert target.exists()
    assert target.read_bytes() == b""


# ---------------------------------------------------------------------------
# atomic_append_jsonl —— max_lines FIFO 算术正确性（example-based）
# ---------------------------------------------------------------------------


def test_max_lines_fifo_truncates_to_latest_k_records(tmp_path: Path) -> None:
    """N=12 写入 + max_lines=5 → 仅保留最近 5 条（i ∈ [7, 11]），按写入顺序。"""
    target = tmp_path / "fifo.jsonl"
    K, N = 5, 12

    for i in range(N):
        atomic_append_jsonl(target, {"i": i, "tag": f"record-{i}"}, max_lines=K)

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == K, f"FIFO 截断后应剩 {K} 行，实际 {len(lines)}"

    records = [json.loads(ln) for ln in lines]
    assert [r["i"] for r in records] == list(range(N - K, N)), (
        "FIFO 截断后顺序应保留最近 K 条插入顺序"
    )


def test_max_lines_below_threshold_keeps_all(tmp_path: Path) -> None:
    """N=3 写入 + max_lines=10 → 全部保留（min(N, K) = 3）。"""
    target = tmp_path / "fifo.jsonl"
    for i in range(3):
        atomic_append_jsonl(target, {"i": i}, max_lines=10)

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(ln)["i"] for ln in lines] == [0, 1, 2]


def test_max_lines_equal_to_count_keeps_all(tmp_path: Path) -> None:
    """N == K 边界：恰好达到上限时不应丢任何记录。"""
    target = tmp_path / "fifo.jsonl"
    K = 7
    for i in range(K):
        atomic_append_jsonl(target, {"i": i}, max_lines=K)

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == K
    assert [json.loads(ln)["i"] for ln in lines] == list(range(K))


def test_max_lines_one_keeps_only_last(tmp_path: Path) -> None:
    """max_lines=1 退化为「永远只保留最后一条」。"""
    target = tmp_path / "fifo.jsonl"
    for i in range(5):
        atomic_append_jsonl(target, {"i": i}, max_lines=1)

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["i"] == 4


def test_max_lines_zero_raises_value_error(tmp_path: Path) -> None:
    """max_lines == 0 是非法值（FIFO 容量为 0 没有意义）。"""
    target = tmp_path / "x.jsonl"
    with pytest.raises(ValueError, match="max_lines"):
        atomic_append_jsonl(target, {"i": 0}, max_lines=0)
    # 不应留下半成品文件 / tmp。
    assert not target.exists()
    assert list(tmp_path.glob("*.tmp.*")) == []


def test_max_lines_negative_raises_value_error(tmp_path: Path) -> None:
    """max_lines 为负数同样应被拒绝。"""
    target = tmp_path / "x.jsonl"
    with pytest.raises(ValueError, match="max_lines"):
        atomic_append_jsonl(target, {"i": 0}, max_lines=-3)
    assert not target.exists()


def test_max_lines_none_disables_cap(tmp_path: Path) -> None:
    """max_lines=None（默认）时无上限（predictor_audit.jsonl 走自管 prune）。"""
    target = tmp_path / "uncapped.jsonl"
    for i in range(20):
        atomic_append_jsonl(target, {"i": i})
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 20
    assert [json.loads(ln)["i"] for ln in lines] == list(range(20))


# ---------------------------------------------------------------------------
# atomic_append_jsonl 其他单元行为
# ---------------------------------------------------------------------------


def test_atomic_append_jsonl_creates_file_when_missing(tmp_path: Path) -> None:
    """目标文件不存在时首次 append 应直接创建。"""
    target = tmp_path / "fresh.jsonl"
    assert not target.exists()
    atomic_append_jsonl(target, {"hello": "world"})
    assert target.exists()
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == {"hello": "world"}


def test_atomic_append_jsonl_creates_parent_dir(tmp_path: Path) -> None:
    """父目录缺失时自动创建。"""
    target = tmp_path / "nested" / "deep" / "factors.jsonl"
    atomic_append_jsonl(target, {"factor": "temperature_drift", "effect_pp": 12.4})
    assert target.exists()


def test_atomic_append_jsonl_appends_without_overwriting(tmp_path: Path) -> None:
    """连续两次 append 不会覆盖之前的记录。"""
    target = tmp_path / "incremental.jsonl"
    atomic_append_jsonl(target, {"i": 0})
    atomic_append_jsonl(target, {"i": 1})
    atomic_append_jsonl(target, {"i": 2})

    lines = target.read_text(encoding="utf-8").splitlines()
    assert [json.loads(ln)["i"] for ln in lines] == [0, 1, 2]


def test_atomic_append_jsonl_chinese_unicode_roundtrip(tmp_path: Path) -> None:
    """中文 / Unicode 应原样写入（ensure_ascii=False）。"""
    target = tmp_path / "zh.jsonl"
    record = {"日期": "2026-05-18", "状态": "深度睡眠", "factor": "noise_level"}
    atomic_append_jsonl(target, record)

    raw = target.read_text(encoding="utf-8")
    assert "深度睡眠" in raw
    # 不允许出现 \u 转义（ensure_ascii=False 的等价断言）。
    assert "\\u" not in raw
    # 反序列化等价。
    parsed = json.loads(raw.splitlines()[0])
    assert parsed == record


def test_atomic_append_jsonl_no_embedded_newline_in_record(tmp_path: Path) -> None:
    """单条记录序列化时使用 separators=(',', ':') 不会产生多行。"""
    target = tmp_path / "compact.jsonl"
    record = {"deeply": {"nested": {"value": [1, 2, 3]}}, "tag": "x"}
    atomic_append_jsonl(target, record)

    raw_lines = target.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 1, "记录序列化必须保持单行"
    assert json.loads(raw_lines[0]) == record
