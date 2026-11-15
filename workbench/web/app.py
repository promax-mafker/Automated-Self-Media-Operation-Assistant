"""FastAPI 应用工厂：模板、静态文件与路由装配。

- ``create_app(cfg)`` 可注入 :class:`workbench.config.Config`，测试与 ``wb serve`` 共用。
- 每次请求在路由里打开/关闭一个 SQLite 连接（ADR §4 的进程模型）。
- 模板与静态文件都从本包目录读取，不依赖仓库工作目录。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment
from starlette.templating import Jinja2Templates

from workbench.config import Config
from workbench.web.routes import router

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


def _fmt_int(value: Any) -> str:
    """整数千分位；``None`` 显示为破折号。"""
    if value is None:
        return "—"
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_num(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value) * 100:.{digits}f}%"
    except (TypeError, ValueError):
        return str(value)


def _fmt_bytes(value: Any) -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def install_filters(env: Environment) -> None:
    """注册模板过滤器；集中在一处，模板里不写格式化逻辑。"""
    env.filters["fmt_int"] = _fmt_int
    env.filters["fmt_num"] = _fmt_num
    env.filters["fmt_pct"] = _fmt_pct
    env.filters["fmt_bytes"] = _fmt_bytes


def create_app(cfg: Config | None = None) -> FastAPI:
    """构造控制台应用；``cfg`` 省略时按环境变量解析（与 CLI 一致）。"""
    resolved = cfg if cfg is not None else Config.from_env()
    resolved.ensure_dirs()

    application = FastAPI(
        title="超级个体获客工作台 · 控制台",
        description="本地单用户控制台；只读 store，写操作走表单。",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.cfg = resolved

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    install_filters(templates.env)
    application.state.templates = templates

    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    application.include_router(router)
    return application


__all__ = ["WEB_DIR", "create_app", "install_filters"]
