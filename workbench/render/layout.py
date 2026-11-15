"""声明式模板的排版计划：解析画布、块、slot，换行并校验安全区。

排版与绘制分离：本模块只产出「绘制计划」（每个块的 bbox 与文本行），
``workbench.render.blocks`` 再把它画到 Pillow 图像上。这样安全区检查、
叠字检查与确定性断言都可以脱离绘制单独测试。

坐标约定：块的 ``x`` / ``y`` 相对画布内边距（``canvas.pad``）后的左上角，
实际像素 = ``pad + x``。所有块的 bbox 必须落在
``[pad, W-pad] × [pad, H-pad]`` 内，否则抛 ``LayoutError``。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import ImageColor

from workbench.errors import WorkbenchError
from workbench.render import text as text_utils
from workbench.render.fonts import WEIGHTS, FontBook

BLOCK_TYPES: tuple[str, ...] = ("text", "list", "panel", "chips", "divider", "image")
ALIGNMENTS: tuple[str, ...] = ("left", "center", "right")
TEXT_LIKE: frozenset[str] = frozenset({"text", "list", "chips"})

DEFAULT_TOKENS: dict[str, Any] = {
    "placeholder": "「待补充」",
    "colors": {
        "ink": "#1F1F1F",
        "muted": "#8C8C8C",
        "line": "#E5E5E5",
        "bg": "#FFFFFF",
        "brand": "#FF5A36",
        "on_brand": "#FFFFFF",
        "panel": "#F7F7F7",
    },
    "font": {
        "sizes": {
            "display": 96,
            "title": 64,
            "heading": 48,
            "body": 32,
            "caption": 24,
            "micro": 20,
        },
        "line_height_ratio": 1.5,
    },
    "spacing": {"pad": 72, "gap_s": 16, "gap_m": 32, "gap_l": 56, "safe_margin": 72},
    "radius": {"panel": 32, "chip": 999, "image": 24},
}


class LayoutError(WorkbenchError):
    """模板版式错误（越界、叠字、非法宽高）；属于模板作者的错误，不是数据错误。"""


class TemplateError(WorkbenchError):
    """模板 JSON 结构不合法（缺字段、未知块类型等）。"""


@dataclass(slots=True)
class Canvas:
    width: int
    height: int
    background: str = "#FFFFFF"
    pad: int = 72


@dataclass(slots=True)
class PagePlan:
    """一页的绘制计划；``ops`` 按模板块顺序排列（后画的覆盖先画的）。"""

    canvas: Canvas
    ops: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    boxes: list[tuple[int, int, int, int]] = field(default_factory=list)

    @property
    def block_count(self) -> int:
        return len(self.ops)


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = json.loads(json.dumps(base))
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_tokens(template_dir: Path | str | None) -> dict[str, Any]:
    """读取 ``tokens.yaml``（JSON 语法的 YAML 子集），缺失或非法时用内置默认值。

    不引入 PyYAML：JSON 是 YAML 1.2 的子集，因此 ``tokens.yaml`` 用 JSON 语法
    写成，仍然是一个合法的 YAML 文件，同时只依赖标准库。
    """
    if template_dir is None:
        return _deep_merge(DEFAULT_TOKENS, {})
    path = Path(template_dir) / "tokens.yaml"
    if not path.is_file():
        return _deep_merge(DEFAULT_TOKENS, {})
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TemplateError(f"tokens.yaml 不是合法的 JSON/YAML 子集：{exc}") from exc
    if not isinstance(raw, Mapping):
        raise TemplateError("tokens.yaml 顶层必须是对象（mapping）")
    return _deep_merge(DEFAULT_TOKENS, raw)


def _as_int(block: Mapping[str, Any], key: str, default: int, *, minimum: int = 0) -> int:
    value = block.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TemplateError(f"{key} 必须是整数，收到 {value!r}")
    if value < minimum:
        raise TemplateError(f"{key} 不能小于 {minimum}，收到 {value}")
    return value


def _as_align(value: object, default: str = "left") -> str:
    align = str(value if value is not None else default)
    if align not in ALIGNMENTS:
        raise TemplateError(f"align 只支持 {'/'.join(ALIGNMENTS)}，收到 {align!r}")
    return align


def _as_weight(value: object, default: str = "regular") -> str:
    weight = str(value if value is not None else default)
    if weight not in WEIGHTS:
        raise TemplateError(f"weight 只支持 {'/'.join(WEIGHTS)}，收到 {weight!r}")
    return weight


def _as_color(value: object, key: str) -> tuple[int, int, int]:
    raw = str(value)
    try:
        rgb = ImageColor.getrgb(raw)
    except ValueError as exc:
        raise TemplateError(f"{key} 不是合法颜色：{raw!r}") from exc
    return (int(rgb[0]), int(rgb[1]), int(rgb[2]))


def parse_canvas(template: Mapping[str, Any]) -> Canvas:
    canvas = template.get("canvas")
    if not isinstance(canvas, Mapping):
        raise TemplateError("模板缺少 canvas 对象")
    width = canvas.get("width")
    height = canvas.get("height")
    if isinstance(width, bool) or not isinstance(width, int) or width <= 0:
        raise TemplateError(f"canvas.width 必须是正整数，收到 {width!r}")
    if isinstance(height, bool) or not isinstance(height, int) or height <= 0:
        raise TemplateError(f"canvas.height 必须是正整数，收到 {height!r}")
    pad = canvas.get("pad", 72)
    if isinstance(pad, bool) or not isinstance(pad, int) or pad < 0:
        raise TemplateError(f"canvas.pad 必须是非负整数，收到 {pad!r}")
    if pad * 2 >= min(width, height):
        raise TemplateError(f"canvas.pad={pad} 过大，画布 {width}×{height} 没有可用安全区")
    background = str(canvas.get("background", "#FFFFFF"))
    _as_color(background, "canvas.background")
    return Canvas(width=width, height=height, background=background, pad=pad)


def _slot_text(slots: Mapping[str, Any], name: str, placeholder: str) -> str:
    if name not in slots or slots[name] is None:
        return placeholder
    value = slots[name]
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return "、".join(items) if items else placeholder
    normalized = text_utils.normalize_text(value)
    return normalized if normalized.strip() else placeholder


def _slot_items(slots: Mapping[str, Any], name: str, placeholder: str) -> list[str]:
    if name not in slots or slots[name] is None:
        return [placeholder]
    value = slots[name]
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items or [placeholder]
    normalized = text_utils.normalize_text(value).strip()
    if not normalized:
        return [placeholder]
    parts = [part.strip() for part in re.split(r"[\n,，、;；]+", normalized) if part.strip()]
    return parts or [placeholder]


def _text_block(
    block: Mapping[str, Any],
    slots: Mapping[str, Any],
    tokens: dict[str, Any],
    fonts: FontBook,
    canvas: Canvas,
    index: int,
) -> dict[str, Any]:
    slot = block.get("slot")
    if not isinstance(slot, str) or not slot:
        raise TemplateError(f"blocks[{index}] text 块缺少 slot")
    sizes = tokens["font"]["sizes"]
    size = _as_int(block, "size", int(sizes.get("body", 32)), minimum=1)
    weight = _as_weight(block.get("weight"))
    color = _as_color(block.get("color", tokens["colors"]["ink"]), f"blocks[{index}].color")
    x = _as_int(block, "x", 0)
    y = _as_int(block, "y", 0)
    max_width = _as_int(block, "max_width", canvas.width - 2 * canvas.pad, minimum=1)
    max_lines = _as_int(block, "max_lines", 8, minimum=1)
    ratio = float(tokens["font"].get("line_height_ratio", 1.5))
    line_height = _as_int(block, "line_height", max(1, round(size * ratio)), minimum=1)
    align = _as_align(block.get("align"))
    lines, truncated = text_utils.wrap_text(
        _slot_text(slots, slot, str(tokens["placeholder"])),
        fonts.font(weight, size),
        max_width,
        max_lines,
    )
    warnings: list[str] = []
    if truncated:
        warnings.append(
            f"模板 blocks[{index}]（text, slot={slot}）文案超过 max_lines={max_lines}，"
            f"已截断并追加省略号"
        )
    # bbox 按模板声明的最大行数预留，保证越界/叠字检查与数据长度无关。
    height = max_lines * line_height
    return {
        "type": "text",
        "index": index,
        "bbox": (x, y, x + max_width, y + height),
        "slot": slot,
        "lines": lines,
        "size": size,
        "weight": weight,
        "color": color,
        "line_height": line_height,
        "max_width": max_width,
        "align": align,
        "warnings": warnings,
    }


def _list_block(
    block: Mapping[str, Any],
    slots: Mapping[str, Any],
    tokens: dict[str, Any],
    fonts: FontBook,
    canvas: Canvas,
    index: int,
) -> dict[str, Any]:
    slot = block.get("slot")
    if not isinstance(slot, str) or not slot:
        raise TemplateError(f"blocks[{index}] list 块缺少 slot")
    sizes = tokens["font"]["sizes"]
    size = _as_int(block, "size", int(sizes.get("body", 32)), minimum=1)
    weight = _as_weight(block.get("weight"))
    color = _as_color(block.get("color", tokens["colors"]["ink"]), f"blocks[{index}].color")
    x = _as_int(block, "x", 0)
    y = _as_int(block, "y", 0)
    max_width = _as_int(block, "max_width", canvas.width - 2 * canvas.pad, minimum=1)
    max_lines = _as_int(block, "max_lines", 10, minimum=1)
    ratio = float(tokens["font"].get("line_height_ratio", 1.5))
    line_height = _as_int(block, "line_height", max(1, round(size * ratio)), minimum=1)
    item_gap = _as_int(block, "item_gap", int(tokens["spacing"].get("gap_s", 16)))
    bullet = str(block.get("bullet", "·"))
    align = _as_align(block.get("align"))

    font = fonts.font(weight, size)
    bullet_width = int(text_utils.measure(font, bullet + " ")) if bullet else 0
    text_width = max(1, max_width - bullet_width)
    items = _slot_items(slots, slot, str(tokens["placeholder"]))

    rendered: list[list[str]] = []
    used = 0
    truncated = False
    for position, item in enumerate(items):
        remaining = max_lines - used
        if remaining <= 0:
            truncated = True
            break
        lines, cut = text_utils.wrap_text(item, font, text_width, remaining)
        truncated = truncated or cut
        rendered.append(lines)
        used += len(lines)
        if used >= max_lines and position < len(items) - 1:
            truncated = True
            break

    warnings: list[str] = []
    if truncated:
        warnings.append(
            f"模板 blocks[{index}]（list, slot={slot}）文案超过 max_lines={max_lines}，"
            f"已截断并追加省略号"
        )
    # 预留高度按最坏情况（每个 item 占一行、共 max_lines 行）计算。
    height = max_lines * line_height + max(0, max_lines - 1) * item_gap
    return {
        "type": "list",
        "index": index,
        "bbox": (x, y, x + max_width, y + height),
        "slot": slot,
        "items": rendered,
        "size": size,
        "weight": weight,
        "color": color,
        "line_height": line_height,
        "item_gap": item_gap,
        "bullet": bullet,
        "bullet_width": bullet_width,
        "max_width": max_width,
        "align": align,
        "warnings": warnings,
    }


def _panel_block(
    block: Mapping[str, Any],
    slots: Mapping[str, Any],
    tokens: dict[str, Any],
    fonts: FontBook,
    canvas: Canvas,
    index: int,
) -> dict[str, Any]:
    x = _as_int(block, "x", 0)
    y = _as_int(block, "y", 0)
    width = _as_int(block, "width", 1, minimum=1)
    height = _as_int(block, "height", 1, minimum=1)
    fill = _as_color(
        block.get("fill", tokens["colors"].get("panel", "#F7F7F7")),
        f"blocks[{index}].fill",
    )
    radius = _as_int(block, "radius", int(tokens["radius"].get("panel", 32)))
    stroke_raw = block.get("stroke")
    stroke = _as_color(stroke_raw, f"blocks[{index}].stroke") if stroke_raw else None
    stroke_width = _as_int(block, "stroke_width", 2 if stroke else 0)
    op: dict[str, Any] = {
        "type": "panel",
        "index": index,
        "bbox": (x, y, x + width, y + height),
        "fill": fill,
        "radius": radius,
        "stroke": stroke,
        "stroke_width": stroke_width,
        "text": None,
    }

    slot = block.get("slot")
    if slot is not None:
        if not isinstance(slot, str) or not slot:
            raise TemplateError(f"blocks[{index}] panel 块的 slot 必须是非空字符串")
        sizes = tokens["font"]["sizes"]
        size = _as_int(block, "size", int(sizes.get("body", 32)), minimum=1)
        weight = _as_weight(block.get("weight"))
        color = _as_color(block.get("color", tokens["colors"]["ink"]), f"blocks[{index}].color")
        padding = _as_int(block, "padding", int(tokens["spacing"].get("gap_m", 32)))
        ratio = float(tokens["font"].get("line_height_ratio", 1.5))
        line_height = _as_int(block, "line_height", max(1, round(size * ratio)), minimum=1)
        align = _as_align(block.get("align"))
        inner_width = max(1, width - 2 * padding)
        room = max(1, (height - 2 * padding) // line_height)
        requested = _as_int(block, "max_lines", room, minimum=1)
        lines, truncated = text_utils.wrap_text(
            _slot_text(slots, slot, str(tokens["placeholder"])),
            fonts.font(weight, size),
            inner_width,
            min(requested, room),
        )
        if truncated:
            op["warnings"] = [
                f"模板 blocks[{index}]（panel, slot={slot}）文案超过面板可用行数，"
                f"已截断并追加省略号"
            ]
        op["text"] = {
            "x": x + padding,
            "y": y + padding,
            "max_width": inner_width,
            "lines": lines,
            "size": size,
            "weight": weight,
            "color": color,
            "line_height": line_height,
            "align": align,
        }
    return op


def _chips_block(
    block: Mapping[str, Any],
    slots: Mapping[str, Any],
    tokens: dict[str, Any],
    fonts: FontBook,
    canvas: Canvas,
    index: int,
) -> dict[str, Any]:
    slot = block.get("slot")
    if not isinstance(slot, str) or not slot:
        raise TemplateError(f"blocks[{index}] chips 块缺少 slot")
    sizes = tokens["font"]["sizes"]
    size = _as_int(block, "size", int(sizes.get("caption", 24)), minimum=1)
    weight = _as_weight(block.get("weight"))
    color = _as_color(
        block.get("color", tokens["colors"].get("on_brand", "#FFFFFF")),
        f"blocks[{index}].color",
    )
    background = _as_color(
        block.get("background", tokens["colors"].get("brand", "#FF5A36")),
        f"blocks[{index}].background",
    )
    x = _as_int(block, "x", 0)
    y = _as_int(block, "y", 0)
    max_width = _as_int(block, "max_width", canvas.width - 2 * canvas.pad, minimum=1)
    max_rows = _as_int(block, "max_rows", 2, minimum=1)
    gap = _as_int(block, "gap", int(tokens["spacing"].get("gap_s", 16)))
    padding = _as_int(block, "padding", 12)
    radius = _as_int(block, "radius", int(tokens["radius"].get("chip", 999)))
    align = _as_align(block.get("align"))

    font = fonts.font(weight, size)
    chip_height = size + 2 * padding
    items = _slot_items(slots, slot, str(tokens["placeholder"]))
    rows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_width = 0
    for item in items:
        label, cut = text_utils.single_line(item, font, max(1, max_width - 2 * padding))
        chip_width = int(text_utils.measure(font, label)) + 2 * padding
        proposed = chip_width if not current else current_width + gap + chip_width
        if current and proposed > max_width:
            rows.append(current)
            current = []
            current_width = 0
            proposed = chip_width
        current.append({"text": label, "width": chip_width, "truncated": cut})
        current_width = proposed
    if current:
        rows.append(current)

    truncated = any(chip["truncated"] for row in rows for chip in row)
    if len(rows) > max_rows:
        rows = rows[:max_rows]
        truncated = True
        last = rows[-1]
        ellipsis_width = int(text_utils.measure(font, text_utils.ELLIPSIS)) + 2 * padding
        if last and last[-1]["width"] + gap + ellipsis_width <= max_width:
            last.append({"text": text_utils.ELLIPSIS, "width": ellipsis_width, "truncated": False})
        elif last:
            last[-1]["text"] = text_utils.ELLIPSIS
            last[-1]["width"] = ellipsis_width

    warnings: list[str] = []
    if truncated:
        warnings.append(
            f"模板 blocks[{index}]（chips, slot={slot}）标签超出 max_rows={max_rows} 或宽度，"
            f"已截断并追加省略号"
        )

    ops_rows: list[list[dict[str, Any]]] = []
    for row_index, row in enumerate(rows):
        row_width = sum(chip["width"] for chip in row) + max(0, len(row) - 1) * gap
        if align == "center":
            cursor = x + (max_width - row_width) // 2
        elif align == "right":
            cursor = x + max_width - row_width
        else:
            cursor = x
        placed: list[dict[str, Any]] = []
        for chip in row:
            placed.append(
                {
                    "text": chip["text"],
                    "x": cursor,
                    "y": y + row_index * (chip_height + gap),
                    "width": chip["width"],
                    "height": chip_height,
                }
            )
            cursor += chip["width"] + gap
        ops_rows.append(placed)

    height = max_rows * chip_height + max(0, max_rows - 1) * gap
    return {
        "type": "chips",
        "index": index,
        "bbox": (x, y, x + max_width, y + height),
        "slot": slot,
        "rows": ops_rows,
        "size": size,
        "weight": weight,
        "color": color,
        "background": background,
        "radius": radius,
        "warnings": warnings,
    }


def _divider_block(block: Mapping[str, Any], tokens: dict[str, Any], index: int) -> dict[str, Any]:
    x = _as_int(block, "x", 0)
    y = _as_int(block, "y", 0)
    width = _as_int(block, "width", 1, minimum=1)
    thickness = _as_int(block, "thickness", 2, minimum=1)
    color = _as_color(block.get("color", tokens["colors"]["line"]), f"blocks[{index}].color")
    return {
        "type": "divider",
        "index": index,
        "bbox": (x, y, x + width, y + thickness),
        "color": color,
        "thickness": thickness,
        "warnings": [],
    }


def _image_block(
    block: Mapping[str, Any],
    slots: Mapping[str, Any],
    tokens: dict[str, Any],
    canvas: Canvas,
    index: int,
) -> dict[str, Any]:
    x = _as_int(block, "x", 0)
    y = _as_int(block, "y", 0)
    width = _as_int(block, "width", 1, minimum=1)
    height = _as_int(block, "height", 1, minimum=1)
    fill = _as_color(block.get("fill", "#EEEEEE"), f"blocks[{index}].fill")
    radius = _as_int(block, "radius", int(tokens["radius"].get("image", 24)))
    stroke_raw = block.get("stroke")
    stroke = _as_color(stroke_raw, f"blocks[{index}].stroke") if stroke_raw else None
    size = _as_int(block, "label_size", int(tokens["font"]["sizes"].get("caption", 24)), minimum=1)
    label_weight = _as_weight(block.get("label_weight"))
    label_color = _as_color(
        block.get("label_color", tokens["colors"]["muted"]),
        f"blocks[{index}].label_color",
    )

    path_value = ""
    slot = block.get("slot")
    if slot is not None:
        if not isinstance(slot, str) or not slot:
            raise TemplateError(f"blocks[{index}] image 块的 slot 必须是非空字符串")
        raw = slots.get(slot)
        if isinstance(raw, (str, Path)):
            path_value = str(raw).strip()
    explicit = block.get("path")
    if isinstance(explicit, str) and explicit.strip():
        path_value = explicit.strip()

    label = block.get("label")
    if not isinstance(label, str):
        label = ""
    label_slot = block.get("label_slot")
    if isinstance(label_slot, str) and label_slot and slots.get(label_slot):
        label = _slot_text(slots, label_slot, label or str(tokens["placeholder"]))

    warnings: list[str] = []
    resolved = Path(path_value).expanduser() if path_value else None
    if resolved is not None and not resolved.is_file():
        warnings.append(
            f"模板 blocks[{index}]（image）引用的图片不存在：{resolved}，已画占位框"
        )
        resolved = None
    if resolved is None and not label:
        label = str(tokens["placeholder"])

    return {
        "type": "image",
        "index": index,
        "bbox": (x, y, x + width, y + height),
        "path": str(resolved) if resolved is not None else None,
        "fill": fill,
        "stroke": stroke,
        "radius": radius,
        "label": label,
        "label_size": size,
        "label_weight": label_weight,
        "label_color": label_color,
        "max_width": width,
        "align": "center",
        "warnings": warnings,
    }


def plan_page(
    blocks: Sequence[Mapping[str, Any]],
    slots: Mapping[str, Any],
    tokens: dict[str, Any],
    fonts: FontBook,
    canvas: Canvas,
    *,
    template_id: str,
    page: int = 1,
) -> PagePlan:
    """把一页模板 + 数据排成绘制计划，并做安全区/叠字检查。"""
    if not isinstance(blocks, Sequence) or not blocks:
        raise TemplateError(f"模板 {template_id} 第 {page} 页缺少 blocks")
    ops: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, raw in enumerate(blocks):
        if not isinstance(raw, Mapping):
            raise TemplateError(f"模板 {template_id} 第 {page} 页 blocks[{index}] 必须是对象")
        block_type = raw.get("type")
        if block_type not in BLOCK_TYPES:
            raise TemplateError(
                f"模板 {template_id} 第 {page} 页 blocks[{index}] 块类型未知："
                f"{block_type!r}；可选：{', '.join(BLOCK_TYPES)}"
            )
        if block_type == "text":
            op = _text_block(raw, slots, tokens, fonts, canvas, index)
        elif block_type == "list":
            op = _list_block(raw, slots, tokens, fonts, canvas, index)
        elif block_type == "panel":
            op = _panel_block(raw, slots, tokens, fonts, canvas, index)
        elif block_type == "chips":
            op = _chips_block(raw, slots, tokens, fonts, canvas, index)
        elif block_type == "divider":
            op = _divider_block(raw, tokens, index)
        else:
            op = _image_block(raw, slots, tokens, canvas, index)
        ops.append(op)
        warnings.extend(op.get("warnings", []))

    plan = PagePlan(canvas=canvas, ops=ops, warnings=warnings)
    plan.boxes = [(op["bbox"][0], op["bbox"][1], op["bbox"][2], op["bbox"][3]) for op in ops]
    validate_plan(plan, template_id=template_id, page=page)
    return plan


def _safe_area(canvas: Canvas) -> tuple[int, int, int, int]:
    return (canvas.pad, canvas.pad, canvas.width - canvas.pad, canvas.height - canvas.pad)


def _intersects(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def validate_plan(plan: PagePlan, *, template_id: str, page: int = 1) -> None:
    """检查每个块 bbox 在安全区内，且文本类块之间不重叠（不允许越界或叠字）。"""
    canvas = plan.canvas
    left, top, right, bottom = _safe_area(canvas)
    for op in plan.ops:
        x1, y1, x2, y2 = (int(value) for value in op["bbox"])
        if x2 <= x1 or y2 <= y1:
            raise LayoutError(
                f"模板 {template_id} 第 {page} 页 blocks[{op['index']}]（{op['type']}）"
                f"宽或高不为正：bbox=({x1}, {y1}, {x2}, {y2})"
            )
        if x1 < left or y1 < top or x2 > right or y2 > bottom:
            raise LayoutError(
                f"模板 {template_id} 第 {page} 页 blocks[{op['index']}]（{op['type']}）越界："
                f"bbox=({x1}, {y1}, {x2}, {y2}) 不在安全区 "
                f"({left}, {top}, {right}, {bottom}) 内（画布 "
                f"{canvas.width}×{canvas.height}，pad {canvas.pad}）"
            )

    text_ops = [op for op in plan.ops if op["type"] in TEXT_LIKE]
    for position, first in enumerate(text_ops):
        for second in text_ops[position + 1 :]:
            if _intersects(first["bbox"], second["bbox"]):
                raise LayoutError(
                    f"模板 {template_id} 第 {page} 页文本块重叠（叠字）："
                    f"blocks[{first['index']}]（{first['type']}）"
                    f"bbox={first['bbox']} 与 blocks[{second['index']}]（{second['type']}）"
                    f"bbox={second['bbox']}"
                )


__all__ = [
    "ALIGNMENTS",
    "BLOCK_TYPES",
    "DEFAULT_TOKENS",
    "TEXT_LIKE",
    "Canvas",
    "LayoutError",
    "PagePlan",
    "TemplateError",
    "load_tokens",
    "parse_canvas",
    "plan_page",
    "validate_plan",
]
