"""把排版计划画到 Pillow 图像上。

只用 ``ImageDraw`` 的基础图元（矩形、圆角矩形、文本、图片粘贴），不引入
浏览器、HTML 或系统字体的渲染路径。每个块都按计划里的 bbox 绘制，绘制前
的越界与叠字检查由 ``workbench.render.layout`` 负责。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from workbench.render import text as text_utils
from workbench.render.fonts import FontBook
from workbench.render.layout import PagePlan

logger = logging.getLogger("workbench.render")


def _clamp_radius(radius: int, width: int, height: int) -> int:
    return max(0, min(int(radius), min(width, height) // 2))


def _rounded(
    draw: ImageDraw.ImageDraw,
    bbox: tuple[int, int, int, int],
    radius: int,
    *,
    fill: tuple[int, int, int] | None,
    outline: tuple[int, int, int] | None = None,
    width: int = 1,
) -> None:
    x1, y1, x2, y2 = bbox
    if x2 <= x1 or y2 <= y1:
        return
    draw.rounded_rectangle(
        (x1, y1, x2 - 1, y2 - 1),
        radius=_clamp_radius(radius, x2 - x1, y2 - y1),
        fill=fill,
        outline=outline,
        width=max(1, width) if outline is not None else 0,
    )


def _line_offset(font: Any, line: str, max_width: int, align: str) -> float:
    width = float(font.getlength(line))
    if align == "center":
        return (max_width - width) / 2
    if align == "right":
        return max_width - width
    return 0.0


def draw_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    *,
    x: int,
    y: int,
    font: Any,
    color: tuple[int, int, int],
    line_height: int,
    max_width: int,
    align: str = "left",
) -> None:
    """逐行绘制文本；每行在 ``line_height`` 的行盒内垂直居中。"""
    for offset, line in enumerate(lines):
        if not line:
            continue
        draw.text(
            (
                x + _line_offset(font, line, max_width, align),
                y + offset * line_height + line_height / 2,
            ),
            line,
            font=font,
            fill=color,
            anchor="lm",
        )


def _draw_text(draw: ImageDraw.ImageDraw, op: dict[str, Any], fonts: FontBook) -> None:
    x, y = op["bbox"][0], op["bbox"][1]
    draw_lines(
        draw,
        op["lines"],
        x=x,
        y=y,
        font=fonts.font(op["weight"], op["size"]),
        color=op["color"],
        line_height=op["line_height"],
        max_width=op["max_width"],
        align=op["align"],
    )


def _draw_list(draw: ImageDraw.ImageDraw, op: dict[str, Any], fonts: FontBook) -> None:
    font = fonts.font(op["weight"], op["size"])
    x, y = op["bbox"][0], op["bbox"][1]
    cursor = y
    for item_index, lines in enumerate(op["items"]):
        if item_index > 0:
            cursor += op["item_gap"]
        if op["bullet"]:
            bullet_x = x + _line_offset(font, op["bullet"], op["max_width"], op["align"])
            draw.text(
                (bullet_x, cursor + op["line_height"] / 2),
                op["bullet"],
                font=font,
                fill=op["color"],
                anchor="lm",
            )
        draw_lines(
            draw,
            lines,
            x=x + op["bullet_width"],
            y=cursor,
            font=font,
            color=op["color"],
            line_height=op["line_height"],
            max_width=op["max_width"] - op["bullet_width"],
            align=op["align"],
        )
        cursor += len(lines) * op["line_height"]


def _draw_panel(draw: ImageDraw.ImageDraw, op: dict[str, Any], fonts: FontBook) -> None:
    x1, y1, x2, y2 = op["bbox"]
    stroke_width = op.get("stroke_width", 0) if op.get("stroke") is not None else 0
    _rounded(
        draw,
        op["bbox"],
        op["radius"],
        fill=op["fill"],
        outline=op.get("stroke"),
        width=stroke_width,
    )
    payload = op.get("text")
    if payload:
        draw_lines(
            draw,
            payload["lines"],
            x=payload["x"],
            y=payload["y"],
            font=fonts.font(payload["weight"], payload["size"]),
            color=payload["color"],
            line_height=payload["line_height"],
            max_width=payload["max_width"],
            align=payload["align"],
        )


def _draw_chips(draw: ImageDraw.ImageDraw, op: dict[str, Any], fonts: FontBook) -> None:
    font = fonts.font(op["weight"], op["size"])
    for row in op["rows"]:
        for chip in row:
            box = (
                chip["x"],
                chip["y"],
                chip["x"] + chip["width"],
                chip["y"] + chip["height"],
            )
            _rounded(draw, box, op["radius"], fill=op["background"])
            draw.text(
                (chip["x"] + chip["width"] / 2, chip["y"] + chip["height"] / 2),
                chip["text"],
                font=font,
                fill=op["color"],
                anchor="mm",
            )


def _draw_divider(draw: ImageDraw.ImageDraw, op: dict[str, Any]) -> None:
    x1, y1, x2, _ = op["bbox"]
    draw.rectangle((x1, y1, x2 - 1, y1 + op["thickness"] - 1), fill=op["color"])


def _load_cover(path: Path, width: int, height: int) -> Image.Image:
    """等比放大后居中裁切到目标尺寸（cover）。"""
    with Image.open(path) as source:
        image = source.convert("RGB")
        scale = max(width / image.width, height / image.height)
        resized = (
            max(width, math.ceil(image.width * scale)),
            max(height, math.ceil(image.height * scale)),
        )
        image = image.resize(resized, Image.LANCZOS)
        left = (image.width - width) // 2
        top = (image.height - height) // 2
        return image.crop((left, top, left + width, top + height))


def _draw_image(
    draw: ImageDraw.ImageDraw, image: Image.Image, op: dict[str, Any], fonts: FontBook
) -> None:
    x1, y1, x2, y2 = op["bbox"]
    width, height = x2 - x1, y2 - y1
    strokes = op.get("stroke") is not None
    if op.get("path"):
        try:
            picture = _load_cover(Path(op["path"]), width, height)
        except (OSError, ValueError) as exc:
            logger.warning(
                "图片无法打开，改用占位框：%s（%s: %s）",
                op["path"],
                type(exc).__name__,
                exc,
            )
            picture = None
    else:
        picture = None

    if picture is None:
        _rounded(
            draw,
            op["bbox"],
            op["radius"],
            fill=op["fill"],
            outline=op.get("stroke"),
            width=3 if strokes else 0,
        )
        if op.get("label"):
            font = fonts.font(op["label_weight"], op["label_size"])
            label, _ = text_utils.single_line(op["label"], font, max(1, width - 32))
            draw.text(
                ((x1 + x2) / 2, (y1 + y2) / 2),
                label,
                font=font,
                fill=op["label_color"],
                anchor="mm",
            )
        return

    mask = Image.new("L", (width, height), 0)
    radius = _clamp_radius(op["radius"], width, height)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width - 1, height - 1), radius=radius, fill=255
    )
    image.paste(picture, (x1, y1), mask)


def draw_plan(image: Image.Image, plan: PagePlan, fonts: FontBook) -> None:
    """按计划顺序绘制全部块（后画的覆盖先画的）。"""
    draw = ImageDraw.Draw(image)
    for op in plan.ops:
        block_type = op["type"]
        if block_type == "text":
            _draw_text(draw, op, fonts)
        elif block_type == "list":
            _draw_list(draw, op, fonts)
        elif block_type == "panel":
            _draw_panel(draw, op, fonts)
        elif block_type == "chips":
            _draw_chips(draw, op, fonts)
        elif block_type == "divider":
            _draw_divider(draw, op)
        elif block_type == "image":
            _draw_image(draw, image, op, fonts)
        else:  # pragma: no cover - layout 已挡住未知类型
            raise ValueError(f"未知块类型：{block_type!r}")


__all__ = ["draw_plan", "draw_lines"]
