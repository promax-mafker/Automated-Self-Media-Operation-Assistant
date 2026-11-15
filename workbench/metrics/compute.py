"""ISO 周计算、指标聚合、分组与异常检测（T02）。

口径以 ``docs/metrics.md`` 为准；本模块只做确定性计算，不读网络、不调模型。
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from workbench.metrics import MetricsError
from workbench.store import repo
from workbench.store.models import Account, Metric, Post

_WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")

#: 发布时段：早 06:00–11:59 / 午 12:00–17:59 / 晚 18:00–23:59 / 深夜 00:00–05:59。
TIME_SLOTS: tuple[str, ...] = ("早", "午", "晚", "深夜")
#: 时长分组：≤15s / 15–30s / 30–60s / >60s；没有时长（图文/文章）单列。
DURATION_BUCKETS: tuple[str, ...] = ("≤15s", "15–30s", "30–60s", ">60s", "无时长")
CONTENT_TYPE_LABELS: dict[str, str] = {
    "image_text": "图文",
    "video": "视频",
    "article": "文章",
}
CONTENT_TYPE_ORDER: tuple[str, ...] = ("image_text", "video", "article")

ANOMALY_THRESHOLD = 2.0
MIN_GROUP_SAMPLE = 3


def resolve_zone(tz_name: str) -> tzinfo:
    """解析时区；非法时退回 UTC（与 ``Config.timezone`` 一致）。"""
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


@dataclass(frozen=True, slots=True)
class WeekRange:
    """一个 ISO 周：周一 00:00（含）到下一个周一 00:00（不含）。"""

    year: int
    week: int
    start: datetime
    end: datetime

    def key(self) -> str:
        return f"{self.year}-W{self.week:02d}"

    def label(self) -> str:
        last_day = (self.end - timedelta(days=1)).date().isoformat()
        return f"{self.key()}（{self.start.date().isoformat()} ~ {last_day}）"


def parse_week(value: str, tz_name: str = "Asia/Shanghai") -> WeekRange:
    """解析 ``YYYY-Www`` 为带时区的周范围；格式或周序号非法时抛 :class:`MetricsError`。"""
    text = (value or "").strip()
    match = _WEEK_RE.fullmatch(text)
    if match is None:
        raise MetricsError(f"周格式应为 YYYY-Www，例如 2026-W45，收到 {value!r}")
    year, week = int(match.group(1)), int(match.group(2))
    try:
        monday = date.fromisocalendar(year, week, 1)
    except ValueError as exc:
        raise MetricsError(f"不是合法的 ISO 周：{value!r}（{exc}）") from exc
    start = datetime.combine(monday, time.min, tzinfo=resolve_zone(tz_name))
    return WeekRange(year=year, week=week, start=start, end=start + timedelta(days=7))


def week_key(value: datetime, tz_name: str = "Asia/Shanghai") -> str:
    """把任意时间换算到 ``WB_TZ`` 后返回所属 ISO 周。"""
    local = (
        value.replace(tzinfo=resolve_zone(tz_name))
        if value.tzinfo is None
        else value.astimezone(resolve_zone(tz_name))
    )
    iso = local.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def parse_iso(value: str, tz_name: str = "Asia/Shanghai") -> datetime:
    """解析数据库里的 ISO 8601 时间；无时区按 ``WB_TZ`` 处理。"""
    text = (value or "").strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MetricsError(f"无法解析时间：{value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=resolve_zone(tz_name))
    return parsed.astimezone(resolve_zone(tz_name))


def quantile(values: Sequence[float], p: float) -> float | None:
    """线性插值分位数（与常见 ``numpy.quantile`` 线性口径一致）。"""
    if not values:
        return None
    if not 0 <= p <= 1:
        raise ValueError(f"分位点应在 0–1：{p}")
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def mean(values: Sequence[float | None]) -> float | None:
    """忽略 ``None`` 的算术平均；全为 ``None`` 时返回 ``None``。"""
    present = [float(value) for value in values if value is not None]
    return sum(present) / len(present) if present else None


def z_scores(values: Sequence[float]) -> list[float]:
    """总体 z-score（标准差除以 n）；常数列返回全 0。"""
    if not values:
        return []
    average = sum(values) / len(values)
    variance = sum((value - average) ** 2 for value in values) / len(values)
    if variance == 0:
        return [0.0 for _ in values]
    deviation = math.sqrt(variance)
    return [(value - average) / deviation for value in values]


def publish_gap_hours(published: Sequence[datetime]) -> float | None:
    """相邻发布时间的间隔中位数（小时）；少于 2 篇返回 ``None``。"""
    if len(published) < 2:
        return None
    ordered = sorted(published)
    gaps = [
        (later - earlier).total_seconds() / 3600
        for earlier, later in zip(ordered, ordered[1:], strict=False)
    ]
    return quantile(gaps, 0.5)


def time_slot(value: datetime) -> str:
    hour = value.hour
    if 6 <= hour < 12:
        return "早"
    if 12 <= hour < 18:
        return "午"
    if 18 <= hour < 24:
        return "晚"
    return "深夜"


def duration_bucket(seconds: float | None) -> str:
    if seconds is None:
        return "无时长"
    if seconds <= 15:
        return "≤15s"
    if seconds <= 30:
        return "15–30s"
    if seconds <= 60:
        return "30–60s"
    return ">60s"


@dataclass(slots=True)
class PostRecord:
    """一篇作品 + 最新一次指标快照 + 派生指标。"""

    post: Post
    metric: Metric
    published: datetime
    views: int
    interaction_rate: float | None
    save_rate: float | None
    follow_rate: float | None
    completion_rate: float | None
    burst_ratio: float | None
    time_slot: str
    duration_bucket: str
    content_type_label: str


@dataclass(slots=True)
class Summary:
    publish_count: int
    total_views: int
    median_views: float | None
    p90_views: float | None
    mean_interaction_rate: float | None
    mean_save_rate: float | None
    mean_follow_rate: float | None
    mean_completion_rate: float | None
    mean_burst_ratio: float | None
    publish_gap_hours: float | None
    anomaly_count: int
    history_count: int
    history_p90_views: float | None


@dataclass(slots=True)
class GroupSummary:
    key: str
    dimension: str
    n: int
    median_views: float | None
    mean_interaction_rate: float | None
    mean_save_rate: float | None
    mean_follow_rate: float | None
    mean_completion_rate: float | None
    mean_burst_ratio: float | None

    @property
    def insufficient(self) -> bool:
        """样本量 < 3 时不得参与结论推荐。"""
        return self.n < MIN_GROUP_SAMPLE


@dataclass(slots=True)
class WeeklyData:
    account: Account
    week: WeekRange
    platform: str
    tz: str
    rows: list[PostRecord]
    summary: Summary
    groups: dict[str, list[GroupSummary]]
    top: list[PostRecord]
    bottom: list[PostRecord]
    zscores: dict[str, float]
    anomalies: list[tuple[PostRecord, float]]
    missing_metric_posts: int


def find_account(conn: sqlite3.Connection, handle: str, platform: str) -> Account | None:
    """按 ``(platform, handle)`` 查账号；handle 大小写不敏感。"""
    wanted = handle.strip().casefold()
    for account in repo.list_accounts(conn, platform=platform):
        if account.handle.casefold() == wanted:
            return account
    return None


def latest_metric(conn: sqlite3.Connection, post_id: str, tz_name: str) -> Metric | None:
    """取一篇作品 ``captured_at`` 最新的一次快照。"""
    metrics = repo.list_metrics(conn, post_id=post_id)
    if not metrics:
        return None
    return max(metrics, key=lambda metric: parse_iso(metric.captured_at, tz_name))


def _metric_rates(metric: Metric) -> tuple[float | None, float | None, float | None]:
    views = metric.views or 0
    if views <= 0:
        return None, None, None
    interaction = (
        (metric.likes or 0)
        + (metric.comments or 0)
        + (metric.saves or 0)
        + (metric.shares or 0)
    ) / views
    save = (metric.saves or 0) / views
    follow = (metric.follows or 0) / views
    return interaction, save, follow


def _completion_rate(post: Post, metric: Metric) -> float | None:
    if metric.completion_rate is not None:
        return min(max(metric.completion_rate, 0.0), 1.0)
    if metric.avg_watch_sec is not None and post.duration_sec and post.duration_sec > 0:
        return min(max(metric.avg_watch_sec / post.duration_sec, 0.0), 1.0)
    return None


def _build_record(
    post: Post,
    metric: Metric,
    published: datetime,
    history_p90: float | None,
) -> PostRecord:
    views = int(metric.views or 0)
    interaction, save, follow = _metric_rates(metric)
    burst = views / history_p90 if history_p90 and history_p90 > 0 else None
    return PostRecord(
        post=post,
        metric=metric,
        published=published,
        views=views,
        interaction_rate=interaction,
        save_rate=save,
        follow_rate=follow,
        completion_rate=_completion_rate(post, metric),
        burst_ratio=burst,
        time_slot=time_slot(published),
        duration_bucket=duration_bucket(post.duration_sec),
        content_type_label=CONTENT_TYPE_LABELS.get(post.content_type, post.content_type),
    )


def _group_key(record: PostRecord, dimension: str) -> str:
    if dimension == "time_slot":
        return record.time_slot
    if dimension == "duration":
        return record.duration_bucket
    return record.content_type_label


def _summarize_group(
    dimension: str, key: str, records: Sequence[PostRecord]
) -> GroupSummary:
    views = [float(record.views) for record in records]
    return GroupSummary(
        key=key,
        dimension=dimension,
        n=len(records),
        median_views=quantile(views, 0.5),
        mean_interaction_rate=mean([record.interaction_rate for record in records]),
        mean_save_rate=mean([record.save_rate for record in records]),
        mean_follow_rate=mean([record.follow_rate for record in records]),
        mean_completion_rate=mean([record.completion_rate for record in records]),
        mean_burst_ratio=mean([record.burst_ratio for record in records]),
    )


def _group_summaries(
    records: Sequence[PostRecord], dimension: str, order: Sequence[str]
) -> list[GroupSummary]:
    return [
        _summarize_group(
            dimension,
            key,
            [record for record in records if _group_key(record, dimension) == key],
        )
        for key in order
    ]


def build_weekly_data(
    conn: sqlite3.Connection,
    account_handle: str,
    week_value: str,
    *,
    platform: str,
    tz: str = "Asia/Shanghai",
) -> WeeklyData:
    """加载账号与指定 ISO 周的数据，计算全部指标、分组、Top/Bottom 与异常。"""
    account = find_account(conn, account_handle, platform)
    if account is None:
        raise MetricsError(
            f"账号不存在：{account_handle!r}（平台 {platform}）；请先运行 wb review import"
        )
    week = parse_week(week_value, tz)
    all_posts = repo.list_posts(conn, account_id=account.id)

    metric_cache: dict[str, Metric | None] = {}

    def metric_for(post_id: str) -> Metric | None:
        if post_id not in metric_cache:
            metric_cache[post_id] = latest_metric(conn, post_id, tz)
        return metric_cache[post_id]

    history_views: list[float] = []
    for post in all_posts:
        metric = metric_for(post.id)
        if metric is not None and metric.views is not None:
            history_views.append(float(metric.views))
    history_p90 = quantile(history_views, 0.9)

    week_posts: list[tuple[Post, datetime, Metric]] = []
    missing_metric_posts = 0
    for post in all_posts:
        published = parse_iso(post.published_at, tz)
        if not (week.start <= published < week.end):
            continue
        metric = metric_for(post.id)
        if metric is None:
            missing_metric_posts += 1
            continue
        week_posts.append((post, published, metric))

    if not week_posts:
        raise MetricsError(
            f"账号 {account.handle} 在 {week.key()} 没有可用的 metric 数据；"
            "请先导入 CSV，或换一个周"
        )

    records = [
        _build_record(post, metric, published, history_p90)
        for post, published, metric in week_posts
    ]
    records.sort(key=lambda record: record.published)

    views = [float(record.views) for record in records]
    scores = z_scores(views)
    zscores = {
        record.post.id: score for record, score in zip(records, scores, strict=True)
    }
    anomalies = [
        (record, zscores[record.post.id])
        for record in records
        if abs(zscores[record.post.id]) >= ANOMALY_THRESHOLD
    ]

    summary = Summary(
        publish_count=len(records),
        total_views=sum(record.views for record in records),
        median_views=quantile(views, 0.5),
        p90_views=quantile(views, 0.9),
        mean_interaction_rate=mean([record.interaction_rate for record in records]),
        mean_save_rate=mean([record.save_rate for record in records]),
        mean_follow_rate=mean([record.follow_rate for record in records]),
        mean_completion_rate=mean([record.completion_rate for record in records]),
        mean_burst_ratio=mean([record.burst_ratio for record in records]),
        publish_gap_hours=publish_gap_hours([record.published for record in records]),
        anomaly_count=len(anomalies),
        history_count=len(history_views),
        history_p90_views=history_p90,
    )

    groups = {
        "content_type": _group_summaries(
            records,
            "content_type",
            [CONTENT_TYPE_LABELS[item] for item in CONTENT_TYPE_ORDER],
        ),
        "time_slot": _group_summaries(records, "time_slot", TIME_SLOTS),
        "duration": _group_summaries(records, "duration", DURATION_BUCKETS),
    }
    by_views = sorted(records, key=lambda record: (-record.views, record.post.id))
    top = by_views[:5]
    bottom = sorted(records, key=lambda record: (record.views, record.post.id))[:5]

    return WeeklyData(
        account=account,
        week=week,
        platform=platform,
        tz=tz,
        rows=records,
        summary=summary,
        groups=groups,
        top=top,
        bottom=bottom,
        zscores=zscores,
        anomalies=anomalies,
        missing_metric_posts=missing_metric_posts,
    )


__all__ = [
    "ANOMALY_THRESHOLD",
    "CONTENT_TYPE_LABELS",
    "CONTENT_TYPE_ORDER",
    "DURATION_BUCKETS",
    "GroupSummary",
    "MIN_GROUP_SAMPLE",
    "PostRecord",
    "Summary",
    "TIME_SLOTS",
    "WeekRange",
    "WeeklyData",
    "build_weekly_data",
    "duration_bucket",
    "find_account",
    "latest_metric",
    "mean",
    "parse_iso",
    "parse_week",
    "publish_gap_hours",
    "quantile",
    "time_slot",
    "week_key",
    "z_scores",
]
