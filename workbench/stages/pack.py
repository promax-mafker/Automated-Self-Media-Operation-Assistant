"""T08 stage：``pack_build`` —— 把 copy_set + asset 打成发布包并写一行 ``pack``。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workbench.errors import WorkbenchError
from workbench.pipeline.registry import register
from workbench.pipeline.stage import RunContext
from workbench.publish.bundle import create_pack


@register("pack_build")
class PackBuild:
    """``wb run pack --input copy=<copy_set_id> [--input platform=xhs]``。"""

    name = "pack_build"
    needs: tuple[str, ...] = ("copy",)

    def run(self, ctx: RunContext) -> dict[str, Any]:
        copy_set_id = (ctx.params.get("copy") or "").strip()
        if not copy_set_id:
            raise WorkbenchError("pack_build: copy 不能为空")
        platform = (ctx.params.get("platform") or "").strip() or None
        assets = (ctx.params.get("assets") or "").strip() or None
        out_param = (ctx.params.get("out") or "").strip()
        out = Path(out_param).expanduser().resolve() if out_param else None

        result = create_pack(
            ctx.db,
            ctx.cfg,
            copy_set_id,
            platform=platform,
            assets=assets,
            out=out,
        )
        ctx.log(
            f"pack_build: pack={result.pack.id} dir={result.dir} "
            f"zip={result.zip_path} files={len(result.files)}"
        )
        return {
            "pack_id": result.pack.id,
            "platform": result.pack.platform,
            "status": result.pack.status,
            "dir": ctx.cfg.relpath(result.dir),
            "manifest": ctx.cfg.relpath(result.manifest_path),
            "zip": ctx.cfg.relpath(result.zip_path),
            "files": list(result.files),
            "checksums": len(result.pack.manifest.get("checksums", {})),
            "suggested_publish_at": result.pack.manifest.get("suggested_publish_at"),
            "suggested_publish_default": result.pack.manifest.get(
                "suggested_publish_default"
            ),
        }


__all__ = ["PackBuild"]
