"""T06 stage：``copy_generate`` 生成 10 条文案，``copy_lint`` 可重复复核。

- ``copy_generate`` 需要 ``--input topic_id=<id>``；可选 ``platform``、
  ``kinds=spoken,note``、``k=<1-5>``。产物写 ``<out>/copies.json``，并逐条
  写入 ``copy_set``。
- ``copy_lint`` 读取 ``<out>/copies.json``（或 ``--input file=<path>``），用文件里
  保存的 evidence 重新 lint、写回 compliance，并同步 ``copy_set.compliance``。
  lint 未通过不抛异常，由 CLI 把 output 的 failed 数映射为退出码 3。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from workbench.contracts import get_kb
from workbench.copy.generate import (
    COPIES_FILENAME,
    generate_copies,
    load_copies,
    to_copy_set,
    write_copies,
)
from workbench.copy.grounding import MAX_EVIDENCE, Grounding, retrieve
from workbench.copy.lint import (
    relint_payload,
    update_compliance,
)
from workbench.copy.schema import normalize_kinds
from workbench.errors import WorkbenchError
from workbench.pipeline.registry import register
from workbench.pipeline.stage import RunContext
from workbench.store import repo


def _evidence_k(ctx: RunContext) -> int:
    raw = ctx.params.get("k", str(MAX_EVIDENCE))
    try:
        value = int(raw)
    except ValueError as exc:
        raise WorkbenchError(f"copy_generate: k 必须是整数，收到 {raw!r}") from exc
    return max(1, min(value, MAX_EVIDENCE))


def _grounding(ctx: RunContext, topic: Any, *, k: int) -> Grounding:
    kb = get_kb(ctx.cfg)
    try:
        return retrieve(kb, topic, k=k)
    except Exception as exc:  # noqa: BLE001 - KB 检索失败时降级为空取证，不阻断生成
        ctx.log(f"copy_generate: 知识库取证失败，按空 evidence 继续：{type(exc).__name__}: {exc}")
        return Grounding(query="")
    finally:
        close = getattr(kb, "close", None)
        if callable(close):
            close()


@register("copy_generate")
class CopyGenerate:
    name = "copy_generate"
    needs: tuple[str, ...] = ("topic_id",)

    def run(self, ctx: RunContext) -> dict[str, Any]:
        topic_id = (ctx.params.get("topic_id") or "").strip()
        if not topic_id:
            raise WorkbenchError("copy_generate: topic_id 不能为空")
        topic = repo.get_topic(ctx.db, topic_id)
        if topic is None:
            raise WorkbenchError(
                f"copy_generate: 找不到 topic={topic_id}；请先用 wb topic suggest 生成选题卡"
            )

        platform = (ctx.params.get("platform") or topic.platform or ctx.cfg.platform).strip()
        kinds = normalize_kinds(ctx.params.get("kinds"))
        grounding = _grounding(ctx, topic, k=_evidence_k(ctx))
        if not grounding.hits:
            ctx.log("copy_generate: 知识库没有命中，evidence 为空；正文不得写具体数字")

        result = generate_copies(
            ctx.llm,
            ctx.cfg,
            topic,
            platform=platform,
            grounding=grounding,
            kinds=kinds,
            run_id=ctx.run_id,
            log=ctx.log,
        )
        target = ctx.out / COPIES_FILENAME
        write_copies(target, result.payload)

        copy_set_ids: list[str] = []
        for generated in result.copies:
            copy_set = to_copy_set(generated.entry, created_at=str(generated.entry["created_at"]))
            repo.insert_copy_set(ctx.db, copy_set)
            copy_set_ids.append(copy_set.id)

        ctx.log(
            f"copy_generate: {len(copy_set_ids)} 条写入 copy_set（"
            f"passed={result.passed} failed={result.failed}），文件 {target}"
        )
        return {
            "topic_id": topic.id,
            "platform": platform,
            "kinds": kinds,
            "count": len(copy_set_ids),
            "copy_set_ids": copy_set_ids,
            "copies_file": ctx.cfg.relpath(target),
            "evidence_count": len(grounding.hits),
            "passed": result.passed,
            "failed": result.failed,
        }


def _copies_path(ctx: RunContext) -> Path:
    raw = (ctx.params.get("file") or COPIES_FILENAME).strip()
    path = Path(raw).expanduser()
    return path.resolve() if path.is_absolute() else (ctx.out / path).resolve()


@register("copy_lint")
class CopyLint:
    name = "copy_lint"
    needs: tuple[str, ...] = ()

    def run(self, ctx: RunContext) -> dict[str, Any]:
        path = _copies_path(ctx)
        payload = load_copies(path)
        report = relint_payload(payload)
        write_copies(path, payload)

        updated = 0
        for item in report.items:
            copy_set_id = item.get("id")
            if not copy_set_id:
                continue
            compliance = {
                "passed": item["passed"],
                "needs_human": item["needs_human"],
                "hits": item["hits"],
                "checked_at": item["checked_at"],
            }
            if update_compliance(ctx.db, str(copy_set_id), compliance):
                updated += 1

        for item in report.items:
            if item["passed"]:
                continue
            ctx.log(f"copy_lint: {item['kind']}/{item['length']} 未通过（title={item['title']}）")
            for hit in item["hits"]:
                note = f"（{hit['note']}）" if hit.get("note") else ""
                ctx.log(
                    f"copy_lint:   [{hit['kind']}] {hit['word']}{note} → "
                    f"{hit.get('suggestion', '')}"
                )

        ctx.log(
            f"copy_lint: passed={report.passed} failed={report.failed} "
            f"needs_human={report.needs_human} copy_set 更新 {updated} 行"
        )
        return {
            "file": ctx.cfg.relpath(path),
            "count": len(report.items),
            "passed": report.passed,
            "failed": report.failed,
            "needs_human": report.needs_human,
            "all_passed": report.all_passed,
            "updated": updated,
            "items": report.items,
        }


__all__ = ["CopyGenerate", "CopyLint"]
