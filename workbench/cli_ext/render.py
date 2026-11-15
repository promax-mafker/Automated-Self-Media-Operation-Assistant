"""``wb render``：列出模板、渲染海报与图文（Pillow + 声明式模板）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table

from workbench.contracts import RenderResult, RenderSpec
from workbench.errors import WorkbenchError
from workbench.render.build import PillowRenderer, build_renderer

app = typer.Typer(name="render", help="图文与海报渲染：list / poster / carousel")


def _abort(exc: WorkbenchError) -> NoReturn:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=exc.exit_code)


def _load_data(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise WorkbenchError(f"数据文件不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkbenchError(f"数据文件不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise WorkbenchError(f"数据文件顶层必须是对象：{path}")
    return data


def _check_kind(renderer: PillowRenderer, template_id: str, kind: str, command: str) -> None:
    for info in renderer.list_templates():
        if info.id == template_id and info.kind != kind:
            raise WorkbenchError(
                f"模板 {template_id} 是 {info.kind} 模板，请用 wb render {command} "
                f"（kind={info.kind}）"
            )


def _print_result(result: RenderResult) -> None:
    meta = result.meta
    typer.echo(
        f"生成：{result.path}（{result.width}×{result.height}，"
        f"{result.bytes} bytes，{meta.get('page_count', 1)} 页）"
    )
    for page in meta.get("pages", []):
        typer.echo(f"  第 {page['page']} 页：{page['file']}（{page['block_count']} 块）")
    if meta.get("manifest"):
        typer.echo(f"  manifest：{result.path / meta['manifest']}")


@app.command("list")
def list_templates() -> None:
    """列出全部模板的 id / 类型 / 尺寸 / 页数。"""
    try:
        renderer = build_renderer()
        infos = renderer.list_templates()
    except WorkbenchError as exc:
        _abort(exc)
    table = Table(title="渲染模板")
    table.add_column("id", style="cyan", no_wrap=True)
    table.add_column("类型", no_wrap=True)
    table.add_column("尺寸", no_wrap=True)
    table.add_column("页数", no_wrap=True)
    table.add_column("说明")
    for info in infos:
        table.add_row(
            info.id,
            info.kind,
            f"{info.width}×{info.height}",
            str(info.pages),
            info.label,
        )
    Console().print(table)
    posters = sum(1 for info in infos if info.kind == "poster")
    carousels = sum(1 for info in infos if info.kind == "carousel")
    typer.echo(f"共 {len(infos)} 套模板（poster {posters} / carousel {carousels}）")


@app.command("poster")
def render_poster(
    template: str = typer.Option(..., "--template", "-t", help="poster 模板 ID"),
    data: Path = typer.Option(..., "--data", "-d", help="数据 JSON（{\"slots\":...}）"),
    out: Path = typer.Option(..., "--out", "-o", help="输出 PNG 路径"),
) -> None:
    """渲染单页海报。"""
    try:
        payload = _load_data(data)
        renderer = build_renderer()
        _check_kind(renderer, template, "poster", "poster")
        result = renderer.render(
            RenderSpec(kind="poster", out=out, template=template, data=payload)
        )
    except WorkbenchError as exc:
        _abort(exc)
    _print_result(result)


@app.command("carousel")
def render_carousel(
    template: str = typer.Option(..., "--template", "-t", help="carousel 模板 ID"),
    data: Path = typer.Option(
        ..., "--data", "-d", help="数据 JSON（{\"slots\":...,\"pages\":...}）"
    ),
    out: Path = typer.Option(..., "--out", "-o", help="输出目录（01.png ... + manifest.json）"),
) -> None:
    """渲染多页图文（九图/封面）。"""
    try:
        payload = _load_data(data)
        renderer = build_renderer()
        _check_kind(renderer, template, "carousel", "carousel")
        result = renderer.render(
            RenderSpec(kind="carousel", out=out, template=template, data=payload)
        )
    except WorkbenchError as exc:
        _abort(exc)
    _print_result(result)


__all__ = ["app"]
