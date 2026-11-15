"""``wb serve``：启动本地 Web 控制台（默认 ``127.0.0.1:8077``）。

- 单进程、单用户，不带登录/权限，只绑定本机回环地址（默认值）。
- 页面只读 store 与本地文件；长任务仍在终端用 ``wb run`` / ``wb copy gen`` 跑。
"""

from __future__ import annotations

from typing import NoReturn

import typer

from workbench.config import Config
from workbench.errors import WorkbenchError

app = typer.Typer(
    name="serve",
    help="本地 Web 控制台：概览 / 选题池 / 文案 / 素材 / 发布包 / 知识库问答",
)


def _abort(exc: WorkbenchError) -> NoReturn:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=exc.exit_code)


@app.callback(invoke_without_command=True)
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址（默认仅本机）"),
    port: int = typer.Option(8077, "--port", help="监听端口（默认 8077）"),
) -> None:
    """启动单进程控制台；浏览器打开提示的地址即可。"""
    try:
        cfg = Config.from_env()
        cfg.ensure_dirs()
    except WorkbenchError as exc:
        _abort(exc)

    try:
        import uvicorn

        from workbench.web.app import create_app
    except ImportError as exc:  # pragma: no cover - 依赖已在 pyproject 预置
        typer.echo(f"启动控制台失败：缺少依赖（{exc}）", err=True)
        raise typer.Exit(code=1) from None

    typer.echo(f"控制台：http://{host}:{port}")
    typer.echo(f"WB_HOME：{cfg.home}")
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="info")


__all__ = ["app"]
