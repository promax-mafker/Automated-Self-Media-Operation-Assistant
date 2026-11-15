"""平台导出 CSV 按 URL 回灌 ``post`` / ``metric``（T08）。

与 T02 的 ``wb review import`` 的分工：

- T02 按 CSV 的 ``post_id`` 匹配，适合完整作品库导入；
- T08 按作品 ``url`` 匹配，适合「发布包 → 人工发布 → 平台导出」的增量回流。

命中 URL 时只补一条 ``metric``；未命中时新建 ``account`` + ``post`` + ``metric``。
幂等键仍是 ``(post_id, captured_at)``，重复行计入 ``skipped``。
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from workbench.config import now_iso
from workbench.ids import new_id
from workbench.metrics.import_csv import ALIASES as _T02_ALIASES
from workbench.metrics.import_csv import CONTENT_TYPES, PLATFORMS
from workbench.publish import PublishError
from workbench.store import repo
from workbench.store.models import Account, Metric, Post

_FIELDS: tuple[str, ...] = (
    "url",
    "post_id",
    "account",
    "display_name",
    "platform",
    "title",
    "published_at",
    "content_type",
    "topic_tags",
    "duration_sec",
    "caption",
    "captured_at",
    "views",
    "likes",
    "comments",
    "shares",
    "saves",
    "follows",
    "completion_rate",
    "avg_watch_sec",
)

ALIASES: dict[str, tuple[str, ...]] = {field: _T02_ALIASES[field] for field in _FIELDS}


def _normalize(value: str) -> str:
    return re.sub(r"[\s_\-./（）()]+", "", value.strip().lower())


_LOOKUP: dict[str, str] = {}
for _canonical, _names in ALIASES.items():
    for _name in _names:
        _key = _normalize(_name)
        _existing = _LOOKUP.get(_key)
        if _existing is not None and _existing != _canonical:
            raise ValueError(f"列别名冲突：{_name!r} 同时映射到 {_existing!r} 与 {_canonical!r}")
        _LOOKUP[_key] = _canonical


@dataclass(frozen=True, slots=True)
class BackfillResult:
    """一次回灌的统计；``summary()`` 是验收约定的输出格式。"""

    matched: int
    created: int
    metrics: int
    skipped: int = 0

    def summary(self) -> str:
        return f"matched={self.matched} created={self.created} metrics={self.metrics}"


@dataclass(slots=True)
class _ParsedRow:
    line: int
    url: str
    post_id: str | None
    account: str
    display_name: str | None
    platform: str
    platform_explicit: bool
    title: str | None
    published_at: str | None
    content_type: str | None
    topic_tags: list[str]
    duration_sec: float | None
    caption: str | None
    captured_at: str | None
    views: int | None
    likes: int | None
    comments: int | None
    shares: int | None
    saves: int | None
    follows: int | None
    completion_rate: float | None
    avg_watch_sec: float | None
    raw: str


def _resolve_zone(tz_name: str) -> tzinfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _read_table(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise PublishError(f"CSV 文件不存在：{path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = [row for row in reader if any((value or "").strip() for value in row.values())]
    except UnicodeDecodeError as exc:
        raise PublishError(f"CSV 不是 UTF-8 编码：{path}（{exc}）") from exc
    except csv.Error as exc:
        raise PublishError(f"CSV 解析失败：{path}（{exc}）") from exc
    if not fieldnames:
        raise PublishError(f"CSV 没有表头：{path}")
    if not rows:
        raise PublishError(f"CSV 没有数据行：{path}")
    return fieldnames, rows


def _resolve_columns(fieldnames: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for header in fieldnames:
        if not header or not header.strip():
            continue
        canonical = _LOOKUP.get(_normalize(header))
        if canonical is None:
            continue
        if canonical in mapping:
            raise PublishError(
                f"CSV 列映射冲突：{header!r} 与 {mapping[canonical]!r} 都映射到 {canonical}"
            )
        mapping[canonical] = header
    if "url" not in mapping:
        raise PublishError(
            "CSV 缺少 url 列；T08 按作品 URL 匹配已有 post。支持别名："
            + " / ".join(ALIASES["url"])
        )
    return mapping


def _cell(row: dict[str, str], mapping: dict[str, str], canonical: str) -> str:
    header = mapping.get(canonical)
    if header is None:
        return ""
    return (row.get(header) or "").strip()


def _parse_int(value: str, canonical: str, line: int) -> int | None:
    if not value:
        return None
    text = value.replace(",", "").replace(" ", "").replace("_", "")
    multiplier = 1
    if text.endswith(("万", "w", "W")):
        multiplier = 10_000
        text = text[:-1]
    elif text.endswith(("k", "K")):
        multiplier = 1_000
        text = text[:-1]
    try:
        number = int(round(float(text) * multiplier))
    except ValueError as exc:
        raise PublishError(f"第 {line} 行 {canonical} 不是数字：{value!r}") from exc
    if number < 0:
        raise PublishError(f"第 {line} 行 {canonical} 不能为负数：{value!r}")
    return number


def _parse_float(value: str, canonical: str, line: int) -> float | None:
    if not value:
        return None
    text = value.replace(",", "").replace(" ", "").replace("_", "")
    try:
        number = float(text)
    except ValueError as exc:
        raise PublishError(f"第 {line} 行 {canonical} 不是数字：{value!r}") from exc
    if number < 0:
        raise PublishError(f"第 {line} 行 {canonical} 不能为负数：{value!r}")
    return number


def _parse_rate(value: str, canonical: str, line: int) -> float | None:
    if not value:
        return None
    text = value.strip()
    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()
    try:
        number = float(text)
    except ValueError as exc:
        raise PublishError(f"第 {line} 行 {canonical} 不是数字：{value!r}") from exc
    if percent:
        number /= 100
    elif number > 1:
        number /= 100
    if not 0 <= number <= 1:
        raise PublishError(f"第 {line} 行 {canonical} 应在 0–1 或 0–100% 之间：{value!r}")
    return number


def _parse_datetime(value: str, canonical: str, line: int, zone: tzinfo) -> str:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = text.replace("/", "-")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PublishError(
            f"第 {line} 行 {canonical} 不是合法时间：{value!r}；"
            "支持 ISO 8601（如 2026-11-02T08:30:00+08:00）或 2026-11-02 08:30:00"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    else:
        parsed = parsed.astimezone(zone)
    return parsed.isoformat(timespec="seconds")


def _parse_duration(value: str, canonical: str, line: int) -> float | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith(("s", "S", "秒")):
        text = text[:-1].strip()
    try:
        if ":" in text:
            parts = text.split(":")
            if len(parts) == 2:
                number = int(parts[0]) * 60 + float(parts[1])
            elif len(parts) == 3:
                number = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
            else:
                raise ValueError(text)
        else:
            number = float(text)
    except ValueError as exc:
        raise PublishError(
            f"第 {line} 行 {canonical} 不是合法时长：{value!r}；"
            "支持秒数（15）、mm:ss 或 hh:mm:ss"
        ) from exc
    if number < 0:
        raise PublishError(f"第 {line} 行 {canonical} 不能为负数：{value!r}")
    return number


def _parse_tags(value: str, line: int) -> list[str]:
    text = value.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PublishError(
                f"第 {line} 行 topic_tags 不是合法 JSON 数组：{value!r}"
            ) from exc
        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            raise PublishError(f"第 {line} 行 topic_tags 必须是字符串数组：{value!r}")
        return [item.strip() for item in data if item.strip()]
    return [part.strip() for part in re.split(r"[|,，、]+", text) if part.strip()]


def _parse_content_type(value: str, line: int) -> str:
    key = _normalize(value)
    if key not in CONTENT_TYPES:
        raise PublishError(
            f"第 {line} 行 content_type 无法识别：{value!r}；"
            "支持 image_text（图文）/ video（视频）/ article（文章）"
        )
    return CONTENT_TYPES[key]


def _parse_platform(value: str, default: str, line: int) -> str:
    platform = (value or default).strip().lower()
    if platform not in PLATFORMS:
        raise PublishError(
            f"第 {line} 行 platform 无法识别：{value!r}；支持 {' / '.join(PLATFORMS)}"
        )
    return platform


def _parse_url(value: str, line: int) -> str:
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PublishError(
            f"第 {line} 行 url 不是 http(s) 链接：{value!r}；"
            "例如 https://example.com/p/1"
        )
    return text


def _parse_row(
    row: dict[str, str],
    mapping: dict[str, str],
    line: int,
    *,
    default_platform: str,
    zone: tzinfo,
) -> _ParsedRow:
    url = _parse_url(_cell(row, mapping, "url"), line)
    platform_raw = _cell(row, mapping, "platform")
    published_raw = _cell(row, mapping, "published_at")
    content_type_raw = _cell(row, mapping, "content_type")
    captured_raw = _cell(row, mapping, "captured_at")
    raw = json.dumps(
        {key: value for key, value in row.items() if (value or "").strip()},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _ParsedRow(
        line=line,
        url=url,
        post_id=_cell(row, mapping, "post_id") or None,
        account=_cell(row, mapping, "account"),
        display_name=_cell(row, mapping, "display_name") or None,
        platform=_parse_platform(platform_raw, default_platform, line),
        platform_explicit=bool(platform_raw),
        title=_cell(row, mapping, "title") or None,
        published_at=(
            _parse_datetime(published_raw, "published_at", line, zone)
            if published_raw
            else None
        ),
        content_type=(
            _parse_content_type(content_type_raw, line) if content_type_raw else None
        ),
        topic_tags=_parse_tags(_cell(row, mapping, "topic_tags"), line),
        duration_sec=_parse_duration(
            _cell(row, mapping, "duration_sec"), "duration_sec", line
        ),
        caption=_cell(row, mapping, "caption") or None,
        captured_at=(
            _parse_datetime(captured_raw, "captured_at", line, zone)
            if captured_raw
            else None
        ),
        views=_parse_int(_cell(row, mapping, "views"), "views", line),
        likes=_parse_int(_cell(row, mapping, "likes"), "likes", line),
        comments=_parse_int(_cell(row, mapping, "comments"), "comments", line),
        shares=_parse_int(_cell(row, mapping, "shares"), "shares", line),
        saves=_parse_int(_cell(row, mapping, "saves"), "saves", line),
        follows=_parse_int(_cell(row, mapping, "follows"), "follows", line),
        completion_rate=_parse_rate(
            _cell(row, mapping, "completion_rate"), "completion_rate", line
        ),
        avg_watch_sec=_parse_float(
            _cell(row, mapping, "avg_watch_sec"), "avg_watch_sec", line
        ),
        raw=raw,
    )


def _today_iso(zone: tzinfo) -> str:
    return datetime.now(zone).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat(timespec="seconds")


def _validate_new_rows(
    conn: sqlite3.Connection,
    rows: list[_ParsedRow],
    known_urls: set[str],
    *,
    account: str | None,
) -> None:
    seen_ids: dict[str, str] = {}
    for row in rows:
        if row.url in known_urls:
            continue
        known_urls.add(row.url)
        if not row.title:
            raise PublishError(f"第 {row.line} 行新建 post 需要 title")
        if not row.published_at:
            raise PublishError(f"第 {row.line} 行新建 post 需要 published_at")
        if not row.content_type:
            raise PublishError(f"第 {row.line} 行新建 post 需要 content_type")
        if not (account or row.account):
            raise PublishError(
                f"第 {row.line} 行缺少账号；CSV 没有 account 列时请用 --account 指定自己的账号"
            )
        if not row.post_id:
            continue
        existing = repo.get_post(conn, row.post_id)
        if existing is not None:
            raise PublishError(
                f"第 {row.line} 行 post_id {row.post_id!r} 已存在，但 CSV 的 URL 没有命中它："
                f"{existing.url}；请检查导出文件或换一个 post_id"
            )
        if row.post_id in seen_ids:
            raise PublishError(
                f"第 {row.line} 行 post_id {row.post_id!r} 与第 {seen_ids[row.post_id]} 行重复，"
                "但 URL 不同"
            )
        seen_ids[row.post_id] = str(row.line)


def backfill(
    conn: sqlite3.Connection,
    csv_path: Path | str,
    *,
    account: str | None = None,
    platform: str | None = None,
    tz: str = "Asia/Shanghai",
) -> BackfillResult:
    """按 URL 回灌平台导出 CSV；返回匹配/新建/写入指标数。"""
    path = Path(csv_path).expanduser()
    fieldnames, raw_rows = _read_table(path)
    mapping = _resolve_columns(fieldnames)
    zone = _resolve_zone(tz)
    default_platform = (platform or "xhs").strip().lower()
    if default_platform not in PLATFORMS:
        raise PublishError(
            f"平台无法识别：{default_platform!r}；支持 {' / '.join(PLATFORMS)}"
        )

    parsed = [
        _parse_row(
            row,
            mapping,
            line,
            default_platform=default_platform,
            zone=zone,
        )
        for line, row in enumerate(raw_rows, start=2)
    ]

    accounts = {
        (item.platform, item.handle.casefold()): item
        for item in repo.list_accounts(conn)
    }
    url_index = {
        post.url: post for post in repo.list_posts(conn) if post.url
    }
    _validate_new_rows(conn, parsed, set(url_index), account=account)

    created_at = now_iso(tz)
    metric_keys: dict[str, set[str]] = {}
    matched = 0
    created = 0
    metrics = 0
    skipped = 0

    for row in parsed:
        post = url_index.get(row.url)
        if post is not None:
            if row.platform_explicit and post.platform != row.platform:
                raise PublishError(
                    f"第 {row.line} 行 URL 已存在，但平台为 {post.platform}，"
                    f"与 CSV 的 {row.platform} 不一致：{row.url}"
                )
            matched += 1
        else:
            handle = (account or row.account).strip()
            key = (row.platform, handle.casefold())
            account_obj = accounts.get(key)
            if account_obj is None:
                account_obj = Account(
                    id=new_id("acc"),
                    platform=row.platform,
                    handle=handle,
                    display_name=row.display_name,
                    is_benchmark=False,
                    niche=None,
                    note="由 wb pack backfill 从平台导出 CSV 创建",
                    created_at=created_at,
                )
                repo.insert_account(conn, account_obj)
                accounts[key] = account_obj
            post = Post(
                id=row.post_id or new_id("post"),
                account_id=account_obj.id,
                platform=row.platform,
                title=row.title or "",
                url=row.url,
                published_at=row.published_at or created_at,
                content_type=row.content_type or "image_text",
                topic_tags=row.topic_tags,
                duration_sec=row.duration_sec,
                caption=row.caption,
                created_at=created_at,
            )
            repo.insert_post(conn, post)
            url_index[row.url] = post
            created += 1

        captured_at = row.captured_at or _today_iso(zone)
        if post.id not in metric_keys:
            metric_keys[post.id] = {
                metric.captured_at for metric in repo.list_metrics(conn, post_id=post.id)
            }
        if captured_at in metric_keys[post.id]:
            skipped += 1
            continue
        repo.insert_metric(
            conn,
            Metric(
                id=new_id("metric"),
                post_id=post.id,
                captured_at=captured_at,
                views=row.views,
                likes=row.likes,
                comments=row.comments,
                shares=row.shares,
                saves=row.saves,
                follows=row.follows,
                completion_rate=row.completion_rate,
                avg_watch_sec=row.avg_watch_sec,
                raw=row.raw,
            ),
        )
        metric_keys[post.id].add(captured_at)
        metrics += 1

    return BackfillResult(
        matched=matched,
        created=created,
        metrics=metrics,
        skipped=skipped,
    )


__all__ = ["ALIASES", "BackfillResult", "backfill"]
