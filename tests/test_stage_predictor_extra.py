"""Extra unit tests for ``src.stage_predictor`` to lift coverage to ≥ 95 %.

This file complements ``test_stage_predictor.py`` (Property 16 + task
5.6 surface) and ``test_stage_predictor_audit.py`` (Properties 9 / 9b)
by drilling into the small defensive branches that were left
uncovered: helper functions, ``try_load`` happy / missing-file paths,
``_load_session`` lazy-import fallbacks, ``predict`` happy /
exception / cool-down-recovery paths, ``maybe_anticipate`` error
branches, ``record_hit`` end-to-end, ``_prune_audit`` malformed-line
handling, ``hit_rate_7d`` cache hit, and the various status / property
surfaces.

All tests construct :class:`StagePredictor` with a ``model_path`` that
either does not exist or points to a tmp-path artifact, then either
monkey-patch ``_load_session`` to return a fake :class:`onnxruntime`
:class:`InferenceSession` or write the audit JSONL directly. No real
``onnxruntime`` ONNX loading occurs.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

import src.stage_predictor as sp_module
from src.data_structures import SleepStage
from src.stage_predictor import (
    HitRecord,
    PredictorInput,
    PredictorOutput,
    StagePredictor,
    _argmax_stage_name,
    _parse_iso_timestamp,
    _validate_probabilities,
)


# ---------------------------------------------------------------------------
# Local helpers
# ---------------------------------------------------------------------------


_FULL_WINDOW_SAMPLES: int = 300


def _full_channel(value: float = 0.5) -> tuple[float, ...]:
    """Return a 300-sample tuple full of *value* (no ``None``)."""
    return tuple(value for _ in range(_FULL_WINDOW_SAMPLES))


def _full_window() -> PredictorInput:
    """Build a fully-populated :class:`PredictorInput` window."""
    return PredictorInput(
        hrv_ms=_full_channel(),
        motion_au=_full_channel(),
        breathing_rate_bpm=_full_channel(),
    )


def _build_predictor(
    tmp_path: Path,
    *,
    audit_name: str = "predictor_audit.jsonl",
    **kwargs: Any,
) -> StagePredictor:
    """Construct a :class:`StagePredictor` rooted at *tmp_path*."""
    return StagePredictor(
        model_path=tmp_path / "missing_stage_predictor.onnx",
        audit_jsonl=tmp_path / audit_name,
        **kwargs,
    )


class _StubSession:
    """ONNX-runtime stand-in returning a fixed probability vector.

    ``probs`` (length 4) is what each ``run`` invocation returns. The
    shape mirrors :class:`onnxruntime.InferenceSession` outputs — a
    list with one ``ndarray`` shaped ``(1, 4)``.
    """

    def __init__(
        self,
        probs: tuple[float, float, float, float] = (0.1, 0.2, 0.6, 0.1),
        *,
        raise_on_run: BaseException | None = None,
    ) -> None:
        self._probs = probs
        self._raise = raise_on_run
        self.run_calls = 0

    def get_inputs(self) -> list[Any]:
        shim = type("InputShim", (), {"name": "input"})()
        return [shim]

    def run(self, _output_names: Any, _feed: Any) -> list[Any]:
        self.run_calls += 1
        if self._raise is not None:
            raise self._raise
        return [np.array([list(self._probs)], dtype=np.float32)]


# ---------------------------------------------------------------------------
# Helper functions: _argmax_stage_name + _parse_iso_timestamp
# ---------------------------------------------------------------------------


def test_argmax_stage_name_returns_highest_probability_stage() -> None:
    """``_argmax_stage_name`` returns the canonical stage name for argmax.

    Each of the four canonical positions is tested explicitly so the
    AWAKE / LIGHT / DEEP / REM mapping is locked against accidental
    permutation.
    """
    cases = [
        ((0.7, 0.1, 0.1, 0.1), "AWAKE"),
        ((0.1, 0.7, 0.1, 0.1), "LIGHT"),
        ((0.1, 0.1, 0.7, 0.1), "DEEP"),
        ((0.1, 0.1, 0.1, 0.7), "REM"),
    ]
    for probs, expected in cases:
        out = PredictorOutput(
            p_awake=probs[0],
            p_light=probs[1],
            p_deep=probs[2],
            p_rem=probs[3],
            confidence=max(probs),
            inference_ms=1.0,
            is_valid=True,
        )
        assert _argmax_stage_name(out) == expected, (
            f"argmax stage mismatch for probs={probs}: "
            f"got {_argmax_stage_name(out)!r}, expected {expected!r}"
        )


def test_parse_iso_timestamp_accepts_z_suffix_and_offset() -> None:
    """``_parse_iso_timestamp`` accepts both ``Z`` and ``+00:00`` suffixes."""
    # ``Z`` shorthand (must be normalized to ``+00:00`` internally).
    z_suffix = "2025-01-15T12:34:56Z"
    parsed_z = _parse_iso_timestamp(z_suffix)
    assert parsed_z is not None
    # ``+00:00`` long form should yield the same Unix seconds.
    plus_form = "2025-01-15T12:34:56+00:00"
    parsed_plus = _parse_iso_timestamp(plus_form)
    assert parsed_plus is not None
    assert abs(parsed_z - parsed_plus) < 1e-6


def test_parse_iso_timestamp_returns_none_on_garbage_string() -> None:
    """Malformed strings yield ``None`` instead of raising (R10.2)."""
    assert _parse_iso_timestamp("not-a-timestamp") is None
    assert _parse_iso_timestamp("") is None


def test_parse_iso_timestamp_returns_none_on_partial_iso_input() -> None:
    """Partial / out-of-range ISO strings yield ``None`` (``ValueError``)."""
    # Month 13 — :func:`datetime.fromisoformat` raises ``ValueError``.
    assert _parse_iso_timestamp("2025-13-01T00:00:00+00:00") is None
    # Looks ISO-ish but with bad day component.
    assert _parse_iso_timestamp("2025-02-30") is None


# ---------------------------------------------------------------------------
# try_load — happy path + missing-file branch
# ---------------------------------------------------------------------------


def test_try_load_returns_predictor_when_artifact_under_size_cap(
    tmp_path: Path,
) -> None:
    """``try_load`` returns a live :class:`StagePredictor` for a valid blob.

    We only need ``onnxruntime`` to *probe-import* — the actual
    :class:`InferenceSession` is built lazily on the first
    :meth:`predict` call (R11.3). A 1-byte file is well under the
    80 KB R9.2 cap so the size check passes too.
    """
    pytest.importorskip("onnxruntime")
    model_path = tmp_path / "stage_predictor.onnx"
    model_path.write_bytes(b"\x00")
    audit_path = tmp_path / "predictor_audit.jsonl"

    result = StagePredictor.try_load(
        model_path=model_path,
        audit_jsonl=audit_path,
    )
    assert isinstance(result, StagePredictor)
    # Surfacing the audit path through the constructed predictor proves
    # we hit the ``cls(...)`` return at the end of try_load.
    assert result._audit_jsonl == audit_path
    assert result._model_path == model_path


def test_try_load_returns_none_when_model_path_missing(
    tmp_path: Path,
) -> None:
    """``try_load`` returns ``None`` when the artifact file is absent."""
    pytest.importorskip("onnxruntime")
    missing_path = tmp_path / "no_such_model.onnx"
    audit_path = tmp_path / "predictor_audit.jsonl"
    assert not missing_path.exists()

    result = StagePredictor.try_load(
        model_path=missing_path,
        audit_jsonl=audit_path,
    )
    assert result is None


# ---------------------------------------------------------------------------
# _load_session — happy / cached / failure / sticky-failure branches
# ---------------------------------------------------------------------------


def test_load_session_returns_cached_session_on_repeat_call(
    tmp_path: Path,
) -> None:
    """Repeat calls reuse the already-cached ``_session`` reference."""
    predictor = _build_predictor(tmp_path)
    sentinel = object()
    predictor._session = sentinel  # type: ignore[assignment]
    # Same identity returned on subsequent calls — no re-import.
    assert predictor._load_session() is sentinel
    assert predictor._load_session() is sentinel


def test_load_session_short_circuits_after_previous_failure(
    tmp_path: Path,
) -> None:
    """``_session_load_failed`` flag short-circuits later calls to ``None``."""
    predictor = _build_predictor(tmp_path)
    predictor._session_load_failed = True
    assert predictor._load_session() is None


def test_load_session_returns_real_session_with_stub_onnxruntime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lazy import + InferenceSession construction flows happily.

    We swap in a fake ``onnxruntime`` module exposing an
    :class:`InferenceSession` that records its constructor arguments,
    then assert ``_load_session`` returns the constructed instance.
    This covers the success branch lines 360–365 + 374.
    """
    captured: dict[str, Any] = {}

    class _FakeSession:
        def __init__(self, model_path: str, *, providers: list[str]) -> None:
            captured["model_path"] = model_path
            captured["providers"] = providers

    fake_module = type("FakeOrt", (), {"InferenceSession": _FakeSession})
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_module)

    predictor = _build_predictor(tmp_path)
    session = predictor._load_session()
    assert isinstance(session, _FakeSession)
    assert captured["model_path"] == str(predictor._model_path)
    assert captured["providers"] == ["CPUExecutionProvider"]
    # Once cached, the second call returns the same instance.
    assert predictor._load_session() is session


