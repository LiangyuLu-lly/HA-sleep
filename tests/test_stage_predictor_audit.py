"""StagePredictor 7 晚命中率 + 自动停用 property（task 5.7）。

**Validates: Requirements 10.2, 10.3, 10.4**

本文件覆盖两个 property：

* **Property 9：7 晚滚动命中率统计正确**
  入口 :func:`test_property_p9_hit_rate_matches_arithmetic`。

  对任意合成 ``(predicted, actual_after_60s, timestamp)`` 序列断言
  :meth:`StagePredictor.hit_rate_7d` 等于「裸算术」基线：

      hit_rate = sum(predicted == actual) / count(actual is not None) * 100

  并覆盖 R10.3 隐含的「distinct UTC 夜数 < 7 → 返回 None」边界（命中率
  对部分周未定义）。

* **Property 9b：连续 3 晚 < 70% 命中率自动停用**
  入口 :func:`test_property_p9b_auto_disable_after_3_consecutive_below_70pct`。

  断言 R10.4：当**最近 3 个**带审计数据的夜的逐夜命中率都 < 70% 时，
  :attr:`StagePredictor.predictor_status` 锁存为 ``"auto_disabled"``。

设计说明
--------
* 我们**直接写** ``predictor_audit.jsonl``（而不是 ``await record_hit``）
  以精确控制 timestamp，跨越 7 个 UTC 自然日。`record_hit` 用 ``time.time()``
  打当前时间戳，无法在不 monkeypatch 时钟的前提下覆盖多日窗口。
* 每个 hypothesis 例子 / 子测试都用独立的 :class:`tempfile.TemporaryDirectory`
  + 全新 :class:`StagePredictor` 实例，避免缓存 / 状态跨样本污染。
* 不需要 ``onnxruntime``：这两个 property 只触碰 ``hit_rate_7d`` 与
  ``predictor_status`` 的纯审计路径，:class:`StagePredictor` 的
  :class:`onnxruntime.InferenceSession` 是惰性加载的。
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.stage_predictor import StagePredictor


# ---------------------------------------------------------------------------
# 共用常量与辅助函数
# ---------------------------------------------------------------------------

#: 与 :data:`src.stage_predictor._STAGE_NAMES` 一致；硬写一份避免依赖 private
#: 名称（其值进入 audit json 字面量，作为 R10.2 数据 schema 的一部分）。
_STAGE_NAMES: tuple[str, ...] = ("AWAKE", "LIGHT", "DEEP", "REM")


def _build_predictor(audit_jsonl: Path) -> StagePredictor:
    """构造一个仅用于审计路径的 :class:`StagePredictor`.

    :param audit_jsonl: 测试沙箱内的临时审计文件路径。

    ``model_path`` 指向不存在的占位 —— 测试只触碰 ``hit_rate_7d`` 与
    ``predictor_status`` ，不会触发 ONNX 加载。
    """
    return StagePredictor(
        model_path=audit_jsonl.parent / "missing_model.onnx",
        audit_jsonl=audit_jsonl,
    )


def _write_jsonl(path: Path, records: list[dict]) -> None:
    """直接写一份 JSONL 文件（绕开 ``record_hit`` 的 ``time.time()``）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(
        json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in records
    )
    if payload:
        payload += "\n"
    path.write_text(payload, encoding="utf-8")


def _iso(dt: datetime) -> str:
    """``datetime`` → ISO-8601 with ``+00:00`` (与 ``record_hit`` 一致)."""
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Property 9：7 晚滚动命中率统计正确
# ---------------------------------------------------------------------------


# 一条审计记录的 hypothesis 描述：
#   * predicted, actual：4 个 stage 名称（含 actual=None 模拟 R10.2 中
#     ground truth 缺失的样本，按设计**不计入**分母 / 分子）。
#   * day_offset：相对于「现在」往前 0..6 天的 UTC 自然日偏移；与 hour
#     拼起来精确覆盖 7 晚滚动窗口。
#   * minutes_offset：当天内的偏移（0..1439 分钟）；多记录同夜聚到同
#     一 night_key。
_record_strategy = st.tuples(
    st.sampled_from(_STAGE_NAMES),
    st.one_of(st.none(), st.sampled_from(_STAGE_NAMES)),
    st.integers(min_value=0, max_value=6),
    st.integers(min_value=0, max_value=1439),
)


