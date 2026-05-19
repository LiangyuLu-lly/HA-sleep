"""``scripts/sanitize_user_data.py`` 脱敏行为测试。

覆盖：

  (a) Property 12：合成 ``user_preferences.json`` / ``causal_factors.jsonl``
      经过 ``main(["--input", ..., "--out", ...])`` 处理后，输出文件中
      不应含原始 ``entity_id`` 字面值；任何时间戳的秒位归零；用户画像
      字段（``age_band`` / ``sex`` / ``chronotype``）一律替换为
      ``"redacted"``。
  (b) 单元行为：单个 entity_id 不泄漏、Unix 秒级时间戳向下取整到分钟、
      JSONL 输出仍是「一条记录一行」格式。

**Validates: Requirements 14.5**

设计说明
--------
``scripts/sanitize_user_data.py`` 的脱敏规则由 design.md Property 12
固定，本测试只关心**对外可观察行为**：
  * 原始 entity_id 字面值不会出现在输出文本中（hash 后只剩 16 hex 字符）。
  * ISO-8601 时间戳秒位永远是 ``"00"`` 或 ``"00Z"`` / ``"00+08:00"``。
  * 用户画像字段值统一变成字符串 ``"redacted"``。

策略上：

  * hypothesis 生成的 entity_id 用「小写字母 / 下划线 . 小写字母 / 数字 /
    下划线」字母表，与 ``scripts/sanitize_user_data.py`` 内置的
    ``_ENTITY_ID_RE`` 严格对齐；
  * 时间戳的秒位强制 ∈ ``[1, 59]``，避免「本来就是 :00 看不出脱敏与否」
    的退化样本；
  * profile 枚举与 ``training_config/config_loader.py`` 的合法集合
    保持一致（age_band / sex / chronotype）。

每个 hypothesis 例子使用独立的 ``tempfile.TemporaryDirectory``，避免跨
样本污染。
"""
from __future__ import annotations

import json
import re
import string
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scripts import sanitize_user_data


# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

#: domain 部分 (``sensor`` / ``input_number`` 等) -> 小写字母 + 下划线。
_DOMAIN_ALPHABET = string.ascii_lowercase + "_"

#: entity slug 部分 -> 字母数字 + 下划线。
_SLUG_ALPHABET = string.ascii_letters + string.digits + "_"

#: profile 枚举值与 ``training_config/config_loader.py`` 完全一致。
_AGE_BAND_VALUES: tuple[str, ...] = ("18-25", "26-35", "36-50", "51-65", "65+")
_SEX_VALUES: tuple[str, ...] = ("M", "F", "unspecified")
_CHRONOTYPE_VALUES: tuple[str, ...] = ("morning", "evening", "neutral")


def _domain_strategy() -> st.SearchStrategy[str]:
    """生成形如 ``sensor`` / ``input_number`` 的 domain。

    必须以小写字母开头，以避免出现 ``__.foo`` 这种合法但奇怪的 domain。
    """
    return st.builds(
        lambda head, tail: head + tail,
        st.sampled_from(string.ascii_lowercase),
        st.text(alphabet=_DOMAIN_ALPHABET, min_size=0, max_size=10),
    )


def _slug_strategy() -> st.SearchStrategy[str]:
    """生成 entity_id 点号后面的 slug。"""
    return st.text(alphabet=_SLUG_ALPHABET, min_size=1, max_size=24)


def _entity_id_strategy() -> st.SearchStrategy[str]:
    """生成形如 ``sensor.bedroom_temp`` 的合法 entity_id。"""
    return st.builds(lambda d, s: f"{d}.{s}", _domain_strategy(), _slug_strategy())


