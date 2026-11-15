"""Pillow 卡片绘制：把分镜场景渲染成 1080×1920 PNG 序列（T07）。

本模块**不依赖** ``workbench.render``（T04）：卡片版式、换行与进度条都在这里
自绘，避免跨 Ticket 耦合。字体只读 ``WB_FONT_DIR``（默认仓库
``assets/fonts/``）里的 Noto Sans SC；缺少字体直接报可读错误，不依赖系统字体。

``points`` 卡按 2-3 个进度帧渲染（1 条 → 2 条 → … → 全部），由 ``compose``
按 ``duration`` 拼成逐条出现的效果；``title`` / ``cta`` 各 1 张。
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from workbench.errors import WorkbenchError
from workbench.video.storyboard import Scene, Storyboard

CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920
MARGIN = 96
CONTENT_WIDTH = CANVAS_WIDTH - 2 * MARGIN

BG_TOP = (11, 18, 32)
BG_BOTTOM = (20, 31, 49)
BRAND = (255, 107, 61)
INK = (247, 248, 250)
MUTED = (154, 167, 184)
PANEL = (22, 32, 47)
LINE = (36, 52, 74)
FAINT = (26, 38, 56)

FONT_FILES: dict[str, str] = {
    "regular": "NotoSansSC-Regular.otf",
    "bold": "NotoSansSC-Bold.otf",
}
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOKEN_RE = re.compile(r"\s+|[A-Za-z0-9_]+(?:[.'’\-][A-Za-z0-9_]+)*|.", re.DOTALL)
ELLIPSIS = "…"


@dataclass(frozen=True, slots=True)
class Segment:
    """一段静态画面：一张 PNG + 展示秒数。"""

    path: Path
    seconds: float


def resolve_font_dir(explicit: Path | str | None = None) -> Path:
    """字体目录：显式参数 > ``WB_FONT_DIR`` > 仓库 ``assets/fonts``。"""
    raw = explicit if explicit is not None else os.environ.get("WB_FONT_DIR")
    if raw is None or str(raw).strip() == "":
        return _REPO_ROOT / "assets" / "fonts"
    return Path(raw).expanduser().resolve()


class Fonts:
    """按 ``(字重, 字号)`` 缓存 Noto Sans SC 字体。"""

    def __init__(self, font_dir: Path | str | None = None) -> None:
        self.font_dir = resolve_font_dir(font_dir)
        missing = [
            name for name in FONT_FILES.values() if not (self.font_dir / name).is_file()
        ]
        if missing:
            raise WorkbenchError(
                f"找不到 CJK 字体文件 {', '.join(missing)}（{self.font_dir}）；"
                "请把 Noto Sans SC 放到 assets/fonts/，或用 WB_FONT_DIR 指定字体目录"
            )
        self._cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    def get(self, weight: str, size: int) -> ImageFont.FreeTypeFont:
        normalized = weight if weight in FONT_FILES else "regular"
        key = (normalized, max(1, int(size)))
        cached = self._cache.get(key)
        if cached is None:
            cached = ImageFont.truetype(str(self.font_dir / FONT_FILES[normalized]), key[1])
            self._cache[key] = cached
        return cached


def _measure(font: ImageFont.FreeTypeFont, text: str) -> float:
    return float(font.getlength(text))


def _ellipsize(font: ImageFont.FreeTypeFont, text: str, max_width: float) -> str:
    base = text.rstrip()
    if _measure(font, base + ELLIPSIS) <= max_width:
        return base + ELLIPSIS
    if _measure(font, ELLIPSIS) > max_width:
        return ""
    while base and _measure(font, base + ELLIPSIS) > max_width:
        base = base[:-1]
    return base + ELLIPSIS


def wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: float,
    *,
    max_lines: int | None = None,
) -> tuple[list[str], bool]:
    """按像素宽度换行（中文按字、英文按词），超行截断并追加省略号。"""
    width = max(1, int(max_width))
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []

    for paragraph in normalized.split("\n"):
        if paragraph == "":
            lines.append("")
            continue
        current = ""
        for token in _TOKEN_RE.findall(paragraph):
            if token.isspace():
                if current and _measure(font, current + " ") <= width:
                    current += " "
                continue
            if _measure(font, current + token) <= width:
                current += token
                continue
            if current.strip():
                lines.append(current.rstrip())
            current = ""
            while len(token) > 1 and _measure(font, token) > width:
                piece = ""
                for char in token:
                    if _measure(font, piece + char) <= width:
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
        lines[-1] = _ellipsize(font, lines[-1], width)
    return lines, truncated


def _lerp_color(
    start: tuple[int, int, int], end: tuple[int, int, int], ratio: float
) -> tuple[int, int, int]:
    return tuple(
        round(first + (second - first) * ratio)
        for first, second in zip(start, end, strict=True)
    )  # type: ignore[return-value]


def _draw_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    *,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    x: int,
    y: int,
    line_height: int,
    width: int | None = None,
    align: str = "left",
) -> None:
    for line in lines:
        draw_x = x
        if align == "center" and width is not None:
            draw_x = int(x + (width - _measure(font, line)) / 2)
        draw.text((draw_x, y), line, font=font, fill=fill)
        y += line_height


def _draw_progress(draw: ImageDraw.ImageDraw, progress: float) -> None:
    bar_y = CANVAS_HEIGHT - 64
    draw.rounded_rectangle(
        [MARGIN, bar_y, CANVAS_WIDTH - MARGIN, bar_y + 6], radius=3, fill=LINE
    )
    ratio = min(1.0, max(0.0, progress))
    filled = int((CANVAS_WIDTH - 2 * MARGIN) * ratio)
    if filled > 0:
        draw.rounded_rectangle(
            [MARGIN, bar_y, MARGIN + filled, bar_y + 6], radius=3, fill=BRAND
        )


def _base_canvas(progress: float) -> Image.Image:
    strip = Image.new("RGB", (1, CANVAS_HEIGHT))
    strip.putdata(
        [
            _lerp_color(BG_TOP, BG_BOTTOM, index / (CANVAS_HEIGHT - 1))
            for index in range(CANVAS_HEIGHT)
        ]
    )
    image = strip.resize((CANVAS_WIDTH, CANVAS_HEIGHT))
    draw = ImageDraw.Draw(image)
    draw.ellipse(
        [CANVAS_WIDTH - 290, 60, CANVAS_WIDTH + 150, 500], outline=LINE, width=3
    )
    draw.ellipse(
        [CANVAS_WIDTH - 200, 150, CANVAS_WIDTH + 60, 410], outline=FAINT, width=2
    )
    _draw_progress(draw, progress)
    return image


def _draw_header(draw: ImageDraw.ImageDraw, fonts: Fonts, label: str) -> None:
    draw.rounded_rectangle([MARGIN, 92, MARGIN + 20, 112], radius=6, fill=BRAND)
    draw.text((MARGIN + 36, 82), label, font=fonts.get("bold", 26), fill=BRAND)


def _draw_footer(draw: ImageDraw.ImageDraw, fonts: Fonts, title: str) -> None:
    caption = title if len(title) <= 30 else f"{title[:29]}{ELLIPSIS}"
    draw.text(
        (MARGIN, CANVAS_HEIGHT - 150), caption, font=fonts.get("regular", 26), fill=MUTED
    )
    hint = "卡片分镜 · 60 秒竖版"
    hint_font = fonts.get("regular", 24)
    hint_width = int(_measure(hint_font, hint))
    draw.text(
        (CANVAS_WIDTH - MARGIN - hint_width, CANVAS_HEIGHT - 150),
        hint,
        font=hint_font,
        fill=(107, 122, 144),
    )


def _draw_big_index(
    draw: ImageDraw.ImageDraw, fonts: Fonts, index: int, *, y: int = 1200
) -> None:
    font = fonts.get("bold", 260)
    text = f"{index:02d}"
    width = int(_measure(font, text))
    draw.text((CANVAS_WIDTH - MARGIN - width, y), text, font=font, fill=FAINT)


def _draw_title_card(
    draw: ImageDraw.ImageDraw, fonts: Fonts, scene: Scene, *, index: int, footer: str
) -> None:
    title_font = fonts.get("bold", 100)
    lines, _ = wrap_text(scene.text, title_font, CONTENT_WIDTH, max_lines=4)
    top = 380
    draw.rounded_rectangle([MARGIN, top - 56, MARGIN + 130, top - 42], radius=7, fill=BRAND)
    _draw_lines(
        draw,
        lines,
        font=title_font,
        fill=INK,
        x=MARGIN,
        y=top,
        line_height=134,
    )
    _draw_big_index(draw, fonts, index)
    _draw_footer(draw, fonts, footer)


def _draw_points_card(
    draw: ImageDraw.ImageDraw,
    fonts: Fonts,
    scene: Scene,
    *,
    visible: int,
    footer: str,
) -> None:
    heading_font = fonts.get("bold", 56)
    heading_lines, _ = wrap_text(scene.text, heading_font, CONTENT_WIDTH, max_lines=2)
    _draw_lines(
        draw,
        heading_lines,
        font=heading_font,
        fill=INK,
        x=MARGIN,
        y=210,
        line_height=76,
    )

    body_font = fonts.get("regular", 36)
    number_font = fonts.get("bold", 32)
    row_height = 132
    gap = 22
    top = 430
    for position, point in enumerate(scene.points[:visible]):
        row_top = top + position * (row_height + gap)
        draw.rounded_rectangle(
            [MARGIN, row_top, CANVAS_WIDTH - MARGIN, row_top + row_height],
            radius=28,
            fill=PANEL,
            outline=LINE,
            width=1,
        )
        center_x = MARGIN + 48
        center_y = row_top + row_height // 2
        draw.ellipse(
            [center_x - 30, center_y - 30, center_x + 30, center_y + 30], fill=BRAND
        )
        number = str(position + 1)
        number_width = _measure(number_font, number)
        draw.text(
            (center_x - number_width / 2, center_y - 21),
            number,
            font=number_font,
            fill=(255, 255, 255),
        )
        text_x = MARGIN + 102
        text_width = CANVAS_WIDTH - MARGIN - 40 - text_x
        point_lines, _ = wrap_text(point, body_font, text_width, max_lines=2)
        block_height = len(point_lines) * 50
        _draw_lines(
            draw,
            point_lines,
            font=body_font,
            fill=INK,
            x=text_x,
            y=row_top + (row_height - block_height) // 2,
            line_height=50,
        )
    _draw_footer(draw, fonts, footer)


def _draw_cta_card(
    draw: ImageDraw.ImageDraw, fonts: Fonts, scene: Scene, *, index: int, footer: str
) -> None:
    cta_font = fonts.get("bold", 92)
    lines, _ = wrap_text(scene.text, cta_font, CONTENT_WIDTH, max_lines=4)
    line_height = 126
    block_height = len(lines) * line_height
    top = 780 - block_height // 2
    _draw_lines(
        draw,
        lines,
        font=cta_font,
        fill=INK,
        x=MARGIN,
        y=top,
        line_height=line_height,
        width=CONTENT_WIDTH,
        align="center",
    )
    underline_y = top + block_height + 36
    draw.rounded_rectangle(
        [CANVAS_WIDTH // 2 - 90, underline_y, CANVAS_WIDTH // 2 + 90, underline_y + 10],
        radius=5,
        fill=BRAND,
    )
    _draw_big_index(draw, fonts, index)
    _draw_footer(draw, fonts, footer)


def _progressive_counts(count: int) -> list[int]:
    """要点卡的进度帧：最多 3 帧，依次展示更多要点，最后一帧显示全部。"""
    if count <= 1:
        return [max(1, count)]
    steps = min(3, count)
    result: list[int] = []
    for step in range(1, steps + 1):
        shown = math.ceil(count * step / steps)
        if not result or shown > result[-1]:
            result.append(shown)
    if result[-1] != count:
        result.append(count)
    return result


def _split_seconds(total: float, weights: list[int]) -> list[float]:
    total_weight = sum(weights)
    if total_weight <= 0:
        return [total]
    parts = [round(total * weight / total_weight, 3) for weight in weights[:-1]]
    last = round(total - sum(parts), 3)
    if last <= 0:
        last = 0.04
    parts.append(last)
    return parts


def render_storyboard(storyboard: Storyboard, out_dir: Path | str) -> list[Segment]:
    """把分镜渲染成 PNG 段序列；返回按播放顺序排列的 ``Segment``。"""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    fonts = Fonts()
    total = storyboard.seconds or 1.0
    segments: list[Segment] = []
    elapsed = 0.0

    for index, scene in enumerate(storyboard.scenes, start=1):
        label = f"{scene.type.upper()} / {index:02d}"
        if scene.type == "points":
            counts = _progressive_counts(len(scene.points))
            weights = [counts[0], *[counts[i] - counts[i - 1] for i in range(1, len(counts))]]
            durations = _split_seconds(scene.seconds, weights)
            for variant, (shown, seconds) in enumerate(
                zip(counts, durations, strict=True), start=1
            ):
                image = _base_canvas((elapsed + seconds / 2) / total)
                draw = ImageDraw.Draw(image)
                _draw_header(draw, fonts, label)
                _draw_points_card(
                    draw, fonts, scene, visible=shown, footer=storyboard.title
                )
                path = target / f"frame_{index:02d}_{variant:02d}.png"
                image.save(path, format="PNG")
                segments.append(Segment(path=path, seconds=seconds))
                elapsed += seconds
        else:
            image = _base_canvas((elapsed + scene.seconds / 2) / total)
            draw = ImageDraw.Draw(image)
            _draw_header(draw, fonts, label)
            if scene.type == "title":
                _draw_title_card(
                    draw, fonts, scene, index=index, footer=storyboard.title
                )
            else:
                _draw_cta_card(draw, fonts, scene, index=index, footer=storyboard.title)
            path = target / f"frame_{index:02d}_01.png"
            image.save(path, format="PNG")
            segments.append(Segment(path=path, seconds=scene.seconds))
            elapsed += scene.seconds

    if not segments:
        raise WorkbenchError("分镜没有任何场景，无法渲染卡片")
    return segments


__all__ = [
    "CANVAS_HEIGHT",
    "CANVAS_WIDTH",
    "Fonts",
    "Segment",
    "render_storyboard",
    "resolve_font_dir",
    "wrap_text",
]
