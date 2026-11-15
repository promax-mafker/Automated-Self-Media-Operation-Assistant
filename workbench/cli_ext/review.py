"""``wb review``：平台导出 CSV 导入与账号周报（T02）。"""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import typer

from workbench.config import Config
from workbench.errors import WorkbenchError
from workbench.ids import new_run_id
from workbench.metrics.import_csv import import_metrics
from workbench.metrics.report import write_weekly_report
from workbench.store.db import init_db

app = typer.Typer(
    help="数据复盘：导入平台导出的 CSV，生成账号周报",
    no_args_is_help=True,
)


def _fail(exc: WorkbenchError) -> NoReturn:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=exc.exit_code) from None


@app.command("import")
def import_command(
    csv_path: Path = typer.Option(..., "--csv", help="平台导出的 CSV 文件"),
    account: str | None = typer.Option(
        None,
        "--account",
        help="自己的账号 handle；CSV 内其他 handle 标记为对标账号",
    ),
    platform: str | None = typer.Option(
        None,
        "--platform",
        help="CSV 没有平台列时的默认平台；默认取 WB_PLATFORM",
    ),
) -> None:
    """导入 CSV，写入 account / post / metric；按 (post_id, captured_at) 幂等。"""
    conn = None
    try:
        cfg = Config.from_env()
        cfg.ensure_dirs()
        conn = init_db(cfg.db_path)
        result = import_metrics(
            conn,
            csv_path,
            account=account,
            platform=platform or cfg.platform,
            tz=cfg.tz,
        )
    except WorkbenchError as exc:
        _fail(exc)
    finally:
        if conn is not None:
            conn.close()
    typer.echo(result.summary())


@app.command("week")
def week_command(
    account: str = typer.Option(..., "--account", help="账号 handle"),
    week: str = typer.Option(..., "--week", help="ISO 周，例如 2026-W45"),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="输出目录（写 report.md）；默认 WB_HOME/runs/<run_id>",
    ),
    platform: str | None = typer.Option(
        None,
        "--platform",
        help="平台；默认取 WB_PLATFORM",
    ),
) -> None:
    """生成 Markdown 周报：<out>/report.md。"""
    conn = None
    try:
        cfg = Config.from_env()
        cfg.ensure_dirs()
        conn = init_db(cfg.db_path)
        if out is not None:
            target = Path(out).expanduser().resolve()
        else:
            target = cfg.runs_dir / new_run_id()
        result = write_weekly_report(
            conn,
            account,
            week,
            target,
            platform=platform or cfg.platform,
            tz=cfg.tz,
        )
    except WorkbenchError as exc:
        _fail(exc)
    finally:
        if conn is not None:
            conn.close()
    typer.echo(
        f"account={account} week={week} posts={result.posts} report={result.path}"
    )


__all__ = ["app"]