def _iso_timestamp_strategy() -> st.SearchStrategy[str]:
    """生成 ISO-8601 时间戳，**秒位强制非 0**。

    覆盖 3 种 timezone 写法：``Z`` / ``+HH:MM`` / 无后缀。
    """
    return st.builds(
        lambda y, mo, d, h, mi, s, tz: (
            f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:{s:02d}{tz}"
        ),
        st.integers(min_value=2024, max_value=2030),
        st.integers(min_value=1, max_value=12),
        st.integers(min_value=1, max_value=28),
        st.integers(min_value=0, max_value=23),
        st.integers(min_value=0, max_value=59),
        # 秒位 ∈ [1, 59]，确保「:00」一定来自脱敏而非生成时就 = 0。
        st.integers(min_value=1, max_value=59),
        st.sampled_from(["Z", "+08:00", "-05:00", ""]),
    )


def _unix_timestamp_strategy() -> st.SearchStrategy[int]:
    """合法的 Unix-second 时间戳，**保证秒位非 0**。"""
    return st.builds(
        lambda minute_aligned, sec: minute_aligned * 60 + sec,
        st.integers(min_value=1_700_000_000 // 60, max_value=1_900_000_000 // 60),
        st.integers(min_value=1, max_value=59),
    )


def _profile_strategy() -> st.SearchStrategy[dict[str, str]]:
    """生成合法的 ``user_profile`` 字段子树。"""
    return st.fixed_dictionaries(
        {
            "age_band": st.sampled_from(_AGE_BAND_VALUES),
            "sex": st.sampled_from(_SEX_VALUES),
            "chronotype": st.sampled_from(_CHRONOTYPE_VALUES),
        }
    )


@st.composite
def _user_preferences_payload(draw: st.DrawFn) -> dict[str, Any]:
    """合成一份合法的 ``user_preferences.json`` 内容。

    内含若干 entity_id（在 ``slot_bindings`` 嵌套字典里和顶层 list 里）、
    若干带秒位的时间戳，以及一份用户画像。
    """
    n_sessions = draw(st.integers(min_value=1, max_value=4))
    sessions = []
    for _ in range(n_sessions):
        sessions.append(
            {
                "started_at": draw(_iso_timestamp_strategy()),
                "ended_at": draw(_iso_timestamp_strategy()),
                "stage_counts": {"AWAKE": 1, "LIGHT": 2, "DEEP": 3, "REM": 4},
                "quality_score": draw(st.floats(min_value=0.0, max_value=100.0)),
            }
        )

    # entity_id 出现在两处：``slot_bindings`` 字典 value 和顶层 list。
    slot_temp = draw(_entity_id_strategy())
    slot_humid = draw(_entity_id_strategy())
    free_floating = draw(st.lists(_entity_id_strategy(), min_size=1, max_size=4))

    return {
        "version": "v3.0.0",
        "sessions": sessions,
        "slot_bindings": {
            "temperature_source": slot_temp,
            "humidity_source": slot_humid,
        },
        "watched_entities": free_floating,
        # 把 entity_id 也放在显式 ``entity_id`` 键下，覆盖 _ENTITY_ID_KEYS 分支。
        "primary": {"entity_id": draw(_entity_id_strategy())},
        "user_profile": draw(_profile_strategy()),
    }


@st.composite
def _causal_factors_lines(draw: st.DrawFn) -> list[dict[str, Any]]:
    """合成若干行 ``causal_factors.jsonl`` 记录。

    每条带 ``timestamp``（ISO 字符串）、画像字段（应被 redact）以及
    一个可选 ``entity_id`` 字段（覆盖显式 entity_id key 路径）。
    """
    n = draw(st.integers(min_value=1, max_value=5))
    records = []
    for _ in range(n):
        rec = {
            "timestamp": draw(_iso_timestamp_strategy()),
            "install_id_hash": "deadbeef" * 4,  # already-hashed; should pass through.
            "factors": {
                "temperature_drift": draw(
                    st.floats(min_value=-5.0, max_value=5.0, allow_nan=False)
                ),
                "noise_level": None,
            },
            "quality_total": draw(st.floats(min_value=0.0, max_value=100.0)),
            # Embed picture-perfect leakage candidates:
            "age_band": draw(st.sampled_from(_AGE_BAND_VALUES)),
            "sex": draw(st.sampled_from(_SEX_VALUES)),
            "chronotype": draw(st.sampled_from(_CHRONOTYPE_VALUES)),
            "entity_id": draw(_entity_id_strategy()),
        }
        records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_originals(
    payload: dict[str, Any] | list[dict[str, Any]],
) -> tuple[set[str], set[str]]:
    """收集 payload 中所有原始 entity_id 与原始 ISO 时间戳字面值。

    返回 ``(entity_ids, timestamps)`` 两个集合。entity_ids 仅含输入端
    生成的合法 entity_id（不含 hash 前缀冲突的 16-hex 串等无关字符）。
    """
    entities: set[str] = set()
    timestamps: set[str] = set()

    def _walk(node: Any, parent_key: str | None = None) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, parent_key=k)
        elif isinstance(node, list):
            for item in node:
                _walk(item, parent_key=parent_key)
        elif isinstance(node, str):
            if sanitize_user_data._ENTITY_ID_RE.match(node):
                entities.add(node)
            elif parent_key in sanitize_user_data._TIMESTAMP_KEYS:
                if sanitize_user_data._ISO8601_RE.match(node):
                    timestamps.add(node)

    _walk(payload)
    return entities, timestamps


