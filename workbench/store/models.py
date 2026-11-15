"""与 ADR §5 表一一对应的 pydantic 模型。

JSON 列在模型里是 ``list`` / ``dict``；序列化与反序列化由 ``store.repo`` 负责。
时间列统一保存 ISO 8601 字符串（带时区）。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Account(_Base):
    id: str
    platform: str
    handle: str
    display_name: str | None = None
    is_benchmark: bool = False
    niche: str | None = None
    note: str | None = None
    created_at: str


class Post(_Base):
    id: str
    account_id: str
    platform: str
    title: str
    url: str | None = None
    published_at: str
    content_type: Literal["image_text", "video", "article"]
    topic_tags: list[str] = Field(default_factory=list)
    duration_sec: float | None = None
    caption: str | None = None
    created_at: str


class Metric(_Base):
    id: str
    post_id: str
    captured_at: str
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    follows: int | None = None
    completion_rate: float | None = None
    avg_watch_sec: float | None = None
    raw: str | None = None


class Topic(_Base):
    id: str
    platform: str
    title: str
    angle: str | None = None
    hook: str | None = None
    score: float = 0.0
    score_parts: dict[str, float] = Field(default_factory=dict)
    source: Literal["hotlist", "rss", "benchmark", "manual"]
    source_ref: str | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal["new", "picked", "used", "dropped"] = "new"
    created_at: str


class CopySet(_Base):
    id: str
    topic_id: str | None = None
    kind: Literal["spoken", "note", "seed", "explain", "hook"]
    length: Literal["short", "long"]
    title: str | None = None
    hook: str | None = None
    body: str
    tags: list[str] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    model: str | None = None
    prompt_version: str | None = None
    compliance: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class Asset(_Base):
    id: str
    copy_set_id: str | None = None
    kind: Literal["poster", "carousel", "cover", "video"]
    template: str | None = None
    path: str
    width: int | None = None
    height: int | None = None
    duration_sec: float | None = None
    bytes: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class Pack(_Base):
    id: str
    copy_set_id: str
    platform: str
    dir: str
    manifest: dict[str, Any] = Field(default_factory=dict)
    status: Literal["draft", "exported", "published"] = "draft"
    created_at: str


class Job(_Base):
    id: str
    pipeline: str
    stage: str
    input_ref: str | None = None
    status: Literal["pending", "running", "done", "failed", "skipped"]
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None
    output_ref: dict[str, Any] | None = None


class KbDoc(_Base):
    id: str
    title: str
    kind: Literal["note", "product", "case", "faq", "other"]
    source_path: str | None = None
    sha256: str | None = None
    updated_at: str
    meta: dict[str, Any] = Field(default_factory=dict)


class KbChunk(_Base):
    id: str
    doc_id: str
    ord: int
    heading: str | None = None
    body: str


__all__ = [
    "Account",
    "Asset",
    "CopySet",
    "Job",
    "KbChunk",
    "KbDoc",
    "Metric",
    "Pack",
    "Post",
    "Topic",
]