def test_load_session_sets_failure_flag_on_constructor_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A constructor raise must latch ``_session_load_failed=True``.

    Subsequent calls must short-circuit to ``None`` without trying
    the import again — covers the exception branch (lines 366–373).
    """

    class _BoomSession:
        def __init__(self, *_a: Any, **_kw: Any) -> None:
            raise RuntimeError("model corrupted")

    fake_module = type("FakeOrt", (), {"InferenceSession": _BoomSession})
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_module)

    predictor = _build_predictor(tmp_path)
    assert predictor._load_session() is None
    assert predictor._session_load_failed is True
    assert predictor._session is None
    # Sticky: subsequent call short-circuits (covers line 358–359).
    assert predictor._load_session() is None


# ---------------------------------------------------------------------------
# predict — happy path, exception path, cool-down recovery
# ---------------------------------------------------------------------------


async def test_predict_returns_valid_output_on_fast_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful fast inference yields a valid :class:`PredictorOutput`.

    Covers:

    * lines 421–430 (``np.asarray`` build of the input tensor),
    * lines 432–435 (``session.get_inputs`` + ``session.run``),
    * lines 440–449 (under-budget branch + error reset),
    * lines 451–470 (probability extraction + ``PredictorOutput`` build).
    """
    stub = _StubSession(probs=(0.1, 0.2, 0.6, 0.1))
    predictor = _build_predictor(tmp_path)
    monkeypatch.setattr(predictor, "_load_session", lambda: stub)

    out = await predictor.predict(_full_window())
    assert out is not None
    assert out.is_valid is True
    assert out.p_deep == pytest.approx(0.6)
    assert out.confidence == pytest.approx(0.6)
    # Successful inference resets the error counter.
    assert predictor.error_count == 0
    assert stub.run_calls == 1


