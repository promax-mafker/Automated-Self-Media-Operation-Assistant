"""``wb copy``：五类文案生成与合规闸门（T06）。

- ``wb copy gen --topic <id> [--kinds spoken,note] [--out DIR]``
- ``wb copy lint --file DIR/copies.json``
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.table import Table

from workbench.config import Config
from workbench.copy.generate import load_copies, summary_lines, write_copies
from workbench.copy.lint import relint_payload, update_compliance
from workbench.copy.schema import normalize_kinds
from workbench.errors import WorkbenchError
from workbench.pipeline.runner import run_pipeline
from workbench.store.db import init_db

app = typer.Typer(
    name="copy",
    help="文案生成：五类 × 两长度，知识库取证 + 合规闸门",
    no_args_is_help=True,
)


def _abort(exc: WorkbenchError) -> NoReturn:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=exc.exit_code)


def _print_suggestions(copies: list[dict]) -> None:
    for item in copies:
        if item.get("passed"):
            continue
        typer.echo(
            f"  {item.get('kind', '?')}/{item.get('length', '?')} "
            f"{item.get('title', '')}：未通过"
        )
        for hit in item.get("hits", []):
            note = f"（{hit['note']}）" if hit.get("note") else ""
            typer.echo(
                f"    [{hit.get('kind')}] {hit.get('word')}{note} → "
                f"{hit.get('suggestion', '')}"
            )


@app.command("gen")
def gen(
    topic: str = typer.Option(..., "--topic", "-t", help="topic 表的选题卡 ID"),
    out: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="输出目录（写 copies.json）；默认 WB_HOME/runs/<run_id>",
    ),
    platform: str | None = typer.Option(
        None,
        "--platform",
        help="平台；默认取选题卡的 platform，再退回 WB_PLATFORM",
    ),
    kinds: str | None = typer.Option(
        None,
        "--kinds",
        help="裁剪 kind，逗号分隔：spoken,note,seed,explain,hook；默认五类全要",
    ),
    evidence_k: int = typer.Option(
        5,
        "--evidence-k",
        min=1,
        max=5,
        help="每条文案最多灌注的 KB 命中数（1-5）",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON 摘要"),
) -> None:
    """生成 5 类 × 2 长度 = 10 条文案并写入 copy_set。"""
    params: dict[str, str] = {"topic_id": topic, "k": str(evidence_k)}
    if platform:
        params["platform"] = platform
    if kinds:
        try:
            selected = normalize_kinds(kinds)
        except WorkbenchError as exc:
            _abort(exc)
        params["kinds"] = ",".join(selected)

    try:
        cfg = Config.from_env()
        cfg.ensure_dirs()
        result = run_pipeline(cfg, "copy", params=params, out=out, log=typer.echo)
    except WorkbenchError as exc:
        _abort(exc)

    generation = result.outputs.get("copy_generate", {})
    lint = result.outputs.get("copy_lint", {})
    copies_file = result.out / "copies.json"
    payload = load_copies(copies_file) if copies_file.is_file() else {}

    if json_output:
        typer.echo(
            json.dumps(
                {
                    "run_id": result.run_id,
                    "out": str(result.out),
                    "copies_file": str(copies_file),
                    "generation": generation,
                    "lint": lint,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        typer.echo(f"run_id={result.run_id}")
        typer.echo(f"out={result.out}")
        typer.echo(f"copies_file={copies_file}")
        typer.echo(
            f"copies={generation.get('count', 0)} "
            f"copy_set={len(generation.get('copy_set_ids', []))} "
            f"evidence={generation.get('evidence_count', 0)}"
        )
        if payload:
            for line in summary_lines(payload):
                typer.echo(f"  {line}")

    if int(lint.get("failed", 0)) > 0:
        typer.echo(
            f"lint 未通过：failed={lint.get('failed')} needs_human={lint.get('needs_human')}；"
            "已落库供人工修改（退出码 3）",
            err=True,
        )
        if not json_output:
            _print_suggestions(list(lint.get("items", [])))
        raise typer.Exit(code=3)


@app.command("lint")
def lint(
    file: Path = typer.Option(
        ...,
        "--file",
        "-f",
        exists=True,
        dir_okay=False,
        readable=True,
        help="copies.json 路径（wb copy gen 的产物）",
    ),
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """重新检查极限词、敏感词与数字出处；未通过时退出码 3。"""
    try:
        payload = load_copies(file)
        report = relint_payload(payload)
    except WorkbenchError as exc:
        _abort(exc)

    try:
        write_copies(file, payload)
    except OSError as exc:
        _abort(WorkbenchError(f"写回 copies.json 失败：{file}（{exc}）"))

    updated = 0
    conn = None
    try:
        cfg = Config.from_env()
        cfg.ensure_dirs()
        conn = init_db(cfg.db_path)
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
            if update_compliance(conn, str(copy_set_id), compliance):
                updated += 1
    except WorkbenchError as exc:
        typer.echo(f"提示：未同步 copy_set（{exc}）", err=True)
    finally:
        if conn is not None:
            conn.close()

    if json_output:
        typer.echo(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        typer.echo(f"file={file}")
        typer.echo(
            f"passed={report.passed} failed={report.failed} "
            f"needs_human={report.needs_human} copy_set_updated={updated}"
        )
        if report.items:
            table = Table(title="文案 lint")
            table.add_column("kind/length", no_wrap=True)
            table.add_column("title", max_width=36)
            table.add_column("passed", no_wrap=True)
            table.add_column("needs_human", no_wrap=True)
            for item in report.items:
                table.add_row(
                    f"{item['kind']}/{item['length']}",
                    str(item.get("title") or ""),
                    "通过" if item["passed"] else "未通过",
                    "是" if item["needs_human"] else "否",
                )
            Console().print(table)
        _print_suggestions(report.items)

    if not report.all_passed:
        raise typer.Exit(code=3)


__all__ = ["app"]
