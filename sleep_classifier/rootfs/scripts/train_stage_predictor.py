"""训练 / 导出 EMST stage predictor 的小型 ONNX 模型（v3.0.0 R9 / R10）.

此脚本是 ``algorithmic-moat-v3.0.0`` 任务 10.2 的实现：把一个微型
transformer / MLP 训练并导出成 INT8 量化后 ≤ 80 KB 的 ONNX，供运行时
``src.stage_predictor.StagePredictor`` 加载。

工作模式
--------

脚本支持两种互不冲突的模式：

* **真训练模式**（默认）—— 当 ``--edf-dir`` 指向一个真实的
  PhysioNet Sleep-EDF 解压目录时，应当走完整的 EDF → (HRV / motion /
  breathing) → 4-fold CV → ONNX export 流水线。本期作为 *scaffold*
  实现：检测到 EDF 文件后给出 "尚未实现" 的清晰错误并退出 ``1``。
  重型科学计算流水线在 v3.0.0 后续 PR 落地，**不在 task 10.2 范围内**。
* **合成模式**（``--synthetic``）—— 跳过真实 PSG 数据，构造一个
  ``Linear(900, 4) → Softmax`` 极小模型（~3.6 K 参数），权重用确定性
  伪随机数填充，直接 ``torch.onnx.export``。INT8 量化前已 ≤ 80 KB；
  开启 ``--quantize`` 后体积会进一步缩小。本模式的目的是让 CI / 集成
  测试在没有真实 EDF 数据的情况下也能拿到一个**结构合法**的 ONNX，
  从而验证：

  - ``onnxruntime.InferenceSession`` 能加载（`R9.2`）；
  - 单次推理 ≤ 50 ms（`R9.4`）；
  - 输入 ``(1, 3, 300) float32``、输出 ``(1, 4)`` softmax（`R9.3` /
    `R9.5`）；
  - 文件大小 ≤ 80 KB（`R9.2`）。

  合成模型**不**输出有意义的睡眠分期；它只用于打通运行时管道。

依赖与镜像隔离
--------------

``torch`` 和 ``onnx`` 来自 ``requirements-train.txt``，只在开发者机器
上手动安装；运行时镜像（``requirements-runtime.txt``）只装
``onnxruntime``。脚本把 ``torch`` / ``onnx`` 的 import 推迟到
``main()`` 内部，``--help`` / 文档生成在没有 torch 的机器上仍可执行。

CLI
---

::

    python scripts/train_stage_predictor.py --help

    # 合成模式（CI / 集成测试用）
    python scripts/train_stage_predictor.py \
        --synthetic --edf-dir /unused --out /tmp/stage_predictor.onnx

    # 真训练模式（开发者机器，需 ``requirements-train.txt`` 全套）
    python scripts/train_stage_predictor.py \
        --edf-dir ~/data/sleep-edfx --out training_config/stage_predictor.onnx \
        --quantize

退出码
------

* ``0`` —— 全部检查通过（导出 + 加载 + 推理 + 体积）。
* ``1`` —— 模型体积 > 80 KB（`R9.2`），或真训练模式下数据 schema /
  scaffold 未实现等致命错误。
* ``2`` —— ``onnxruntime.InferenceSession`` 加载失败。
* ``3`` —— 单次推理 > 50 ms（`R9.4`）。

:Design reference: design.md §3.8.2
:Requirements: 9.1, 9.2, 9.3, 9.4, 10.5, 12.5, 15.5
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# 注意：torch / onnx / onnxruntime 都**不能**在模块顶部 import，
# 否则 ``--help`` 也会要求安装这些重依赖；与设计文档的 R12.5 / R11.3
# 隔离契约相违背。

logger = logging.getLogger("train_stage_predictor")


# ---------------------------------------------------------------------------
# 协议常量 —— 与 src/stage_predictor.py 必须保持一致
# ---------------------------------------------------------------------------

# (R9.2) INT8 量化后 ONNX 必须 ≤ 80 KB。
_MAX_MODEL_BYTES: int = 80 * 1024

# (R9.4) 单次推理 wall-clock 上限 50 ms。
_MAX_INFERENCE_MS: float = 50.0

# (R9.3) 输入窗口长度：5 分钟 × 1 Hz = 300 个样本。
_WINDOW_SAMPLES: int = 300

# (R9.5) 输出 4 个 stage 概率（AWAKE / LIGHT / DEEP / REM）。
_NUM_STAGES: int = 4

# 输入通道：HRV / motion / breathing。
_NUM_CHANNELS: int = 3

# 默认随机种子（与 design §3.8.2 对齐）。
_DEFAULT_SEED: int = 20260518


# ---------------------------------------------------------------------------
# 退出码常量 —— 让 caller / CI 可以按 enum 解读
# ---------------------------------------------------------------------------


class _ExitCode:
    """退出码语义（详见模块 docstring）."""

    OK: int = 0
    SIZE_EXCEEDED: int = 1
    LOAD_FAILED: int = 2
    INFERENCE_TOO_SLOW: int = 3


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="train_stage_predictor",
        description=(
            "训练 / 导出 EMST stage_predictor.onnx —— v3.0.0 task 10.2. "
            "Use --synthetic to skip EDF parsing and emit a structurally "
            "valid ONNX (CI / integration test)."
        ),
    )
    parser.add_argument(
        "--edf-dir",
        type=Path,
        required=True,
        help=(
            "PhysioNet Sleep-EDF 解压目录。在 --synthetic 模式下被忽略，"
            "但仍需提供占位路径以保持 CLI 一致。"
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="ONNX 输出路径，例如 sleep_classifier/rootfs/training_config/stage_predictor.onnx。",
    )
    parser.add_argument(
        "--quantize",
        action="store_true",
        help=(
            "启用 INT8 动态量化（onnxruntime.quantization.quantize_dynamic）。"
            "默认关闭，因为合成模型已 ≤ 80 KB；真训练时建议开启。"
        ),
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=_DEFAULT_SEED,
        help=f"伪随机种子，默认 {_DEFAULT_SEED}（design §3.8.2）。",
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help=(
            "跳过真实 EDF 训练，使用确定性伪随机权重导出一个微型模型。"
            "仅用于 CI / 集成测试验证运行时管道，不输出有意义的分期。"
        ),
    )
    return parser


# ---------------------------------------------------------------------------
# 合成模型构建（仅在 --synthetic 模式下执行）
# ---------------------------------------------------------------------------


def _build_synthetic_model(torch_module):  # type: ignore[no-untyped-def]
    """构造极小的 (1, 3, 300) → (1, 4) softmax 模型.

    选择 ``Flatten + Linear(900, 4) + Softmax`` 而非多层 MLP / Transformer：

    * 参数量 ``900 × 4 + 4 = 3 604`` 个 float32 = ~14 KB，远低于 80 KB 上限；
    * 结构最小，导出的 ONNX 静态图节点 ≤ 5 个，``InferenceSession`` 加载延
      迟基本可以忽略，便于 ``--quantize`` 关闭时也能稳定通过 50 ms 预算；
    * 不掩盖未来真训练阶段的失败 —— 真训练模式有自己的 scaffold 错误路径。

    模型权重用 ``torch.manual_seed`` 后的默认初始化，因此对相同的
    ``--seed`` 输出字节级一致的 ONNX，便于复现。
    """
    nn = torch_module.nn

    class _SyntheticStagePredictor(nn.Module):
        """合成 stage predictor —— 不代表任何真实生理学."""

        def __init__(self) -> None:
            super().__init__()
            # 输入 (B, 3, 300) → flatten 到 (B, 900)；选 ``start_dim=1`` 保留
            # batch 维以便 ONNX 的 dynamic batch 后续可扩展（v3.1.0 联邦
            # 推理可能用到 B > 1，本期固定 B=1）。
            self.flatten = nn.Flatten(start_dim=1)
            self.fc = nn.Linear(
                _NUM_CHANNELS * _WINDOW_SAMPLES, _NUM_STAGES,
            )

        def forward(self, x):  # type: ignore[no-untyped-def]
            x = self.flatten(x)
            x = self.fc(x)
            # 显式 softmax，让 ONNX 输出直接是概率，符合 R9.5 契约
            # （probabilities ∈ [0, 1] 且 sum ≈ 1）。
            return torch_module.softmax(x, dim=-1)

    return _SyntheticStagePredictor()


# ---------------------------------------------------------------------------
# ONNX 导出 + 量化
# ---------------------------------------------------------------------------


def _export_to_onnx(
    model,  # type: ignore[no-untyped-def]
    out_path: Path,
    torch_module,  # type: ignore[no-untyped-def]
) -> None:
    """把 PyTorch 模型导出成 ONNX，输入名 ``input``、输出名 ``probs``.

    输入名固定为 ``"input"`` 以匹配 ``StagePredictor._load_session`` 在
    运行时通过 ``session.get_inputs()[0].name`` 动态拿名字的逻辑（任何
    名字都能跑，但锁死有助于离线分析）。
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    dummy = torch_module.zeros(1, _NUM_CHANNELS, _WINDOW_SAMPLES)
    model.eval()
    # PyTorch 2.5+ 把 ``torch.onnx.export(dynamo=True)`` 设为默认，要求
    # 额外安装 ``onnxscript``。本脚本只走最小依赖闭环（onnx + onnxruntime），
    # 因此显式 ``dynamo=False`` 回退到 TorchScript-based legacy exporter，
    # 老版本 torch 不识别该 kwarg 时降级走默认路径。
    export_kwargs: dict[str, object] = dict(
        input_names=["input"],
        output_names=["probs"],
        opset_version=17,
        # batch 维保持静态 (B=1)，简化 INT8 量化器分析。
        dynamic_axes=None,
    )
    with torch_module.no_grad():
        try:
            torch_module.onnx.export(
                model, dummy, str(out_path),
                dynamo=False,
                **export_kwargs,
            )
        except TypeError:
            # 老版本 torch 不识别 ``dynamo`` kwarg —— 直接走默认路径。
            torch_module.onnx.export(
                model, dummy, str(out_path),
                **export_kwargs,
            )


