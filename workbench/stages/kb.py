"""知识库 stage：``kb_ingest`` 与可选的 ``kb_ask``。

流水线定义在 ``workbench/pipeline/definitions.py``（内核只读），本 Ticket 只负责
注册 stage，供后续 `copy` / `topic` 流水线或其他 Ticket 用 ``--input`` 调用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workbench.errors import WorkbenchError
from workbench.knowledge.answer import ask as ask_kb
from workbench.knowledge.answer import hit_to_dict
from workbench.knowledge.index import KnowledgeBase
from workbench.pipeline.registry import register
from workbench.pipeline.stage import RunContext

DEFAULT_KB_PATH = "data/kb/seed"


@register("kb_ingest")
class KbIngest:
    """把一个目录或单文件导入知识库；参数 ``path`` 可覆盖默认 seed 目录。"""

    name = "kb_ingest"
    needs: tuple[str, ...] = ()

    def run(self, ctx: RunContext) -> dict[str, Any]:
        raw = ctx.params.get("path") or DEFAULT_KB_PATH
        targets = [Path(raw)]
        if not any(target.exists() for target in targets):
            raise WorkbenchError(f"kb_ingest: 路径不存在：{raw}")

        kb = KnowledgeBase(ctx.cfg, ctx.db)
        stats = kb.add(targets)
        ctx.log(
            f"kb_ingest: docs={stats['docs']} skipped={stats['skipped']} "
            f"chunks={stats['chunks']}"
        )
        return {"path": raw, **stats}


@register("kb_ask")
class KbAsk:
    """检索知识库，返回与 ``wb kb ask --json`` 相同的结构化命中。"""

    name = "kb_ask"
    needs: tuple[str, ...] = ("q",)

    def run(self, ctx: RunContext) -> dict[str, Any]:
        query = ctx.params.get("q", "")
        try:
            k = int(ctx.params.get("k", "5"))
        except ValueError as exc:
            raise WorkbenchError(f"kb_ask: k 必须是整数，收到 {ctx.params.get('k')!r}") from exc

        result = ask_kb(KnowledgeBase(ctx.cfg, ctx.db), query, k=k)
        ctx.log(f"kb_ask: query={query!r} hits={len(result.hits)}")
        return {
            "query": result.query,
            "k": k,
            "hits": [hit_to_dict(hit) for hit in result.hits],
            "hint": result.hint,
        }


__all__ = ["DEFAULT_KB_PATH", "KbAsk", "KbIngest"]
