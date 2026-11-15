"""T04 渲染层：Pillow + 声明式模板（ADR §3/§6.4）。

对外入口是 ``workbench.render.build.build_renderer()``，返回满足
``workbench.contracts.Renderer`` 协议的实现。本包只依赖 Pillow 与标准库，
不访问网络、不依赖浏览器或系统字体。
"""

from __future__ import annotations

from workbench.render.build import PillowRenderer, TemplateError, build_renderer
from workbench.render.fonts import FontBook, MissingFont
from workbench.render.layout import LayoutError

__all__ = [
    "FontBook",
    "LayoutError",
    "MissingFont",
    "PillowRenderer",
    "TemplateError",
    "build_renderer",
]