def _quantize_int8(src_path: Path) -> None:
    """对 ``src_path`` 做 INT8 动态量化，结果**原位**覆盖.

    走 ``onnxruntime.quantization.quantize_dynamic``，这是
    ``requirements-train.txt`` 已经声明的训练时依赖；运行时镜像不需要
    安装 ``onnx`` / ``onnxruntime.quantization``，只需要
    ``onnxruntime`` 推理。
    """
    from onnxruntime.quantization import QuantType, quantize_dynamic

    # quantize_dynamic 要求 src ≠ dst，使用同目录 .tmp 文件做中转。
    tmp_path = src_path.with_suffix(src_path.suffix + ".int8.tmp")
    try:
        quantize_dynamic(
            model_input=str(src_path),
            model_output=str(tmp_path),
            weight_type=QuantType.QInt8,
        )
        # 原子替换（os.replace 在同一卷下保证原子性）。
        os.replace(tmp_path, src_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 验证（加载 + 推理 + 体积）
# ---------------------------------------------------------------------------


def _verify_onnx(out_path: Path) -> tuple[int, str]:
    """加载 ``out_path`` 并跑一次推理，返回 ``(exit_code, message)``.

    检查顺序遵循退出码定义：先体积、再 InferenceSession 加载、再推理时延。
    任何一步失败立即返回非零退出码 + 中文错误说明。
    """
    # 1. 体积检查（R9.2）
    size = out_path.stat().st_size
    if size > _MAX_MODEL_BYTES:
        return _ExitCode.SIZE_EXCEEDED, (
            f"ONNX 大小 {size} 字节 > 上限 {_MAX_MODEL_BYTES} 字节 (R9.2)"
        )

    # 2. InferenceSession 加载（R9.2 副作用 / 与 stage_predictor.try_load 等价）
    try:
        import onnxruntime  # 训练时机器一定装了；但仍 try/except 防万一
        session = onnxruntime.InferenceSession(
            str(out_path), providers=["CPUExecutionProvider"],
        )
    except Exception as exc:  # noqa: BLE001 — 任何加载错误都是退出码 2
        return _ExitCode.LOAD_FAILED, (
            f"onnxruntime.InferenceSession 加载失败：{exc.__class__.__name__}: {exc}"
        )

    # 3. 推理时延检查（R9.4）—— 跑 3 次取最快值，避免冷启动单次毛刺误判。
    import numpy as np  # numpy 是 runtime 依赖，肯定可用

    rng = np.random.default_rng(0)
    dummy_input = rng.standard_normal(
        (1, _NUM_CHANNELS, _WINDOW_SAMPLES),
    ).astype(np.float32)
    input_name = session.get_inputs()[0].name

    # 第一次跑作为 warm-up（首次推理含图优化 + 算子加载，不计入预算）。
    try:
        session.run(None, {input_name: dummy_input})
    except Exception as exc:  # noqa: BLE001
        return _ExitCode.LOAD_FAILED, (
            f"warm-up 推理失败：{exc.__class__.__name__}: {exc}"
        )

    durations_ms: list[float] = []
    for _ in range(3):
        start = time.perf_counter()
        try:
            outputs = session.run(None, {input_name: dummy_input})
        except Exception as exc:  # noqa: BLE001
            return _ExitCode.LOAD_FAILED, (
                f"推理执行失败：{exc.__class__.__name__}: {exc}"
            )
        durations_ms.append((time.perf_counter() - start) * 1000.0)

    fastest_ms = min(durations_ms)
    if fastest_ms > _MAX_INFERENCE_MS:
        return _ExitCode.INFERENCE_TOO_SLOW, (
            f"最快单次推理 {fastest_ms:.2f} ms > 预算 "
            f"{_MAX_INFERENCE_MS:.1f} ms (R9.4)"
        )

    # 输出 shape sanity（R9.3 / R9.5）；不达标则当作加载失败处理。
    out_arr = np.asarray(outputs[0])
    if out_arr.shape != (1, _NUM_STAGES):
        return _ExitCode.LOAD_FAILED, (
            f"输出 shape {out_arr.shape} ≠ 期望 (1, {_NUM_STAGES}) (R9.3)"
        )

    return _ExitCode.OK, (
        f"加载 OK，最快推理 {fastest_ms:.2f} ms，大小 {size} 字节"
    )


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def _write_report(
    out_path: Path,
    *,
    mode: str,
    seed: int,
    quantize: bool,
    size_bytes: int,
    fastest_inference_ms: float | None,
) -> None:
    """写 ``<out>.report.md`` —— 4-fold CV 命中率（合成模式下为占位符）.

    真训练模式下后续 PR 会把 4 折 hit rate 真实写入；本期合成模式给出
    清晰的 "scaffold" 声明，避免被误读为真实评估结果。
    """
    report_path = out_path.with_suffix(out_path.suffix + ".report.md")
    fastest_str = (
        f"{fastest_inference_ms:.2f} ms"
        if fastest_inference_ms is not None
        else "n/a"
    )
    quant_str = "INT8 动态量化已启用" if quantize else "未启用量化（fp32）"

    lines: list[str] = [
        f"# Stage Predictor 训练报告 ({mode})",
        "",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat()}",
        f"- 输出路径：`{out_path}`",
        f"- 随机种子：`{seed}`",
        f"- 量化设置：{quant_str}",
        f"- ONNX 大小：{size_bytes} 字节（上限 {_MAX_MODEL_BYTES} 字节，R9.2）",
        f"- 最快单次推理：{fastest_str}（上限 {_MAX_INFERENCE_MS:.0f} ms，R9.4）",
        "",
        "## 输入 / 输出契约",
        "",
        "- 输入：`(1, 3, 300)` float32 —— 5 分钟 × 1 Hz × (HRV, motion, breathing)。",
        "- 输出：`(1, 4)` softmax —— AWAKE / LIGHT / DEEP / REM 概率分布。",
        "",
        "## 4-fold CV 命中率（按 stage 分类）",
        "",
    ]

    if mode == "synthetic":
        lines.extend(
            [
                "| Stage | Hit Rate |",
                "| --- | --- |",
                "| AWAKE | n/a (synthetic) |",
                "| LIGHT | n/a (synthetic) |",
                "| DEEP  | n/a (synthetic) |",
                "| REM   | n/a (synthetic) |",
                "",
                "> **声明**：合成模式下模型权重为伪随机，**不**代表任何真实",
                "> 睡眠分期能力。本报告只用于验证运行时管道（ONNX 加载 + 推理",
                "> 时延 + 文件大小）；真实 4-fold CV 命中率由真训练模式产出。",
            ]
        )
    else:
        lines.extend(
            [
                "| Stage | Hit Rate |",
                "| --- | --- |",
                "| AWAKE | (待真训练 PR 落地后填入) |",
                "| LIGHT | (待真训练 PR 落地后填入) |",
                "| DEEP  | (待真训练 PR 落地后填入) |",
                "| REM   | (待真训练 PR 落地后填入) |",
                "",
                "> **声明**：本期 task 10.2 实现了 CLI / scaffold + 合成模式；",
                "> 真实 PSG 训练 + 4-fold CV 在 v3.0.0 后续 PR 中实现。",
            ]
        )

    lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# 真训练 scaffold
