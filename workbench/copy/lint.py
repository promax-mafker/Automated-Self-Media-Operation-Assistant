"""合规与引用校验（两道闸门中的第二道）。

规则（Ticket §4）：

1. 正文里的数字（价格、百分比、天数、人数）必须能在 ``evidence`` 原文里找到同数字，
   否则 ``passed=false``。
2. 命中 ``data/compliance/absolute_terms.txt`` 即 ``passed=false``，并给出
   ``source: target`` 映射里的替换建议。
3. 命中 ``data/compliance/sensitive_terms.txt`` 标记 ``needs_human=true``；按 ADR §5.2
   “lint 命中即 passed=false”的口径同时置 ``passed=false``，交给人判断。
4. ``passed=false`` 不阻断落库；CLI 用退出码 3 提示脚本。

``compliance`` 的结构遵循 ADR §5：``{passed, hits[], checked_at}``；本 Ticket 额外
写入 ``needs_human``，并给每条 hit 附 ``suggestion``，便于人工直接修改。
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from workbench.config import now_iso
from workbench.copy.grounding import load_evidence
from workbench.copy.schema import CopyBase, CopyValidationError, payload_text, schema_for
from workbench.errors import WorkbenchError

COMPLIANCE_DIR = Path(__file__).resolve().parents[2] / "data" / "compliance"
ABSOLUTE_TERMS_FILE = "absolute_terms.txt"
SENSITIVE_TERMS_FILE = "sensitive_terms.txt"
GENERIC_SUGGESTION = "改用更具体、可核验的表达"
SENSITIVE_SUGGESTION = "发布前需人工确认，必要时删除或改写"
NUMBER_SUGGESTION = "在知识库补充该数字的出处，或删除具体数字"

_NUMBER_RE = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")
_TERM_SPLIT_RE = re.compile(r"[:：]")


class ComplianceError(WorkbenchError):
    """词表缺失或格式错误。"""


class CopyLintFailed(WorkbenchError):
    """合规/取证未通过；CLI 退出码 3。"""

    exit_code = 3

    def __init__(self, passed: int, failed: int) -> None:
        self.passed = passed
        self.failed = failed
        super().__init__(f"文案 lint 未通过：passed={passed} failed={failed}（退出码 3）")


@dataclass(frozen=True, slots=True)
class Term:
    word: str
    suggestion: str | None = None


@dataclass(frozen=True, slots=True)
class ComplianceResult:
    passed: bool
    needs_human: bool
    hits: list[dict[str, Any]] = field(default_factory=list)
    checked_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "needs_human": self.needs_human,
            "hits": self.hits,
            "checked_at": self.checked_at,
        }


@dataclass(frozen=True, slots=True)
class LintReport:
    passed: int
    failed: int
    needs_human: int
    items: list[dict[str, Any]] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return self.failed == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "failed": self.failed,
            "needs_human": self.needs_human,
            "all_passed": self.all_passed,
            "items": self.items,
        }


def load_terms(path: Path) -> list[Term]:
    """读取词表；``source: target`` 的右侧作为替换建议，可省略。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ComplianceError(f"读取合规词表失败：{path}（{exc}）") from exc
    terms: list[Term] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = _TERM_SPLIT_RE.split(line, maxsplit=1)
        word = parts[0].strip()
        suggestion = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
        if not word or word in seen:
            continue
        seen.add(word)
        terms.append(Term(word=word, suggestion=suggestion))
    return terms


def load_compliance_terms(
    directory: Path | None = None,
) -> tuple[list[Term], list[Term]]:
    base = Path(directory) if directory is not None else COMPLIANCE_DIR
    absolute = load_terms(base / ABSOLUTE_TERMS_FILE)
    sensitive = load_terms(base / SENSITIVE_TERMS_FILE)
    if not absolute:
        raise ComplianceError(f"极限词表为空：{base / ABSOLUTE_TERMS_FILE}")
    if not sensitive:
        raise ComplianceError(f"敏感词表为空：{base / SENSITIVE_TERMS_FILE}")
    return absolute, sensitive


def numeric_values(text: str) -> set[Decimal]:
    """提取文本里的阿拉伯数字并归一化（``99`` 与 ``99.0`` 视为同一个数）。"""
    values: set[Decimal] = set()
    for match in _NUMBER_RE.finditer(text or ""):
        try:
            values.add(Decimal(match.group().replace(",", "")))
        except InvalidOperation:
            continue
    return values


def _find_terms(text: str, terms: Iterable[Term], kind: str) -> list[dict[str, Any]]:
    hits: list[dict[str, Any]] = []
    lowered = text.lower()
    for term in terms:
        index = lowered.find(term.word.lower())
        if index < 0:
            continue
        hit: dict[str, Any] = {
            "word": term.word,
            "kind": kind,
            "index": index,
            "note": "极限词" if kind == "absolute" else "敏感词，需人工确认",
            "suggestion": term.suggestion
            or (GENERIC_SUGGESTION if kind == "absolute" else SENSITIVE_SUGGESTION),
        }
        hits.append(hit)
    return hits


