"""``wb video``：打印内置分镜模板、合成竖版 MP4。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import typer

from workbench.errors import WorkbenchError
from workbench.video.build import build_composer
from workbench.video.storyboard import load_default_template, load_storyboard_file

app = typer.Typer(name="video", help="60 秒短视频：template / build")


def _abort(exc: WorkbenchError) -> NoReturn:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=exc.exit_code)


@app.command("template")
def show_template() -> None:
    """打印内置分镜模板（可直接重定向成 JSON 文件）。"""
    try:
        data = load_default_template()
    except WorkbenchError as exc:
        _abort(exc)
    typer.echo(json.dumps(data, ensure_ascii=False, indent=2))


@app.command("build")
def build(
    storyboard: Path = typer.Option(..., "--storyboard", "-s", help="分镜 JSON 路径"),
    out: Path = typer.Option(..., "--out", "-o", help="输出 MP4 路径"),
    tts: bool = typer.Option(
        False, "--tts", help="强制启用配音；无网络时自动降级为静音"
    ),
) -> None:
    """把分镜合成为 1080×1920 / H.264 / 25fps 的竖版 MP4。"""
    try:
        data = load_storyboard_file(storyboard)
        if tts:
            data = {**data, "tts": True}
        result = build_composer().compose(storyboard=data, out=out)
    except WorkbenchError as exc:
        _abort(exc)

    for warning in result.get("warnings", []):
        typer.echo(f"warning: {warning}", err=True)

    meta = result["meta"]
    typer.echo(
        f"视频：{result['path']}（{result['width']}×{result['height']}，"
        f"{result['duration_sec']:.2f}s，{result['bytes']} bytes，"
        f"{meta['scenes']} 个场景，tts={'on' if meta['tts'] else 'off'}）"
    )


__all__ = ["app"]