async def test_predict_resets_error_count_after_a_fast_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-existing errors clear once a fast inference completes (line 449)."""
    stub = _StubSession()
    predictor = _build_predictor(tmp_path)
    predictor._error_count = 2  # simulate two prior errors
    monkeypatch.setattr(predictor, "_load_session", lambda: stub)

    out = await predictor.predict(_full_window())
    assert out is not None
    assert predictor.error_count == 0


async def test_predict_records_error_on_session_run_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``session.run`` raising bumps ``error_count`` and returns ``None``.

    Covers lines 432–438 (the ``except Exception`` branch around
    ``session.run``). Three consecutive exceptions also latch the
    cool-down at 1 hour (R9.4).
    """
    stub = _StubSession(raise_on_run=RuntimeError("ort broke"))
    predictor = _build_predictor(tmp_path)
    monkeypatch.setattr(predictor, "_load_session", lambda: stub)

    out = await predictor.predict(_full_window())
    assert out is None
    assert predictor.error_count == 1
    assert predictor.disabled_until == 0.0  # not yet latched


async def test_predict_returns_none_when_session_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_load_session`` returning ``None`` short-circuits ``predict``.

    Covers lines 417–419: when the lazy session-load fails (e.g.
    ``onnxruntime`` is uninstalled or the artifact is missing), the
    predict path bails out without bumping ``error_count``.
    """
    predictor = _build_predictor(tmp_path)
    monkeypatch.setattr(predictor, "_load_session", lambda: None)

    out = await predictor.predict(_full_window())
    assert out is None
    assert predictor.error_count == 0


async def test_predict_recovers_after_cool_down_window_expires(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An expired ``disabled_until`` window resets the error budget.

    Covers lines 411–413: the post-cool-down recovery branch where
    ``disabled_until`` is reset to ``0.0`` and ``error_count`` is
    cleared so the predictor gets a fresh three-strike budget.
    """
    stub = _StubSession()
    predictor = _build_predictor(tmp_path)
    monkeypatch.setattr(predictor, "_load_session", lambda: stub)

    # Simulate a previous cool-down whose window has just expired.
    predictor._error_count = 3
    predictor._disabled_until = time.time() - 1.0

    out = await predictor.predict(_full_window())
    assert out is not None
    # Recovery branch resets both counters.
    assert predictor.error_count == 0
    assert predictor.disabled_until == 0.0


