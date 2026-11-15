"""知识库取证：用选题卡检索 KB，给 prompt 灌 evidence / brand，并生成可回溯引用。

约定：

- 每次生成最多使用 5 条 KB 命中（``contracts.KB.search`` 的返回顺序）。
- ``evidence`` 只来自 KB；没有命中时留空，prompt 会明确要求“不要写具体数字”。
- ``brand`` 是 ``data/kb`` 中品牌/语气类资料的摘要，可为空；命中时也会被当作
  数字取证的来源之一。
- ``citations`` 写进 ``copy_set.citations``，形如
  ``[{doc_id, chunk_id, quote}]``，quote 为片段前 80 字。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from workbench.contracts import KB, KBHit
from workbench.copy.schema import LENGTH_TARGET_CHARS

MAX_EVIDENCE = 5
BRAND_QUERY = "品牌 定位 目标人群 语气"
QUOTE_CHARS = 80
BRAND_CHARS = 200


def snippet(text: str, limit: int = QUOTE_CHARS) -> str:
    """压平空白并截断，用于引用与 prompt 展示。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else f"{flat[:limit]}…"


TopicInput = BaseModel | Mapping[str, Any]


def _field(topic: TopicInput, name: str) -> str:
    value = topic.get(name) if isinstance(topic, Mapping) else getattr(topic, name, None)
    return str(value).strip() if value else ""


def topic_query(topic: TopicInput) -> str:
    """用选题卡的标题、角度、钩子拼检索串；空字段跳过。"""
    parts = [_field(topic, name) for name in ("title", "angle", "hook")]
    return " ".join(part for part in parts if part)


def topic_brief(topic: TopicInput, *, kind: str, length: str) -> dict[str, Any]:
    """给 prompt 的 ``{brief}``：选题卡 + 本次 kind/length/目标字数。"""
    if isinstance(topic, BaseModel):
        brief = topic.model_dump(mode="json")
    else:
        brief = dict(topic)
    brief["kind"] = kind
    brief["length"] = length
    brief["target_chars"] = LENGTH_TARGET_CHARS[length]
    return brief


@dataclass(frozen=True, slots=True)
class Grounding:
    """一次生成共用的取证结果。"""

    query: str
    hits: tuple[KBHit, ...] = ()
    brand: str | None = None

    @property
    def evidence_text(self) -> str:
        if not self.hits:
            return ""
        blocks: list[str] = []
        for index, hit in enumerate(self.hits, start=1):
            heading = f" / {hit.heading}" if hit.heading else ""
            blocks.append(f"[{index}] 《{hit.title}》{heading}\n{hit.body}")
        return "\n\n".join(blocks)

    @property
    def grounded_text(self) -> str:
        """数字取证可用的全部原文：命中片段 + 品牌摘要。"""
        parts = [hit.body for hit in self.hits]
        if self.brand:
            parts.append(self.brand)
        return "\n".join(parts)

    @property
    def citations(self) -> list[dict[str, Any]]:
        return [
            {
                "doc_id": hit.doc_id,
                "chunk_id": hit.chunk_id,
                "quote": snippet(hit.body),
            }
            for hit in self.hits
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "brand": self.brand,
            "evidence": [
                {
                    "doc_id": hit.doc_id,
                    "chunk_id": hit.chunk_id,
                    "title": hit.title,
                    "heading": hit.heading,
                    "score": hit.score,
                    "body": hit.body,
                }
                for hit in self.hits
            ],
            "citations": self.citations,
        }


def _dedupe(hits: list[KBHit], limit: int = MAX_EVIDENCE) -> list[KBHit]:
    seen: set[str] = set()
    result: list[KBHit] = []
    for hit in hits:
        if hit.chunk_id in seen:
            continue
        seen.add(hit.chunk_id)
        result.append(hit)
        if len(result) >= limit:
            break
    return result


def retrieve(
    kb: KB,
    topic: TopicInput,
    *,
    k: int = MAX_EVIDENCE,
) -> Grounding:
    """检索选题相关片段与品牌摘要；KB 为空时返回空取证。"""
    query = topic_query(topic)
    hits = kb.search(query, k=k) if query else []
    brand: str | None = None
    brand_hits = kb.search(BRAND_QUERY, k=1)
    if brand_hits:
        brand = snippet(brand_hits[0].body, BRAND_CHARS) or None
    merged = _dedupe([*hits, *brand_hits], limit=k)
    return Grounding(query=query, hits=tuple(merged), brand=brand)


def evidence_from_payload(payload: Mapping[str, Any]) -> tuple[str, str | None]:
    """从 ``copies.json`` 取回 evidence 原文与 brand，供重新 lint。"""
    evidence: list[str] = []
    for item in payload.get("evidence", []) or []:
        if isinstance(item, Mapping) and item.get("body"):
            evidence.append(str(item["body"]))
        elif isinstance(item, Mapping) and item.get("quote"):
            evidence.append(str(item["quote"]))
    brand = payload.get("brand")
    return "\n".join(evidence), str(brand) if brand else None


def load_evidence(payload: Mapping[str, Any]) -> str:
    """``copies.json`` 中的取证原文（evidence + brand）。"""
    evidence, brand = evidence_from_payload(payload)
    return f"{evidence}\n{brand}" if brand else evidence


def brief_json(brief: Mapping[str, Any]) -> str:
    """调试/日志用：紧凑 JSON。"""
    return json.dumps(brief, ensure_ascii=False, separators=(",", ":"))


__all__ = [
    "BRAND_QUERY",
    "MAX_EVIDENCE",
    "Grounding",
    "TopicInput",
    "evidence_from_payload",
    "brief_json",
    "load_evidence",
    "retrieve",
    "snippet",
    "topic_brief",
    "topic_query",
]
