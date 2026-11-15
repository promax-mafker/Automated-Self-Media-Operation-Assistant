"""模型层公共协议（ADR §6.3）。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, TypedDict, TypeVar

from pydantic import BaseModel


class Message(TypedDict):
    role: str
    content: str


T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    model: str

    def text(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1200,
    ) -> str: ...

    def json(
        self,
        messages: Sequence[Message],
        schema: type[T],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> T: ...


__all__ = ["LLMClient", "Message"]
