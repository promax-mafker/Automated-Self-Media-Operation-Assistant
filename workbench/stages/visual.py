"""T04 stage：``render_assets`` —— 把一份文案数据渲染成海报/图文并落库。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workbench.config import now_iso
from workbench.contracts import RenderSpec, get_renderer
from workbench.errors import WorkbenchError
from workbench.ids import new_id
from workbench.pipeline.registry import register
from workbench.pipeline.stage import RunContext
from workbench.store import repo
from workbench.store.models import Asset


def _load_data_file(raw: str) -> dict[str, Any]:
    path = Path(raw).expanduser()
    if not path.is_file():
        raise WorkbenchError(f"render_assets: 数据文件不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkbenchError(f"render_assets: 数据文件不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise WorkbenchError(f"render_assets: 数据文件顶层必须是对象：{path}")
    return data


def _slots_from_copy_set(ctx: RunContext, copy_set_id: str) -> dict[str, Any]:
    copy_set = repo.get_copy_set(ctx.db, copy_set_id)
    if copy_set is None:
        raise WorkbenchError(f"render_assets: 找不到 copy_set：{copy_set_id}")
    points = [line.strip() for line in copy_set.body.splitlines() if line.strip()][:6]
    slots: dict[str, Any] = {
        "title": copy_set.title or copy_set.hook or "",
        "subtitle": copy_set.hook or "",
        "body": copy_set.body,
        "tags": list(copy_set.tags),
    }
    if points:
        slots["points"] = points
    return slots


@register("render_assets")
class RenderAssets:
    """``wb run visual --input template=<id> [--input data=<json>|copy_set_id=<id>]``。"""

    name = "render_assets"
    needs: tuple[str, ...] = ("template",)

    def run(self, ctx: RunContext) -> dict[str, Any]:
        template_id = (ctx.params.get("template") or "").strip()
        if not template_id:
            raise WorkbenchError("render_assets: template 不能为空")
        copy_set_id = (ctx.params.get("copy_set_id") or "").strip() or None
        data_param = (ctx.params.get("data") or "").strip()

        if data_param:
            data = _load_data_file(data_param)
        elif copy_set_id:
            data = {
                "slots": _slots_from_copy_set(ctx, copy_set_id),
                "meta": {"copy_set_id": copy_set_id},
            }
        else:
            raise WorkbenchError(
                "render_assets: 需要 --input data=<数据 JSON> 或 --input copy_set_id=<copy_set ID>"
            )

        renderer = get_renderer()
        kind = (ctx.params.get("kind") or "").strip()
        if not kind:
            lister = getattr(renderer, "list_templates", None)
            if callable(lister):
                for info in lister():
                    if info.id == template_id:
                        kind = info.kind
                        break
        kind = kind or "poster"

        assets_dir = ctx.out / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        if kind == "poster":
            out = assets_dir / f"{template_id}.png"
        else:
            out = assets_dir / template_id
        spec = RenderSpec(kind=kind, out=out, template=template_id, data=data)
        result = renderer.render(spec)

        warnings = [str(item) for item in result.meta.get("warnings", [])]
        for warning in warnings:
            ctx.log(f"render_assets: warning: {warning}")

        created_at = now_iso(ctx.cfg.tz)
        asset_ids: list[str] = []
        files: list[str] = []
        pages = result.meta.get("pages", [])
        total_pages = max(1, len(pages))
        for page in pages or [{"page": 1, "file": Path(result.path).name}]:
            if result.meta.get("kind") == "poster":
                page_path = result.path
            else:
                page_path = result.path / page["file"]
            rel_path = ctx.cfg.relpath(page_path)
            asset = Asset(
                id=new_id("asset"),
                copy_set_id=copy_set_id,
                kind="poster" if result.meta.get("kind") == "poster" else "carousel",
                template=template_id,
                path=rel_path,
                width=result.width,
                height=result.height,
                duration_sec=None,
                bytes=page_path.stat().st_size if page_path.is_file() else None,
                meta={
                    "run_id": ctx.run_id,
                    "kind": kind,
                    "page": int(page.get("page", 1)),
                    "page_count": total_pages,
                    "manifest": result.meta.get("manifest"),
                    "warnings": warnings,
                },
                created_at=created_at,
            )
            repo.insert_asset(ctx.db, asset)
            asset_ids.append(asset.id)
            files.append(rel_path)
            ctx.log(f"render_assets: 写入 asset {asset.id}（{rel_path}）")

        if not asset_ids:
            raise WorkbenchError("render_assets: 渲染器没有返回任何页面")

        return {
            "template": template_id,
            "kind": kind,
            "width": result.width,
            "height": result.height,
            "page_count": total_pages,
            "assets": asset_ids,
            "files": files,
            "warnings": warnings,
        }
