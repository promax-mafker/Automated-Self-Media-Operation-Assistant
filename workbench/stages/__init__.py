"""Stage 自动发现：用 ``pkgutil.iter_modules`` 导入本包下所有模块。

新增 stage 只需在本目录新增文件，并在模块里 ``@register("stage_name")``，
不需要修改本文件或任何共享文件。
"""

from __future__ import annotations

import importlib
import pkgutil

from workbench.errors import WorkbenchError

_LOADED = False


def load_stages() -> None:
    """导入全部 stage 模块；失败时指出模块名与异常。幂等。"""
    global _LOADED
    if _LOADED:
        return
    for module_info in pkgutil.iter_modules(__path__):
        if module_info.ispkg or module_info.name.startswith("_"):
            continue
        qualified = f"{__name__}.{module_info.name}"
        try:
            importlib.import_module(qualified)
        except Exception as exc:  # noqa: BLE001 - 需要把模块名和原异常一起报告
            raise WorkbenchError(
                f"stage 模块导入失败：{qualified}（{type(exc).__name__}: {exc}）"
            ) from exc
    _LOADED = True


__all__ = ["load_stages"]
