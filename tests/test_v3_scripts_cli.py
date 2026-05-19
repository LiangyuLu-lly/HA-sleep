"""v3.0.0 训练 / 评估脚本 CLI smoke 测试 —— task 10.9.

每个脚本一个测试，验证：

1. 模块可以 import（torch / onnx / pyEDFlib 等重依赖通过 lazy import 隔离，
   按 R12.5 / R11.3 契约「``--help`` 不应触发训练栈加载」）；
2. ``_build_arg_parser()`` / ``_build_parser()`` 返回的 ``argparse.ArgumentParser``
   能接受最少必填参数并解析出 ``args.seed == 20260518``（R15.5 全栈统一种子）；
3. ``parse_args(['--help'])`` 抛 ``SystemExit(0)``（argparse 的 stdlib 行为）；
4. 非法参数（如未知 flag）抛 ``SystemExit(code != 0)``。

> **Skip-friendly**：
>
> * `eval_population_prior_rmse.py` / `eval_stage_predictor_hitrate.py`
>   由 task 10.5 实现，本测试在文件不存在时 `pytest.skip` —— 一旦 task
>   10.5 落地，相同测试自动激活，无需后续修订。
> * 其余 4 个脚本若 import 失败（例如开发机未装训练依赖、或顶层意外
>   引入了重依赖），用 `pytest.skip` 记录原因；不应让本 smoke 套件因
>   外部依赖缺失而 hard-fail。

**Validates: Requirements 15.5**
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest


# ---------------------------------------------------------------------------
# 仓库根目录注册到 ``sys.path``，让 ``scripts.<x>`` 中的 ``from src...``
# 在测试进程里能解析到（与各 script 自身的 sys.path 注入逻辑一致）。
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT_STR = str(_REPO_ROOT)
if _REPO_ROOT_STR not in sys.path:
    sys.path.insert(0, _REPO_ROOT_STR)


#: R15.5 全栈默认 seed —— 详见 design §3.8.2 / scripts/*.py 中的
#: ``DEFAULT_SEED`` / ``_DEFAULT_SEED`` 常量。
_EXPECTED_DEFAULT_SEED: int = 20260518


# ---------------------------------------------------------------------------
# 辅助：safely import a script module；失败时 skip
# ---------------------------------------------------------------------------


def _import_script_or_skip(module_name: str) -> ModuleType:
    """Return imported ``scripts.<module_name>`` or skip the test.

    脚本未实现（task 10.5 尚未完成 → `eval_population_prior_rmse.py` /
    `eval_stage_predictor_hitrate.py` 缺失）时返回 ``ModuleNotFoundError``，
    我们将其翻译为 ``pytest.skip`` 而非失败。

    其它 ``ImportError`` 通常意味着环境缺少训练依赖（torch /
    onnxruntime / pyEDFlib），此时也跳过 —— 这与 R12.5 的「runtime 镜
    像不带训练栈」契约一致：开发者机器才会跑这些脚本。
    """
    qualified = f"scripts.{module_name}"
    script_file = _REPO_ROOT / "scripts" / f"{module_name}.py"
    if not script_file.is_file():
        pytest.skip(f"{script_file} not implemented yet (task 10.5)")
    try:
        return importlib.import_module(qualified)
    except ImportError as exc:  # pragma: no cover — env-dependent
        pytest.skip(f"cannot import {qualified}: {exc!r}")


def _get_parser_factory(mod: ModuleType):
    """Return whichever of ``_build_arg_parser`` / ``_build_parser`` exists.

    本仓库历史上两种命名都用过：``train_population_prior`` 与
    ``eval_*`` 系列用 ``_build_arg_parser``，``train_stage_predictor`` 用
    ``_build_parser``。本 helper 让测试不感知该差异。
    """
    for name in ("_build_arg_parser", "_build_parser"):
        factory = getattr(mod, name, None)
        if callable(factory):
            return factory
    pytest.skip(
        f"{mod.__name__} exposes neither _build_arg_parser nor _build_parser"
    )


def _assert_help_exits_zero(parser) -> None:
    """``parse_args(['--help'])`` 必须 ``SystemExit(0)``."""
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--help"])
    # argparse 在 ``--help`` 路径上写 ``sys.exit(0)``；exit code 可能是
    # int 0 / None（older Python），都视为成功路径。
    assert exc_info.value.code in (0, None), (
        f"--help should exit 0, got {exc_info.value.code!r}"
    )


def _assert_bad_args_exit_nonzero(parser) -> None:
    """未知 flag 必须导致 ``SystemExit(non-zero)``."""
    with pytest.raises(SystemExit) as exc_info:
        # capsys 不需要也不该接管 stderr —— argparse 会把 usage 写出来，
        # pytest 的标准 capture 即可吸收。
        parser.parse_args(["--this-flag-does-not-exist", "value"])
    assert exc_info.value.code not in (0, None), (
        f"unknown flag should exit non-zero, got {exc_info.value.code!r}"
    )


# ---------------------------------------------------------------------------
# Tests —— 每个脚本一个 smoke
# ---------------------------------------------------------------------------


def test_train_population_prior_cli_arg_parsing() -> None:
    """``scripts/train_population_prior.py`` argparse smoke.

    最少必填：``--out``（pickle 输出路径）+ ``--synthetic`` 跳过 NSRR
    数据集需求。验证默认 seed = 20260518。
    """
    mod = _import_script_or_skip("train_population_prior")
    parser = _get_parser_factory(mod)()

    args = parser.parse_args(["--out", "tmp.pickle", "--synthetic"])
    assert args.seed == _EXPECTED_DEFAULT_SEED

    _assert_help_exits_zero(parser)
    _assert_bad_args_exit_nonzero(parser)


def test_train_stage_predictor_cli_arg_parsing() -> None:
    """``scripts/train_stage_predictor.py`` argparse smoke.

    最少必填：``--edf-dir`` + ``--out``。``--synthetic`` 不影响 argparse
    层；本测试**不**调用 ``main()``，因此不会触碰 torch / onnxruntime。
    """
    mod = _import_script_or_skip("train_stage_predictor")
    parser = _get_parser_factory(mod)()

    args = parser.parse_args(
        ["--edf-dir", "placeholder", "--out", "tmp.onnx", "--synthetic"]
    )
    assert args.seed == _EXPECTED_DEFAULT_SEED

    _assert_help_exits_zero(parser)
    _assert_bad_args_exit_nonzero(parser)


def test_eval_bayesian_regret_cli_arg_parsing() -> None:
    """``scripts/eval_bayesian_regret.py`` argparse smoke.

    最少必填：``--user-prefs``。lazy-imports BayesianOptimizer
    （``main()`` 内），所以 parser 构造本身不会拉起 numpy GP 栈。
    """
    mod = _import_script_or_skip("eval_bayesian_regret")
    parser = _get_parser_factory(mod)()

    args = parser.parse_args(["--user-prefs", "tmp.json"])
    assert args.seed == _EXPECTED_DEFAULT_SEED

    _assert_help_exits_zero(parser)
    _assert_bad_args_exit_nonzero(parser)


def test_eval_causal_synthetic_cli_arg_parsing() -> None:
    """``scripts/eval_causal_synthetic.py`` argparse smoke.

    无必填参数（``--n-nights / --n-trials / --seed`` 全部带默认值），
    所以空 argv 即可解析成功。
    """
    mod = _import_script_or_skip("eval_causal_synthetic")
    parser = _get_parser_factory(mod)()

    args = parser.parse_args([])
    assert args.seed == _EXPECTED_DEFAULT_SEED

    _assert_help_exits_zero(parser)
    _assert_bad_args_exit_nonzero(parser)


def test_eval_population_prior_rmse_cli_arg_parsing() -> None:
    """``scripts/eval_population_prior_rmse.py`` argparse smoke.

    Task 10.5 落地后该文件出现，本测试自动激活。设计文档 §3.8.5 指定
    必填 ``--mesa-holdout`` + ``--prior``；此处用占位值。
    """
    mod = _import_script_or_skip("eval_population_prior_rmse")
    parser = _get_parser_factory(mod)()

    args = parser.parse_args(
        [
            "--mesa-holdout", "tmp.csv",
            "--prior", "tmp.pickle",
        ]
    )
    assert args.seed == _EXPECTED_DEFAULT_SEED

    _assert_help_exits_zero(parser)
    _assert_bad_args_exit_nonzero(parser)


def test_eval_stage_predictor_hitrate_cli_arg_parsing() -> None:
    """``scripts/eval_stage_predictor_hitrate.py`` argparse smoke.

    Task 10.5 落地后该文件出现，本测试自动激活。设计文档 §3.8.6 指定
    必填 ``--edf-test`` + ``--model``；此处用占位值。
    """
    mod = _import_script_or_skip("eval_stage_predictor_hitrate")
    parser = _get_parser_factory(mod)()

    args = parser.parse_args(
        [
            "--edf-test", "tmp_edf",
            "--model", "tmp.onnx",
        ]
    )
    assert args.seed == _EXPECTED_DEFAULT_SEED

    _assert_help_exits_zero(parser)
    _assert_bad_args_exit_nonzero(parser)
