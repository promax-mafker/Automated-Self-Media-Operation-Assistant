"""Stage 注册表。新增 stage 用 ``@register("stage_name")``，不改本文件。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from workbench.errors import WorkbenchError
from workbench.pipeline.stage import Stage

_REGISTRY: dict[str, Any] = {}


def register(name: str) -> Callable[[Any], Any]:
    """把 stage 类（或实例）注册到全局表；重复注册不同对象会报错。"""
    normalized = name.strip()
    if not normalized:
        raise ValueError("stage 名称不能为空")
    if "/" in normalized:
        raise ValueError(f"stage 名称不能包含 '/': {normalized!r}")

    def decorator(obj: Any) -> Any:
        existing = _REGISTRY.get(normalized)
        if existing is not None and existing is not obj:
            raise WorkbenchError(f"stage 重名：{normalized}")
        _REGISTRY[normalized] = obj
        return obj

    return decorator


def get_stage(name: str) -> Stage:
    """实例化并校验 stage；未注册时抛 ``KeyError``。"""
    obj = _REGISTRY.get(name)
    if obj is None:
        raise KeyError(name)
    stage = obj() if isinstance(obj, type) else obj
    stage_name = getattr(stage, "name", None)
    if stage_name != name:
        raise WorkbenchError(
            f"stage 注册名与实例 name 不一致：注册为 {name!r}，实例为 {stage_name!r}"
        )
    if not callable(getattr(stage, "run", None)):
        raise WorkbenchError(f"stage {name!r} 缺少可调用的 run()")
    return stage  # type: ignore[no-any-return]


def has_stage(name: str) -> bool:
    return name in _REGISTRY


def stage_names() -> list[str]:
    return sorted(_REGISTRY)


def clear_registry() -> None:
    """仅用于测试/工具；正常流程不应清空注册表。"""
    _REGISTRY.clear()


__all__ = ["clear_registry", "get_stage", "has_stage", "register", "stage_names"]
