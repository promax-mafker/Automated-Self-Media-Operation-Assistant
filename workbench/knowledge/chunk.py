"""Markdown 切块：按标题层级切，单块尽量落在 300-800 字。

规则（与 ``docs/kb.md`` 保持一致）：

- H1 视为文档标题，不进入 ``heading`` 路径；H2/H3/... 用 ``/`` 连接，
  例如 ``## 产品`` + ``### 价格`` → ``产品/价格``。
- 标题本身开启新块；同标题下的段落优先合并到 300 字以上，但不跨标题。
- 单段超过 800 字时按 ``。！？；`` 等句末标点切开，再按 300-800 字合并。
- 没有标题的文档使用文件名作为标题，``heading`` 为 ``None``。

切块是纯函数，不读写数据库，方便单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MIN_CHARS = 300
MAX_CHARS = 800

_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")


@dataclass(frozen=True, slots=True)
class Chunk:
    """一个待入库的片段。``heading`` 是 ``产品/价格`` 形式的路径。"""

    heading: str | None
    body: str


@dataclass(frozen=True, slots=True)
class ParsedDoc:
    """一篇文档的标题与片段。"""

    title: str
    chunks: list[Chunk]


def _split_long_sentences(text: str) -> list[str]:
    """把超长段落按句末标点切开；单句仍超长时硬切。"""
    sentences = [match.group(0).strip() for match in _SENTENCE_RE.finditer(text)]
    sentences = [sentence for sentence in sentences if sentence]
    if not sentences:
        sentences = [text.strip()] if text.strip() else []
    pieces: list[str] = []
    for sentence in sentences:
        while len(sentence) > MAX_CHARS:
            pieces.append(sentence[:MAX_CHARS])
            sentence = sentence[MAX_CHARS:]
        if sentence:
            pieces.append(sentence)
    return pieces


def _merge_short_tail(parts: list[str], separator: str = "") -> list[str]:
    """最后一块过短且能塞进前一块时合并，避免产生孤立的几十字碎片。"""
    if len(parts) < 2:
        return parts
    last = parts[-1]
    previous = parts[-2]
    if len(last) < MIN_CHARS and len(previous) + len(separator) + len(last) <= MAX_CHARS:
        parts[-2] = previous + separator + last
        parts.pop()
    return parts


def _pack(parts: list[str]) -> list[str]:
    """把句子合并成目标 300-800 字的块；中文句子直接相连，不额外加空格。"""
    packed: list[str] = []
    buffer: list[str] = []
    size = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if buffer and size + len(part) > MAX_CHARS:
            packed.append("".join(buffer))
            buffer = []
            size = 0
        buffer.append(part)
        size += len(part)
    if buffer:
        packed.append("".join(buffer))
    return _merge_short_tail([item for item in packed if item.strip()])


def _chunk_section(heading: str | None, paragraphs: list[str]) -> list[Chunk]:
    chunks: list[Chunk] = []
    buffer: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buffer, size
        body = "\n".join(buffer).strip()
        if body:
            chunks.append(Chunk(heading=heading, body=body))
        buffer = []
        size = 0

    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > MAX_CHARS:
            flush()
            for body in _pack(_split_long_sentences(paragraph)):
                chunks.append(Chunk(heading=heading, body=body))
            continue
        extra = 1 if buffer else 0
        if buffer and size + extra + len(paragraph) > MAX_CHARS:
            flush()
        buffer.append(paragraph)
        size += len(paragraph) + (1 if len(buffer) > 1 else 0)
    flush()
    # 同一标题下的最后一块过短时并入前一块，避免 100 字以下的孤立块。
    if len(chunks) >= 2 and chunks[-1].heading == chunks[-2].heading:
        last = chunks[-1]
        previous = chunks[-2]
        if (
            len(last.body) < MIN_CHARS
            and len(previous.body) + 1 + len(last.body) <= MAX_CHARS
        ):
            chunks[-2] = Chunk(heading=previous.heading, body=previous.body + "\n" + last.body)
            chunks.pop()
    return chunks


def _split_paragraphs(lines: list[str]) -> list[str]:
    paragraphs: list[str] = []
    buffer: list[str] = []
    for line in lines:
        if line.strip():
            buffer.append(line.rstrip())
        elif buffer:
            paragraphs.append("\n".join(buffer).strip())
            buffer = []
    if buffer:
        paragraphs.append("\n".join(buffer).strip())
    return [paragraph for paragraph in paragraphs if paragraph]


def parse_markdown(text: str, *, fallback_title: str = "") -> ParsedDoc:
    """解析 Markdown 文本，返回文档标题与片段列表。"""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    lines = normalized.split("\n")

    title = fallback_title.strip()
    h1_seen = False
    heading_path: list[str] = []
    sections: list[tuple[str | None, list[str]]] = []
    current: list[str] = []
    in_fence = False

    def flush_section() -> None:
        heading = "/".join(heading_path) or None
        sections.append((heading, _split_paragraphs(current)))
        current.clear()

    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            current.append(line)
            continue
        match = None if in_fence else _HEADING_RE.match(line)
        if match is None:
            current.append(line)
            continue

        flush_section()
        level = len(match.group(1))
        heading_text = match.group(2).strip()
        if level == 1 and not h1_seen:
            h1_seen = True
            title = heading_text or title
            heading_path = []
        elif level == 1:
            # 第二个 H1 当作普通标题，避免把多个文档拼在一起时丢标题。
            heading_path = [heading_text]
        else:
            heading_path = heading_path[: level - 2] + [heading_text]
    flush_section()

    chunks: list[Chunk] = []
    for heading, paragraphs in sections:
        chunks.extend(_chunk_section(heading, paragraphs))

    final_title = title or "未命名文档"
    return ParsedDoc(title=final_title, chunks=chunks)


def parse_file(path: Path) -> ParsedDoc:
    """读取 md/txt 文件并切块；文件名作为无 H1 文档的标题。"""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return parse_markdown(text, fallback_title=path.stem)


__all__ = [
    "MAX_CHARS",
    "MIN_CHARS",
    "Chunk",
    "ParsedDoc",
    "parse_file",
    "parse_markdown",
]
