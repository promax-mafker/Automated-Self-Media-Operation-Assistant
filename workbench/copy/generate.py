"""五类 × 两长度文案生成：每个组合一次模型调用，逐条过合规/取证闸门。

设计取舍：

- 一次 CLI 调用 = 10 次独立的 ``LLMClient.json`` 调用（每类每长度一次），
  这样 prompt 可以分别约束口播、笔记、种草、科普、钩子的结构；同一次 run 的
  10 行 ``copy_set`` 共用同一个 ``created_at``。
- 生成阶段就算出 ``compliance`` 并落库，``copy_lint`` 只是可重复执行的复核；
  ``passed=false`` 不阻断落库，但 CLI 返回退出码 3。
- stub 模式对 ``hook.variants`` 补足 5 条确定性占位变体；真实模型不足 5 条时
  报错，不替模型编造内容。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from workbench.config import Config, now_iso
from workbench.copy.grounding import Grounding, topic_brief
from workbench.copy.lint import (
    ComplianceResult,
    Term,
    check_text,
    load_compliance_terms,
)
from workbench.copy.prompts import PromptSpec, load_prompt
from workbench.copy.schema import (
    LENGTHS,
    CopyBase,
    CopyValidationError,
    finalize_payload,
    normalize_kinds,
    payload_text,
    schema_for,
    validate_payload,
)
from workbench.errors import LLMError, WorkbenchError
from workbench.ids import new_id
from workbench.llm.base import LLMClient
from workbench.llm.stub import StubLLM
from workbench.store.models import CopySet

COPIES_FILENAME = "copies.json"
COPIES_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class GeneratedCopy:
    """一条生成结果；``entry`` 是写入 copies.json 的字典。"""

    entry: dict[str, Any]
    compliance: ComplianceResult

    @property
    def copy_set_id(self) -> str:
        return str(self.entry["id"])


@dataclass(frozen=True, slots=True)
class GenerationResult:
    payload: dict[str, Any]
    copies: tuple[GeneratedCopy, ...]
    grounding: Grounding

    @property
    def failed(self) -> int:
        return sum(1 for copy in self.copies if not copy.compliance.passed)

    @property
    def passed(self) -> int:
        return len(self.copies) - self.failed


def _model_check(kind: str, length: str, exc: BaseException) -> CopyValidationError:
    return CopyValidationError(kind, length, f"模型输出不可用：{exc}")


def generate_copies(
    llm: LLMClient,
    cfg: Config,
    topic: Any,
    *,
    platform: str,
    grounding: Grounding,
    kinds: Sequence[str] | str | None = None,
    prompt_dir: Path | None = None,
    absolute_terms: Sequence[Term] | None = None,
    sensitive_terms: Sequence[Term] | None = None,
    created_at: str | None = None,
    run_id: str | None = None,
    log: Any = None,
) -> GenerationResult:
    """生成 ``kinds × (short, long)`` 条文案并完成 lint。"""
    selected = normalize_kinds(kinds if isinstance(kinds, str) else ",".join(kinds or []))
    specs: dict[str, PromptSpec] = {
        kind: load_prompt(kind, prompt_dir=prompt_dir) for kind in selected
    }
    if absolute_terms is None or sensitive_terms is None:
        loaded_absolute, loaded_sensitive = load_compliance_terms()
        absolute_terms = absolute_terms if absolute_terms is not None else loaded_absolute
        sensitive_terms = sensitive_terms if sensitive_terms is not None else loaded_sensitive

    stamp = created_at or now_iso(cfg.tz)
    is_stub = isinstance(llm, StubLLM)
    generated: list[GeneratedCopy] = []

    for kind in selected:
        spec = specs[kind]
        model_cls = schema_for(kind)
        for length in LENGTHS:
            brief = _brief(topic, kind, length)
            messages = spec.render(
                brief=brief,
                evidence=grounding.evidence_text,
                brand=grounding.brand,
            )
            if callable(log):
                log(f"copy_generate: {kind}/{length} 调用模型 {llm.model}")
            try:
                raw = llm.json(
                    messages,
                    model_cls,
                    temperature=spec.temperature,
                    max_tokens=spec.max_tokens,
                )
            except (LLMError, ValidationError) as exc:
                raise _model_check(kind, length, exc) from exc
            payload = validate_payload(kind, length, raw)
            payload = finalize_payload(kind, length, payload, is_stub=is_stub)
            compliance = check_text(
                payload_text(payload),
                grounding.grounded_text,
                absolute_terms=absolute_terms,
                sensitive_terms=sensitive_terms,
                checked_at=stamp,
            )
            entry = _entry(
                payload,
                kind=kind,
                length=length,
                model=llm.model,
                prompt_version=spec.prompt_version,
                compliance=compliance,
                citations=grounding.citations,
                platform=platform,
                topic_id=_topic_id(topic),
                created_at=stamp,
            )
            generated.append(GeneratedCopy(entry=entry, compliance=compliance))

    payload = {
        "schema_version": COPIES_SCHEMA_VERSION,
        "run_id": run_id,
        "topic_id": _topic_id(topic),
        "platform": platform,
        "generated_at": stamp,
        "model": llm.model,
        "kinds": list(selected),
        "lengths": list(LENGTHS),
        "count": len(generated),
        "query": grounding.query,
        "brand": grounding.brand,
        "evidence": [
            {
                "doc_id": hit.doc_id,
                "chunk_id": hit.chunk_id,
                "title": hit.title,
                "heading": hit.heading,
                "score": hit.score,
                "body": hit.body,
            }
            for hit in grounding.hits
        ],
        "citations": grounding.citations,
        "copies": [copy.entry for copy in generated],
    }
    return GenerationResult(payload=payload, copies=tuple(generated), grounding=grounding)


def _topic_id(topic: Any) -> str | None:
    value = topic.get("id") if isinstance(topic, Mapping) else getattr(topic, "id", None)
    return str(value) if value else None


def _brief(topic: Any, kind: str, length: str) -> dict[str, Any]:
    return topic_brief(topic, kind=kind, length=length)


def _entry(
    payload: CopyBase,
    *,
    kind: str,
    length: str,
    model: str,
    prompt_version: str,
    compliance: ComplianceResult,
    citations: list[dict[str, Any]],
    platform: str,
    topic_id: str | None,
    created_at: str,
) -> dict[str, Any]:
    entry = payload.model_dump(mode="json")
    entry.update(
        {
            "id": new_id("copy"),
            "topic_id": topic_id,
            "platform": platform,
            "kind": kind,
            "length": length,
            "model": model,
            "prompt_version": prompt_version,
            "citations": list(citations),
            "compliance": compliance.as_dict(),
            "created_at": created_at,
        }
    )
    # 公共字段顺序固定，便于人读与 diff。
    ordered_keys = (
        "id",
        "topic_id",
        "platform",
        "kind",
        "length",
        "title",
        "hook",
        "body",
        "tags",
        "shots",
        "variants",
        "model",
        "prompt_version",
        "citations",
        "compliance",
        "created_at",
    )
    return {key: entry[key] for key in ordered_keys if key in entry}


def to_copy_set(entry: Mapping[str, Any], *, created_at: str) -> CopySet:
    """把 copies.json 条目转成 ``copy_set`` 行；字段缺失时抛清晰错误。"""
    kind = str(entry.get("kind", ""))
    length = str(entry.get("length", ""))
    try:
        return CopySet(
            id=str(entry["id"]),
            topic_id=entry.get("topic_id"),
            kind=kind,
            length=length,
            title=entry.get("title"),
            hook=entry.get("hook"),
            body=str(entry.get("body", "")),
            tags=list(entry.get("tags") or []),
            citations=list(entry.get("citations") or []),
            model=entry.get("model"),
            prompt_version=entry.get("prompt_version"),
            compliance=dict(entry.get("compliance") or {}),
            created_at=created_at,
        )
    except ValidationError as exc:
        raise CopyValidationError(kind, length, f"copy_set 落库校验失败：{exc}") from exc


def write_copies(path: Path | str, payload: Mapping[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def load_copies(path: Path | str) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        raise WorkbenchError(f"copies.json 不存在：{target}")
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkbenchError(f"copies.json 不是合法 JSON：{target}（{exc}）") from exc
    if not isinstance(payload, dict):
        raise WorkbenchError(f"copies.json 顶层必须是对象：{target}")
    copies = payload.get("copies")
    if not isinstance(copies, list) or not copies:
        raise WorkbenchError(f"copies.json 缺少非空的 copies 列表：{target}")
    return payload


def summary_lines(payload: Mapping[str, Any]) -> list[str]:
    """CLI/PR 用的一行摘要：``kind/length/title``。"""
    return [
        f"{item.get('kind', '?')}/{item.get('length', '?')}\t{item.get('title', '')}"
        for item in payload.get("copies", [])
    ]


__all__ = [
    "COPIES_FILENAME",
    "COPIES_SCHEMA_VERSION",
    "GenerationResult",
    "GeneratedCopy",
    "generate_copies",
    "load_copies",
    "summary_lines",
    "to_copy_set",
    "write_copies",
]