async def test_predict_returns_invalid_output_when_session_emits_short_vector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Short ONNX outputs are NaN-padded and flagged ``is_valid=False``.

    Covers the ``i < flat.size else NaN`` padding on line 454 and the
    ``is_valid=False`` ⇒ ``confidence=0.0`` short-circuit on line 461.
    """

    class _ShortSession(_StubSession):
        def run(self, _names: Any, _feed: Any) -> list[Any]:
            self.run_calls += 1
            # Only two probabilities: pads the remainder with NaN.
            return [np.array([[0.5, 0.5]], dtype=np.float32)]

    stub = _ShortSession()
    predictor = _build_predictor(tmp_path)
    monkeypatch.setattr(predictor, "_load_session", lambda: stub)

    out = await predictor.predict(_full_window())
    assert out is not None
    assert out.is_valid is False
    assert out.confidence == 0.0


async def test_predict_treats_inference_exactly_at_budget_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inference at the budget boundary (``== max``) is **not** an error.

    The ``> max_inference_ms`` check uses strict ``>``; a measurement
    landing exactly at the budget should pass through. We pin
    ``time.perf_counter`` so the elapsed wall time is exactly
    ``max_inference_ms``.
    """
    stub = _StubSession()
    predictor = _build_predictor(tmp_path, max_inference_ms=50.0)
    monkeypatch.setattr(predictor, "_load_session", lambda: stub)

    perf_clock = iter([0.0, 0.05])  # 50.0 ms
    monkeypatch.setattr(
        sp_module.time, "perf_counter", lambda: next(perf_clock),
    )

    out = await predictor.predict(_full_window())
    assert out is not None
    assert predictor.error_count == 0


# ---------------------------------------------------------------------------
# maybe_anticipate — error branches
# ---------------------------------------------------------------------------


async def test_maybe_anticipate_swallows_attribute_error_from_controller(
    tmp_path: Path,
) -> None:
    """A controller missing ``dispatch_with_lookahead`` is logged and skipped.

    Covers lines 543–548: the ``except AttributeError`` branch that
    documents the task-5.3 lazy-binding pre-condition.
    """
    predictor = _build_predictor(tmp_path)

    class _NoDispatchController:
        async def dispatch_with_lookahead(
            self, *, stage: SleepStage, lead_seconds: int,
        ) -> None:
            raise AttributeError("dispatch_with_lookahead missing")

    out = PredictorOutput(
        p_awake=0.05, p_light=0.05, p_deep=0.85, p_rem=0.05,
        confidence=0.85, inference_ms=10.0, is_valid=True,
    )
    # Should not raise.
    await predictor.maybe_anticipate(
        current_stage=SleepStage.LIGHT,
        predicted=out,
        controller=_NoDispatchController(),  # type: ignore[arg-type]
    )


