"""Bug 1.2 契约测试 — Ingress 前端 AJAX 路径必须为相对路径。

修复前：当前 ``sleep_classifier/web_ui.py`` 的前端 JS 已用相对路径
（``fetch('api/entities')`` / ``fetch('api/options', ...)``），所以本
测试**修复前即 PASS**。Bug 1.2 是"契约 + 防回归"性质——不是行为
bug，而是缺少自动化守护，未来有人把 ``fetch`` 改回绝对路径会
直接让 Ingress 反向代理失效。

两条契约：

1. ``test_frontend_uses_relative_fetch_paths``——
   静态 regex 扫描 ``web_ui.py``，禁止出现 ``fetch('/api/...)``
   这类绝对路径写法。Ingress 会把 ``/api/hassio_ingress/<token>/``
   作为前缀注入；绝对路径会脱出该前缀命中 HA Core REST 命名空间，
   表现为 404 或 500。

2. ``test_aiohttp_routes_cover_api_paths``——
   ``make_app()`` 必须把 ``GET /api/entities`` 与
   ``POST /api/options`` 挂到 aiohttp router 上，否则相对路径
   也会因后端缺路由而失败。

参考：``.kiro/specs/post-v2.0.2-full-pipeline-audit/bugfix.md`` §1.2 +
``design.md`` §3.2。
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType


_WEB_UI_PATH = (
    Path(__file__).resolve().parent.parent / "sleep_classifier" / "web_ui.py"
)
_MODULE_NAME = "web_ui_ingress_contract_module"


def _load_web_ui_module() -> ModuleType:
    """以独立模块名加载 ``web_ui.py``，避免污染 ``tests/test_web_ui.py``
    自身的 ``import web_ui`` 缓存。

    ``sleep_classifier/`` 不是 Python 包（无 ``__init__.py``，是 HA
    Add-on 打包目录），因此走 ``importlib.util.spec_from_file_location``
    直接按路径加载更干净。
    """
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _WEB_UI_PATH)
    assert spec is not None and spec.loader is not None, (
        f"无法为 {_WEB_UI_PATH} 构造 importlib spec"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_MODULE_NAME, None)
        raise
    return module


def test_frontend_uses_relative_fetch_paths() -> None:
    """前端 JS 不得出现 ``fetch('/api/...)`` 形式的绝对路径。

    Ingress 在浏览器侧的 base URL 形如
    ``http://<ha>:8123/api/hassio_ingress/<token>/``。前端必须用
    相对路径（``api/entities``）才能落到该前缀下；绝对路径
    ``/api/entities`` 会被浏览器解析为站点根路径，绕过 Ingress
    反代，命中 HA Core REST，返回 404 / 500。
    """
    assert _WEB_UI_PATH.is_file(), f"找不到 {_WEB_UI_PATH}"
    content = _WEB_UI_PATH.read_text(encoding="utf-8")

    # 匹配 fetch( 后紧跟可选空白 + 引号 + /api/ 的写法。
    matches = re.findall(r"""fetch\(\s*['"]/api/""", content)

    assert matches == [], (
        f"web_ui.py 中检测到 {len(matches)} 处绝对路径 fetch('/api/...'); "
        "Ingress 契约要求使用相对路径 (api/entities, api/options)，"
        "否则会脱离 /api/hassio_ingress/<token>/ 前缀导致 404/500。"
    )


def test_aiohttp_routes_cover_api_paths() -> None:
    """``make_app()`` 必须挂载 ``GET /api/entities`` 与 ``POST /api/options``。

    前端相对路径只有在后端存在对应路由时才有意义；这条测试与
    上一条互补，构成"前端写法 ↔ 后端路由"双向契约。
    """
    web_ui = _load_web_ui_module()
    app = web_ui.make_app()

    routes: set[tuple[str, str]] = set()
    for route in app.router.routes():
        # PlainResource / DynamicResource 都暴露 ``canonical`` 属性，
        # 返回原始注册路径（如 "/api/entities"）。
        canonical = getattr(route.resource, "canonical", None)
        if canonical is None:
            # 兜底：极个别 Resource 子类没有 canonical 时退化用 str()
            canonical = str(route.resource)
        routes.add((route.method, canonical))

    assert ("GET", "/api/entities") in routes, (
        f"缺少 GET /api/entities 路由；现有 routes: {sorted(routes)}"
    )
    assert ("POST", "/api/options") in routes, (
        f"缺少 POST /api/options 路由；现有 routes: {sorted(routes)}"
    )
