"""迁移幂等、表结构与 repo CRUD/JSON 往返。"""

from __future__ import annotations

import sqlite3

import pytest

from workbench.config import now_iso
from workbench.errors import StoreError
from workbench.store import repo
from workbench.store.db import LATEST_VERSION, init_db, table_names, user_version
from workbench.store.models import Account, Metric, Post, Topic

REQUIRED_TABLES = {
    "account",
    "post",
    "metric",
    "topic",
    "copy_set",
    "asset",
    "pack",
    "job",
    "kb_doc",
    "kb_chunk",
    "kb_fts",
}


def test_migration_is_idempotent(tmp_path) -> None:
    path = tmp_path / "wb.sqlite3"
    first = init_db(path)
    first_version = user_version(first)
    first_tables = set(table_names(first))
    first.close()

    second = init_db(path)
    second_version = user_version(second)
    second_tables = set(table_names(second))
    second.close()

    assert first_version == LATEST_VERSION
    assert second_version == first_version
    assert second_tables == first_tables
    assert REQUIRED_TABLES <= first_tables


def test_fts5_table_accepts_chinese_tokens(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO kb_fts (chunk_id, seg) VALUES (?, ?)",
        ("chunk_demo", "测试 分词 内容"),
    )
    conn.commit()
    row = conn.execute("SELECT chunk_id FROM kb_fts WHERE kb_fts MATCH ?", ("分词",)).fetchone()
    assert row is not None
    assert row["chunk_id"] == "chunk_demo"


def test_repo_account_post_metric_crud_and_json(conn: sqlite3.Connection) -> None:
    account = repo.insert_account(
        conn,
        Account(
            id="acc_demo",
            platform="xhs",
            handle="demo_account",
            display_name="演示账号（虚构）",
            is_benchmark=True,
            niche="演示",
            created_at=now_iso(),
        ),
    )
    fetched_account = repo.get_account(conn, account.id)
    assert fetched_account is not None
    assert fetched_account.is_benchmark is True
    assert fetched_account.display_name == "演示账号（虚构）"
    assert repo.list_accounts(conn, platform="xhs")[0].id == account.id

    post = repo.insert_post(
        conn,
        Post(
            id="post_demo",
            account_id=account.id,
            platform="xhs",
            title="演示笔记（虚构）",
            url="https://example.invalid/post/1",
            published_at=now_iso(),
            content_type="image_text",
            topic_tags=["选题", "演示"],
            duration_sec=None,
            caption="虚构内容，仅用于测试",
            created_at=now_iso(),
        ),
    )
    fetched_post = repo.get_post(conn, post.id)
    assert fetched_post is not None
    assert fetched_post.topic_tags == ["选题", "演示"]
    assert repo.list_posts(conn, account_id=account.id)[0].id == post.id

    raw = conn.execute("SELECT topic_tags FROM post WHERE id = ?", (post.id,)).fetchone()
    assert raw["topic_tags"] == '["选题","演示"]'

    metric = repo.insert_metric(
        conn,
        Metric(
            id="metric_demo",
            post_id=post.id,
            captured_at=now_iso(),
            views=100,
            likes=10,
            comments=2,
            shares=1,
            saves=3,
            follows=4,
            completion_rate=0.5,
            avg_watch_sec=12.5,
            raw='{"source":"fixture"}',
        ),
    )
    fetched_metric = repo.get_metric(conn, metric.id)
    assert fetched_metric is not None
    assert fetched_metric.views == 100
    assert fetched_metric.completion_rate == 0.5
    assert repo.list_metrics(conn, post_id=post.id)[0].id == metric.id


def test_repo_dict_json_roundtrip(conn: sqlite3.Connection) -> None:
    topic = repo.insert_topic(
        conn,
        Topic(
            id="topic_demo",
            platform="xhs",
            title="演示选题（虚构）",
            score=0.5,
            score_parts={"heat": 0.4, "fit": 0.6},
            source="manual",
            evidence=[{"kind": "manual", "ref": "fixture", "note": "演示"}],
            created_at=now_iso(),
        ),
    )
    fetched = repo.get_topic(conn, topic.id)
    assert fetched is not None
    assert fetched.score_parts == {"heat": 0.4, "fit": 0.6}
    assert fetched.evidence == [{"kind": "manual", "ref": "fixture", "note": "演示"}]


def test_repo_boundaries(conn: sqlite3.Connection) -> None:
    assert repo.get_account(conn, "missing") is None

    account = repo.insert_account(
        conn,
        Account(id="acc_dup", platform="xhs", handle="dup", created_at=now_iso()),
    )
    with pytest.raises(StoreError):
        repo.insert_account(
            conn,
            Account(id="acc_dup_2", platform="xhs", handle="dup", created_at=now_iso()),
        )

    with pytest.raises(StoreError):
        repo.insert_post(
            conn,
            Post(
                id="post_orphan",
                account_id="acc_not_exists",
                platform="xhs",
                title="孤儿笔记",
                published_at=now_iso(),
                content_type="image_text",
                created_at=now_iso(),
            ),
        )
    assert account.id == "acc_dup"