async def test_maybe_anticipate_swallows_runtime_error_from_controller(
    tmp_path: Path,
) -> None:
    """An unexpected runtime error from ``dispatch_with_lookahead`` is logged.

    Covers lines 549–553: the broad ``except Exception`` branch.
    """
    predictor = _build_predictor(tmp_path)
    controller = MagicMock()
    controller.dispatch_with_lookahead = AsyncMock(
        side_effect=RuntimeError("HA unavailable"),
    )
    out = PredictorOutput(
        p_awake=0.05, p_light=0.05, p_deep=0.85, p_rem=0.05,
        confidence=0.85, inference_ms=10.0, is_valid=True,
    )
    await predictor.maybe_anticipate(
        current_stage=SleepStage.LIGHT,
        predicted=out,
        controller=controller,
    )
    # Confirms we entered the dispatch path despite the raise.
    assert controller.dispatch_with_lookahead.call_count == 1


async def test_maybe_anticipate_skips_when_argmax_is_not_deep(
    tmp_path: Path,
) -> None:
    """Argmax = LIGHT must not trigger early DEEP dispatch (line 532–533)."""
    predictor = _build_predictor(tmp_path)
    controller = MagicMock()
    controller.dispatch_with_lookahead = AsyncMock(return_value=None)
    out = PredictorOutput(
        p_awake=0.05, p_light=0.85, p_deep=0.05, p_rem=0.05,
        confidence=0.85, inference_ms=10.0, is_valid=True,
    )
    await predictor.maybe_anticipate(
        current_stage=SleepStage.LIGHT,
        predicted=out,
        controller=controller,
    )
    assert controller.dispatch_with_lookahead.call_count == 0


# ---------------------------------------------------------------------------
# record_hit — full append + prune cycle
# ---------------------------------------------------------------------------


async def test_record_hit_appends_jsonl_line_and_invalidates_cache(
    tmp_path: Path,
) -> None:
    """``record_hit`` writes a JSONL row and clears the hit-rate cache.

    Covers lines 574–588 (the full ``record_hit`` body).
    """
    predictor = _build_predictor(tmp_path)
    # Pre-seed a stale cache so we can assert it's invalidated.
    predictor._hit_rate_cache = 88.0
    predictor._hit_rate_cache_ts = time.time()

    await predictor.record_hit(
        predicted_stage="DEEP",
        confidence=0.91,
        actual_stage_after_60s="DEEP",
    )

    raw = (tmp_path / "predictor_audit.jsonl").read_text(encoding="utf-8")
    lines = [ln for ln in raw.splitlines() if ln]
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["predicted_stage"] == "DEEP"
    assert payload["actual_stage_60s_later"] == "DEEP"
    assert payload["confidence"] == pytest.approx(0.91)
    # ``timestamp`` must be ISO-8601 with timezone (round-trippable).
    assert _parse_iso_timestamp(payload["timestamp"]) is not None

    # Cache invalidated.
    assert predictor._hit_rate_cache is None
    assert predictor._hit_rate_cache_ts == 0.0


async def test_record_hit_prunes_entries_older_than_seven_days(
    tmp_path: Path,
) -> None:
    """An old row is dropped on the next ``record_hit`` (R10.2 pruning)."""
    audit = tmp_path / "predictor_audit.jsonl"
    # Manually seed an entry from 8 days ago.
    old_ts = (
        datetime.now(timezone.utc) - timedelta(days=8)
    ).isoformat()
    audit.write_text(
        json.dumps({
            "timestamp": old_ts,
            "predicted_stage": "DEEP",
            "actual_stage_60s_later": "DEEP",
            "confidence": 0.5,
        }) + "\n",
        encoding="utf-8",
    )

    predictor = _build_predictor(tmp_path)
    await predictor.record_hit(
        predicted_stage="LIGHT",
        confidence=0.7,
        actual_stage_after_60s="LIGHT",
    )

    rows = [
        json.loads(ln)
        for ln in audit.read_text(encoding="utf-8").splitlines()
        if ln
    ]
    # Only the freshly appended row survives.
    assert len(rows) == 1
    assert rows[0]["predicted_stage"] == "LIGHT"


# ---------------------------------------------------------------------------
# _prune_audit — defensive branches
# ---------------------------------------------------------------------------


