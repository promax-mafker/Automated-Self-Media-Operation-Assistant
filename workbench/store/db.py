"""SQLite 连接、PRAGMA、迁移与事务（ADR §5）。

DDL 是唯一权威，逐字来自 ADR §5；不允许在别处 ``CREATE TABLE``。
迁移按 ``PRAGMA user_version`` 推进，重复执行幂等。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from workbench.errors import StoreError

# 名字 → 单条迁移 SQL。只允许追加，不允许修改已发布迁移的语义。
MIGRATIONS: list[tuple[str, str]] = [
    (
        "0001_initial",
        """
CREATE TABLE IF NOT EXISTS account (
  id           TEXT PRIMARY KEY,
  platform     TEXT NOT NULL,                       -- xhs | douyin | shipinhao | wechat
  handle       TEXT NOT NULL,
  display_name TEXT,
  is_benchmark INTEGER NOT NULL DEFAULT 0,
  niche        TEXT,
  note         TEXT,
  created_at   TEXT NOT NULL,
  UNIQUE (platform, handle)
);

CREATE TABLE IF NOT EXISTS post (
  id           TEXT PRIMARY KEY,
  account_id   TEXT NOT NULL REFERENCES account(id) ON DELETE CASCADE,
  platform     TEXT NOT NULL,
  title        TEXT NOT NULL,
  url          TEXT,
  published_at TEXT NOT NULL,                       -- ISO 8601 +08:00
  content_type TEXT NOT NULL,                       -- image_text | video | article
  topic_tags   TEXT NOT NULL DEFAULT '[]',          -- JSON string[]
  duration_sec REAL,
  caption      TEXT,
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS post_account_time ON post (account_id, published_at);

CREATE TABLE IF NOT EXISTS metric (
  id              TEXT PRIMARY KEY,
  post_id         TEXT NOT NULL REFERENCES post(id) ON DELETE CASCADE,
  captured_at     TEXT NOT NULL,
  views           INTEGER,
  likes           INTEGER,
  comments        INTEGER,
  shares          INTEGER,
  saves           INTEGER,
  follows         INTEGER,
  completion_rate REAL,                             -- 0..1
  avg_watch_sec   REAL,
  raw             TEXT,
  UNIQUE (post_id, captured_at)
);

CREATE TABLE IF NOT EXISTS topic (
  id          TEXT PRIMARY KEY,
  platform    TEXT NOT NULL,
  title       TEXT NOT NULL,
  angle       TEXT,
  hook        TEXT,
  score       REAL NOT NULL DEFAULT 0,
  score_parts TEXT NOT NULL DEFAULT '{}',           -- JSON {heat,fit,benchmark,timing}
  source      TEXT NOT NULL,                        -- hotlist | rss | benchmark | manual
  source_ref  TEXT,
  evidence    TEXT NOT NULL DEFAULT '[]',           -- JSON [{kind,ref,note}]
  status      TEXT NOT NULL DEFAULT 'new',          -- new | picked | used | dropped
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS topic_score ON topic (platform, score DESC);

CREATE TABLE IF NOT EXISTS copy_set (
  id             TEXT PRIMARY KEY,
  topic_id       TEXT REFERENCES topic(id) ON DELETE SET NULL,
  kind           TEXT NOT NULL,                     -- spoken | note | seed | explain | hook
  length         TEXT NOT NULL,                     -- short | long
  title          TEXT,
  hook           TEXT,
  body           TEXT NOT NULL,
  tags           TEXT NOT NULL DEFAULT '[]',        -- JSON string[]
  citations      TEXT NOT NULL DEFAULT '[]',        -- JSON [{doc_id,chunk_id,quote}]
  model          TEXT,
  prompt_version TEXT,
  compliance     TEXT NOT NULL DEFAULT '{}',        -- JSON {passed,hits:[{word,kind,index}]}
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS copy_set_topic ON copy_set (topic_id, kind, length);

CREATE TABLE IF NOT EXISTS asset (
  id           TEXT PRIMARY KEY,
  copy_set_id  TEXT REFERENCES copy_set(id) ON DELETE SET NULL,
  kind         TEXT NOT NULL,                       -- poster | carousel | cover | video
  template     TEXT,
  path         TEXT NOT NULL,                       -- 相对 WB_HOME
  width        INTEGER,
  height       INTEGER,
  duration_sec REAL,
  bytes        INTEGER,
  meta         TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pack (
  id          TEXT PRIMARY KEY,
  copy_set_id TEXT NOT NULL REFERENCES copy_set(id) ON DELETE CASCADE,
  platform    TEXT NOT NULL,
  dir         TEXT NOT NULL,                        -- 相对 WB_HOME
  manifest    TEXT NOT NULL DEFAULT '{}',
  status      TEXT NOT NULL DEFAULT 'draft',        -- draft | exported | published
  created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job (
  id         TEXT PRIMARY KEY,
  pipeline   TEXT NOT NULL,
  stage      TEXT NOT NULL,
  input_ref  TEXT,
  status     TEXT NOT NULL,                         -- pending | running | done | failed | skipped
  started_at TEXT,
  ended_at   TEXT,
  error      TEXT,
  output_ref TEXT
);
CREATE INDEX IF NOT EXISTS job_pipeline ON job (pipeline, started_at);

CREATE TABLE IF NOT EXISTS kb_doc (
  id          TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  kind        TEXT NOT NULL,                        -- note | product | case | faq | other
  source_path TEXT,
  sha256      TEXT,
  updated_at  TEXT NOT NULL,
  meta        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS kb_chunk (
  id      TEXT PRIMARY KEY,
  doc_id  TEXT NOT NULL REFERENCES kb_doc(id) ON DELETE CASCADE,
  ord     INTEGER NOT NULL,
  heading TEXT,
  body    TEXT NOT NULL
);

-- 检索表：seg 是 jieba 分词后用空格连接的中文词串
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5 (chunk_id UNINDEXED, seg);
""",
    ),
]

LATEST_VERSION = len(MIGRATIONS)


def connect(db_path: str | Path) -> sqlite3.Connection:
    """打开连接并设置 ADR §5 的 PRAGMA。"""
    target = str(db_path)
    if target != ":memory:":
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        target = str(path)
    conn = sqlite3.connect(target, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _split_sql(script: str) -> Iterator[str]:
    """把多语句脚本拆成单条 SQL，保留块内注释。"""
    buffer: list[str] = []
    for line in script.splitlines(keepends=True):
        buffer.append(line)
        candidate = "".join(buffer)
        if sqlite3.complete_statement(candidate):
            statement = candidate.strip()
            if statement:
                yield statement
            buffer = []
    tail = "".join(buffer).strip()
    if tail:
        yield tail


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name"
    ).fetchall()
    return [str(row[0]) for row in rows]


def migrate(conn: sqlite3.Connection) -> int:
    """按 ``user_version`` 推进迁移，返回最新版本。"""
    current = user_version(conn)
    if current > LATEST_VERSION:
        raise StoreError(
            f"数据库版本 {current} 高于代码支持的最新版本 {LATEST_VERSION}，"
            "请更新代码或换一个 WB_HOME"
        )
    if current == LATEST_VERSION:
        return current

    for index in range(current, LATEST_VERSION):
        name, sql = MIGRATIONS[index]
        target_version = index + 1
        try:
            conn.execute("BEGIN")
            for statement in _split_sql(sql):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {target_version}")
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            hint = ""
            if "fts5" in str(exc).lower():
                hint = "（当前 sqlite3 未编译 FTS5，请换用带 FTS5 的 Python/SQLite）"
            raise StoreError(f"迁移 {name} 失败：{exc}{hint}") from exc
    return user_version(conn)


def init_db(db_path: str | Path) -> sqlite3.Connection:
    """打开并迁移数据库；重复调用幂等。"""
    conn = connect(db_path)
    try:
        migrate(conn)
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务；异常回滚。"""
    try:
        conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


__all__ = [
    "LATEST_VERSION",
    "MIGRATIONS",
    "connect",
    "init_db",
    "migrate",
    "table_names",
    "transaction",
    "user_version",
]
