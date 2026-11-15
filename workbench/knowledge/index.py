"""SQLite FTS5 + jieba 索引与检索。

写入：

- ``kb_doc`` 以 ``sha256`` 去重；同一 ``source_path`` 内容变化时替换旧文档。
- ``kb_chunk`` 按切块顺序写入。
- ``kb_fts(chunk_id UNINDEXED, seg)`` 的 ``seg`` 是
  ``jieba.cut_for_search(text)`` 用空格连接的结果。

查询：

- 查询文本同样先分词，再用 ``MATCH '"词1" OR "词2"'`` 匹配。
- 使用 ``bm25(kb_fts)`` 升序排序（值越小越相关）。
- 返回的 ``KBHit.score`` 用本次结果集做 min-max 归一化到 0-1，越大越相关。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Iterable, Sequence
from pathlib import Path

import jieba

from workbench.config import Config, now_iso
from workbench.contracts import KBHit
from workbench.errors import StoreError
from workbench.ids import new_id
from workbench.knowledge.chunk import ParsedDoc, parse_file
from workbench.store.db import transaction

jieba.setLogLevel(logging.WARNING)

SUPPORTED_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
MIN_QUERY_CHARS = 2
MAX_K = 20
_TOKEN_RE = re.compile(r"[0-9A-Za-z_\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    """jieba 搜索模式分词；过滤标点与单字，保留至少 2 个字符的词。"""
    tokens: list[str] = []
    seen: set[str] = set()
    for raw in jieba.cut_for_search(text or ""):
        for token in _TOKEN_RE.findall(raw):
            if len(token) < 2 or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
    return tokens


def build_seg(text: str) -> str:
    """把文本转成 ``kb_fts.seg``：分词后空格连接。"""
    return " ".join(tokenize(text))


def match_expression(tokens: Sequence[str]) -> str:
    return " OR ".join(f'"{token}"' for token in tokens)


def collect_files(paths: Iterable[Path]) -> list[Path]:
    """展开目录与单文件，只保留 md/txt，按路径排序去重。"""
    files: list[Path] = []
    seen: set[Path] = set()
    for raw in paths:
        path = Path(raw).expanduser()
        candidates: list[Path] = []
        if path.is_dir():
            candidates = sorted(
                item
                for item in path.rglob("*")
                if item.is_file() and item.suffix.lower() in SUPPORTED_SUFFIXES
            )
        elif path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            candidates = [path]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(resolved)
    return files


def infer_kind(path: Path) -> str:
    """按文件名推断 ``kb_doc.kind``；无法判断时用 ``other``。"""
    name = path.stem.lower()
    if "faq" in name or "问答" in name:
        return "faq"
    if "case" in name or "案例" in name:
        return "case"
    if "product" in name or "price" in name or "产品" in name or "价格" in name:
        return "product"
    if any(word in name for word in ("brand", "voice", "audience", "note", "品牌", "语气", "人群")):
        return "note"
    return "other"


def infer_tags(path: Path, kind: str) -> list[str]:
    parts = [part for part in path.stem.lower().split("_") if part and part != "demo"]
    tags = [*parts, kind]
    return list(dict.fromkeys(tags))


def score_hits(ranks: Sequence[float]) -> list[float]:
    """把 bm25 值做 min-max 归一化：越小（越相关）得分越接近 1。"""
    if not ranks:
        return []
    best = min(ranks)
    worst = max(ranks)
    if abs(worst - best) < 1e-12:
        return [1.0 for _ in ranks]
    return [round((worst - rank) / (worst - best), 6) for rank in ranks]


class KnowledgeBase:
    """基于 SQLite FTS5 的知识库实现（``contracts.KB``）。"""

    def __init__(self, cfg: Config, conn: sqlite3.Connection) -> None:
        self.cfg = cfg
        self.conn = conn

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> KnowledgeBase:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ 导入
    def add(self, paths: Sequence[Path]) -> dict[str, int]:
        """导入 md/txt；返回 ``{docs, skipped, chunks}``。

        - ``docs``：本次新写入的文档数。
        - ``skipped``：sha256 已存在而跳过的文件数（幂等）。
        - ``chunks``：本次新写入的片段数。
        """
        paths = list(paths)
        if not paths:
            return {}
        stats = {"docs": 0, "skipped": 0, "chunks": 0}
        for path in collect_files(paths):
            raw = path.read_bytes()
            sha256 = hashlib.sha256(raw).hexdigest()
            existing = self.conn.execute(
                "SELECT id FROM kb_doc WHERE sha256 = ? LIMIT 1", (sha256,)
            ).fetchone()
            if existing is not None:
                stats["skipped"] += 1
                continue

            parsed = parse_file(path)
            source_path = path.resolve().as_posix()
            old_doc_id = self._find_by_source_path(source_path)
            doc_id = new_id("kbdoc")
            kind = infer_kind(path)
            meta = {"tags": infer_tags(path, kind), "title": parsed.title}
            try:
                with transaction(self.conn):
                    if old_doc_id is not None:
                        self._delete_doc_rows(old_doc_id)
                    self.conn.execute(
                        "INSERT INTO kb_doc "
                        "(id, title, kind, source_path, sha256, updated_at, meta) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            doc_id,
                            parsed.title,
                            kind,
                            source_path,
                            sha256,
                            now_iso(self.cfg.tz),
                            json.dumps(meta, ensure_ascii=False, separators=(",", ":")),
                        ),
                    )
                    self._insert_chunks(doc_id, parsed)
            except sqlite3.Error as exc:
                raise StoreError(f"导入 {path} 失败：{exc}") from exc

            stats["docs"] += 1
            stats["chunks"] += len(parsed.chunks)
        return stats

    def _insert_chunks(self, doc_id: str, parsed: ParsedDoc) -> None:
        for index, chunk in enumerate(parsed.chunks):
            chunk_id = new_id("kbchunk")
            self.conn.execute(
                "INSERT INTO kb_chunk (id, doc_id, ord, heading, body) VALUES (?, ?, ?, ?, ?)",
                (chunk_id, doc_id, index, chunk.heading, chunk.body),
            )
            seg_source = f"{chunk.heading or ''}\n{parsed.title}\n{chunk.body}"
            self.conn.execute(
                "INSERT INTO kb_fts (chunk_id, seg) VALUES (?, ?)",
                (chunk_id, build_seg(seg_source)),
            )

    def _find_by_source_path(self, source_path: str) -> str | None:
        row = self.conn.execute(
            "SELECT id FROM kb_doc WHERE source_path = ? LIMIT 1", (source_path,)
        ).fetchone()
        return str(row[0]) if row is not None else None

    def _delete_doc_rows(self, doc_id: str) -> None:
        self.conn.execute(
            "DELETE FROM kb_fts WHERE chunk_id IN "
            "(SELECT id FROM kb_chunk WHERE doc_id = ?)",
            (doc_id,),
        )
        self.conn.execute("DELETE FROM kb_chunk WHERE doc_id = ?", (doc_id,))
        self.conn.execute("DELETE FROM kb_doc WHERE id = ?", (doc_id,))

    # ------------------------------------------------------------------ 检索
    def search(self, query: str, *, k: int = 5) -> list[KBHit]:
        """检索命中片段；空查询、单字查询或命中为空都返回 ``[]``。"""
        normalized = (query or "").strip()
        if len(normalized) < MIN_QUERY_CHARS:
            return []
        tokens = tokenize(normalized)
        if not tokens:
            return []

        limit = max(1, min(int(k), MAX_K))
        sql = (
            "SELECT c.id AS chunk_id, c.doc_id, c.heading, c.body, d.title, "
            "bm25(kb_fts) AS rank "
            "FROM kb_fts "
            "JOIN kb_chunk AS c ON c.id = kb_fts.chunk_id "
            "JOIN kb_doc AS d ON d.id = c.doc_id "
            "WHERE kb_fts MATCH ? "
            "ORDER BY rank ASC, c.id ASC "
            "LIMIT ?"
        )
        try:
            rows = self.conn.execute(sql, (match_expression(tokens), limit)).fetchall()
        except sqlite3.Error as exc:
            raise StoreError(f"知识库检索失败：{exc}") from exc

        ranks = [float(row["rank"]) for row in rows]
        scores = score_hits(ranks)
        return [
            KBHit(
                doc_id=str(row["doc_id"]),
                chunk_id=str(row["chunk_id"]),
                title=str(row["title"]),
                body=str(row["body"]),
                heading=str(row["heading"]) if row["heading"] is not None else None,
                score=score,
            )
            for row, score in zip(rows, scores, strict=True)
        ]


__all__ = [
    "KnowledgeBase",
    "MAX_K",
    "MIN_QUERY_CHARS",
    "SUPPORTED_SUFFIXES",
    "build_seg",
    "collect_files",
    "infer_kind",
    "infer_tags",
    "match_expression",
    "score_hits",
    "tokenize",
]