def test_prune_audit_no_op_when_audit_file_missing(tmp_path: Path) -> None:
    """``_prune_audit`` swallows ``FileNotFoundError`` (line 595–596)."""
    predictor = _build_predictor(tmp_path)
    assert not predictor._audit_jsonl.exists()
    predictor._prune_audit(time.time())  # must not raise
    # File still doesn't exist — pruning didn't accidentally create it.
    assert not predictor._audit_jsonl.exists()


def test_prune_audit_no_op_when_audit_file_empty(tmp_path: Path) -> None:
    """``_prune_audit`` returns early on a zero-byte audit file (line 597–598)."""
    predictor = _build_predictor(tmp_path)
    predictor._audit_jsonl.write_text("", encoding="utf-8")
    predictor._prune_audit(time.time())
    # Still empty.
    assert predictor._audit_jsonl.read_text(encoding="utf-8") == ""


def test_prune_audit_keeps_malformed_lines_for_debug_visibility(
    tmp_path: Path,
) -> None:
    """Malformed JSON lines survive pruning (line 606–609)."""
    audit = tmp_path / "predictor_audit.jsonl"
    fresh_ts = datetime.now(timezone.utc).isoformat()
    audit.write_text(
        "this is not json\n"
        + json.dumps({
            "timestamp": fresh_ts,
            "predicted_stage": "DEEP",
            "actual_stage_60s_later": "DEEP",
            "confidence": 0.5,
        })
        + "\n",
        encoding="utf-8",
    )
    # Seed an extra old row to force the «any_dropped» branch (line 615+).
    old_ts = (
        datetime.now(timezone.utc) - timedelta(days=10)
    ).isoformat()
    with audit.open("a", encoding="utf-8") as fh:
        fh.write(
            json.dumps({
                "timestamp": old_ts,
                "predicted_stage": "DEEP",
                "actual_stage_60s_later": "DEEP",
                "confidence": 0.5,
            }) + "\n"
        )

    predictor = _build_predictor(tmp_path)
    predictor._prune_audit(time.time())

    surviving = [
        ln
        for ln in audit.read_text(encoding="utf-8").splitlines()
        if ln
    ]
    # Malformed line preserved verbatim.
    assert "this is not json" in surviving
    # Fresh row preserved; old row dropped.
    assert any("DEEP" in s and fresh_ts in s for s in surviving)
    assert not any(old_ts in s for s in surviving)


def test_prune_audit_skips_blank_lines(tmp_path: Path) -> None:
    """Empty lines are skipped without raising (line 602–603)."""
    audit = tmp_path / "predictor_audit.jsonl"
    fresh_ts = datetime.now(timezone.utc).isoformat()
    # Force at least one drop (old entry) so pruning rewrites the file.
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    audit.write_text(
        "\n\n"
        + json.dumps({
            "timestamp": fresh_ts,
            "predicted_stage": "DEEP",
            "actual_stage_60s_later": "DEEP",
            "confidence": 0.5,
        }) + "\n"
        + json.dumps({
            "timestamp": old_ts,
            "predicted_stage": "LIGHT",
            "actual_stage_60s_later": "LIGHT",
            "confidence": 0.5,
        }) + "\n",
        encoding="utf-8",
    )
    predictor = _build_predictor(tmp_path)
    predictor._prune_audit(time.time())

    surviving = [
        ln
        for ln in audit.read_text(encoding="utf-8").splitlines()
        if ln
    ]
    # Old row dropped, fresh row survives, blanks gone.
    assert len(surviving) == 1
    assert fresh_ts in surviving[0]


# ---------------------------------------------------------------------------
# hit_rate_7d — cache hit + missing-file branches
# ---------------------------------------------------------------------------


def test_hit_rate_7d_returns_cached_value_within_ttl(tmp_path: Path) -> None:
    """A cached rate is returned on calls within the 1-hour TTL (line 637–641)."""
    predictor = _build_predictor(tmp_path)
    predictor._hit_rate_cache = 87.5
    predictor._hit_rate_cache_ts = time.time()  # very fresh

    # Without an audit file on disk: returning the cache proves we
    # short-circuited before the read.
    assert predictor.hit_rate_7d() == 87.5