_TS_TAIL_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:(?P<sec>\d{2})(?:Z|[+-]\d{2}:?\d{2})?"
)


def _all_timestamp_seconds(text: str) -> list[str]:
    """提取文本中所有 ISO-8601 时间戳的秒位字段。"""
    return [m.group("sec") for m in _TS_TAIL_RE.finditer(text)]


# ---------------------------------------------------------------------------
# Property 12 — main hypothesis test
# ---------------------------------------------------------------------------


@given(prefs=_user_preferences_payload(), factors=_causal_factors_lines())
@settings(max_examples=30, deadline=None)
def test_property_p12_sanitize_removes_entity_ids_and_seconds(
    prefs: dict[str, Any],
    factors: list[dict[str, Any]],
) -> None:
    """Property 12：脱敏后输出文件不含原始 entity_id / 完整秒位时间戳。

    **Validates: Requirements 14.5**

    断言：

      1. 输入端出现过的所有 entity_id 字面值（无论作为 ``entity_id`` 键
         的值还是自由出现的字符串）在输出文本中**全部消失**；
      2. 输出文本中所有 ISO-8601 时间戳的秒位字段必须是 ``"00"``；
      3. 用户画像字段（``age_band`` / ``sex`` / ``chronotype``）的值统一
         为字符串 ``"redacted"``。
    """
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        prefs_in = td_path / "user_preferences.json"
        factors_in = td_path / "causal_factors.jsonl"

        prefs_in.write_text(
            json.dumps(prefs, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        factors_in.write_text(
            "\n".join(
                json.dumps(rec, ensure_ascii=False) for rec in factors
            )
            + "\n",
            encoding="utf-8",
        )

        out_dir = td_path / "sanitised"
        rc = sanitize_user_data.main(
            ["--input", str(td_path), "--out", str(out_dir)]
        )
        assert rc == 0, "sanitize_user_data.main 应返回 0"

        prefs_out = out_dir / "user_preferences.json"
        factors_out = out_dir / "causal_factors.jsonl"
        assert prefs_out.exists(), "脱敏后的 user_preferences.json 缺失"
        assert factors_out.exists(), "脱敏后的 causal_factors.jsonl 缺失"

        prefs_text = prefs_out.read_text(encoding="utf-8")
        factors_text = factors_out.read_text(encoding="utf-8")
        combined = prefs_text + "\n" + factors_text

        # ---- 断言 1：原始 entity_id 不应出现 -----------------------------
        prefs_entities, prefs_ts = _collect_originals(prefs)
        factors_entities, factors_ts = _collect_originals(factors)
        for eid in prefs_entities | factors_entities:
            assert eid not in combined, (
                f"原始 entity_id 字面值仍泄漏在输出中：{eid!r}"
            )

        # ---- 断言 2：输出中所有时间戳秒位 = 00 -----------------------------
        seconds = _all_timestamp_seconds(combined)
        # 输入端至少有 1 个时间戳，否则属于退化样本（hypothesis 生成时
        # 我们强制了 sessions / records 至少 1 条）。
        assert seconds, "输出中找不到任何 ISO 时间戳，无法验证脱敏"
        bad = [s for s in seconds if s != "00"]
        assert not bad, f"存在未归零的秒位：{bad[:5]}（共 {len(bad)} 处）"

        # ---- 断言 3：profile 字段全部 = "redacted" -------------------------
        sanitised_prefs = json.loads(prefs_text)
        profile = sanitised_prefs.get("user_profile", {})
        for key in ("age_band", "sex", "chronotype"):
            assert profile.get(key) == "redacted", (
                f"user_profile.{key} 未脱敏，实际值={profile.get(key)!r}"
            )

        for raw_line in factors_text.splitlines():
            if not raw_line.strip():
                continue
            rec = json.loads(raw_line)
            for key in ("age_band", "sex", "chronotype"):
                assert rec.get(key) == "redacted", (
                    f"jsonl 行画像字段 {key} 未脱敏，"
                    f"实际值={rec.get(key)!r}"
                )

        # 顺带：确认输入端的时间戳都被改写过（即原始字面值不再出现）。
        # 这给单个时间戳级别的 leak 一个更强的兜底。
        for ts in prefs_ts | factors_ts:
            # 原始时间戳一定带秒位 != 00（生成器约束）；若它整串仍出现
            # 在输出里，说明根本没被脱敏。
            assert ts not in combined, f"原始时间戳泄漏：{ts!r}"


# ---------------------------------------------------------------------------
# Example-based tests
# ---------------------------------------------------------------------------


def test_no_entity_id_leaks(tmp_path: Path) -> None:
    """单次穷举检查：4 个常见 entity_id + 1 个画像不应泄漏。"""
    payload = {
        "slot_bindings": {
            "temperature_source": "sensor.bedroom_temperature",
            "humidity_source": "sensor.bedroom_humidity",
            "light_source": "light.bedroom_main",
        },
        "watched_entities": ["input_number.bedtime_target"],
        "primary": {"entity_id": "media_player.bedroom"},
        "user_profile": {
            "age_band": "26-35",
            "sex": "M",
            "chronotype": "evening",
        },
        "sessions": [
            {
                "started_at": "2026-05-18T22:14:37Z",
                "ended_at": "2026-05-19T06:55:42Z",
            }
        ],
    }

    src = tmp_path / "user_preferences.json"
    dst = tmp_path / "out.json"
    src.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    rc = sanitize_user_data.main(["--input", str(src), "--out", str(dst)])
    assert rc == 0
    out = dst.read_text(encoding="utf-8")

    for leaked in (
        "sensor.bedroom_temperature",
        "sensor.bedroom_humidity",
        "light.bedroom_main",
        "input_number.bedtime_target",
        "media_player.bedroom",
    ):
        assert leaked not in out, f"entity_id 字面值泄漏：{leaked}"

    # Profile 字段应全 redacted。
    parsed = json.loads(out)
    assert parsed["user_profile"] == {
        "age_band": "redacted",
        "sex": "redacted",
        "chronotype": "redacted",
    }

    # Hash 是 16 字符的 hex；至少有一个出现在输出里证明替换发生过。
    assert re.search(r'"[0-9a-f]{16}"', out), (
        "未在输出中找到 sha256 hash 替身（脱敏未发生）"
    )


def test_unix_timestamp_floors_to_minute(tmp_path: Path) -> None:
    """Unix 秒级时间戳应向下取整到分钟边界（``1716000017 -> 1716000000``）。"""
    payload = {
        "sessions": [
            {"started_at": 1716000017, "ended_at": 1716000059},
            {"started_at": 1716000060, "ended_at": 1716000061},  # 已对齐 + 偏 1 秒
        ]
    }
    src = tmp_path / "in.json"
    dst = tmp_path / "out.json"
    src.write_text(json.dumps(payload), encoding="utf-8")

    rc = sanitize_user_data.main(["--input", str(src), "--out", str(dst)])
    assert rc == 0

    parsed = json.loads(dst.read_text(encoding="utf-8"))
    assert parsed["sessions"][0]["started_at"] == 1716000000
    assert parsed["sessions"][0]["ended_at"] == 1716000000
    assert parsed["sessions"][1]["started_at"] == 1716000060  # 已对齐保持不变
    assert parsed["sessions"][1]["ended_at"] == 1716000060


def test_jsonl_format_preserved(tmp_path: Path) -> None:
    """JSONL 输出每条记录仍占独立一行，且能逐行 json.loads 还原。"""
    records = [
        {
            "timestamp": "2026-05-18T22:14:37Z",
            "factors": {"temperature_drift": 1.5, "noise_level": None},
            "age_band": "26-35",
            "sex": "F",
            "chronotype": "morning",
            "entity_id": "sensor.bedroom_temperature",
        },
        {
            "timestamp": "2026-05-19T06:55:42+08:00",
            "factors": {"temperature_drift": -0.8, "noise_level": 32.0},
            "age_band": "36-50",
            "sex": "M",
            "chronotype": "neutral",
            "entity_id": "sensor.bedroom_humidity",
        },
    ]

    src = tmp_path / "causal_factors.jsonl"
    dst = tmp_path / "out.jsonl"
    src.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )

    rc = sanitize_user_data.main(["--input", str(src), "--out", str(dst)])
    assert rc == 0

    text = dst.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) == 2, f"JSONL 输出行数应保持为 2，实际 {len(lines)}"

    parsed = [json.loads(ln) for ln in lines]
    for orig, sanitised in zip(records, parsed):
        # 时间戳秒位归零。
        assert sanitised["timestamp"].endswith(":00Z") or sanitised[
            "timestamp"
        ].endswith(":00+08:00"), sanitised["timestamp"]
        # 画像字段全部 redacted。
        assert sanitised["age_band"] == "redacted"
        assert sanitised["sex"] == "redacted"
        assert sanitised["chronotype"] == "redacted"
        # entity_id 已被替换为 hash（16 hex），且不等于原值。
        assert sanitised["entity_id"] != orig["entity_id"]
        assert re.fullmatch(r"[0-9a-f]{16}", sanitised["entity_id"])
        # 业务数值字段保持不变（不在脱敏白名单内）。
        assert sanitised["factors"] == orig["factors"]


