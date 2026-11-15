"""字体查找与缓存：只用 ``WB_FONT_DIR`` 里的 Noto Sans SC，不依赖系统字体。

默认目录是仓库内的 ``assets/fonts``；换字体时保持文件名不变，或用
``WB_FONT_DIR`` 指向另一个目录。两个文件都缺失/缺一时抛 ``MissingFont``
（``Unavailable`` 的子类），提示把 CJK 字体放到 ``assets/fonts/``。
"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import ImageFont

from workbench.errors import Unavailable, WorkbenchError

FONT_FILES: dict[str, str] = {
    "regular": "NotoSansSC-Regular.otf",
    "bold": "NotoSansSC-Bold.otf",
}
WEIGHTS: tuple[str, ...] = ("regular", "bold")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HINT = "请把 CJK 字体放到 assets/fonts/，或用 WB_FONT_DIR 指定字体目录"


def default_font_dir() -> Path:
    """``WB_FONT_DIR`` 未设置时的默认字体目录（仓库内 ``assets/fonts``）。"""
    return _REPO_ROOT / "assets" / "fonts"


def resolve_font_dir(explicit: Path | str | None = None) -> Path:
    """解析字体目录：显式参数 > ``WB_FONT_DIR`` > 仓库内 ``assets/fonts``。"""
    raw = explicit if explicit is not None else os.environ.get("WB_FONT_DIR")
    if raw is None or str(raw).strip() == "":
        return default_font_dir()
    return Path(raw).expanduser().resolve()


class MissingFont(Unavailable):
    """字体目录里缺少 Noto Sans SC 文件；继承 ``Unavailable``，CLI 退出码为 1。"""

    def __init__(self, font_dir: Path, missing: list[str]) -> None:
        self.font_dir = font_dir
        self.missing = missing
        self.capability = "CJK 字体"
        self.ticket = "T04"
        message = (
            f"Unavailable: 找不到 CJK 字体文件 {', '.join(missing)}（{font_dir}）。{_HINT}。"
        )
        WorkbenchError.__init__(self, message)


class FontBook:
    """按 ``(weight, size)`` 缓存 ``ImageFont``；构造时校验字体文件存在。"""

    def __init__(self, font_dir: Path | str | None = None) -> None:
        self.font_dir = resolve_font_dir(font_dir)
        missing = [name for name in FONT_FILES.values() if not (self.font_dir / name).is_file()]
        if missing:
            raise MissingFont(self.font_dir, missing)
        self._cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    def path(self, weight: str) -> Path:
        try:
            name = FONT_FILES[weight]
        except KeyError as exc:
            raise ValueError(f"未知字重：{weight!r}；可选：{', '.join(WEIGHTS)}") from exc
        return self.font_dir / name

    def font(self, weight: str, size: int) -> ImageFont.FreeTypeFont:
        normalized = weight if weight in FONT_FILES else "regular"
        size = max(1, int(size))
        key = (normalized, size)
        cached = self._cache.get(key)
        if cached is None:
            cached = ImageFont.truetype(str(self.path(normalized)), size)
            self._cache[key] = cached
        return cached


__all__ = [
    "FONT_FILES",
    "WEIGHTS",
    "FontBook",
    "MissingFont",
    "default_font_dir",
    "resolve_font_dir",
]
