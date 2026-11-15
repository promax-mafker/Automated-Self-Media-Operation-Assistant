"""``wb pack``：构建发布包、查看状态、记录发布 URL、回灌 CSV（T08）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.table import Table

from workbench.config import Config
from workbench.errors import WorkbenchError
from workbench.publish import PublishError
from workbench.publish.backfill import backfill
from workbench.publish.bundle import create_pack, update_pack_log
from workbench.publish.manifest import PLATFORMS
from workbench.store import repo
from workbench.store.db import init_db

app = typer.Typer(
    name="pack",
    help="发布包：build / list / show / log / backfill",
    no_args_is_help=True,
)


def _fail(exc: WorkbenchError) -> NoReturn:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=exc.exit_code) from None


@app.command("build")
def build_command(
    copy: str = typer.Option(..., "--copy", "-c", help="copy_set ID"),
    platform: str | None = typer.Option(
        None,
        "--platform",
        help=f"平台：{' / '.join(PLATFORMS)}；默认取 WB_PLATFORM",
    ),
    assets: str | None = typer.Option(
        None,
        "--assets",
        help="逗号分隔的 asset ID；省略时自动取该 copy 关联的 asset",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        "-o",
        help="包目录；默认 WB_HOME/packs/<pack_id>（zip 写在同级）",
    ),
) -> None:
    """按平台生成发布目录、manifest.json 与 zip。"""
    conn = None
    try:
        cfg = Config.from_env()
        cfg.ensure_dirs()
        conn = init_db(cfg.db_path)
        result = create_pack(
            conn,
            cfg,
            copy,
            platform=platform,
            assets=assets,
            out=out,
        )
    except WorkbenchError as exc:
        _fail(exc)
    finally:
        if conn is not None:
            conn.close()

    manifest = result.pack.manifest
    if not manifest["assets"]:
        typer.echo("warning: 该 copy 没有关联 asset，生成纯文案包", err=True)
    if not manifest["compliance"]["passed"]:
        typer.echo(
            "warning: compliance.passed=false，请按发布说明人工复核后再发布",
            err=True,
        )
    typer.echo(f"pack={result.pack.id}")
    typer.echo(f"platform={result.pack.platform}")
    typer.echo(f"status={result.pack.status}")
    typer.echo(f"dir={result.dir}")
    typer.echo(f"manifest={result.manifest_path}")
    typer.echo(f"zip={result.zip_path}")
    typer.echo(f"files={len(result.files)}")
    typer.echo(f"suggested_publish_at={manifest['suggested_publish_at']}")


@app.command("list")
def list_command(
    status: str | None = typer.Option(
        None,
        "--status",
        help="按状态过滤：draft / exported / published",
    ),
) -> None:
    """列出本地发布包。"""
    conn = None
    try:
        cfg = Config.from_env()
        cfg.ensure_dirs()
        conn = init_db(cfg.db_path)
        packs = repo.list_packs(conn, status=status)
    except WorkbenchError as exc:
        _fail(exc)
    finally:
        if conn is not None:
            conn.close()

    if not packs:
        typer.echo("暂无发布包；先运行 wb pack build --copy <copy_set_id>")
        return
    table = Table(title="发布包")
    table.add_column("pack_id", style="cyan", no_wrap=True)
    table.add_column("平台", no_wrap=True)
    table.add_column("状态", no_wrap=True)
    table.add_column("标题")
    table.add_column("创建时间", no_wrap=True)
    table.add_column("目录")
    for pack in packs:
        manifest = pack.manifest or {}
        title = str(manifest.get("title") or "")
        if len(title) > 24:
            title = title[:23] + "…"
        table.add_row(
            pack.id,
            pack.platform,
            pack.status,
            title,
            pack.created_at,
            pack.dir,
        )
    Console().print(table)
    typer.echo(f"共 {len(packs)} 个发布包")


@app.command("show")
def show_command(
    pack: str = typer.Option(..., "--pack", "-p", help="pack_id"),
    as_json: bool = typer.Option(False, "--json", help="只输出 manifest JSON"),
) -> None:
    """查看一个发布包的 manifest 与状态。"""
    conn = None
    try:
        cfg = Config.from_env()
        cfg.ensure_dirs()
        conn = init_db(cfg.db_path)
        record = repo.get_pack(conn, pack)
    except WorkbenchError as exc:
        _fail(exc)
    finally:
        if conn is not None:
            conn.close()

    if record is None:
        _fail(PublishError(f"找不到发布包：{pack}"))
    if as_json:
        typer.echo(json.dumps(record.manifest, ensure_ascii=False, indent=2))
        return

    manifest = record.manifest
    typer.echo(f"pack: {record.id}")
    typer.echo(f"platform: {record.platform}")
    typer.echo(f"status: {record.status}")
    typer.echo(f"dir: {record.dir}")
    typer.echo(f"title: {manifest.get('title', '')}")
    typer.echo(f"suggested_publish_at: {manifest.get('suggested_publish_at', '')}")
    typer.echo(f"files_with_checksum: {len(manifest.get('checksums', {}))}")
    if manifest.get("published_url"):
        typer.echo(f"published_url: {manifest['published_url']}")
        typer.echo(f"published_at: {manifest.get('published_at', '')}")


@app.command("log")
def log_command(
    pack: str = typer.Option(..., "--pack", "-p", help="pack_id"),
    url: str = typer.Option(..., "--url", help="人工发布后的平台作品 URL"),
    at: str | None = typer.Option(
        None,
        "--at",
        help="发布时间（ISO 8601 带时区）；默认为当前时间",
    ),
) -> None:
    """记录人工发布结果：status=published，URL 写进 manifest。"""
    conn = None
    try:
        cfg = Config.from_env()
        cfg.ensure_dirs()
        conn = init_db(cfg.db_path)
        updated = update_pack_log(conn, cfg, pack, url, at=at)
    except WorkbenchError as exc:
        _fail(exc)
    finally:
        if conn is not None:
            conn.close()

    typer.echo(
        f"pack={updated.id} status={updated.status} "
        f"url={updated.manifest.get('published_url')} "
        f"at={updated.manifest.get('published_at')}"
    )


@app.command("backfill")
def backfill_command(
    csv: Path = typer.Option(..., "--csv", help="平台导出的 CSV 文件"),
    account: str | None = typer.Option(
        None,
        "--account",
        help="自己的账号 handle；CSV 没有 account 列时必填",
    ),
    platform: str | None = typer.Option(
        None,
        "--platform",
        help="CSV 没有平台列时的默认平台；默认取 WB_PLATFORM",
    ),
) -> None:
    """按 URL 回灌 CSV，写入 post + metric；同 (post_id, captured_at) 幂等。"""
    conn = None
    try:
        cfg = Config.from_env()
        cfg.ensure_dirs()
        conn = init_db(cfg.db_path)
        result = backfill(
            conn,
            csv,
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


__all__ = ["app"]