def _number_hits(text: str, evidence_text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    grounded = numeric_values(evidence_text)
    hits: list[dict[str, Any]] = []
    seen: set[Decimal] = set()
    for match in _NUMBER_RE.finditer(text):
        raw = match.group()
        try:
            value = Decimal(raw.replace(",", ""))
        except InvalidOperation:
            continue
        if value in grounded or value in seen:
            continue
        seen.add(value)
        hits.append(
            {
                "word": raw,
                "kind": "ungrounded_number",
                "index": match.start(),
                "note": "数字无出处",
                "suggestion": NUMBER_SUGGESTION,
            }
        )
    return hits


def check_text(
    text: str,
    evidence_text: str,
    *,
    absolute_terms: Sequence[Term],
    sensitive_terms: Sequence[Term],
    checked_at: str,
) -> ComplianceResult:
    """对一段完整文案做极限词、敏感词与数字出处检查。"""
    hits: list[dict[str, Any]] = []
    hits.extend(_find_terms(text, absolute_terms, "absolute"))
    hits.extend(_find_terms(text, sensitive_terms, "sensitive"))
    hits.extend(_number_hits(text, evidence_text))
    hits.sort(key=lambda item: (int(item["index"]), str(item["kind"]), str(item["word"])))

    absolute_hit = any(hit["kind"] == "absolute" for hit in hits)
    sensitive_hit = any(hit["kind"] == "sensitive" for hit in hits)
    number_hit = any(hit["kind"] == "ungrounded_number" for hit in hits)
    return ComplianceResult(
        passed=not (absolute_hit or sensitive_hit or number_hit),
        needs_human=sensitive_hit,
        hits=hits,
        checked_at=checked_at,
    )


def lint_payload(
    kind: str,
    length: str,
    payload: CopyBase | Mapping[str, Any],
    evidence_text: str,
    *,
    absolute_terms: Sequence[Term],
    sensitive_terms: Sequence[Term],
    checked_at: str,
) -> ComplianceResult:
    """按 kind 校验字段后 lint；字段缺失同样抛带 kind 的错误。"""
    model = payload if isinstance(payload, CopyBase) else schema_for(kind).model_validate(payload)
    return check_text(
        payload_text(model),
        evidence_text,
        absolute_terms=absolute_terms,
        sensitive_terms=sensitive_terms,
        checked_at=checked_at,
    )


def relint_payload(
    payload: Mapping[str, Any],
    *,
    absolute_terms: Sequence[Term] | None = None,
    sensitive_terms: Sequence[Term] | None = None,
    checked_at: str | None = None,
) -> LintReport:
    """重新 lint 一份 ``copies.json``；就地更新每个条目的 ``compliance``。

    evidence 从文件里的 ``evidence`` / ``brand`` 还原，不重新检索，保证可复算。
    """
    if absolute_terms is None or sensitive_terms is None:
        loaded_absolute, loaded_sensitive = load_compliance_terms()
        absolute_terms = absolute_terms if absolute_terms is not None else loaded_absolute
        sensitive_terms = sensitive_terms if sensitive_terms is not None else loaded_sensitive
    stamp = checked_at or now_iso()
    evidence_text = load_evidence(payload)
    copies = payload.get("copies")
    if not isinstance(copies, list) or not copies:
        raise WorkbenchError("copies.json 缺少非空的 copies 列表")

    items: list[dict[str, Any]] = []
    passed = 0
    failed = 0
    needs_human = 0
    for entry in copies:
        if not isinstance(entry, Mapping):
            raise WorkbenchError("copies.json 的 copies[] 必须是对象")
        kind = str(entry.get("kind", ""))
        length = str(entry.get("length", ""))
        try:
            model = schema_for(kind).model_validate(entry)
        except Exception as exc:  # noqa: BLE001 - 统一转成带 kind 的清晰错误
            raise CopyValidationError(kind, length, str(exc)) from exc
        result = check_text(
            payload_text(model),
            evidence_text,
            absolute_terms=absolute_terms,
            sensitive_terms=sensitive_terms,
            checked_at=stamp,
        )
        entry["compliance"] = result.as_dict()  # type: ignore[index]
        if result.passed:
            passed += 1
        else:
            failed += 1
        if result.needs_human:
            needs_human += 1
        items.append(
            {
                "id": entry.get("id"),
                "kind": kind,
                "length": length,
                "title": entry.get("title"),
                "passed": result.passed,
                "needs_human": result.needs_human,
                "hits": result.hits,
                "checked_at": result.checked_at,
            }
        )
    return LintReport(passed=passed, failed=failed, needs_human=needs_human, items=items)


def update_compliance(
    conn: sqlite3.Connection,
    copy_set_id: str,
    compliance: Mapping[str, Any],
) -> bool:
    """把重新 lint 的结果写回 ``copy_set.compliance``；找不到该行时返回 False。"""
    cursor = conn.execute(
        "UPDATE copy_set SET compliance = ? WHERE id = ?",
        (json.dumps(dict(compliance), ensure_ascii=False, separators=(",", ":")), copy_set_id),
    )
    conn.commit()
    return cursor.rowcount > 0


__all__ = [
    "ABSOLUTE_TERMS_FILE",
    "COMPLIANCE_DIR",
    "SENSITIVE_TERMS_FILE",
    "ComplianceError",
    "ComplianceResult",
    "CopyLintFailed",
    "LintReport",
    "Term",
    "check_text",
    "lint_payload",
    "load_compliance_terms",
    "load_terms",
    "numeric_values",
    "relint_payload",
    "update_compliance",
]