# ---------------------------------------------------------------------------


def _train_real(args: argparse.Namespace) -> int:
    """真训练模式占位符 —— 返回退出码 1 并打印清晰说明.

    本期 task 10.2 把真实 PSG 训练流水线作为 scaffold 留给后续 PR；
    立即返回 1 而非 0，避免 CI 误以为 "训练成功"。``--synthetic`` 模式
    才是 CI / 集成测试的稳定路径。
    """
    edf_dir: Path = args.edf_dir
    if not edf_dir.exists():
        logger.error(
            "真训练模式需要存在的 --edf-dir，但 %s 不存在；如果只是想验证",
            edf_dir,
        )
        logger.error("运行时管道，请加上 --synthetic 标志。")
        return _ExitCode.SIZE_EXCEEDED  # 退出码 1：scaffold / 数据 schema 不符

    logger.error(
        "真训练流水线（Sleep-EDF → HRV/motion/breathing → 4-fold CV → ONNX）",
    )
    logger.error(
        "尚未在本期 task 10.2 范围内实现；请使用 --synthetic 进行 CI 验证，",
    )
    logger.error("或在后续 PR 中提交完整 scaffold。")
    return _ExitCode.SIZE_EXCEEDED  # 退出码 1


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI 入口 —— 返回退出码（见模块 docstring）."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = _build_parser().parse_args(argv)

    # 真训练路径 —— scaffold，立即返回 1。
    if not args.synthetic:
        return _train_real(args)

    # ---- 合成路径 ----
    # 训练时依赖延迟 import；缺失时给出清晰错误。
    try:
        import torch  # type: ignore[import-not-found]
    except ImportError as exc:
        logger.error(
            "需要安装训练时依赖才能导出 ONNX：%s。请运行 "
            "`pip install -r requirements-train.txt` 或单独 `pip install torch`。",
            exc,
        )
        return _ExitCode.SIZE_EXCEEDED

    try:
        import onnx  # noqa: F401 — torch.onnx.export 隐式依赖 onnx
    except ImportError as exc:
        logger.error(
            "缺少 onnx 包（torch.onnx.export 隐式依赖）：%s。请运行 "
            "`pip install onnx`（或 `pip install -r requirements-train.txt`）。",
            exc,
        )
        return _ExitCode.SIZE_EXCEEDED

    out_path: Path = args.out

    # 确定性种子 —— 让相同 --seed 输出字节级一致的 ONNX，便于复现 / CI 缓存。
    torch.manual_seed(args.seed)

    logger.info(
        "构建合成 stage predictor (Linear(%d, %d) + Softmax)，seed=%d",
        _NUM_CHANNELS * _WINDOW_SAMPLES, _NUM_STAGES, args.seed,
    )
    model = _build_synthetic_model(torch)

    # 先导出到一个临时位置，再 atomic replace 到 out_path，避免半成品。
    tmp_dir = Path(tempfile.mkdtemp(prefix="train_stage_predictor_"))
    tmp_onnx = tmp_dir / "model.onnx"
    try:
        logger.info("导出 ONNX 到临时路径 %s", tmp_onnx)
        _export_to_onnx(model, tmp_onnx, torch)

        if args.quantize:
            logger.info("对 %s 执行 INT8 动态量化", tmp_onnx)
            _quantize_int8(tmp_onnx)

        # 拷贝到目标路径（用 shutil.move 跨卷友好；同卷下走 os.replace）。
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_onnx), str(out_path))
    finally:
        # 清理临时目录（即使 move 已带走 model.onnx，目录也要删）。
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except OSError:
            pass

    size_bytes = out_path.stat().st_size
    logger.info(
        "导出完成：%s（%d 字节，上限 %d 字节）",
        out_path, size_bytes, _MAX_MODEL_BYTES,
    )

    exit_code, message = _verify_onnx(out_path)
    fastest_ms: float | None = None
    if exit_code == _ExitCode.OK:
        # 从 message 中解析最快推理时间（仅用于报告，不影响退出码）。
        try:
            fastest_ms = float(
                message.split("最快推理")[1].split("ms")[0].strip()
            )
        except (IndexError, ValueError):
            fastest_ms = None
        logger.info("验证通过：%s", message)
    else:
        logger.error("验证失败（退出码 %d）：%s", exit_code, message)

    # 报告无论成功 / 失败都写一份，便于 debug。
    _write_report(
        out_path,
        mode="synthetic",
        seed=args.seed,
        quantize=args.quantize,
        size_bytes=size_bytes,
        fastest_inference_ms=fastest_ms,
    )

    return exit_code


if __name__ == "__main__":  # pragma: no cover - CLI passthrough
    raise SystemExit(main())
