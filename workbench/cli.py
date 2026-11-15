"""``wb`` 根命令：doctor / db / run，并自动挂载 ``cli_ext/*`` 子命令。"""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.table import Table

from workbench import __version__
from workbench.cli_ext import iter_subcommands
from workbench.config import Config
from workbench.errors import WorkbenchError
from workbench.pipeline.definitions import PIPELINES, stage_ticket
from workbench.pipeline.registry import has_stage
from workbench.pipeline.runner import run_pipeline
from workbench.stages import load_stages
from workbench.store.db import init_db, table_names, user_version

app = typer.Typer(
    help="超级个体获客工作台（MVP）：选题 → 文案 → 图文/视频 → 发布包 → 回灌 → 复盘",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="数据库迁移与查看")
app.add_typer(db_app, name="db")


def _abort(exc: WorkbenchError) -> NoReturn:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=exc.exit_code)


def _parse_inputs(values: list[str] | None) -> dict[str, str]:
    params: dict[str, str] = {}
    for raw in values or []:
        if "=" not in raw:
            raise typer.BadParameter(f"--input 需要 k=v 形式，收到 {raw!r}")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise typer.BadParameter(f"--input 的 key 不能为空：{raw!r}")
        params[key] = value
    return params


@app.command("doctor")
def doctor() -> None:
    """查看环境、数据库版本与流水线 stage 覆盖情况。"""
    conn = None
    try:
        cfg = Config.from_env()
        cfg.ensure_dirs()
        conn = init_db(cfg.db_path)
        version = user_version(conn)
        tables = table_names(conn)
        load_stages()
    except WorkbenchError as exc:
        _abort(exc)

    console = Console()
    console.print(f"[bold]Workbench {__version__}[/bold]")
    console.print(f"WB_HOME: {cfg.home}")
    console.print(f"SQLite: {cfg.db_path}")
    console.print(f"user_version: {version}")
    console.print(f"table_count: {len(tables)}")
    console.print(f"TZ: {cfg.tz}    WB_PLATFORM: {cfg.platform}")
    if cfg.llm_mode == "stub":
        model = "stub"
    else:
        model = cfg.llm_model
    auto_note = "（自动选择：未设置 WB_LLM）" if cfg.llm_auto else ""
    console.print(f"LLM: {cfg.llm_mode}    model: {model}{auto_note}")

    console.print("stage 覆盖：ok=已注册，missing=待后续 Ticket")
    table = Table(title="流水线 stage 覆盖")
    table.add_column("流水线", style="cyan", no_wrap=True)
    table.add_column("stage", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("Ticket", no_wrap=True)
    for pipeline, names in PIPELINES.items():
        for name in names:
            status = "ok" if has_stage(name) else "missing"
            style = "green" if status == "ok" else "yellow"
            table.add_row(pipeline, name, f"[{style}]{status}[/{style}]", stage_ticket(name) or "-")
    console.print(table)

    if conn is not None:
        conn.close()


@db_app.command("init")
def db_init() -> None:
    """执行数据库迁移（幂等）。"""
    conn = None
    try:
        cfg = Config.from_env()
        cfg.ensure_dirs()
        conn = init_db(cfg.db_path)
        version = user_version(conn)
        count = len(table_names(conn))
    except WorkbenchError as exc:
        _abort(exc)
    typer.echo(f"SQLite: {cfg.db_path}")
    typer.echo(f"user_version: {version}")
    typer.echo(f"table_count: {count}")
    if conn is not None:
        conn.close()


@app.command("run")
def run(
    pipeline: str = typer.Argument(..., help="流水线名：review/topic/copy/visual/video/pack/demo"),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="输出目录；默认 WB_HOME/runs/<run_id>",
    ),
    stop_after: str | None = typer.Option(
        None,
        "--stop-after",
        help="执行到指定 stage（含）后停止",
    ),
    inputs: list[str] | None = typer.Option(
        None,
        "--input",
        "-i",
        help="stage 参数，k=v 形式，可重复；例如 --input topic_id=topic_xxx",
    ),
) -> None:
    """执行一条流水线并写出 manifest。"""
    try:
        params = _parse_inputs(inputs)
        cfg = Config.from_env()
        result = run_pipeline(
            cfg,
            pipeline,
            params=params,
            out=out,
            stop_after=stop_after,
            log=typer.echo,
        )
    except WorkbenchError as exc:
        _abort(exc)
    typer.echo(f"run_id={result.run_id}")
    typer.echo(f"out={result.out}")
    typer.echo(f"manifest={result.out / 'manifest.json'}")


def _register_extensions() -> None:
    for sub in iter_subcommands():
        app.add_typer(sub, name=sub.info.name)


_register_extensions()


def main() -> None:
    try:
        app(prog_name="wb")
    except WorkbenchError as exc:
        typer.echo(str(exc), err=True)
        raise SystemExit(exc.exit_code) from None


if __name__ == "__main__":
    main()
