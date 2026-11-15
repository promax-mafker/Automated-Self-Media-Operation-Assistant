"""文本度量与换行：中文按字、英文按词，超行追加省略号。

只使用 Pillow 的 ``ImageFont`` 度量（``getlength`` / ``getbbox``），不依赖
字体文件之外的任何东西，因此同一字体 + 同一尺寸的换行结果是确定的。
"""

from __future__ import annotations

import re

from PIL import ImageFont

# 一个 token：空白、一个英文/数字词（可带内部标点）、或一个字符（含中文）。
_TOKEN_RE = re.compile(r"\s+|[A-Za-z0-9_]+(?:[.'’\-][A-Za-z0-9_]+)*|.", re.DOTALL)

ELLIPSIS = "…"


def normalize_text(value: object) -> str:
    """把任意 slot 值归一化成可渲染文本；``None`` 视为空串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.replace("\r\n", "\n").replace("\r", "\n")
    if isinstance(value, (list, tuple)):
        return "\n".join(normalize_text(item) for item in value)
    return str(value)


def tokenize(paragraph: str) -> list[str]:
    """切分为换行 token：中文按单字，英文按词，空白单独成 token。"""
    return _TOKEN_RE.findall(paragraph)


def measure(font: ImageFont.FreeTypeFont, text: str) -> float:
    """文本宽度（像素，浮点）。"""
    return float(font.getlength(text))


def text_height(font: ImageFont.FreeTypeFont, text: str) -> int:
    """文本墨迹高度（不含行距）。"""
    left, top, right, bottom = font.getbbox(text)
    if right <= left or bottom <= top:
        return 0
    return int(bottom - top)


def ellipsize(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    ellipsis: str = ELLIPSIS,
) -> str:
    """把单行文本压到 ``max_width`` 内并保证以省略号结尾。"""
    base = text.rstrip()
    if measure(font, base) <= max_width:
        return base + ellipsis if base else ellipsis
    if measure(font, ellipsis) > max_width:
        return ""
    while base and measure(font, base + ellipsis) > max_width:
        base = base[:-1]
    return base + ellipsis


def single_line(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
) -> tuple[str, bool]:
    """压成一行；返回 ``(文本, 是否发生截断)``，截断时以省略号结尾。"""
    normalized = normalize_text(text).replace("\n", " ")
    if measure(font, normalized) <= max_width:
        return normalized, False
    return ellipsize(normalized, font, max_width), True


def wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    max_lines: int | None = None,
) -> tuple[list[str], bool]:
    """按 ``max_width`` 逐 token 换行，超过 ``max_lines`` 截断并追加 ``…``。

    返回 ``(行列表, 是否截断)``。``max_width`` 小于一个字符宽度时也会
    返回可绘制的行（必要时只有省略号），不会死循环。
    """
    width = max(1, int(max_width))
    raw = normalize_text(text)
    lines: list[str] = []

    for paragraph in raw.split("\n"):
        if paragraph == "":
            lines.append("")
            continue
        current = ""
        for token in tokenize(paragraph):
            if token.isspace():
                if current and measure(font, current + " ") <= width:
                    current += " "
                continue
            if measure(font, current + token) <= width:
                current += token
                continue
            if current.strip():
                lines.append(current.rstrip())
            current = ""
            # 单个 token 比整行还宽（例如超长英文词）：按字符硬切。
            while len(token) > 1 and measure(font, token) > width:
                piece = ""
                for char in token:
                    if measure(font, piece + char) <= width:
                        piece += char
                    else:
                        break
                if not piece:
                    piece = token[0]
                lines.append(piece)
                token = token[len(piece) :]
            current = token
        lines.append(current.rstrip())

    while len(lines) > 1 and lines[-1] == "":
        lines.pop()

    truncated = False
    if max_lines is not None and len(lines) > max(1, int(max_lines)):
        lines = lines[: max(1, int(max_lines))]
        truncated = True
        lines[-1] = ellipsize(lines[-1], font, width)
    return lines, truncated


__all__ = [
    "ELLIPSIS",
    "ellipsize",
    "measure",
    "normalize_text",
    "single_line",
    "text_height",
    "tokenize",
    "wrap_text",
]