def _records_to_jsonl_payload(
    records: list[tuple[str, str | None, int, int]],
    *,
    anchor: datetime,
) -> list[dict]:
    """把 hypothesis 元组扁平化成 JSONL dict 列表，时间戳基于 ``anchor``."""
    payload: list[dict] = []
    for predicted, actual, day_offset, minute_offset in records:
        ts = anchor - timedelta(days=day_offset, minutes=minute_offset)
        payload.append({
            "timestamp": _iso(ts),
            "predicted_stage": predicted,
            "actual_stage_60s_later": actual,
            "confidence": 0.75,  # 任意合法值，不影响命中率算术。
        })
    return payload


def _expected_hit_rate(
    records: list[dict],
) -> float | None:
    """与 :meth:`StagePredictor.hit_rate_7d` 一致的纯算术基线。

    复刻 ``hit_rate_7d`` 的核心契约：

      1. 仅考虑 ``actual_stage_60s_later`` 非 ``None`` 的记录；
      2. 统计 distinct UTC 自然日数（key = ``timestamp.date().isoformat()``）；
      3. distinct 夜数 < 7 → ``None``；否则返回 ``hits / total * 100``。

    我们故意**不复用** module-private helper，让测试与实现彼此独立校验。
    """
    per_night_keys: set[str] = set()
    rolling_total = 0
    rolling_hits = 0
    for r in records:
        actual = r.get("actual_stage_60s_later")
        if actual is None:
            continue
        ts = r["timestamp"]
        # ``fromisoformat`` 能直接处理 "+00:00"；与实现路径一致。
        night_key = (
            datetime.fromisoformat(ts).astimezone(timezone.utc).date().isoformat()
        )
        per_night_keys.add(night_key)
        rolling_total += 1
        if r["predicted_stage"] == actual:
            rolling_hits += 1
    if len(per_night_keys) < 7 or rolling_total == 0:
        return None
    return (rolling_hits / rolling_total) * 100.0


