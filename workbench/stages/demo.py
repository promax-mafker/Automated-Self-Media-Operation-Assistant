"""T01 演示流水线：证明 stage 注册、落库、落盘与可复算。

全部数据均为虚构演示数据；不访问网络，不调用真实模型，不做外部动作。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from workbench.config import now_iso
from workbench.errors import WorkbenchError
from workbench.ids import new_id
from workbench.pipeline.registry import register
from workbench.pipeline.stage import RunContext
from workbench.store import repo
from workbench.store.models import Asset, CopySet, Topic


@register("demo_source")
class DemoSource:
    name = "demo_source"
    needs: tuple[str, ...] = ()

    def run(self, ctx: RunContext) -> dict[str, Any]:
        items = [
            {
                "title": "演示选题：三步搭好内容流水线（虚构）",
                "platform": ctx.cfg.platform,
                "source": "manual",
                "score": 0.82,
                "evidence": "T01 演示数据，不是平台真实数据",
            },
            {
                "title": "演示选题：一周复盘模板（虚构）",
                "platform": ctx.cfg.platform,
                "source": "manual",
                "score": 0.64,
                "evidence": "T01 演示数据，不是平台真实数据",
            },
        ]
        ctx.log(f"demo_source: 产出 {len(items)} 条虚构素材")
        return {"items": items}


@register("demo_topic")
class DemoTopic:
    name = "demo_topic"
    needs: tuple[str, ...] = ()

    def run(self, ctx: RunContext) -> dict[str, Any]:
        topic = Topic(
            id=new_id("topic"),
            platform=ctx.cfg.platform,
            title="演示选题：三步搭好内容流水线（虚构）",
            angle="用一条 demo 流水线验证落库、落盘与复算",
            hook="先跑通骨架，再填业务",
            score=0.82,
            score_parts={"heat": 0.7, "fit": 0.9, "benchmark": 0.8, "timing": 0.9},
            source="manual",
            source_ref="t01-demo",
            evidence=[
                {
                    "kind": "manual",
                    "ref": "tickets/T01.md",
                    "note": "仅用于演示的虚构选题，不代表真实平台数据",
                }
            ],
            status="picked",
            created_at=now_iso(ctx.cfg.tz),
        )
        repo.insert_topic(ctx.db, topic)
        ctx.log(f"demo_topic: 写入 topic {topic.id}")
        return {"topic_id": topic.id, "title": topic.title, "score": topic.score}


@register("demo_copy")
class DemoCopy:
    name = "demo_copy"
    needs: tuple[str, ...] = ()

    def run(self, ctx: RunContext) -> dict[str, Any]:
        topics = repo.list_topics(ctx.db)
        if not topics:
            raise WorkbenchError("demo_copy: 数据库里没有 topic，demo_topic 未执行？")
        topic = topics[-1]
        variants = [
            ("hook", "short", "演示短钩子（虚构）", "三步跑通内容流水线，先看骨架再看效果。"),
            (
                "note",
                "short",
                "演示短笔记（虚构）",
                "T01 只验证骨架：SQLite 迁移、stage 注册、demo 流水线与产物落盘。",
            ),
            (
                "note",
                "long",
                "演示长笔记（虚构）",
                "这篇笔记由 T01 的 demo_copy 生成，用于验证 copy_set 表、JSON 列与 3 条演示数据。"
                "所有内容均为虚构占位，不用于发布。",
            ),
        ]
        copy_ids: list[str] = []
        for kind, length, title, body in variants:
            copy_set = CopySet(
                id=new_id("copy"),
                topic_id=topic.id,
                kind=kind,
                length=length,
                title=title,
                hook="先跑通骨架，再填业务" if kind == "hook" else None,
                body=body,
                tags=["演示", "虚构", "T01"],
                citations=[],
                model="demo-template",
                prompt_version="demo-v1",
                compliance={"passed": True, "hits": []},
                created_at=now_iso(ctx.cfg.tz),
            )
            repo.insert_copy_set(ctx.db, copy_set)
            copy_ids.append(copy_set.id)
        ctx.log(f"demo_copy: 写入 {len(copy_ids)} 条 copy_set")
        return {"copy_set_ids": copy_ids, "topic_id": topic.id}


@register("demo_asset")
class DemoAsset:
    name = "demo_asset"
    needs: tuple[str, ...] = ()

    def run(self, ctx: RunContext) -> dict[str, Any]:
        copy_sets = repo.list_copy_sets(ctx.db)
        if not copy_sets:
            raise WorkbenchError("demo_asset: 数据库里没有 copy_set，demo_copy 未执行？")
        copy_set = copy_sets[-1]

        image_path = ctx.out / f"demo_asset_{ctx.run_id}.png"
        image = Image.new("RGB", (1080, 1440), (30, 136, 229))
        image.save(image_path, format="PNG")
        size = image_path.stat().st_size

        asset = Asset(
            id=new_id("asset"),
            copy_set_id=copy_set.id,
            kind="poster",
            template="demo-solid",
            path=ctx.cfg.relpath(image_path),
            width=1080,
            height=1440,
            duration_sec=None,
            bytes=size,
            meta={
                "run_id": ctx.run_id,
                "format": "PNG",
                "color": "#1E88E5",
                "note": "T01 演示用纯色块，真实渲染由 T04 提供",
            },
            created_at=now_iso(ctx.cfg.tz),
        )
        repo.insert_asset(ctx.db, asset)
        ctx.log(f"demo_asset: 写入 asset {asset.id}（{image_path.name}）")
        return {
            "asset_id": asset.id,
            "path": asset.path,
            "width": asset.width,
            "height": asset.height,
            "bytes": asset.bytes,
        }


@register("demo_report")
class DemoReport:
    name = "demo_report"
    needs: tuple[str, ...] = ()

    def run(self, ctx: RunContext) -> dict[str, Any]:
        jobs = repo.list_jobs(ctx.db, pipeline="demo")
        topics = repo.list_topics(ctx.db)
        copy_sets = repo.list_copy_sets(ctx.db)
        assets = [
            asset for asset in repo.list_assets(ctx.db) if asset.meta.get("run_id") == ctx.run_id
        ]
        current_asset = assets[-1] if assets else None

        lines = [
            "# Demo 流水线报告（T01）",
            "",
            f"- run_id：`{ctx.run_id}`",
            f"- pipeline：`{ctx.pipeline}`",
            f"- 输出目录：`{ctx.out}`",
            f"- 生成时间：{now_iso(ctx.cfg.tz)}",
            "",
            "## 本次产物",
            "",
        ]
        if current_asset is not None:
            lines.append(f"- 演示图片：`{current_asset.path}`（1080×1440 PNG）")
        else:
            lines.append("- 演示图片：未找到")
        lines.extend(
            [
                "- manifest：`manifest.json`（runner 在本 stage 之后写入）",
                "",
                "## 数据库累计",
                "",
                f"- job 行数：{len(jobs)}",
                f"- topic 行数：{len(topics)}",
                f"- copy_set 行数：{len(copy_sets)}",
                f"- 本次 run 的 asset 行数：{len(assets)}",
                "",
                "## 说明",
                "",
                "本报告由 `demo_report` stage 生成。demo 流水线的全部内容都是虚构演示数据，",
                "不访问网络、不调用真实模型、不执行任何外部动作。",
                "",
            ]
        )
        report_path = ctx.out / "report.md"
        report_path.write_text("\n".join(lines), encoding="utf-8")
        ctx.log(f"demo_report: 写入 {report_path.name}")
        return {
            "report": str(report_path),
            "bytes": Path(report_path).stat().st_size,
            "asset_id": current_asset.id if current_asset is not None else None,
        }
