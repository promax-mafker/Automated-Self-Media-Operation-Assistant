"""OpenAI 兼容 ``/chat/completions`` 客户端（ADR §6.3）。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from workbench.errors import LLMError
from workbench.llm.base import Message

T = TypeVar("T", bound=BaseModel)


def _extract_json(content: str) -> Any:
    """容忍模型把 JSON 包在 Markdown 代码块里的情况。"""
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


class OpenAICompatLLM:
    """任意 OpenAI 兼容端点；只依赖 ``httpx``，不绑定厂商 SDK。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def text(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.3,
        max_tokens: int = 1200,
    ) -> str:
        return self._chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=None,
        )

    def json(
        self,
        messages: Sequence[Message],
        schema: type[T],
        *,
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> T:
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise LLMError("OpenAICompatLLM.json 只支持 pydantic BaseModel schema")
        last_raw = ""
        for attempt in range(2):
            content = self._chat(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            last_raw = content
            try:
                return schema.model_validate(_extract_json(content))
            except (ValueError, ValidationError) as exc:
                if attempt == 0:
                    continue
                raise LLMError(
                    f"模型返回的 JSON 无法按 {schema.__name__} 校验：{exc}", raw=content
                ) from exc
        raise LLMError("模型返回的 JSON 无法解析", raw=last_raw)

    def _chat(
        self,
        messages: Sequence[Message],
        *,
        temperature: float,
        max_tokens: int,
        response_format: dict[str, str] | None,
    ) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format is not None:
            payload["response_format"] = response_format
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"请求模型失败：{exc}") from exc
        if response.status_code >= 400:
            raise LLMError(f"模型返回 HTTP {response.status_code}", raw=response.text)
        try:
            data = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                "模型返回结构不符合 choices[0].message.content", raw=response.text
            ) from exc
        return str(content or "")


__all__ = ["OpenAICompatLLM"]