@given(records=st.lists(_record_strategy, min_size=0, max_size=80))
@settings(
    max_examples=30,
    deadline=None,
    # 用 TemporaryDirectory 上下文管理器（不是 fixture），关掉无关警告。
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property_p9_hit_rate_matches_arithmetic(
    records: list[tuple[str, str | None, int, int]],
) -> None:
    """Property 9：``hit_rate_7d`` 等于裸算术；< 7 晚返回 ``None``。

    **Validates: Requirements 10.2, 10.3, 10.4**
    """
    with tempfile.TemporaryDirectory() as td:
        audit_jsonl = Path(td) / "predictor_audit.jsonl"

        # 锚点选「now - 1 hour」：保证 day_offset=6 那一档（≈ 6 天前）落在
        # 7 天保留窗口内，不被 ``hit_rate_7d`` 内部 cutoff 误剪。
        anchor = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        payload = _records_to_jsonl_payload(records, anchor=anchor)
        _write_jsonl(audit_jsonl, payload)

        predictor = _build_predictor(audit_jsonl)

        observed = predictor.hit_rate_7d()
        expected = _expected_hit_rate(payload)

        if expected is None:
            assert observed is None, (
                "distinct 夜数 < 7 应返回 None，实际 "
                f"observed={observed!r}; n_records={len(payload)}"
            )
        else:
            assert observed is not None, (
                "expected 命中率非 None，但 hit_rate_7d 返回 None；"
                f"n_records={len(payload)} expected={expected!r}"
            )
            # 浮点对齐：核心是「相等」，但允许 1e-9 级别的 IEEE 误差。
            assert abs(observed - expected) <= 1e-9, (
                f"命中率与算术基线不一致：observed={observed!r} "
                f"expected={expected!r} n_records={len(payload)}"
            )


# ---------------------------------------------------------------------------
# Property 9b：连续 3 晚 < 70% 命中率 → 自动停用
# ---------------------------------------------------------------------------


# 7 晚每晚的「记录数」——保证全部 ≥ 1 让每一夜都形成一个 night_key。
# 上界 8 控制单例数据规模，hypothesis 仍能搜索到极小 / 极大边界。
_per_night_size = st.integers(min_value=1, max_value=8)


def _build_night_records(
    *,
    anchor: datetime,
    day_offset: int,
    n: int,
    n_hits: int,
    stage_for_hits: str,
    stage_for_misses_predicted: str,
    stage_for_misses_actual: str,
) -> list[dict]:
    """构造一夜内的 ``n`` 条审计记录，其中 ``n_hits`` 条命中。

    确保所有记录都落在同一 UTC 自然日上（共享 ``day_offset``，分钟错开）。
    """
    assert 0 <= n_hits <= n
    assert stage_for_misses_predicted != stage_for_misses_actual
    out: list[dict] = []
    for i in range(n):
        ts = anchor - timedelta(days=day_offset, minutes=i)
        if i < n_hits:
            out.append({
                "timestamp": _iso(ts),
                "predicted_stage": stage_for_hits,
                "actual_stage_60s_later": stage_for_hits,
                "confidence": 0.8,
            })
        else:
            out.append({
                "timestamp": _iso(ts),
                "predicted_stage": stage_for_misses_predicted,
                "actual_stage_60s_later": stage_for_misses_actual,
                "confidence": 0.8,
            })
    return out


@given(
    bad_sizes=st.lists(_per_night_size, min_size=3, max_size=3),
    bad_hit_fractions=st.lists(
        st.floats(min_value=0.0, max_value=0.69),
        min_size=3,
        max_size=3,
    ),
    good_sizes=st.lists(_per_night_size, min_size=4, max_size=4),
)
@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_property_p9b_auto_disable_after_3_consecutive_below_70pct(
    bad_sizes: list[int],
    bad_hit_fractions: list[float],
    good_sizes: list[int],
) -> None:
    """Property 9b：最近 3 晚每晚命中率 < 70% → ``predictor_status = auto_disabled``。

    **Validates: Requirements 10.4**

    构造布局（按 UTC 自然日从旧到新，距 ``anchor`` 由远及近）：

      day_offset=6 .. 3 → 4 个「健康」夜（hit_rate = 100%，凑齐 7 晚分母）
      day_offset=2 .. 0 → 3 个「不健康」夜（hit_rate < 70%）

    布局保证：

      * distinct 夜数 = 7 ≥ 7，``hit_rate_7d`` 不会因不够 7 晚提前返回。
      * 健康夜均 100% 命中，所以「最近 3 晚」严格指 day_offset = 0/1/2。
      * 三个「不健康」夜各自命中率 < 0.70，触发 R10.4 latch。
    """
    with tempfile.TemporaryDirectory() as td:
        audit_jsonl = Path(td) / "predictor_audit.jsonl"

        anchor = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        payload: list[dict] = []

        # 远端 4 个健康夜（day_offset 6..3，命中率 100%）。
        for i, n in enumerate(good_sizes):
            day_offset = 6 - i  # 6, 5, 4, 3
            payload.extend(_build_night_records(
                anchor=anchor,
                day_offset=day_offset,
                n=n,
                n_hits=n,
                stage_for_hits="DEEP",
                stage_for_misses_predicted="DEEP",
                stage_for_misses_actual="LIGHT",
            ))

        # 最近 3 个不健康夜（day_offset 2..0）。
        # 计算 n_hits = floor(n * bad_hit_fractions[k])；fraction ≤ 0.69 保证
        # 逐夜命中率严格 < 0.70（即使 ceil 到 n_hits/n 也在 0.69 区间内）。
        for k in range(3):
            n = bad_sizes[k]
            frac = bad_hit_fractions[k]
            n_hits = int(n * frac)
            # 数值保险：若 n=1 且 frac>0 但 int(n*frac)=0 仍然 OK（0/1 = 0 < 0.7）；
            # 若 n=2 且 frac=0.69 → int=1 → 1/2=0.5 < 0.7；不会越界。
            assert (n_hits / n) < 0.70, (
                f"build error: 第 {k} 个 bad night n={n} n_hits={n_hits} "
                f"hit_rate={n_hits / n} 不 < 0.70"
            )
            day_offset = 2 - k  # 2, 1, 0
            payload.extend(_build_night_records(
                anchor=anchor,
                day_offset=day_offset,
                n=n,
                n_hits=n_hits,
                stage_for_hits="REM",
                stage_for_misses_predicted="REM",
                stage_for_misses_actual="AWAKE",
            ))

        _write_jsonl(audit_jsonl, payload)
        predictor = _build_predictor(audit_jsonl)

        # 驱动一次 hit_rate_7d 以触发 _update_auto_disable。
        rate = predictor.hit_rate_7d()
        assert rate is not None, (
            "测试布局保证 7 晚均有数据，hit_rate_7d 不应返回 None；"
            f"n_records={len(payload)}"
        )

        assert predictor.predictor_status == "auto_disabled", (
            "最近 3 晚命中率均 < 70% 后应锁存为 auto_disabled，实际 "
            f"status={predictor.predictor_status!r} rate={rate!r}"
        )


# ---------------------------------------------------------------------------
# 一组 example-based smoke 测试，把 property 在边界条件上的预期固定下来。
# ---------------------------------------------------------------------------


def test_hit_rate_returns_none_when_fewer_than_seven_distinct_nights(
    tmp_path: Path,
) -> None:
    """Distinct 夜数 < 7（哪怕样本数很多）应返回 ``None``（R10.3 边界）。"""
    audit_jsonl = tmp_path / "predictor_audit.jsonl"
    anchor = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    payload: list[dict] = []
    # 6 个不同 UTC 自然日，每天 5 条全部命中。
    for day_offset in range(6):
        payload.extend(_build_night_records(
            anchor=anchor,
            day_offset=day_offset,
            n=5,
            n_hits=5,
            stage_for_hits="LIGHT",
            stage_for_misses_predicted="LIGHT",
            stage_for_misses_actual="DEEP",
        ))
    _write_jsonl(audit_jsonl, payload)
    predictor = _build_predictor(audit_jsonl)
    assert predictor.hit_rate_7d() is None
    # auto_disable 不应在样本不足时被错误触发。
    assert predictor.predictor_status == "healthy"


def test_hit_rate_zero_when_no_hits_across_seven_nights(tmp_path: Path) -> None:
    """7 晚全 miss → 命中率 = 0.0；同时三连 miss 触发 auto_disabled。"""
    audit_jsonl = tmp_path / "predictor_audit.jsonl"
    anchor = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    payload: list[dict] = []
    for day_offset in range(7):
        payload.extend(_build_night_records(
            anchor=anchor,
            day_offset=day_offset,
            n=3,
            n_hits=0,
            stage_for_hits="REM",
            stage_for_misses_predicted="REM",
            stage_for_misses_actual="AWAKE",
        ))
    _write_jsonl(audit_jsonl, payload)
    predictor = _build_predictor(audit_jsonl)
    assert predictor.hit_rate_7d() == 0.0
    assert predictor.predictor_status == "auto_disabled"


def test_hit_rate_one_hundred_when_every_record_hits(tmp_path: Path) -> None:
    """7 晚全命中 → 命中率 = 100.0；不应触发 auto_disabled。"""
    audit_jsonl = tmp_path / "predictor_audit.jsonl"
    anchor = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    payload: list[dict] = []
    for day_offset in range(7):
        payload.extend(_build_night_records(
            anchor=anchor,
            day_offset=day_offset,
            n=4,
            n_hits=4,
            stage_for_hits="DEEP",
            stage_for_misses_predicted="DEEP",
            stage_for_misses_actual="LIGHT",
        ))
    _write_jsonl(audit_jsonl, payload)
    predictor = _build_predictor(audit_jsonl)
    assert predictor.hit_rate_7d() == 100.0
    assert predictor.predictor_status == "healthy"


def test_records_with_actual_none_excluded_from_arithmetic(tmp_path: Path) -> None:
    """``actual_stage_60s_later=None`` 既不计分母也不计分子（R10.2）。"""
    audit_jsonl = tmp_path / "predictor_audit.jsonl"
    anchor = datetime.now(tz=timezone.utc) - timedelta(hours=1)
    payload: list[dict] = []
    # 7 晚，每晚 1 条 actual=None + 1 条 actual=DEEP（命中）；
    # 期望命中率 = 7/7 = 100%（None 行被忽略）。
    for day_offset in range(7):
        ts1 = anchor - timedelta(days=day_offset, minutes=0)
        ts2 = anchor - timedelta(days=day_offset, minutes=1)
        payload.append({
            "timestamp": _iso(ts1),
            "predicted_stage": "DEEP",
            "actual_stage_60s_later": None,  # 应被忽略
            "confidence": 0.5,
        })
        payload.append({
            "timestamp": _iso(ts2),
            "predicted_stage": "DEEP",
            "actual_stage_60s_later": "DEEP",
            "confidence": 0.9,
        })
    _write_jsonl(audit_jsonl, payload)
    predictor = _build_predictor(audit_jsonl)
    assert predictor.hit_rate_7d() == 100.0
    assert predictor.predictor_status == "healthy"
