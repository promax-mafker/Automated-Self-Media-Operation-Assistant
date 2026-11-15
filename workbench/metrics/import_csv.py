"""平台导出 CSV 导入（T02）。

设计要点：

- 列名通过 :data:`ALIASES` 映射到统一字段；缺列时一次性报「缺哪列 + 支持哪些别名」。
- 未知列不报错，原样存进 ``metric.raw``，便于追溯平台原始行。
- 账号列可选：CSV 有账号列时逐行读取；没有时用 ``--account`` 兜底。
- 幂等键是 ``(post_id, captured_at)``；重复导入只累加 ``skipped``，不新增行。
- 先完整解析并校验，再写库，避免用户输入错误导致半截导入。
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from workbench.config import now_iso
from workbench.ids import new_id
from workbench.metrics import MetricsError
from workbench.store import repo
from workbench.store.models import Account, Metric, Post

PLATFORMS = ("xhs", "douyin", "shipinhao", "wechat")

# 统一字段 → 支持的导出列名（第一个是规范名；大小写、空格、下划线、连字符不敏感）。
ALIASES: dict[str, tuple[str, ...]] = {
    "post_id": ("post_id", "作品ID", "笔记ID", "视频ID", "帖子ID", "id"),
    "account": ("account", "账号", "账号handle", "博主", "作者", "handle"),
    "display_name": ("display_name", "账号名称", "昵称", "作者名称"),
    "niche": ("niche", "领域", "赛道"),
    "platform": ("platform", "平台"),
    "title": ("title", "标题", "作品标题", "笔记标题"),
    "url": ("url", "链接", "作品链接"),
    "published_at": ("published_at", "发布时间", "发布日期", "publish_time", "post_time"),
    "content_type": ("content_type", "内容类型", "作品类型", "类型", "type"),
    "topic_tags": ("topic_tags", "话题", "标签", "tags"),
    "duration_sec": ("duration_sec", "时长", "视频时长", "duration"),
    "caption": ("caption", "正文", "描述", "文案"),
    "captured_at": ("captured_at", "采集时间", "抓取时间", "快照时间", "snapshot_at", "数据时间"),
    "views": ("views", "播放量", "观看量", "阅读量", "view_count", "play_count"),
    "likes": ("likes", "点赞", "点赞数", "like_count"),
    "comments": ("comments", "评论", "评论数", "comment_count"),
    "shares": ("shares", "分享", "分享数", "转发", "转发数", "share_count"),
    "saves": ("saves", "收藏", "收藏数", "save_count", "collect_count"),
    "follows": ("follows", "关注", "涨粉", "新增关注", "follow_count"),
    "completion_rate": ("completion_rate", "完播率", "完播", "completion"),
    "avg_watch_sec": ("avg_watch_sec", "平均观看时长", "平均播放时长", "avg_watch"),
}

#: CSV 必须提供的列；缺一列就拒绝导入，避免「静默算出错误指标」。
REQUIRED_COLUMNS: tuple[str, ...] = (
    "post_id",
    "published_at",
    "title",
    "content_type",
    "captured_at",
    "views",
    "likes",
    "comments",
    "shares",
    "saves",
    "follows",
)

CONTENT_TYPES: dict[str, str] = {
    "imagetext": "image_text",
    "图文": "image_text",
    "图片": "image_text",
    "note": "image_text",
    "video": "video",
    "视频": "video",
    "短视频": "video",
    "article": "article",
    "文章": "article",
    "长文": "article",
}


def _normalize(value: str) -> str:
    """归一化列名/枚举值：小写，去掉空白、下划线、连字符与常见括号。"""
    return re.sub(r"[\s_\-./（）()]+", "", value.strip().lower())


_LOOKUP: dict[str, str] = {}
for _canonical, _names in ALIASES.items():
    for _name in _names:
        _key = _normalize(_name)
        _existing = _LOOKUP.get(_key)
        if _existing is not None and _existing != _canonical:
            raise ValueError(f"列别名冲突：{_name!r} 同时映射到 {_existing!r} 与 {_canonical!r}")
        _LOOKUP[_key] = _canonical


class MissingColumns(MetricsError):
    """CSV 缺少必需列；消息里带上每列支持的别名。"""

    def __init__(self, missing: list[str]) -> None:
        details = [
            f"  - {name}（支持别名：{' / '.join(ALIASES[name])}）" for name in missing
        ]
        super().__init__("CSV 缺少必需列：\n" + "\n".join(details))
        self.missing = tuple(missing)


@dataclass(frozen=True, slots=True)
class ImportResult:
    """一次导入的统计；``summary()`` 用于 CLI 输出。"""

    imported: int
    skipped: int
    accounts: int
    posts_created: int
    posts_existing: int
    handles: tuple[str, ...]

    def summary(self) -> str:
        return f"imported={self.imported} accounts={self.accounts} skipped={self.skipped}"


@dataclass(slots=True)
class _ParsedRow:
    line: int
    post_id: str
    account: str
    platform: str
    title: str
    url: str | None
    published_at: str
    content_type: str
    topic_tags: list[str]
    duration_sec: float | None
    caption: str | None
    captured_at: str
    views: int
    likes: int
    comments: int
    shares: int
    saves: int
    follows: int
    completion_rate: float | None
    avg_watch_sec: float | None
    display_name: str | None
    niche: str | None
    raw: str


def _resolve_zone(tz_name: str) -> tzinfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        from datetime import timezone

        return timezone.utc


def _read_table(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.is_file():
        raise MetricsError(f"CSV 文件不存在：{path}")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            rows = [row for row in reader if any((value or "").strip() for value in row.values())]
    except UnicodeDecodeError as exc:
        raise MetricsError(f"CSV 不是 UTF-8 编码：{path}（{exc}）") from exc
    except csv.Error as exc:
        raise MetricsError(f"CSV 解析失败：{path}（{exc}）") from exc

    if not fieldnames:
        raise MetricsError(f"CSV 没有表头：{path}")
    if not rows:
        raise MetricsError(f"CSV 没有数据行：{path}")
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
            raise MetricsError(
                f"CSV 列映射冲突：{header!r} 与 {mapping[canonical]!r} 都映射到 {canonical}"
            )
        mapping[canonical] = header

    missing = [name for name in REQUIRED_COLUMNS if name not in mapping]
    if missing:
        raise MissingColumns(missing)
    return mapping


def _cell(row: dict[str, str], mapping: dict[str, str], canonical: str) -> str:
    header = mapping.get(canonical)
    if header is None:
        return ""
    return (row.get(header) or "").strip()


def _text(value: str, canonical: str, line: int) -> str:
    if not value:
        raise MetricsError(f"第 {line} 行 {canonical} 为空")
    return value


def _parse_int(value: str, canonical: str, line: int) -> int:
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
        raise MetricsError(f"第 {line} 行 {canonical} 不是数字：{value!r}") from exc
    if number < 0:
        raise MetricsError(f"第 {line} 行 {canonical} 不能为负数：{value!r}")
    return number


def _parse_float(value: str, canonical: str, line: int) -> float:
    text = value.replace(",", "").replace(" ", "").replace("_", "")
    try:
        number = float(text)
    except ValueError as exc:
        raise MetricsError(f"第 {line} 行 {canonical} 不是数字：{value!r}") from exc
    if number < 0:
        raise MetricsError(f"第 {line} 行 {canonical} 不能为负数：{value!r}")
    return number


def _parse_rate(value: str, canonical: str, line: int) -> float:
    text = value.strip()
    percent = text.endswith("%")
    if percent:
        text = text[:-1].strip()
    try:
        number = float(text)
    except ValueError as exc:
        raise MetricsError(f"第 {line} 行 {canonical} 不是数字：{value!r}") from exc
    if percent:
        number /= 100
    elif number > 1:
        # 平台导出常用 85 表示 85%；0–1 视为已归一化。
        number /= 100
    if not 0 <= number <= 1:
        raise MetricsError(f"第 {line} 行 {canonical} 应在 0–1 或 0–100% 之间：{value!r}")
    return number


def _parse_datetime(value: str, canonical: str, line: int, zone: tzinfo) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = text.replace("/", "-")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise MetricsError(
            f"第 {line} 行 {canonical} 不是合法时间：{value!r}；"
            "支持 ISO 8601（如 2026-11-02T08:30:00+08:00）或 2026-11-02 08:30:00"
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone)


def _parse_duration(value: str, canonical: str, line: int) -> float:
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
        raise MetricsError(
            f"第 {line} 行 {canonical} 不是合法时长：{value!r}；支持秒数（15）、mm:ss 或 hh:mm:ss"
        ) from exc
    if number < 0:
        raise MetricsError(f"第 {line} 行 {canonical} 不能为负数：{value!r}")
    return number


def _parse_tags(value: str, line: int) -> list[str]:
    text = value.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MetricsError(f"第 {line} 行 topic_tags 不是合法 JSON 数组：{value!r}") from exc
        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            raise MetricsError(f"第 {line} 行 topic_tags 必须是字符串数组：{value!r}")
        return [item.strip() for item in data if item.strip()]
    return [part.strip() for part in re.split(r"[|,，、]+", text) if part.strip()]


def _parse_content_type(value: str, line: int) -> str:
    key = _normalize(value)
    if key not in CONTENT_TYPES:
        raise MetricsError(
            f"第 {line} 行 content_type 无法识别：{value!r}；"
            "支持 image_text（图文）/ video（视频）/ article（文章）"
        )
    return CONTENT_TYPES[key]


def _parse_platform(value: str, default: str, line: int) -> str:
    platform = (value or default).strip().lower()
    if platform not in PLATFORMS:
        raise MetricsError(
            f"第 {line} 行 platform 无法识别：{value!r}；支持 {' / '.join(PLATFORMS)}"
        )
    return platform


def _parse_row(
    row: dict[str, str],
    mapping: dict[str, str],
    line: int,
    *,
    default_platform: str,
    zone: tzinfo,
) -> _ParsedRow:
    post_id = _text(_cell(row, mapping, "post_id"), "post_id", line)
    title = _text(_cell(row, mapping, "title"), "title", line)
    published_raw = _text(_cell(row, mapping, "published_at"), "published_at", line)
    captured_raw = _text(_cell(row, mapping, "captured_at"), "captured_at", line)
    content_type_raw = _text(_cell(row, mapping, "content_type"), "content_type", line)

    published = _parse_datetime(published_raw, "published_at", line, zone)
    captured = _parse_datetime(captured_raw, "captured_at", line, zone)

    duration_raw = _cell(row, mapping, "duration_sec")
    completion_raw = _cell(row, mapping, "completion_rate")
    avg_watch_raw = _cell(row, mapping, "avg_watch_sec")

    raw = json.dumps(
        {key: value for key, value in row.items() if (value or "").strip()},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _ParsedRow(
        line=line,
        post_id=post_id,
        account=_cell(row, mapping, "account"),
        platform=_parse_platform(_cell(row, mapping, "platform"), default_platform, line),
        title=title,
        url=_cell(row, mapping, "url") or None,
        published_at=published.isoformat(timespec="seconds"),
        content_type=_parse_content_type(content_type_raw, line),
        topic_tags=_parse_tags(_cell(row, mapping, "topic_tags"), line),
        duration_sec=_parse_duration(duration_raw, "duration_sec", line) if duration_raw else None,
        caption=_cell(row, mapping, "caption") or None,
        captured_at=captured.isoformat(timespec="seconds"),
        views=_parse_int(_text(_cell(row, mapping, "views"), "views", line), "views", line),
        likes=_parse_int(_text(_cell(row, mapping, "likes"), "likes", line), "likes", line),
        comments=_parse_int(
            _text(_cell(row, mapping, "comments"), "comments", line), "comments", line
        ),
        shares=_parse_int(_text(_cell(row, mapping, "shares"), "shares", line), "shares", line),
        saves=_parse_int(_text(_cell(row, mapping, "saves"), "saves", line), "saves", line),
        follows=_parse_int(
            _text(_cell(row, mapping, "follows"), "follows", line), "follows", line
        ),
        completion_rate=(
            _parse_rate(completion_raw, "completion_rate", line) if completion_raw else None
        ),
        avg_watch_sec=(
            _parse_float(avg_watch_raw, "avg_watch_sec", line) if avg_watch_raw else None
        ),
        display_name=_cell(row, mapping, "display_name") or None,
        niche=_cell(row, mapping, "niche") or None,
        raw=raw,
    )


def _load_accounts(conn: sqlite3.Connection) -> dict[tuple[str, str], Account]:
    return {
        (account.platform, account.handle.casefold()): account
        for account in repo.list_accounts(conn)
    }


def _validate_accounts(
    rows: list[_ParsedRow], account: str | None
) -> list[_ParsedRow]:
    own_handle = account.strip() if account else None
    missing = [row.line for row in rows if not row.account]
    if missing:
        lines = "、".join(str(line) for line in missing)
        if own_handle is None:
            raise MetricsError(
                f"第 {lines} 行缺少账号；CSV 没有账号列时必须用 --account 指定自己的账号"
            )
        for row in rows:
            if not row.account:
                row.account = own_handle

    handles = sorted({row.account.casefold() for row in rows})
    if len(handles) > 1 and own_handle is None:
        names = sorted({row.account for row in rows})
        raise MetricsError(
            f"CSV 含多个账号（{'、'.join(names)}）；"
            "请用 --account 指定自己的账号，其余账号按对标账号导入"
        )
    return rows


def import_metrics(
    conn: sqlite3.Connection,
    csv_path: Path | str,
    *,
    account: str | None = None,
    platform: str | None = None,
    tz: str = "Asia/Shanghai",
) -> ImportResult:
    """把平台导出 CSV 导入 ``account`` / ``post`` / ``metric``。

    - ``account``：自己的账号 handle；CSV 内其他 handle 标记为对标账号。
    - ``platform``：CSV 没有平台列时的默认平台。
    - 返回累计统计；重复的 ``(post_id, captured_at)`` 计入 ``skipped``。
    """
    path = Path(csv_path).expanduser()
    fieldnames, raw_rows = _read_table(path)
    mapping = _resolve_columns(fieldnames)
    zone = _resolve_zone(tz)

    parsed = [
        _parse_row(
            row,
            mapping,
            line,
            default_platform=(platform or "xhs").strip().lower(),
            zone=zone,
        )
        for line, row in enumerate(raw_rows, start=2)
    ]
    parsed = _validate_accounts(parsed, account)

    post_accounts: dict[str, str] = {}
    for row in parsed:
        existing_account = post_accounts.get(row.post_id)
        if existing_account is not None and existing_account.casefold() != row.account.casefold():
            raise MetricsError(
                f"CSV 内 post_id {row.post_id!r} 同时属于 {existing_account!r} 与 "
                f"{row.account!r}；请检查导出文件"
            )
        post_accounts[row.post_id] = row.account

    own_handle = account.strip() if account else None
    accounts = _load_accounts(conn)
    for row in parsed:
        existing_post = repo.get_post(conn, row.post_id)
        if existing_post is None:
            continue
        existing_account = accounts.get((row.platform, row.account.casefold()))
        if existing_account is None or existing_post.account_id != existing_account.id:
            raise MetricsError(
                f"第 {row.line} 行 post_id {row.post_id!r} 已存在，但属于其他账号；"
                "请检查 CSV 或换一个 post_id"
            )
    snapshots: dict[str, set[str]] = {}
    created_at = now_iso(tz)
    imported = 0
    skipped = 0
    posts_created = 0
    posts_existing = 0

    for row in parsed:
        key = (row.platform, row.account.casefold())
        account_obj = accounts.get(key)
        if account_obj is None:
            account_obj = Account(
                id=new_id("acc"),
                platform=row.platform,
                handle=row.account,
                display_name=row.display_name,
                is_benchmark=bool(own_handle and row.account.casefold() != own_handle.casefold()),
                niche=row.niche,
                note=None,
                created_at=created_at,
            )
            repo.insert_account(conn, account_obj)
            accounts[key] = account_obj

        post = repo.get_post(conn, row.post_id)
        if post is None:
            post = Post(
                id=row.post_id,
                account_id=account_obj.id,
                platform=row.platform,
                title=row.title,
                url=row.url,
                published_at=row.published_at,
                content_type=row.content_type,
                topic_tags=row.topic_tags,
                duration_sec=row.duration_sec,
                caption=row.caption,
                created_at=created_at,
            )
            repo.insert_post(conn, post)
            posts_created += 1
        else:
            if post.account_id != account_obj.id:
                raise MetricsError(
                    f"第 {row.line} 行 post_id {row.post_id!r} 已存在，但属于其他账号；"
                    "请检查 CSV 或换一个 post_id"
                )
            posts_existing += 1

        if row.post_id not in snapshots:
            snapshots[row.post_id] = {
                metric.captured_at for metric in repo.list_metrics(conn, post_id=row.post_id)
            }
        if row.captured_at in snapshots[row.post_id]:
            skipped += 1
            continue
        snapshots[row.post_id].add(row.captured_at)

        repo.insert_metric(
            conn,
            Metric(
                id=new_id("metric"),
                post_id=row.post_id,
                captured_at=row.captured_at,
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
        imported += 1

    handles = tuple(sorted({row.account for row in parsed}))
    return ImportResult(
        imported=imported,
        skipped=skipped,
        accounts=len(handles),
        posts_created=posts_created,
        posts_existing=posts_existing,
        handles=handles,
    )


__all__ = [
    "ALIASES",
    "ImportResult",
    "MissingColumns",
    "PLATFORMS",
    "REQUIRED_COLUMNS",
    "import_metrics",
]
