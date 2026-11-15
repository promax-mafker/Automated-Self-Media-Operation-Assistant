"""知识库工厂：``build_kb(cfg) -> KB``（``contracts.get_kb`` 的懒加载目标）。

初始化失败必须抛 ``Unavailable``，让 ``get_kb`` 降级为 ``NullKB`` 而不是崩掉上层。
"""

from __future__ import annotations

import sqlite3

from workbench.config import Config
from workbench.errors import Unavailable
from workbench.knowledge.index import KnowledgeBase
from workbench.store.db import init_db


def build_kb(cfg: Config | None = None) -> KnowledgeBase:
    """打开数据库并校验 FTS5 表；失败统一转成 ``Unavailable``。"""
    try:
        resolved = cfg if cfg is not None else Config.from_env()
        resolved.ensure_dirs()
        conn = init_db(resolved.db_path)
    except Exception as exc:  # noqa: BLE001 - 工厂失败必须降级，不向上层抛原始异常
        raise Unavailable("knowledge base", "T03") from exc

    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'kb_fts'"
        ).fetchone()
        if row is None:
            raise Unavailable("knowledge base", "T03")
    except Unavailable:
        conn.close()
        raise
    except sqlite3.Error as exc:
        conn.close()
        raise Unavailable("knowledge base", "T03") from exc
    return KnowledgeBase(cfg=resolved, conn=conn)


__all__ = ["build_kb"]