# ---------------------------------------------------------------------------
# Light sanity test for the `--out == --input` guard rail.
# ---------------------------------------------------------------------------


def test_refuses_to_overwrite_input(tmp_path: Path) -> None:
    """``--out`` 解析到 ``--input`` 同一路径时应拒绝写入并返回非 0。"""
    src = tmp_path / "x.json"
    src.write_text("{}", encoding="utf-8")
    rc = sanitize_user_data.main(["--input", str(src), "--out", str(src)])
    assert rc != 0, "sanitize_user_data.main 应拒绝覆盖输入文件"


# ---------------------------------------------------------------------------
# Negative path: nothing weird happens for missing input.
# ---------------------------------------------------------------------------


def test_missing_input_returns_nonzero(tmp_path: Path) -> None:
    """``--input`` 不存在时返回非 0，不抛异常。"""
    rc = sanitize_user_data.main(
        ["--input", str(tmp_path / "nonexistent.json"), "--out", str(tmp_path / "o")]
    )
    assert rc != 0


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """避免脱敏工具在仓库根写出残留文件。

    ``main()`` 走 ``Path.resolve()``，本身不依赖 cwd；这里仅作为防御性
    隔离，确保任何意外的相对路径都落到 tmp_path 下。
    """
    monkeypatch.chdir(tmp_path)
