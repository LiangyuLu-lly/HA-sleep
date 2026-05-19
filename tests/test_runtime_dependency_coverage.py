"""v3.0.0 运行时依赖 import 覆盖率静态扫描。

覆盖：
  Property 11：镜像内 ``numpy`` / ``scipy`` / ``onnxruntime`` 必须有 ``import``
  路径覆盖率 —— 即 ``src/`` 下至少有 1 个 ``.py`` 文件以 ``import`` 或
  ``from`` 语法引用了对应顶级模块。如果某依赖被列入
  ``requirements-runtime.txt`` 却在 ``src/`` 内零引用，则属于「装了不用」
  的镜像膨胀，CI 应当当场拦截。

**Validates: Requirements 12.1, 12.4**

实现要点
--------
* 用 ``ast.parse`` 语法树静态扫描，避免 ``import`` 语句被字符串里的
  注释 / docstring 误识别。
* ``Import`` 与 ``ImportFrom`` 两类节点都收集顶级模块名（按 ``.``
  分割后取第一段），覆盖 ``import numpy as np`` / ``import scipy.linalg``
  / ``from onnxruntime import InferenceSession`` 等所有惯用写法。
* 扫描范围限定 ``src/``（运行时模块根，与 PR4 镜像构建 context 一致）；
  ``scripts/`` 中的训练脚本不在镜像内，不参与本断言。
* 失败时输出每个缺失模块在 ``src/`` 内被实际 import 的全集，便于排查
  是否是改名 / 拼写错误。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

#: v3.0.0 因「算法护城河」破例引入的 3 个科学计算依赖。每一个都对应
#: 一个具体算法模块（详见 ``.kiro/steering/tech.md`` 的「v3.0.0 破例
#: 理由」段落），CI 守护这 3 个名字必须各自至少有 1 处 import 路径。
REQUIRED_RUNTIME_MODULES: tuple[str, ...] = ("numpy", "scipy", "onnxruntime")


def _collect_top_level_imports(src_dir: Path) -> dict[str, list[Path]]:
    """递归扫描 ``src_dir`` 下所有 ``.py`` 文件，收集顶级模块 → 引用文件列表。

    :param src_dir: 待扫描的源码根目录。
    :return: 形如 ``{"numpy": [Path("src/bayesian_optimizer.py"), ...]}``
        的字典；键为顶级模块名（``import a.b.c`` 与 ``from a.b import x``
        都映射到 ``"a"``），值为引用该模块的源码文件列表（去重后按路径
        排序）。
    """
    imports: dict[str, set[Path]] = {}

    for py_file in sorted(src_dir.rglob("*.py")):
        # ``__pycache__`` 不会进入 rglob 结果（``.py`` 后缀过滤），
        # 但仍显式跳过隐藏 / 缓存目录，保险起见。
        if any(part.startswith(".") or part == "__pycache__" for part in py_file.parts):
            continue

        try:
            source = py_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:  # pragma: no cover - 保护性
            pytest.fail(f"无法读取 {py_file}: {exc!r}")

        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError as exc:  # pragma: no cover - 保护性
            pytest.fail(f"{py_file} 语法错误，无法静态扫描: {exc!r}")

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".", 1)[0]
                    imports.setdefault(top, set()).add(py_file)
            elif isinstance(node, ast.ImportFrom):
                # ``from . import x`` / ``from .foo import x`` 时
                # ``node.module`` 可能为 None 或包内相对路径，跳过 —— 它们
                # 不可能引用顶级第三方依赖。
                if node.level and node.level > 0:
                    continue
                if node.module is None:
                    continue
                top = node.module.split(".", 1)[0]
                imports.setdefault(top, set()).add(py_file)

    return {mod: sorted(files) for mod, files in imports.items()}


def test_property_p11_runtime_deps_actually_imported() -> None:
    """Property 11：``numpy`` / ``scipy`` / ``onnxruntime`` 各 ≥ 1 处 import。

    防止 ``requirements-runtime.txt`` 中列出的依赖在 ``src/`` 内零引用，
    导致镜像无谓膨胀（R12.1 / R12.4）。
    """
    assert SRC_DIR.is_dir(), f"src 目录不存在: {SRC_DIR}"

    imports_by_module = _collect_top_level_imports(SRC_DIR)

    missing: list[str] = []
    diagnostics: list[str] = []
    for mod in REQUIRED_RUNTIME_MODULES:
        files = imports_by_module.get(mod, [])
        if not files:
            missing.append(mod)
            diagnostics.append(f"  - {mod!r}: no imports found")
        else:
            rel = ", ".join(str(p.relative_to(REPO_ROOT)) for p in files)
            diagnostics.append(f"  - {mod!r}: imported in [{rel}]")

    assert not missing, (
        "运行时依赖在 src/ 内零 import 路径覆盖（违反 R12.1 / R12.4）；"
        f"缺失模块: {missing}。\n"
        "扫描详情:\n" + "\n".join(diagnostics)
    )
