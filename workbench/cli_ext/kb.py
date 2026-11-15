"""``wb kb``：导入资料、检索引用、离线评测。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.table import Table

from workbench.config import Config
from workbench.contracts import KBHit
from workbench.errors import WorkbenchError
from workbench.knowledge.answer import ask as ask_kb
from workbench.knowledge.answer import evaluate, hit_to_dict, load_eval
from workbench.knowledge.build import build_kb
from workbench.knowledge.index import MAX_K, KnowledgeBase

DEFAULT_EVAL = Path("data/kb/eval.jsonl")
DEFAULT_THRESHOLD = 0.7

app = typer.Typer(help="知识库：导入 md/txt、检索引用、离线评测")


def _abort(exc: WorkbenchError) -> NoReturn:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=exc.exit_code)


def _open_kb() -> KnowledgeBase:
    try:
        return build_kb(Config.from_env())
    except WorkbenchError as exc:
        _abort(exc)


def _hits_table(title: str, hits: list[KBHit]) -> Table:
    table = Table(title=title)
    table.add_column("doc_title", style="cyan", max_width=32)
    table.add_column("heading", max_width=32)
    table.add_column("score", justify="right", no_wrap=True)
    table.add_column("片段（80 字）", max_width=80)
    for hit in hits:
        payload = hit_to_dict(hit)
        heading = payload["heading"]
        table.add_row(
            str(payload["doc_title"]),
            str(heading) if heading else "-",
            f"{float(payload['score']):.3f}",
            str(payload["snippet"]),
        )
    return table


@app.command("add")
def add(
    paths: list[Path] = typer.Argument(
        ...,
        exists=True,
        readable=True,
        help="md/txt 文件或目录，可传多个",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """导入资料；按 sha256 幂等，重复导入不会产生重复块。"""
    kb = _open_kb()
    try:
        stats = kb.add(paths)
    except WorkbenchError as exc:
        _abort(exc)
    finally:
        kb.close()

    if json_output:
        typer.echo(json.dumps(stats, ensure_ascii=False))
        return
    parts = [f"docs={stats.get('docs', 0)}"]
    skipped = stats.get("skipped", 0)
    if skipped:
        parts.append(f"skipped={skipped}")
    parts.append(f"chunks={stats.get('chunks', 0)}")
    typer.echo(" ".join(parts))


@app.command("ask")
def ask(
    query: str = typer.Argument("", help="问题或关键词；至少 2 个字"),
    k: int = typer.Option(5, "--k", "-k", min=1, max=MAX_K, help="返回命中数（默认 5）"),
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """检索 3-5 条可引用片段：doc_title / heading / score / 80 字片段。"""
    kb = _open_kb()
    try:
        result = ask_kb(kb, query, k=k)
    except WorkbenchError as exc:
        _abort(exc)
    finally:
        kb.close()

    if json_output:
        payload = {
            "query": result.query,
            "hits": [hit_to_dict(hit) for hit in result.hits],
            "hint": result.hint,
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if result.hint:
        typer.echo(result.hint)
    if result.hits:
        Console().print(_hits_table(f"知识库命中：{result.query}", result.hits))


@app.command("eval")
def eval_cmd(
    eval_file: Path = typer.Option(
        DEFAULT_EVAL,
        "--eval",
        exists=True,
        dir_okay=False,
        readable=True,
        help="JSONL 评测集路径",
    ),
    k: int = typer.Option(3, "--k", "-k", min=1, max=MAX_K, help="recall@k 的 k"),
    threshold: float = typer.Option(
        DEFAULT_THRESHOLD,
        "--threshold",
        min=0.0,
        max=1.0,
        help="通过阈值（默认 0.70）",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """跑离线评测；recall@k 低于阈值时退出码为 1。"""
    try:
        items = load_eval(eval_file)
    except WorkbenchError as exc:
        _abort(exc)

    kb = _open_kb()
    try:
        report = evaluate(kb, items, k=k)
    except WorkbenchError as exc:
        _abort(exc)
    finally:
        kb.close()

    passed = report.total > 0 and report.recall >= threshold
    if json_output:
        payload = {
            "k": report.k,
            "total": report.total,
            "hits": report.hits,
            "recall": round(report.recall, 6),
            "threshold": threshold,
            "passed": passed,
            "misses": [
                {"q": miss.query, "expected": miss.expected, "got": miss.got}
                for miss in report.misses
            ],
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        verdict = "通过" if passed else "未通过"
        typer.echo(f"recall@{report.k}={report.recall:.2f} (>={threshold:.2f}) {verdict}")
        if report.total == 0:
            typer.echo("评测集为空：至少需要 1 条 q/doc 记录。")
        for miss in report.misses:
            got = "、".join(miss.got) if miss.got else "无命中"
            typer.echo(f"  未命中：{miss.query} → 期望《{miss.expected}》，实际：{got}")

    if not passed:
        raise typer.Exit(code=1)


__all__ = ["app"]
