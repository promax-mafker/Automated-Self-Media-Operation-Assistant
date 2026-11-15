"""离线 stub 模型：对任意 pydantic schema 生成确定性合法值（ADR §6.3）。"""

from __future__ import annotations

import types
import typing
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel

from workbench.errors import LLMError
from workbench.llm.base import Message

T = TypeVar("T", bound=BaseModel)

_UNION_ORIGINS = {typing.Union, types.UnionType}
_MAX_DEPTH = 10


class StubLLM:
    """确定性占位模型；``WB_LLM=stub`` 时全链路离线可跑。"""

    def __init__(self, model: str = "stub") -> None:
        self.model = model

    def text(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1200,
    ) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return str(message.get("content", ""))[:80]
        if messages:
            return str(messages[-1].get("content", ""))[:80]
        return ""

    def json(
        self,
        messages: Sequence[Message],
        schema: type[T],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> T:
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise LLMError("StubLLM.json 只支持 pydantic BaseModel schema", raw=repr(schema))
        payload = self._model_payload(schema, depth=0)
        try:
            return schema.model_validate(payload)
        except Exception as exc:
            raise LLMError(f"StubLLM 无法为 {schema.__name__} 生成合法值：{exc}") from exc

    # ------------------------------------------------------------------ 递归填充
    def _model_payload(self, model: type[BaseModel], depth: int) -> dict[str, Any]:
        if depth > _MAX_DEPTH:
            raise LLMError(f"StubLLM 不支持超过 {_MAX_DEPTH} 层嵌套的 schema")
        payload: dict[str, Any] = {}
        for name, field in model.model_fields.items():
            annotation = field.annotation if field.annotation is not None else Any
            payload[name] = self._value(annotation, name, depth + 1)
        return payload

    def _value(self, annotation: Any, field_name: str, depth: int) -> Any:
        if annotation is None or annotation is Any:
            return f"【stub】{field_name}"
        if annotation is type(None):
            return None

        origin = get_origin(annotation)
        args = get_args(annotation)

        if origin is typing.Annotated and args:
            return self._value(args[0], field_name, depth)

        if origin in _UNION_ORIGINS:
            candidates = [arg for arg in args if arg is not type(None)]
            if not candidates:
                return None
            return self._value(candidates[0], field_name, depth)

        if origin is typing.Literal:
            return args[0] if args else None

        if origin in (list, Sequence, typing.Sequence):  # type: ignore[comparison-overlap]
            item_type = args[0] if args else Any
            return [self._value(item_type, field_name, depth)]

        if origin in (set, frozenset):
            item_type = args[0] if args else Any
            return {self._value(item_type, field_name, depth)}

        if origin is tuple:
            if not args:
                return ()
            if len(args) == 2 and args[1] is Ellipsis:
                return (self._value(args[0], field_name, depth),)
            return tuple(self._value(arg, field_name, depth) for arg in args)

        if origin in (dict, Mapping, typing.Mapping):  # type: ignore[comparison-overlap]
            key_type = args[0] if len(args) >= 1 else str
            value_type = args[1] if len(args) >= 2 else Any
            return {
                self._key(key_type, field_name): self._value(value_type, field_name, depth)
            }

        # 无类型参数的裸容器（如 ``items: list``）也要给出合法值。
        if annotation is list:
            return [f"【stub】{field_name}"]
        if annotation is set:
            return {f"【stub】{field_name}"}
        if annotation is tuple:
            return (f"【stub】{field_name}",)
        if annotation is dict:
            return {f"stub_{field_name}": f"【stub】{field_name}"}

        if isinstance(annotation, type):
            if issubclass(annotation, BaseModel):
                return self._model_payload(annotation, depth)
            if issubclass(annotation, Enum):
                member = next(iter(annotation), None)
                return member.value if member is not None else None
            if annotation is bool:
                return True
            if annotation is int:
                return 1
            if annotation is float:
                return 1.0
            if annotation is str:
                return f"【stub】{field_name}"
            if annotation is datetime:
                return datetime(2026, 1, 1, tzinfo=timezone.utc)
            if annotation is date:
                return date(2026, 1, 1)
            if annotation is time:
                return time(0, 0)
            if annotation is timedelta:
                return timedelta(seconds=1)
            if annotation is Decimal:
                return Decimal("1")
            if annotation is UUID:
                return UUID("00000000-0000-0000-0000-000000000001")
            if annotation is Path:
                return Path(f"stub-{field_name}.txt")
            if annotation is bytes:
                return b"stub"
            if issubclass(annotation, (str, int, float, bool)):
                return self._value(
                    str if issubclass(annotation, str) else int, field_name, depth
                )

        # 兜底：用字符串占位，交给 pydantic 决定是否接受。
        return f"【stub】{field_name}"

    @staticmethod
    def _key(key_type: Any, field_name: str) -> Any:
        if key_type is int:
            return 1
        if key_type is bool:
            return True
        return f"stub_{field_name}"


__all__ = ["StubLLM"]