def test_hit_rate_7d_returns_none_when_audit_file_absent(
    tmp_path: Path,
) -> None:
    """Missing audit file → ``None`` and cache slot reset (line 643–648)."""
    predictor = _build_predictor(tmp_path)
    assert not predictor._audit_jsonl.exists()
    assert predictor.hit_rate_7d() is None
    # Cache is set to (None, now) so the *next* call doesn't re-read either.
    assert predictor._hit_rate_cache is None
    assert predictor._hit_rate_cache_ts > 0.0


def test_hit_rate_7d_returns_none_when_audit_file_empty(
    tmp_path: Path,
) -> None:
    """Empty audit file → ``None`` (line 649–652)."""
    predictor = _build_predictor(tmp_path)
    predictor._audit_jsonl.write_text("", encoding="utf-8")
    assert predictor.hit_rate_7d() is None


def test_hit_rate_7d_skips_malformed_lines_blank_and_old(
    tmp_path: Path,
) -> None:
    """Non-JSON lines, blanks, old rows, and ``actual=None`` are filtered out.

    Covers lines 658–670: blank ``continue``, ``json.JSONDecodeError``
    ``continue``, ``ts < cutoff`` ``continue``, and ``actual is None``
    ``continue`` paths inside the rolling-window aggregator.
    """
    audit = tmp_path / "predictor_audit.jsonl"
    now = datetime.now(timezone.utc) - timedelta(hours=1)

    payload = ["", "not-json"]  # blank + JSONDecodeError lines

    # 7 distinct fresh nights, each with one ``actual=None`` row plus
    # one ``actual=DEEP`` (hit) so the rolling rate is 100 %.
    for day_offset in range(7):
        ts1 = (now - timedelta(days=day_offset, minutes=0)).isoformat()
        ts2 = (now - timedelta(days=day_offset, minutes=1)).isoformat()
        payload.append(json.dumps({
            "timestamp": ts1,
            "predicted_stage": "DEEP",
            "actual_stage_60s_later": None,  # filtered
            "confidence": 0.5,
        }))
        payload.append(json.dumps({
            "timestamp": ts2,
            "predicted_stage": "DEEP",
            "actual_stage_60s_later": "DEEP",
            "confidence": 0.9,
        }))

    # An old row that should be excluded by the cutoff.
    old_ts = (now - timedelta(days=20)).isoformat()
    payload.append(json.dumps({
        "timestamp": old_ts,
        "predicted_stage": "AWAKE",
        "actual_stage_60s_later": "DEEP",
        "confidence": 0.1,
    }))

    audit.write_text("\n".join(payload) + "\n", encoding="utf-8")
    predictor = _build_predictor(tmp_path)
    rate = predictor.hit_rate_7d()
    assert rate == 100.0


# ---------------------------------------------------------------------------
# _update_auto_disable — defensive branches
# ---------------------------------------------------------------------------


def test_update_auto_disable_short_circuits_when_already_latched(
    tmp_path: Path,
) -> None:
    """No re-evaluation when ``_auto_disabled`` is already ``True`` (line 708–709)."""
    predictor = _build_predictor(tmp_path)
    predictor._auto_disabled = True
    # Even with three perfectly healthy nights we shouldn't reset the latch.
    perfect = {f"2025-01-0{i + 1}": [True, True] for i in range(3)}
    predictor._update_auto_disable(perfect)
    assert predictor._auto_disabled is True


def test_update_auto_disable_returns_when_fewer_than_three_nights(
    tmp_path: Path,
) -> None:
    """Insufficient distinct nights leave the latch untouched (line 710–711)."""
    predictor = _build_predictor(tmp_path)
    predictor._update_auto_disable({"2025-01-01": [False]})
    assert predictor._auto_disabled is False


def test_update_auto_disable_skips_when_a_recent_night_has_no_records(
    tmp_path: Path,
) -> None:
    """An empty bucket on a recent night breaks the «all bad» streak.

    Covers the ``if not bucket`` branch (lines 718–720). The
    aggregator never produces empty buckets in production — this test
    drives the helper directly to lock down the defensive code path.
    """
    predictor = _build_predictor(tmp_path)
    per_night = {
        "2025-01-01": [False],   # bad
        "2025-01-02": [],        # empty bucket trips the early break
        "2025-01-03": [False],   # bad
    }
    predictor._update_auto_disable(per_night)
    assert predictor._auto_disabled is False


