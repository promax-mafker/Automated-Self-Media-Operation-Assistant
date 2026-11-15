"""每张表的最小 CRUD；JSON 列在出口处还原为 ``list`` / ``dict``。

- ``insert_*`` / ``get_*`` / ``list_*`` 覆盖 ADR §5 的 10 张表。
- ``job`` 额外提供 ``update_job`` 供 runner 记录生命周期。
- 所有写入即时提交；runner 的每个 stage 各占一行 job，不做跨 stage 事务合并。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from workbench.errors import StoreError
from workbench.store.models import (
    Account,
    Asset,
    CopySet,
    Job,
    KbChunk,
    KbDoc,
    Metric,
    Pack,
    Post,
    Topic,
)


@dataclass(frozen=True, slots=True)
class _Spec:
    table: str
    model: type[BaseModel]
    json_columns: frozenset[str] = frozenset()
    bool_columns: frozenset[str] = frozenset()


_SPECS: dict[str, _Spec] = {
    "account": _Spec("account", Account, bool_columns=frozenset({"is_benchmark"})),
    "post": _Spec("post", Post, json_columns=frozenset({"topic_tags"})),
    "metric": _Spec("metric", Metric),
    "topic": _Spec("topic", Topic, json_columns=frozenset({"score_parts", "evidence"})),
    "copy_set": _Spec(
        "copy_set",
        CopySet,
        json_columns=frozenset({"tags", "citations", "compliance"}),
    ),
    "asset": _Spec("asset", Asset, json_columns=frozenset({"meta"})),
    "pack": _Spec("pack", Pack, json_columns=frozenset({"manifest"})),
    "job": _Spec("job", Job, json_columns=frozenset({"output_ref"})),
    "kb_doc": _Spec("kb_doc", KbDoc, json_columns=frozenset({"meta"})),
    "kb_chunk": _Spec("kb_chunk", KbChunk),
}


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    return json.loads(value)


def _prepare(spec: _Spec, data: BaseModel | Mapping[str, Any]) -> tuple[BaseModel, dict[str, Any]]:
    obj = data if isinstance(data, spec.model) else spec.model.model_validate(data)
    values = obj.model_dump()
    for column in spec.json_columns:
        values[column] = _dump_json(values[column])
    for column in spec.bool_columns:
        value = values[column]
        values[column] = None if value is None else int(bool(value))
    return obj, values


def _decode(spec: _Spec, row: sqlite3.Row) -> BaseModel:
    values = dict(row)
    try:
        for column in spec.json_columns:
            if column in values:
                values[column] = _load_json(values[column])
        for column in spec.bool_columns:
            if column in values and values[column] is not None:
                values[column] = bool(values[column])
        return spec.model.model_validate(values)
    except Exception as exc:
        raise StoreError(f"读取 {spec.table} 行失败：{exc}") from exc


def _insert(conn: sqlite3.Connection, spec: _Spec, data: BaseModel | Mapping[str, Any]) -> Any:
    obj, values = _prepare(spec, data)
    columns = list(values)
    placeholders = ", ".join("?" for _ in columns)
    sql = (
        f"INSERT INTO {spec.table} ({', '.join(columns)}) "  # noqa: S608 - 表名来自内部白名单
        f"VALUES ({placeholders})"
    )
    try:
        conn.execute(sql, [values[column] for column in columns])
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise StoreError(f"写入 {spec.table} 失败：{exc}") from exc
    return obj


def _get(conn: sqlite3.Connection, spec: _Spec, row_id: str) -> Any:
    row = conn.execute(f"SELECT * FROM {spec.table} WHERE id = ?", (row_id,)).fetchone()
    if row is None:
        return None
    return _decode(spec, row)


def _list(
    conn: sqlite3.Connection,
    spec: _Spec,
    filters: Mapping[str, Any] | None = None,
    *,
    order: str = "rowid ASC",
    limit: int | None = None,
) -> list[Any]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (filters or {}).items():
        if value is None:
            continue
        clauses.append(f"{column} = ?")
        params.append(value)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM {spec.table}{where} ORDER BY {order}"
    if limit is not None:
        if limit < 0:
            raise ValueError("limit 不能为负数")
        sql += " LIMIT ?"
        params.append(limit)
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.Error as exc:
        raise StoreError(f"查询 {spec.table} 失败：{exc}") from exc
    return [_decode(spec, row) for row in rows]


# --------------------------------------------------------------------------- account
def insert_account(
    conn: sqlite3.Connection, account: Account | Mapping[str, Any]
) -> Account:
    return _insert(conn, _SPECS["account"], account)


def get_account(conn: sqlite3.Connection, account_id: str) -> Account | None:
    return _get(conn, _SPECS["account"], account_id)


def list_accounts(
    conn: sqlite3.Connection,
    *,
    platform: str | None = None,
    is_benchmark: bool | None = None,
    limit: int | None = None,
) -> list[Account]:
    filters = {"platform": platform}
    if is_benchmark is not None:
        filters["is_benchmark"] = int(bool(is_benchmark))
    return _list(conn, _SPECS["account"], filters, limit=limit)


# --------------------------------------------------------------------------- post
def insert_post(conn: sqlite3.Connection, post: Post | Mapping[str, Any]) -> Post:
    return _insert(conn, _SPECS["post"], post)


def get_post(conn: sqlite3.Connection, post_id: str) -> Post | None:
    return _get(conn, _SPECS["post"], post_id)


def list_posts(
    conn: sqlite3.Connection,
    *,
    account_id: str | None = None,
    platform: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[Post]:
    filters: dict[str, Any] = {"account_id": account_id, "platform": platform}
    rows = _list(conn, _SPECS["post"], filters, order="rowid ASC")
    if since is not None:
        rows = [row for row in rows if row.published_at >= since]
    return rows[:limit] if limit is not None else rows


# --------------------------------------------------------------------------- metric
def insert_metric(conn: sqlite3.Connection, metric: Metric | Mapping[str, Any]) -> Metric:
    return _insert(conn, _SPECS["metric"], metric)


def get_metric(conn: sqlite3.Connection, metric_id: str) -> Metric | None:
    return _get(conn, _SPECS["metric"], metric_id)


def list_metrics(
    conn: sqlite3.Connection,
    *,
    post_id: str | None = None,
    since: str | None = None,
    limit: int | None = None,
) -> list[Metric]:
    filters: dict[str, Any] = {"post_id": post_id}
    rows = _list(conn, _SPECS["metric"], filters)
    if since is not None:
        rows = [row for row in rows if row.captured_at >= since]
    return rows[:limit] if limit is not None else rows


# --------------------------------------------------------------------------- topic
def insert_topic(conn: sqlite3.Connection, topic: Topic | Mapping[str, Any]) -> Topic:
    return _insert(conn, _SPECS["topic"], topic)


def get_topic(conn: sqlite3.Connection, topic_id: str) -> Topic | None:
    return _get(conn, _SPECS["topic"], topic_id)


def list_topics(
    conn: sqlite3.Connection,
    *,
    platform: str | None = None,
    status: str | None = None,
    limit: int | None = None,
) -> list[Topic]:
    return _list(
        conn,
        _SPECS["topic"],
        {"platform": platform, "status": status},
        limit=limit,
    )


# --------------------------------------------------------------------------- copy_set
def insert_copy_set(conn: sqlite3.Connection, copy_set: CopySet | Mapping[str, Any]) -> CopySet:
    return _insert(conn, _SPECS["copy_set"], copy_set)


def get_copy_set(conn: sqlite3.Connection, copy_set_id: str) -> CopySet | None:
    return _get(conn, _SPECS["copy_set"], copy_set_id)


def list_copy_sets(
    conn: sqlite3.Connection,
    *,
    topic_id: str | None = None,
    kind: str | None = None,
    length: str | None = None,
    limit: int | None = None,
) -> list[CopySet]:
    return _list(
        conn,
        _SPECS["copy_set"],
        {"topic_id": topic_id, "kind": kind, "length": length},
        limit=limit,
    )


# --------------------------------------------------------------------------- asset
def insert_asset(conn: sqlite3.Connection, asset: Asset | Mapping[str, Any]) -> Asset:
    return _insert(conn, _SPECS["asset"], asset)


def get_asset(conn: sqlite3.Connection, asset_id: str) -> Asset | None:
    return _get(conn, _SPECS["asset"], asset_id)


def list_assets(
    conn: sqlite3.Connection,
    *,
    copy_set_id: str | None = None,
    kind: str | None = None,
    limit: int | None = None,
) -> list[Asset]:
    return _list(
        conn,
        _SPECS["asset"],
        {"copy_set_id": copy_set_id, "kind": kind},
        limit=limit,
    )


# --------------------------------------------------------------------------- pack
def insert_pack(conn: sqlite3.Connection, pack: Pack | Mapping[str, Any]) -> Pack:
    return _insert(conn, _SPECS["pack"], pack)


def get_pack(conn: sqlite3.Connection, pack_id: str) -> Pack | None:
    return _get(conn, _SPECS["pack"], pack_id)


def list_packs(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int | None = None,
) -> list[Pack]:
    return _list(conn, _SPECS["pack"], {"status": status}, limit=limit)


# --------------------------------------------------------------------------- job
def insert_job(conn: sqlite3.Connection, job: Job | Mapping[str, Any]) -> Job:
    return _insert(conn, _SPECS["job"], job)


def get_job(conn: sqlite3.Connection, job_id: str) -> Job | None:
    return _get(conn, _SPECS["job"], job_id)


def list_jobs(
    conn: sqlite3.Connection,
    *,
    pipeline: str | None = None,
    status: str | None = None,
    limit: int | None = None,
) -> list[Job]:
    return _list(
        conn,
        _SPECS["job"],
        {"pipeline": pipeline, "status": status},
        limit=limit,
    )


_UNSET = object()


def update_job(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    status: str | None = None,
    started_at: str | None | object = _UNSET,
    ended_at: str | None | object = _UNSET,
    error: str | None | object = _UNSET,
    output_ref: dict[str, Any] | None | object = _UNSET,
    input_ref: str | None | object = _UNSET,
) -> Job:
    values: dict[str, Any] = {}
    if status is not None:
        values["status"] = status
    if started_at is not _UNSET:
        values["started_at"] = started_at
    if ended_at is not _UNSET:
        values["ended_at"] = ended_at
    if error is not _UNSET:
        values["error"] = error
    if output_ref is not _UNSET:
        values["output_ref"] = _dump_json(output_ref) if output_ref is not None else None
    if input_ref is not _UNSET:
        values["input_ref"] = input_ref

    if values:
        assignments = ", ".join(f"{column} = ?" for column in values)
        try:
            cursor = conn.execute(
                f"UPDATE job SET {assignments} WHERE id = ?",  # noqa: S608 - 列名来自内部字典
                [*values.values(), job_id],
            )
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            raise StoreError(f"更新 job 失败：{exc}") from exc
        if cursor.rowcount == 0:
            raise StoreError(f"job 不存在：{job_id}")

    job = get_job(conn, job_id)
    if job is None:
        raise StoreError(f"job 不存在：{job_id}")
    return job


# --------------------------------------------------------------------------- kb_doc
def insert_kb_doc(conn: sqlite3.Connection, doc: KbDoc | Mapping[str, Any]) -> KbDoc:
    return _insert(conn, _SPECS["kb_doc"], doc)


def get_kb_doc(conn: sqlite3.Connection, doc_id: str) -> KbDoc | None:
    return _get(conn, _SPECS["kb_doc"], doc_id)


def list_kb_docs(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    limit: int | None = None,
) -> list[KbDoc]:
    return _list(conn, _SPECS["kb_doc"], {"kind": kind}, limit=limit)


# --------------------------------------------------------------------------- kb_chunk
def insert_kb_chunk(conn: sqlite3.Connection, chunk: KbChunk | Mapping[str, Any]) -> KbChunk:
    return _insert(conn, _SPECS["kb_chunk"], chunk)


def get_kb_chunk(conn: sqlite3.Connection, chunk_id: str) -> KbChunk | None:
    return _get(conn, _SPECS["kb_chunk"], chunk_id)


def list_kb_chunks(
    conn: sqlite3.Connection,
    *,
    doc_id: str | None = None,
    limit: int | None = None,
) -> list[KbChunk]:
    return _list(conn, _SPECS["kb_chunk"], {"doc_id": doc_id}, limit=limit)


__all__ = [
    "get_account",
    "get_asset",
    "get_copy_set",
    "get_job",
    "get_kb_chunk",
    "get_kb_doc",
    "get_metric",
    "get_pack",
    "get_post",
    "get_topic",
    "insert_account",
    "insert_asset",
    "insert_copy_set",
    "insert_job",
    "insert_kb_chunk",
    "insert_kb_doc",
    "insert_metric",
    "insert_pack",
    "insert_post",
    "insert_topic",
    "list_accounts",
    "list_assets",
    "list_copy_sets",
    "list_jobs",
    "list_kb_chunks",
    "list_kb_docs",
    "list_metrics",
    "list_packs",
    "list_posts",
    "list_topics",
    "update_job",
]
