"""``Renderer`` 的 Pillow 实现与模板加载（ADR §6.4）。

模板目录默认是仓库内 ``templates/``（可用 ``WB_TEMPLATE_DIR`` 覆盖）：

- ``poster/<id>.json``：单页海报；
- ``carousel/<id>.json``：多页图文，``pages`` 数组决定页数；
- ``tokens.yaml``：品牌色、字号、间距与占位文案（JSON 语法的 YAML 子集）。

``build_renderer()`` 是契约固定入口，返回 ``PillowRenderer``。渲染不访问网络，
同输入同模板的输出结构（尺寸、块位置、文本）确定。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from workbench.contracts import RenderResult, RenderSpec
from workbench.errors import Unavailable, WorkbenchError
from workbench.render.blocks import draw_plan
from workbench.render.fonts import FontBook
from workbench.render.layout import (
    BLOCK_TYPES,
    Canvas,
    TemplateError,
    load_tokens,
    parse_canvas,
    plan_page,
)

logger = logging.getLogger("workbench.render")

KINDS: tuple[str, ...] = ("poster", "carousel")
_KIND_ALIASES = {"poster": "poster", "carousel": "carousel", "cover": "carousel"}

_REPO_ROOT = Path(__file__).resolve().parents[2]


class TemplateNotSpecified(Unavailable):
    """``RenderSpec.template`` 为空：无法确定画布与版式。"""

    def __init__(self) -> None:
        self.capability = "渲染模板"
        self.ticket = "T04"
        WorkbenchError.__init__(
            self,
            "Unavailable: 未指定渲染模板（请用 --template 或 RenderSpec.template 指定模板 ID）",
        )


@dataclass(slots=True)
class TemplateInfo:
    """一套模板的元信息，供 ``wb render list`` 与渲染使用。"""

    id: str
    label: str
    kind: str
    width: int
    height: int
    pages: int
    path: Path


def default_template_dir() -> Path:
    """``WB_TEMPLATE_DIR`` 未设置时的默认模板目录（仓库内 ``templates/``）。"""
    return _REPO_ROOT / "templates"


def resolve_template_dir(explicit: Path | str | None = None) -> Path:
    """解析模板目录：显式参数 > ``WB_TEMPLATE_DIR`` > 仓库内 ``templates/``。"""
    raw = explicit if explicit is not None else os.environ.get("WB_TEMPLATE_DIR")
    if raw is None or str(raw).strip() == "":
        return default_template_dir()
    return Path(raw).expanduser().resolve()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise TemplateError(f"模板文件读取失败：{path}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise TemplateError(f"模板不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise TemplateError(f"模板顶层必须是对象：{path}")
    return raw


def _validate_blocks(blocks: object, *, template_id: str, page: int) -> list[dict[str, Any]]:
    if not isinstance(blocks, list) or not blocks:
        raise TemplateError(f"模板 {template_id} 第 {page} 页 blocks 必须是非空数组")
    normalized: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise TemplateError(f"模板 {template_id} 第 {page} 页 blocks[{index}] 必须是对象")
        block_type = block.get("type")
        if block_type not in BLOCK_TYPES:
            raise TemplateError(
                f"模板 {template_id} 第 {page} 页 blocks[{index}] 块类型未知：{block_type!r}"
            )
        normalized.append(block)
    return normalized


def _validate_template(
    raw: Mapping[str, Any], *, path: Path, kind: str
) -> dict[str, Any]:
    template_id = raw.get("id")
    if not isinstance(template_id, str) or not template_id.strip():
        raise TemplateError(f"模板缺少 id：{path}")
    if template_id != path.stem:
        raise TemplateError(
            f"模板 id 必须与文件名一致：{template_id!r} != {path.stem!r}（{path}）"
        )
    label = raw.get("label")
    if not isinstance(label, str) or not label.strip():
        raise TemplateError(f"模板 {template_id} 缺少 label")
    parse_canvas(raw)
    if kind == "poster":
        _validate_blocks(raw.get("blocks"), template_id=template_id, page=1)
    else:
        pages = raw.get("pages")
        if not isinstance(pages, list) or not pages:
            raise TemplateError(f"图文模板 {template_id} 的 pages 必须是非空数组")
        for page_index, page in enumerate(pages, start=1):
            if not isinstance(page, Mapping):
                raise TemplateError(f"模板 {template_id} 第 {page_index} 页必须是对象")
            _validate_blocks(page.get("blocks"), template_id=template_id, page=page_index)
    return dict(raw)


def _template_info(template: Mapping[str, Any], path: Path, kind: str) -> TemplateInfo:
    canvas = parse_canvas(template)
    pages = 1 if kind == "poster" else len(list(template.get("pages") or []))
    return TemplateInfo(
        id=str(template["id"]),
        label=str(template["label"]),
        kind=kind,
        width=canvas.width,
        height=canvas.height,
        pages=pages,
        path=path,
    )


def _block_summary(op: Mapping[str, Any]) -> dict[str, Any]:
    """块的结构摘要：用于 ``RenderResult.meta`` 与确定性断言。"""
    summary: dict[str, Any] = {"type": op["type"], "bbox": list(op["bbox"])}
    if op["type"] == "text":
        summary["lines"] = list(op["lines"])
    elif op["type"] == "list":
        summary["items"] = [list(lines) for lines in op["items"]]
    elif op["type"] == "panel":
        payload = op.get("text")
        summary["lines"] = list(payload["lines"]) if payload else []
    elif op["type"] == "chips":
        summary["labels"] = [chip["text"] for row in op["rows"] for chip in row]
    elif op["type"] == "image":
        summary["label"] = op.get("label") or ""
        summary["placeholder"] = op.get("path") is None
    return summary


def _page_slots(base: Mapping[str, Any], override: object) -> dict[str, Any]:
    if isinstance(override, Mapping):
        nested = override.get("slots")
        source = nested if isinstance(nested, Mapping) else override
        merged = dict(base)
        merged.update(source)
        return merged
    return dict(base)



class PillowRenderer:
    """声明式模板 + Pillow 的渲染器；实现 ``workbench.contracts.Renderer``。"""

    def __init__(
        self,
        template_dir: Path | str | None = None,
        *,
        font_dir: Path | str | None = None,
    ) -> None:
        self.template_dir = resolve_template_dir(template_dir)
        self.font_dir = font_dir

    # ------------------------------------------------------------------ 模板
    def template_paths(self, kind: str) -> list[Path]:
        directory = self.template_dir / kind
        if not directory.is_dir():
            return []
        return sorted(path for path in directory.glob("*.json") if path.is_file())

    def load_template(self, kind: str, template_id: str) -> tuple[dict[str, Any], Path]:
        if kind not in KINDS:
            raise TemplateError(f"未知模板类型：{kind!r}；可选：{', '.join(KINDS)}")
        for path in self.template_paths(kind):
            if path.stem == template_id:
                return _validate_template(_read_json(path), path=path, kind=kind), path
        available = [path.stem for path in self.template_paths(kind)]
        raise TemplateError(
            f"找不到 {kind} 模板 {template_id!r}；可选：{', '.join(available) or '（无）'}"
        )

    def list_templates(self) -> list[TemplateInfo]:
        infos: list[TemplateInfo] = []
        for kind in KINDS:
            for path in self.template_paths(kind):
                template = _validate_template(_read_json(path), path=path, kind=kind)
                infos.append(_template_info(template, path, kind))
        return infos

    # ------------------------------------------------------------------ 渲染
    def _resolve_kind(self, spec: RenderSpec) -> str:
        requested = _KIND_ALIASES.get(str(spec.kind or "").strip().lower())
        return requested or "poster"

    def render(self, spec: RenderSpec) -> RenderResult:
        template_id = (spec.template or "").strip()
        if not template_id:
            raise TemplateNotSpecified()
        if not isinstance(spec.data, Mapping):
            raise WorkbenchError("渲染数据必须是对象：{\"slots\": {...}, \"meta\": {...}}")
        data = dict(spec.data)
        slots = data.get("slots", {})
        if not isinstance(slots, Mapping):
            raise WorkbenchError("渲染数据的 slots 必须是对象")
        kind = self._resolve_kind(spec)
        template, _path = self.load_template(kind, template_id)
        canvas = parse_canvas(template)
        fonts = FontBook(self.font_dir)
        tokens = load_tokens(self.template_dir)

        if kind == "poster":
            return self._render_poster(spec, template, canvas, dict(slots), tokens, fonts)
        return self._render_carousel(spec, template, canvas, data, dict(slots), tokens, fonts)

    def _pages_of(self, template: Mapping[str, Any]) -> list[dict[str, Any]]:
        pages = template.get("pages")
        return [dict(page) for page in pages] if isinstance(pages, list) else []

    def _render_poster(
        self,
        spec: RenderSpec,
        template: Mapping[str, Any],
        canvas: Canvas,
        slots: dict[str, Any],
        tokens: dict[str, Any],
        fonts: FontBook,
    ) -> RenderResult:
        plan = plan_page(
            template["blocks"],
            slots,
            tokens,
            fonts,
            canvas,
            template_id=str(template["id"]),
            page=1,
        )
        out = Path(spec.out).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (canvas.width, canvas.height), canvas.background)
        draw_plan(image, plan, fonts)
        image.save(out, format="PNG")
        for warning in plan.warnings:
            logger.warning(warning)

        page_meta = self._page_meta(1, out.name, canvas, plan, slots)
        meta: dict[str, Any] = {
            "template": template["id"],
            "label": template["label"],
            "kind": "poster",
            "width": canvas.width,
            "height": canvas.height,
            "page_count": 1,
            "pages": [page_meta],
            "warnings": list(plan.warnings),
        }
        return RenderResult(
            path=out,
            width=canvas.width,
            height=canvas.height,
            bytes=out.stat().st_size,
            meta=meta,
        )

    def _render_carousel(
        self,
        spec: RenderSpec,
        template: Mapping[str, Any],
        canvas: Canvas,
        data: Mapping[str, Any],
        base_slots: dict[str, Any],
        tokens: dict[str, Any],
        fonts: FontBook,
    ) -> RenderResult:
        pages = self._pages_of(template)
        out_dir = Path(spec.out).expanduser().resolve()
        if out_dir.exists() and not out_dir.is_dir():
            raise WorkbenchError(f"图文模板的输出必须是目录：{out_dir}")
        out_dir.mkdir(parents=True, exist_ok=True)
        overrides = data.get("pages")
        page_overrides = overrides if isinstance(overrides, list) else []

        page_meta: list[dict[str, Any]] = []
        warnings: list[str] = []
        total_bytes = 0
        digits = max(2, len(str(len(pages))))
        for page_index, page in enumerate(pages, start=1):
            override = (
                page_overrides[page_index - 1]
                if page_index <= len(page_overrides)
                else None
            )
            slots = _page_slots(base_slots, override)
            plan = plan_page(
                page["blocks"],
                slots,
                tokens,
                fonts,
                canvas,
                template_id=str(template["id"]),
                page=page_index,
            )
            filename = f"{page_index:0{digits}d}.png"
            path = out_dir / filename
            image = Image.new("RGB", (canvas.width, canvas.height), canvas.background)
            draw_plan(image, plan, fonts)
            image.save(path, format="PNG")
            total_bytes += path.stat().st_size
            for warning in plan.warnings:
                logger.warning(warning)
            warnings.extend(plan.warnings)
            page_meta.append(self._page_meta(page_index, filename, canvas, plan, slots))

        manifest = {
            "template": template["id"],
            "label": template["label"],
            "kind": "carousel",
            "width": canvas.width,
            "height": canvas.height,
            "page_count": len(pages),
            "pages": page_meta,
            "warnings": warnings,
        }
        manifest_path = out_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        meta = dict(manifest)
        meta["manifest"] = manifest_path.name
        return RenderResult(
            path=out_dir,
            width=canvas.width,
            height=canvas.height,
            bytes=total_bytes,
            meta=meta,
        )

    @staticmethod
    def _page_meta(
        page: int,
        filename: str,
        canvas: Canvas,
        plan: Any,
        slots: Mapping[str, Any],
    ) -> dict[str, Any]:
        return {
            "page": page,
            "file": filename,
            "width": canvas.width,
            "height": canvas.height,
            "block_count": len(plan.ops),
            "blocks": [_block_summary(op) for op in plan.ops],
            "slots": json.loads(json.dumps(slots, ensure_ascii=False, default=str)),
            "warnings": list(plan.warnings),
        }


def build_renderer() -> PillowRenderer:
    """契约固定工厂：``workbench.contracts.get_renderer()`` 用它加载实现。"""
    return PillowRenderer()


__all__ = [
    "KINDS",
    "PillowRenderer",
    "TemplateInfo",
    "TemplateNotSpecified",
    "build_renderer",
    "default_template_dir",
    "resolve_template_dir",
]
