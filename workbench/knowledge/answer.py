"""引用回答、片段截断与离线评测。

- ``ask``：把用户问题变成 3-5 条 ``KBHit``，空查询/无命中时给出提示而不抛异常。
- ``evaluate``：对 ``data/kb/eval.jsonl`` 计算 recall@k（期望文档是否出现在前 k 条）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from workbench.contracts import KB, KBHit
from workbench.errors import WorkbenchError
from workbench.knowledge.index import MIN_QUERY_CHARS

SNIPPET_CHARS = 80

EMPTY_QUERY_HINT = "查询词为空或少于 2 个字，未执行检索；请输入至少 2 个字的主题词。"
NO_HIT_HINT = "未找到相关片段：换个关键词，或先运行 `wb kb add data/kb/seed` 导入资料。"


@dataclass(frozen=True, slots=True)
class AskResult:
    query: str
    hits: list[KBHit]
    hint: str | None = None


@dataclass(frozen=True, slots=True)
class EvalItem:
    query: str
    doc_title: str
    note: str | None = None


@dataclass(frozen=True, slots=True)
class EvalMiss:
    query: str
    expected: str
    got: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class EvalReport:
    total: int
    hits: int
    recall: float
    k: int
    misses: list[EvalMiss] = field(default_factory=list)


def snippet(body: str, *, limit: int = SNIPPET_CHARS) -> str:
    """压平空白后截取前 ``limit`` 个字符；不改变原片段含义。"""
    text = re.sub(r"\s+", " ", body or "").strip()
    return text[:limit]


def hit_to_dict(hit: KBHit) -> dict[str, object]:
    """CLI ``--json`` 的结构化命中；``doc_title`` 与 Table 表头对应。"""
    return {
        "doc_id": hit.doc_id,
        "chunk_id": hit.chunk_id,
        "doc_title": hit.title,
        "heading": hit.heading,
        "score": round(float(hit.score), 6),
        "snippet": snippet(hit.body),
    }


def ask(kb: KB, query: str, *, k: int = 5) -> AskResult:
    normalized = (query or "").strip()
    if len(normalized) < MIN_QUERY_CHARS:
        return AskResult(query=normalized, hits=[], hint=EMPTY_QUERY_HINT)
    hits = kb.search(normalized, k=k)
    return AskResult(
        query=normalized,
        hits=hits,
        hint=None if hits else NO_HIT_HINT,
    )


def load_eval(path: Path | str) -> list[EvalItem]:
    """读取 JSONL 评测集；每行至少包含 ``q`` 与 ``doc``。"""
    target = Path(path)
    items: list[EvalItem] = []
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise WorkbenchError(f"读取评测集失败：{target}（{exc}）") from exc
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("//"):
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise WorkbenchError(f"评测集第 {number} 行不是合法 JSON：{exc}") from exc
        query = str(payload.get("q") or "").strip()
        doc_title = str(payload.get("doc") or "").strip()
        if not query or not doc_title:
            raise WorkbenchError(f"评测集第 {number} 行缺少 q 或 doc 字段")
        items.append(EvalItem(query=query, doc_title=doc_title, note=payload.get("note")))
    return items


def evaluate(kb: KB, items: Sequence[EvalItem], *, k: int = 3) -> EvalReport:
    """按“期望文档标题是否出现在前 k 条命中”计算 recall@k。"""
    if not items:
        return EvalReport(total=0, hits=0, recall=0.0, k=k)
    missed: list[EvalMiss] = []
    hit_count = 0
    for item in items:
        hits = kb.search(item.query, k=k)
        titles = [hit.title for hit in hits]
        if item.doc_title in titles:
            hit_count += 1
        else:
            missed.append(EvalMiss(query=item.query, expected=item.doc_title, got=titles))
    return EvalReport(
        total=len(items),
        hits=hit_count,
        recall=hit_count / len(items),
        k=k,
        misses=missed,
    )


__all__ = [
    "EMPTY_QUERY_HINT",
    "NO_HIT_HINT",
    "SNIPPET_CHARS",
    "AskResult",
    "EvalItem",
    "EvalMiss",
    "EvalReport",
    "ask",
    "evaluate",
    "hit_to_dict",
    "load_eval",
    "snippet",
]
