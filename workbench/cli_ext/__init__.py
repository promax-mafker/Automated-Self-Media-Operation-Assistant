"""``workbench/cli_ext/*.py`` 自动发现。

后续 Ticket 只需新增文件并定义 ``app = typer.Typer(...)``；若未给 ``name``，
默认使用模块文件名作为子命令名。
"""

from __future__ import annotations

import importlib
import pkgutil

import typer

from workbench.errors import WorkbenchError


def iter_subcommands() -> list[typer.Typer]:
    apps: list[typer.Typer] = []
    for module_info in sorted(pkgutil.iter_modules(__path__), key=lambda item: item.name):
        if module_info.ispkg or module_info.name.startswith("_"):
            continue
        qualified = f"{__name__}.{module_info.name}"
        try:
            module = importlib.import_module(qualified)
        except Exception as exc:  # noqa: BLE001 - 需要把模块名和原异常一起报告
            raise WorkbenchError(
                f"cli_ext 模块导入失败：{qualified}（{type(exc).__name__}: {exc}）"
            ) from exc
        sub = getattr(module, "app", None)
        if sub is None:
            continue
        if not isinstance(sub, typer.Typer):
            raise WorkbenchError(
                f"cli_ext 模块 {qualified} 的 app 必须是 typer.Typer，"
                f"实际为 {type(sub).__name__}"
            )
        if not sub.info.name:
            sub.info.name = module_info.name
        apps.append(sub)
    return apps


__all__ = ["iter_subcommands"]