def test_update_auto_disable_does_not_latch_when_one_recent_night_is_healthy(
    tmp_path: Path,
) -> None:
    """A single healthy night in the recent-3 window blocks the latch."""
    predictor = _build_predictor(tmp_path)
    per_night = {
        "2025-01-01": [False, False, False],   # 0 % bad
        "2025-01-02": [True, True, True, True],  # 100 % good
        "2025-01-03": [False, False, False],   # 0 % bad
    }
    predictor._update_auto_disable(per_night)
    assert predictor._auto_disabled is False


# ---------------------------------------------------------------------------
# Status / property surface
# ---------------------------------------------------------------------------


def test_predictor_status_returns_degraded_during_cool_down(
    tmp_path: Path,
) -> None:
    """``predictor_status == "degraded"`` while the cool-down is active."""
    predictor = _build_predictor(tmp_path)
    predictor._disabled_until = time.time() + 3600
    assert predictor.predictor_status == "degraded"


def test_predictor_status_returns_healthy_default(tmp_path: Path) -> None:
    """A fresh predictor reports ``"healthy"``."""
    predictor = _build_predictor(tmp_path)
    assert predictor.predictor_status == "healthy"


def test_should_disable_flips_at_three_consecutive_errors(
    tmp_path: Path,
) -> None:
    """``should_disable`` is ``True`` iff ``error_count >= 3`` (R11.3)."""
    predictor = _build_predictor(tmp_path)
    assert predictor.should_disable is False
    predictor._error_count = 2
    assert predictor.should_disable is False
    predictor._error_count = 3
    assert predictor.should_disable is True
    predictor._error_count = 5
    assert predictor.should_disable is True


def test_disabled_until_property_returns_internal_timestamp(
    tmp_path: Path,
) -> None:
    """``disabled_until`` mirrors ``_disabled_until`` verbatim."""
    predictor = _build_predictor(tmp_path)
    assert predictor.disabled_until == 0.0
    predictor._disabled_until = 1234567890.0
    assert predictor.disabled_until == 1234567890.0


def test_error_count_property_returns_internal_counter(
    tmp_path: Path,
) -> None:
    """``error_count`` mirrors ``_error_count`` verbatim."""
    predictor = _build_predictor(tmp_path)
    assert predictor.error_count == 0
    predictor._error_count = 4
    assert predictor.error_count == 4


# ---------------------------------------------------------------------------
# HitRecord dataclass — surface coverage (re-exported for downstream use)
# ---------------------------------------------------------------------------


def test_hit_record_dataclass_round_trip() -> None:
    """The :class:`HitRecord` dataclass holds the four documented fields."""
    record = HitRecord(
        timestamp="2025-01-15T12:34:56+00:00",
        predicted_stage="DEEP",
        actual_stage_60s_later="DEEP",
        confidence=0.91,
    )
    assert record.predicted_stage == "DEEP"
    assert record.actual_stage_60s_later == "DEEP"
    assert record.confidence == pytest.approx(0.91)


# ---------------------------------------------------------------------------
# Validate _validate_probabilities edge cases (already touched by Property 16
# but kept here for explicit branch coverage of the early-exit path).
# ---------------------------------------------------------------------------


def test_validate_probabilities_rejects_out_of_range_value() -> None:
    """A negative entry trips the ``[0, 1]`` guard (returns ``False``)."""
    assert _validate_probabilities(-0.1, 0.4, 0.4, 0.3) is False


def test_validate_probabilities_rejects_over_one_value() -> None:
    """A > 1 entry trips the ``[0, 1]`` guard (returns ``False``)."""
    assert _validate_probabilities(0.0, 0.0, 1.5, 0.0) is False


def test_validate_probabilities_rejects_when_sum_far_from_one() -> None:
    """Sum drift beyond 0.01 fails the simplex check."""
    assert _validate_probabilities(0.1, 0.1, 0.1, 0.1) is False
    # Within tolerance still passes.
    assert _validate_probabilities(0.25, 0.25, 0.25, 0.25) is True
    # Sum 1.005 — within 0.01 tolerance.
    assert _validate_probabilities(0.255, 0.25, 0.25, 0.25) is True
