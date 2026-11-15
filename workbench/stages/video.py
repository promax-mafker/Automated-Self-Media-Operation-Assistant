"""T07 stage：``video_build`` —— 分镜 JSON → MP4，并写一行 ``asset``。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workbench.config import now_iso
from workbench.contracts import get_composer
from workbench.errors import WorkbenchError
from workbench.ids import new_id
from workbench.pipeline.registry import register
from workbench.pipeline.stage import RunContext
from workbench.store import repo
from workbench.store.models import Asset
from workbench.video.storyboard import load_storyboard_file

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


@register("video_build")
class VideoBuild:
    """``wb run video --input storyboard=s.json [--input tts=true]``。"""

    name = "video_build"
    needs: tuple[str, ...] = ("storyboard",)

    def run(self, ctx: RunContext) -> dict[str, Any]:
        raw_path = (ctx.params.get("storyboard") or "").strip()
        if not raw_path:
            raise WorkbenchError("video_build: storyboard 不能为空")
        data = load_storyboard_file(Path(raw_path))

        tts_param = ctx.params.get("tts")
        if tts_param is not None and tts_param.strip():
            data["tts"] = tts_param.strip().lower() in _TRUE_VALUES

        assets_dir = ctx.out / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        out = assets_dir / "video.mp4"

        composer = get_composer()
        result = composer.compose(storyboard=data, out=out)

        warnings = [str(item) for item in result.get("warnings", [])]
        for warning in warnings:
            ctx.log(f"video_build: warning: {warning}")

        copy_set_id = (ctx.params.get("copy_set_id") or "").strip() or None
        template = (ctx.params.get("template") or "").strip() or "default"
        rel_path = ctx.cfg.relpath(out)
        meta = dict(result.get("meta", {}))
        asset = Asset(
            id=new_id("asset"),
            copy_set_id=copy_set_id,
            kind="video",
            template=template,
            path=rel_path,
            width=int(result["width"]),
            height=int(result["height"]),
            duration_sec=float(result["duration_sec"]),
            bytes=int(result["bytes"]),
            meta=meta,
            created_at=now_iso(ctx.cfg.tz),
        )
        repo.insert_asset(ctx.db, asset)
        ctx.log(f"video_build: 写入 asset {asset.id}（{rel_path}）")

        return {
            "asset_id": asset.id,
            "path": rel_path,
            "width": asset.width,
            "height": asset.height,
            "duration_sec": asset.duration_sec,
            "bytes": asset.bytes,
            "scenes": meta.get("scenes"),
            "fps": meta.get("fps"),
            "tts": meta.get("tts"),
            "warnings": warnings,
        }


__all__ = ["VideoBuild"]
