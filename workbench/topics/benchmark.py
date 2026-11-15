"""对标账号爆款画像（T05）。

口径（与 ``docs/topic.md`` 一致）：

- ``burst = views / P50(同账号 views)``，views 取每篇作品最新一次 ``metric``。
- 「爆款集」= ``burst >= 1.5`` 的作品按 burst 降序取 Top 10%（至少 1 条）。
- 画像统计时长中位区间、标题长度区间、jieba 高频词 Top 10、发布时段分布、
  内容类型占比，每项都带样本数。
- 「近 90 天」以对标库最新 ``captured_at`` 为基准（数据快照时间），
  避免静态 fixture 随真实时间推移而失效；也可用 ``as_of`` 显式覆盖。
- 全部计算确定性：jieba 关闭 HMM、排序带稳定 tie-breaker、不调 LLM。

对标 fixture 位于 ``data/seed/benchmark/*.jsonl``，每行形如
``{"account": {...}, "post": {...}, "metric": {...}}``，账号名一律 ``demo_bench_*``。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import jieba

from workbench.config import now_iso
from workbench.metrics.compute import (
    CONTENT_TYPE_ORDER,
    TIME_SLOTS,
    latest_metric,
    parse_iso,
    quantile,
    time_slot,
)
from workbench.store import repo
from workbench.store.models import Account, Metric, Post
from workbench.topics import TopicError

BURST_THRESHOLD = 1.5
TOP_PCT = 0.1
WINDOW_DAYS = 90
KEYWORD_TOP_N = 10
EVIDENCE_POSTS = 5

#: jieba 分词后丢弃的常见虚词；只影响关键词与相似度，不影响原文。
STOPWORDS: frozenset[str] = frozenset(
    {
        "一个",
        "一些",
        "这个",
        "那个",
        "这些",
        "那些",
        "什么",
        "怎么",
        "为什么",
        "可以",
        "不是",
        "没有",
        "就是",
        "还是",
        "已经",
        "因为",
        "所以",
        "但是",
        "如果",
        "然后",
        "而且",
        "以及",
        "还有",
        "我们",
        "你们",
        "他们",
        "自己",
        "时候",
        "现在",
        "今天",
        "本周",
        "真的",
        "非常",
        "比较",
        "一点",
        "一下",
        "一种",
        "这样",
        "那样",
        "其实",
        "需要",
        "应该",
        "可能",
        "很多",
        "只是",
        "只有",
        "不要",
        "不会",
        "不能",
        "让我",
        "给你",
    }
)

_PUNCT_RE = re.compile(r"^[\W_]+$", re.UNICODE)
_DIGITS_RE = re.compile(r"^\d+$")

CONTENT_TYPE_LABELS: dict[str, str] = {
    "image_text": "图文",
    "video": "视频",
    "article": "文章",
}


# --------------------------------------------------------------------------- 数据类
def tokenize(text: str) -> list[str]:
    """jieba 分词并过滤标点、纯数字、单字与常见虚词（确定性）。"""
    tokens: list[str] = []
    for raw in jieba.lcut(text or "", HMM=False):
        token = raw.strip().lower()
        if len(token) < 2:
            continue
        if _PUNCT_RE.fullmatch(token) or _DIGITS_RE.fullmatch(token):
            continue
        if token in STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def extract_keywords(texts: Iterable[str], *, top_n: int = KEYWORD_TOP_N) -> list[tuple[str, int]]:
    """跨文本统计 jieba 高频词；按 (次数降序, 词升序) 取 Top N。"""
    counter: Counter[str] = Counter()
    for text in texts:
        counter.update(tokenize(text))
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:top_n]


@dataclass(frozen=True, slots=True)
class RangeStat:
    """一组数值的中位/四分位区间；``n`` 是有效样本数。"""

    n: int
    median: float | None = None
    p25: float | None = None
    p75: float | None = None
    minimum: float | None = None
    maximum: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "median": self.median,
            "p25": self.p25,
            "p75": self.p75,
            "min": self.minimum,
            "max": self.maximum,
        }


@dataclass(frozen=True, slots=True)
class Bucket:
    """分布中的一档；``share`` 为占全部样本的比例。"""

    key: str
    n: int
    share: float

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "n": self.n, "share": self.share}


@dataclass(frozen=True, slots=True)
class HitPost:
    """进入画像/证据的爆款作品；``is_hit`` 区分补足证据的非爆款样本。"""

    post_id: str
    account: str
    title: str
    url: str | None
    published_at: str
    content_type: str
    duration_sec: float | None
    tags: list[str]
    views: int
    burst: float
    title_length: int
    time_slot: str
    is_hit: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "post_id": self.post_id,
            "account": self.account,
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at,
            "content_type": self.content_type,
            "duration_sec": self.duration_sec,
            "tags": list(self.tags),
            "views": self.views,
            "burst": self.burst,
            "title_length": self.title_length,
            "time_slot": self.time_slot,
            "is_hit": self.is_hit,
        }


@dataclass(slots=True)
class BenchmarkProfile:
    """一个账号（或全部对标账号聚合）的爆款画像。"""

    account: str
    platform: str
    as_of: str
    window_days: int
    post_count: int
    hit_count: int
    burst_threshold: float
    p50_views: float | None
    duration: RangeStat
    title_length: RangeStat
    keywords: list[tuple[str, int]]
    time_slots: list[Bucket]
    content_types: list[Bucket]
    top_posts: list[HitPost]

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "platform": self.platform,
            "as_of": self.as_of,
            "window_days": self.window_days,
            "post_count": self.post_count,
            "hit_count": self.hit_count,
            "burst_threshold": self.burst_threshold,
            "p50_views": self.p50_views,
            "duration": self.duration.to_dict(),
            "title_length": self.title_length.to_dict(),
            "keywords": [{"token": token, "count": count} for token, count in self.keywords],
            "time_slots": [bucket.to_dict() for bucket in self.time_slots],
            "content_types": [bucket.to_dict() for bucket in self.content_types],
            "top_posts": [post.to_dict() for post in self.top_posts],
        }

    def top_content_type(self) -> str | None:
        """样本量最大的内容类型；并列时按 ADR 的固定顺序。"""
        best: Bucket | None = None
        for bucket in self.content_types:
            if bucket.n == 0:
                continue
            if best is None or bucket.n > best.n:
                best = bucket
        return best.key if best is not None else None


@dataclass(slots=True)
class _PostSample:
    post: Post
    metric: Metric
    published: datetime
    views: int
    burst: float = 0.0


@dataclass(frozen=True, slots=True)
class FixtureRecord:
    """一行对标 fixture；seed 与画像共用同一份解析结果。"""

    account: dict[str, Any]
    post: dict[str, Any]
    metric: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SeedResult:
    accounts: int
    posts: int
    metrics: int
    skipped: int

    def summary(self) -> str:
        return (
            f"accounts={self.accounts} posts={self.posts} metrics={self.metrics} "
            f"skipped={self.skipped}"
        )


# --------------------------------------------------------------------------- 纯函数
def burst_ratio(views: int | float, p50_views: float | None) -> float:
    """``views / P50``；P50 缺失或非正时返回 0。"""
    if not p50_views or p50_views <= 0:
        return 0.0
    return float(views) / float(p50_views)


def select_hits(
    samples: Sequence[_PostSample],
    *,
    threshold: float = BURST_THRESHOLD,
    top_pct: float = TOP_PCT,
) -> list[_PostSample]:
    """先按 ``burst >= threshold`` 过滤，再取 Top ``top_pct``（至少 1 条）。"""
    qualified = [sample for sample in samples if sample.burst >= threshold]
    if not qualified:
        return []
    limit = max(1, math.ceil(len(samples) * top_pct))
    qualified.sort(key=lambda sample: (-sample.burst, sample.post.id))
    return qualified[:limit]


def _range_stat(values: Sequence[float]) -> RangeStat:
    if not values:
        return RangeStat(n=0)
    return RangeStat(
        n=len(values),
        median=quantile(values, 0.5),
        p25=quantile(values, 0.25),
        p75=quantile(values, 0.75),
        minimum=min(values),
        maximum=max(values),
    )


def _buckets(keys: Sequence[str], values: Sequence[str]) -> list[Bucket]:
    total = len(values)
    counter = Counter(values)
    return [
        Bucket(
            key=key,
            n=counter.get(key, 0),
            share=round(counter.get(key, 0) / total, 6) if total else 0.0,
        )
        for key in keys
    ]


def _hit_from_sample(sample: _PostSample, *, is_hit: bool) -> HitPost:
    return HitPost(
        post_id=sample.post.id,
        account=sample.post.account_id,
        title=sample.post.title,
        url=sample.post.url,
        published_at=sample.post.published_at,
        content_type=sample.post.content_type,
        duration_sec=sample.post.duration_sec,
        tags=list(sample.post.topic_tags),
        views=sample.views,
        burst=round(sample.burst, 6),
        title_length=len(sample.post.title.strip()),
        time_slot=time_slot(sample.published),
        is_hit=is_hit,
    )


def _summarize(
    *,
    account: str,
    platform: str,
    as_of: str,
    window_days: int,
    post_count: int,
    hits: Sequence[_PostSample],
    p50_views: float | None,
    threshold: float,
    evidence_posts: Sequence[HitPost],
) -> BenchmarkProfile:
    hit_posts = [_hit_from_sample(sample, is_hit=True) for sample in hits]
    durations = [post.duration_sec for post in hit_posts if post.duration_sec is not None]
    title_lengths = [float(post.title_length) for post in hit_posts]
    keyword_texts = [f"{post.title} {' '.join(post.tags)}" for post in hit_posts]
    return BenchmarkProfile(
        account=account,
        platform=platform,
        as_of=as_of,
        window_days=window_days,
        post_count=post_count,
        hit_count=len(hit_posts),
        burst_threshold=threshold,
        p50_views=p50_views,
        duration=_range_stat([float(value) for value in durations]),
        title_length=_range_stat(title_lengths),
        keywords=extract_keywords(keyword_texts),
        time_slots=_buckets(TIME_SLOTS, [post.time_slot for post in hit_posts]),
        content_types=_buckets(
            CONTENT_TYPE_ORDER, [post.content_type for post in hit_posts]
        ),
        top_posts=list(evidence_posts),
    )


# --------------------------------------------------------------------------- fixture
def load_fixture_records(fixture_dir: str | Path) -> list[FixtureRecord]:
    """读取 ``*.jsonl`` 对标 fixture；格式错误时指出文件与行号。"""
    directory = Path(fixture_dir).expanduser()
    if not directory.is_dir():
        raise TopicError(f"对标 fixture 目录不存在：{directory}")
    files = sorted(directory.glob("*.jsonl"))
    if not files:
        raise TopicError(f"对标 fixture 目录为空：{directory}（需要 *.jsonl）")

    records: list[FixtureRecord] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise TopicError(f"对标 fixture 读取失败：{path}（{exc}）") from exc
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TopicError(f"{path.name} 第 {line_number} 行不是合法 JSON：{exc}") from exc
            if not isinstance(payload, dict):
                raise TopicError(f"{path.name} 第 {line_number} 行必须是 JSON 对象")
            account = payload.get("account")
            post = payload.get("post")
            metric = payload.get("metric") or {}
            if not isinstance(account, dict) or not isinstance(post, dict):
                raise TopicError(
                    f"{path.name} 第 {line_number} 行缺少 account / post 对象"
                )
            if not isinstance(metric, dict):
                raise TopicError(f"{path.name} 第 {line_number} 行 metric 必须是对象")
            records.append(FixtureRecord(account=account, post=post, metric=metric))
    if not records:
        raise TopicError(f"对标 fixture 没有有效记录：{directory}")
    return records


def _fixture_account_id(platform: str, handle: str) -> str:
    digest = hashlib.sha1(f"{platform}|{handle}".encode()).hexdigest()[:12]
    return f"acct_{digest}"


def _fixture_metric_id(post_id: str, captured_at: str) -> str:
    digest = hashlib.sha1(f"{post_id}|{captured_at}".encode()).hexdigest()[:12]
    return f"metric_{digest}"


def find_account(
    conn: sqlite3.Connection, handle: str, platform: str
) -> Account | None:
    """按 ``(platform, handle)`` 找账号；handle 大小写不敏感。"""
    wanted = handle.strip().casefold()
    for account in repo.list_accounts(conn, platform=platform):
        if account.handle.casefold() == wanted:
            return account
    return None


def list_benchmark_accounts(conn: sqlite3.Connection, platform: str) -> list[Account]:
    accounts = repo.list_accounts(conn, platform=platform, is_benchmark=True)
    return sorted(accounts, key=lambda account: account.handle)


def seed_benchmark(conn: sqlite3.Connection, fixture_dir: str | Path) -> SeedResult:
    """把对标 fixture 幂等写入 ``account / post / metric``。

    重复执行只补缺失行：账号按 ``(platform, handle)``、作品按 ``post.id``、
    指标按 ``(post_id, captured_at)`` 去重，不会产生重复爆款样本。
    """
    records = load_fixture_records(fixture_dir)
    accounts = posts = metrics = skipped = 0
    for record in records:
        account_data = record.account
        post_data = record.post
        metric_data = record.metric

        platform = str(account_data.get("platform") or post_data.get("platform") or "").strip()
        handle = str(account_data.get("handle") or "").strip()
        if not platform or not handle:
            raise TopicError("对标 fixture 每条记录都需要 account.platform 与 account.handle")
        account = find_account(conn, handle, platform)
        if account is None:
            captured = str(metric_data.get("captured_at") or "").strip() or now_iso()
            account = repo.insert_account(
                conn,
                Account(
                    id=str(account_data.get("id") or _fixture_account_id(platform, handle)),
                    platform=platform,
                    handle=handle,
                    display_name=account_data.get("display_name"),
                    is_benchmark=bool(account_data.get("is_benchmark", True)),
                    niche=account_data.get("niche"),
                    note=account_data.get("note"),
                    created_at=captured,
                ),
            )
            accounts += 1

        post_id = str(post_data.get("id") or "").strip()
        title = str(post_data.get("title") or "").strip()
        if not post_id or not title:
            raise TopicError("对标 fixture 每条记录都需要 post.id 与 post.title")
        if repo.get_post(conn, post_id) is None:
            content_type = str(post_data.get("content_type") or "").strip()
            if content_type not in {"image_text", "video", "article"}:
                raise TopicError(
                    f"对标 fixture {post_id} 的 content_type 非法：{content_type!r}"
                )
            tags = post_data.get("topic_tags") or []
            if isinstance(tags, str):
                tags = [part.strip() for part in re.split(r"[|,，、]+", tags) if part.strip()]
            if not isinstance(tags, list):
                raise TopicError(f"对标 fixture {post_id} 的 topic_tags 必须是数组")
            duration_raw = post_data.get("duration_sec")
            repo.insert_post(
                conn,
                Post(
                    id=post_id,
                    account_id=account.id,
                    platform=platform,
                    title=title,
                    url=post_data.get("url"),
                    published_at=str(post_data.get("published_at") or "").strip()
                    or now_iso(),
                    content_type=content_type,
                    topic_tags=[str(tag).strip() for tag in tags if str(tag).strip()],
                    duration_sec=float(duration_raw) if duration_raw is not None else None,
                    caption=post_data.get("caption"),
                    created_at=account.created_at,
                ),
            )
            posts += 1
        else:
            skipped += 1

        captured_at = str(metric_data.get("captured_at") or "").strip()
        if not captured_at:
            captured_at = account.created_at
        existing = [
            metric
            for metric in repo.list_metrics(conn, post_id=post_id)
            if metric.captured_at == captured_at
        ]
        if existing:
            skipped += 1
            continue
        metric_id = str(metric_data.get("id") or "").strip() or _fixture_metric_id(
            post_id, captured_at
        )
        repo.insert_metric(
            conn,
            Metric(
                id=metric_id,
                post_id=post_id,
                captured_at=captured_at,
                views=_optional_int(metric_data.get("views")),
                likes=_optional_int(metric_data.get("likes")),
                comments=_optional_int(metric_data.get("comments")),
                shares=_optional_int(metric_data.get("shares")),
                saves=_optional_int(metric_data.get("saves")),
                follows=_optional_int(metric_data.get("follows")),
                completion_rate=_optional_float(metric_data.get("completion_rate")),
                avg_watch_sec=_optional_float(metric_data.get("avg_watch_sec")),
                raw=None,
            ),
        )
        metrics += 1

    return SeedResult(accounts=accounts, posts=posts, metrics=metrics, skipped=skipped)


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value)))
    except (TypeError, ValueError) as exc:
        raise TopicError(f"指标不是数字：{value!r}") from exc


def _optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value))
    except (TypeError, ValueError) as exc:
        raise TopicError(f"指标不是数字：{value!r}") from exc


# --------------------------------------------------------------------------- 画像
def reference_time(conn: sqlite3.Connection, platform: str) -> str:
    """对标库在指定平台上的最新采集时间；缺失时退回当前时间。"""
    row = conn.execute(
        """
        SELECT MAX(m.captured_at)
        FROM metric m
        JOIN post p ON p.id = m.post_id
        JOIN account a ON a.id = p.account_id
        WHERE a.is_benchmark = 1 AND p.platform = ?
        """,
        (platform,),
    ).fetchone()
    value = str(row[0]) if row and row[0] else ""
    return value or now_iso()


def _load_samples(
    conn: sqlite3.Connection,
    account: Account,
    *,
    as_of: str,
    days: int,
    tz: str,
) -> list[_PostSample]:
    reference = parse_iso(as_of, tz)
    window_start = reference - timedelta(days=days)
    samples: list[_PostSample] = []
    for post in repo.list_posts(conn, account_id=account.id):
        published = parse_iso(post.published_at, tz)
        if published < window_start:
            continue
        metric = latest_metric(conn, post.id, tz)
        if metric is None or metric.views is None:
            continue
        samples.append(
            _PostSample(post=post, metric=metric, published=published, views=int(metric.views))
        )
    return samples


def build_profile(
    conn: sqlite3.Connection,
    account_handle: str,
    *,
    platform: str,
    as_of: str | None = None,
    days: int = WINDOW_DAYS,
    threshold: float = BURST_THRESHOLD,
    top_pct: float = TOP_PCT,
    tz: str = "Asia/Shanghai",
) -> BenchmarkProfile:
    """计算单个对标账号的爆款画像；账号不存在时抛 :class:`TopicError`。"""
    account = find_account(conn, account_handle, platform)
    if account is None:
        raise TopicError(
            f"对标账号不存在：{account_handle!r}（平台 {platform}）；"
            "请确认 --account，或先用 wb topic seed 导入 data/seed/benchmark"
        )
    reference = as_of or reference_time(conn, platform)
    samples = _load_samples(conn, account, as_of=reference, days=days, tz=tz)
    p50 = quantile([float(sample.views) for sample in samples], 0.5)
    for sample in samples:
        sample.burst = burst_ratio(sample.views, p50)
    hits = select_hits(samples, threshold=threshold, top_pct=top_pct)

    evidence = [_hit_from_sample(sample, is_hit=True) for sample in hits]
    hit_ids = {post.post_id for post in evidence}
    for sample in sorted(samples, key=lambda item: (-item.views, item.post.id)):
        if len(evidence) >= EVIDENCE_POSTS:
            break
        if sample.post.id in hit_ids:
            continue
        evidence.append(_hit_from_sample(sample, is_hit=False))
        hit_ids.add(sample.post.id)

    return _summarize(
        account=account.handle,
        platform=platform,
        as_of=reference,
        window_days=days,
        post_count=len(samples),
        hits=hits,
        p50_views=p50,
        threshold=threshold,
        evidence_posts=evidence,
    )


def _empty_profile(*, platform: str, as_of: str, days: int) -> BenchmarkProfile:
    return BenchmarkProfile(
        account="all",
        platform=platform,
        as_of=as_of,
        window_days=days,
        post_count=0,
        hit_count=0,
        burst_threshold=BURST_THRESHOLD,
        p50_views=None,
        duration=RangeStat(n=0),
        title_length=RangeStat(n=0),
        keywords=[],
        time_slots=_buckets(TIME_SLOTS, []),
        content_types=_buckets(CONTENT_TYPE_ORDER, []),
        top_posts=[],
    )


def aggregate_profiles(
    profiles: Sequence[BenchmarkProfile],
    *,
    platform: str,
    as_of: str | None = None,
    days: int = WINDOW_DAYS,
) -> BenchmarkProfile:
    """把多个账号画像聚合成一个平台画像（burst 已按账号归一化，可直接合并）。"""
    if not profiles:
        return _empty_profile(
            platform=platform, as_of=as_of or now_iso(), days=days
        )
    reference = as_of or max(profile.as_of for profile in profiles)
    hit_posts: list[HitPost] = []
    evidence: list[HitPost] = []
    seen: set[str] = set()
    for profile in profiles:
        for post in profile.top_posts:
            if post.post_id in seen:
                continue
            seen.add(post.post_id)
            evidence.append(post)
            if post.is_hit:
                hit_posts.append(post)
    evidence.sort(key=lambda post: (-int(post.is_hit), -post.burst, post.post_id))
    hit_posts.sort(key=lambda post: (-post.burst, post.post_id))
    p50_values = [
        float(profile.p50_views) for profile in profiles if profile.p50_views is not None
    ]
    durations = [post.duration_sec for post in hit_posts if post.duration_sec is not None]
    title_lengths = [float(post.title_length) for post in hit_posts]
    keyword_texts = [f"{post.title} {' '.join(post.tags)}" for post in hit_posts]
    return BenchmarkProfile(
        account="all",
        platform=platform,
        as_of=reference,
        window_days=days,
        post_count=sum(profile.post_count for profile in profiles),
        hit_count=len(hit_posts),
        burst_threshold=profiles[0].burst_threshold,
        p50_views=quantile(p50_values, 0.5),
        duration=_range_stat([float(value) for value in durations]),
        title_length=_range_stat(title_lengths),
        keywords=extract_keywords(keyword_texts),
        time_slots=_buckets(TIME_SLOTS, [post.time_slot for post in hit_posts]),
        content_types=_buckets(
            CONTENT_TYPE_ORDER, [post.content_type for post in hit_posts]
        ),
        top_posts=evidence[:20],
    )


def build_platform_profile(
    conn: sqlite3.Connection,
    *,
    platform: str,
    as_of: str | None = None,
    days: int = WINDOW_DAYS,
    threshold: float = BURST_THRESHOLD,
    top_pct: float = TOP_PCT,
    tz: str = "Asia/Shanghai",
) -> BenchmarkProfile:
    """平台内全部对标账号的聚合画像；没有对标账号时返回空画像。"""
    accounts = list_benchmark_accounts(conn, platform)
    if not accounts:
        return _empty_profile(
            platform=platform,
            as_of=as_of or reference_time(conn, platform),
            days=days,
        )
    profiles = [
        build_profile(
            conn,
            account.handle,
            platform=platform,
            as_of=as_of,
            days=days,
            threshold=threshold,
            top_pct=top_pct,
            tz=tz,
        )
        for account in accounts
    ]
    return aggregate_profiles(profiles, platform=platform, as_of=as_of, days=days)


__all__ = [
    "BURST_THRESHOLD",
    "BenchmarkProfile",
    "Bucket",
    "CONTENT_TYPE_LABELS",
    "EVIDENCE_POSTS",
    "FixtureRecord",
    "HitPost",
    "KEYWORD_TOP_N",
    "RangeStat",
    "STOPWORDS",
    "SeedResult",
    "TOP_PCT",
    "WINDOW_DAYS",
    "aggregate_profiles",
    "build_platform_profile",
    "build_profile",
    "burst_ratio",
    "extract_keywords",
    "find_account",
    "list_benchmark_accounts",
    "load_fixture_records",
    "reference_time",
    "seed_benchmark",
    "select_hits",
    "tokenize",
]
