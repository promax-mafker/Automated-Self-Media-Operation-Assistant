"""控制台页面数据装配：只读 ``store.repo``，不重写业务逻辑。

每个函数返回模板可直接消费的 dict；``None`` 统一显示为「—」。
空库时各页面返回空列表，模板负责给出「先跑 ``wb run demo``」的引导。
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from workbench.config import Config
from workbench.knowledge.answer import ask as ask_kb
from workbench.knowledge.answer import hit_to_dict
from workbench.knowledge.build import build_kb
from workbench.metrics import MetricsError
from workbench.metrics.compute import (
    latest_metric,
    parse_iso,
    parse_week,
    quantile,
    week_key,
)
from workbench.store import repo
from workbench.store.models import Account

EMPTY_TOPIC_HINT = (
    "先跑 `wb run demo` 看完整链路，或 `wb topic suggest` 从本地 fixture 生成选题卡。"
)
EMPTY_COPY_HINT = "先跑 `wb run demo` 看完整链路，或 `wb copy gen --topic <topic_id>` 生成文案。"
EMPTY_ASSET_HINT = "先跑 `wb run demo` 看完整链路，或 `wb run visual` / `wb run video` 生成素材。"
EMPTY_PACK_HINT = (
    "先跑 `wb run demo` 看完整链路，或 `wb pack build --copy <copy_set_id>` 生成发布包。"
)
EMPTY_KB_HINT = "先跑 `wb run demo` 看完整链路，或 `wb kb add data/kb/seed` 导入本地资料。"


# --------------------------------------------------------------------------- 路径
def _safe_join(home: Path, raw: str) -> Path | None:
    """把 WB_HOME 下的相对路径拼成绝对路径；越界返回 ``None``。"""
    text = (raw or "").strip()
    if not text or text.startswith(("/", "\\")) or "\\" in text:
        return None
    parts = Path(text).parts
    if any(part in ("..", "") for part in parts):
        return None
    return (home / text).resolve()


def _within(home: Path, target: Path) -> bool:
    try:
        target.relative_to(home)
    except ValueError:
        return False
    return True


def home_relative(cfg: Config, stored_path: str) -> str | None:
    """把库里的 ``asset.path`` / ``pack.dir`` 归一成 WB_HOME 相对路径。"""
    home = cfg.home.resolve()
    raw = (stored_path or "").strip()
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute():
        target = candidate.resolve()
    else:
        joined = _safe_join(home, raw)
        if joined is None:
            return None
        target = joined
    if not _within(home, target):
        return None
    return target.relative_to(home).as_posix()


def file_url(cfg: Config, stored_path: str) -> str | None:
    """返回 ``/files/<相对路径>``；文件不存在或越界时返回 ``None``。"""
    relative = home_relative(cfg, stored_path)
    if relative is None:
        return None
    if not (cfg.home.resolve() / relative).is_file():
        return None
    return "/files/" + quote(relative)


def resolve_file(cfg: Config, raw: str) -> Path | None:
    """``/files/{path}`` 的路径解析：只接受 WB_HOME 下已存在的普通文件。"""
    home = cfg.home.resolve()
    target = _safe_join(home, raw)
    if target is None or not _within(home, target) or not target.is_file():
        return None
    return target


# --------------------------------------------------------------------------- 概览
def _own_accounts(conn: sqlite3.Connection) -> dict[str, Account]:
    return {account.id: account for account in repo.list_accounts(conn) if not account.is_benchmark}


def _week_bounds(cfg: Config) -> tuple[str, Any]:
    now = datetime.now(cfg.timezone())
    key = week_key(now, cfg.tz)
    return key, parse_week(key, cfg.tz)


def overview(cfg: Config, conn: sqlite3.Connection) -> dict[str, Any]:
    """概览：本周发布数、播放中位数、Top3 选题、待发布包数、最近一次 job。"""
    week, week_range = _week_bounds(cfg)
    own = _own_accounts(conn)
    publish_count = 0
    week_views: list[float] = []
    for post in repo.list_posts(conn):
        # 只统计自己的账号；对标账号的数据不进入「本周发布数」。
        if post.account_id not in own:
            continue
        try:
            published_at = parse_iso(post.published_at, cfg.tz)
        except MetricsError:
            continue
        if not (week_range.start <= published_at < week_range.end):
            continue
        publish_count += 1
        metric = latest_metric(conn, post.id, cfg.tz)
        if metric is not None and metric.views is not None:
            week_views.append(float(metric.views))

    topics = sorted(repo.list_topics(conn), key=lambda item: (-item.score, item.title))[:3]
    pending_packs = [pack for pack in repo.list_packs(conn) if pack.status != "published"]
    jobs = repo.list_jobs(conn)
    latest_job = jobs[-1] if jobs else None

    counts = {
        "topics": len(repo.list_topics(conn)),
        "copies": len(repo.list_copy_sets(conn)),
        "assets": len(repo.list_assets(conn)),
        "packs": len(repo.list_packs(conn)),
        "kb_docs": len(repo.list_kb_docs(conn)),
        "jobs": len(jobs),
        "posts": publish_count,
    }
    return {
        "week_key": week,
        "week_label": week_range.label(),
        "publish_count": publish_count,
        "median_views": quantile(week_views, 0.5),
        "pending_packs": len(pending_packs),
        "topics": topics,
        "latest_job": latest_job,
        "counts": counts,
        "has_data": any(counts.values()),
    }


# --------------------------------------------------------------------------- 选题池
def topics_page(conn: sqlite3.Connection) -> dict[str, Any]:
    topics = sorted(repo.list_topics(conn), key=lambda item: (-item.score, item.title))
    copies = Counter(
        copy_set.topic_id for copy_set in repo.list_copy_sets(conn) if copy_set.topic_id
    )
    rows = [
        {"topic": topic, "copies": copies.get(topic.id, 0), "evidence": len(topic.evidence)}
        for topic in topics
    ]
    return {"rows": rows, "total": len(rows), "hint": EMPTY_TOPIC_HINT}


# --------------------------------------------------------------------------- 文案
def copies_page(conn: sqlite3.Connection, copy_id: str | None = None) -> dict[str, Any]:
    copies = sorted(repo.list_copy_sets(conn), key=lambda item: item.created_at, reverse=True)
    topic_titles = {topic.id: topic.title for topic in repo.list_topics(conn)}
    selected = None
    missing = False
    if copy_id:
        selected = next((item for item in copies if item.id == copy_id), None)
        missing = selected is None
    return {
        "copies": copies,
        "selected": selected,
        "selected_id": copy_id,
        "missing_selected": missing,
        "topic_titles": topic_titles,
        "hint": EMPTY_COPY_HINT,
    }


# --------------------------------------------------------------------------- 素材
def assets_page(cfg: Config, conn: sqlite3.Connection) -> dict[str, Any]:
    assets = sorted(repo.list_assets(conn), key=lambda item: item.created_at, reverse=True)
    rows = []
    for asset in assets:
        url = file_url(cfg, asset.path)
        rows.append({"asset": asset, "url": url, "missing": url is None})
    return {"rows": rows, "total": len(rows), "hint": EMPTY_ASSET_HINT}


# --------------------------------------------------------------------------- 发布包
def packs_page(conn: sqlite3.Connection, pack_id: str | None = None) -> dict[str, Any]:
    packs = sorted(repo.list_packs(conn), key=lambda item: item.created_at, reverse=True)
    selected = None
    missing = False
    if pack_id:
        selected = next((item for item in packs if item.id == pack_id), None)
        missing = selected is None
    manifest_text = None
    if selected is not None:
        manifest_text = json.dumps(selected.manifest, ensure_ascii=False, indent=2)
    return {
        "packs": packs,
        "selected": selected,
        "selected_id": pack_id,
        "missing_selected": missing,
        "manifest_text": manifest_text,
        "hint": EMPTY_PACK_HINT,
    }


# --------------------------------------------------------------------------- 知识库
def kb_page(
    cfg: Config,
    conn: sqlite3.Connection,
    query: str | None = None,
    k: int = 5,
) -> dict[str, Any]:
    docs = sorted(repo.list_kb_docs(conn), key=lambda item: item.updated_at, reverse=True)
    chunk_count = len(repo.list_kb_chunks(conn))
    result = None
    if query is not None:
        kb = build_kb(cfg)
        try:
            result = ask_kb(kb, query, k=k)
        finally:
            kb.close()
    hits = [hit_to_dict(hit) for hit in result.hits] if result is not None else []
    return {
        "docs": docs,
        "chunk_count": chunk_count,
        "query": result.query if result is not None else "",
        "hits": hits,
        "hint": result.hint if result is not None else None,
        "empty_hint": EMPTY_KB_HINT,
        "asked": result is not None,
    }


__all__ = [
    "EMPTY_ASSET_HINT",
    "EMPTY_COPY_HINT",
    "EMPTY_KB_HINT",
    "EMPTY_PACK_HINT",
    "EMPTY_TOPIC_HINT",
    "assets_page",
    "copies_page",
    "file_url",
    "home_relative",
    "kb_page",
    "overview",
    "packs_page",
    "resolve_file",
    "topics_page",
]
