"""三个离线优先的信源适配器（T05）。

- :class:`HotlistSource`：DailyHotApi 兼容的热榜 JSON（``{data: [...]}``，也接受根数组）。
- :class:`RssSource`：RSS / Atom（``feedparser``），摘要去 HTML。
- :class:`LocalSource`：本地 JSON / JSONL，默认读取 ``data/seed/benchmark/*.jsonl``。

三个适配器都实现 ADR §6.4 的 ``name`` + ``fetch(since=...)``，并额外提供
``fetch_candidates()`` 返回带对标元信息（post_id / content_type / views）的
:class:`Candidate`。真实 HTTP 实现只在 ``offline=False`` 且显式给 URL 时启用；
测试与默认 CLI 都只读 fixture，不联网。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import feedparser
from bs4 import BeautifulSoup

from workbench.config import Config
from workbench.contracts import RawItem
from workbench.metrics.compute import parse_iso
from workbench.topics import TopicError

logger = logging.getLogger("workbench.topics.sources")

_REPO_ROOT = Path(__file__).resolve().parents[2]

SOURCE_NAMES: tuple[str, ...] = ("hotlist", "rss", "local")
KIND_NAMES: tuple[str, ...] = ("hotlist", "rss", "post")

PLATFORM_ALIASES: dict[str, str] = {
    "xhs": "xhs",
    "xiaohongshu": "xhs",
    "小红书": "xhs",
    "douyin": "douyin",
    "抖音": "douyin",
    "shipinhao": "shipinhao",
    "视频号": "shipinhao",
    "wechat": "wechat",
    "weixin": "wechat",
    "公众号": "wechat",
}


def default_seed_dir() -> Path:
    """定位 ``data/seed``：优先当前工作目录，其次是仓库根目录。"""
    for base in (Path.cwd(), _REPO_ROOT):
        candidate = base / "data" / "seed"
        if candidate.is_dir():
            return candidate
    return _REPO_ROOT / "data" / "seed"


def default_hotlist_path() -> Path:
    return default_seed_dir() / "hotlist_sample.json"


def default_rss_path() -> Path:
    return default_seed_dir() / "rss_sample.xml"


def default_benchmark_dir() -> Path:
    return default_seed_dir() / "benchmark"


# --------------------------------------------------------------------------- 工具函数
def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _first(mapping: Mapping[str, Any], keys: Sequence[str]) -> object | None:
    for key in keys:
        value = mapping.get(key)
        if value is not None and value != "":
            return value
    return None


def _strip_html(value: object) -> str | None:
    text = _text(value)
    if not text:
        return None
    cleaned = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return cleaned or None


def parse_heat(value: object) -> float | None:
    """解析热榜热度：数字或 ``1.2万`` / ``3.4k`` 这类字符串。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _text(value).replace(",", "").replace(" ", "")
    multiplier = 1.0
    if text.endswith(("万", "w", "W")):
        multiplier = 10_000.0
        text = text[:-1]
    elif text.endswith(("k", "K")):
        multiplier = 1_000.0
        text = text[:-1]
    try:
        number = float(text) * multiplier
    except ValueError:
        return None
    return number if number >= 0 else None


def parse_datetime(value: object, tz: str = "Asia/Shanghai") -> str | None:
    """解析毫秒/秒时间戳或 ISO 时间，统一成带时区 ISO 字符串；失败返回 ``None``。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and re.fullmatch(r"\d+(\.\d+)?", value.strip())
    ):
        number = float(value)
        if number > 1e11:
            number /= 1000.0
        try:
            stamp = datetime.fromtimestamp(number, tz=timezone.utc).astimezone(
                _resolve_zone(tz)
            )
        except (OverflowError, OSError, ValueError):
            return None
        return stamp.isoformat(timespec="seconds")
    text = _text(value)
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = text.replace("/", "-")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_resolve_zone(tz))
    return parsed.isoformat(timespec="seconds")


def _resolve_zone(tz: str) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def parse_tags(value: object) -> list[str]:
    """解析标签：数组、JSON 数组字符串，或 ``a|b,c`` 分隔字符串。"""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, list):
                return [str(item).strip() for item in data if str(item).strip()]
        return [part.strip() for part in re.split(r"[|,，、\s]+", text) if part.strip()]
    return []


def normalize_platform(value: object, default: str | None = None) -> str | None:
    text = _text(value).lower()
    if not text:
        return default
    return PLATFORM_ALIASES.get(text, default)


def _within_since(published_at: str | None, since: str | None, tz: str) -> bool:
    if not since or not published_at:
        return True
    try:
        return parse_iso(published_at, tz) >= parse_iso(since, tz)
    except Exception:  # noqa: BLE001 - 单条时间坏掉不应拖垮整个信源
        return True


def _stable_ref(*parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]
    return f"ref_{digest}"


# --------------------------------------------------------------------------- 候选
@dataclass(slots=True)
class Candidate:
    """一条待打分的选题候选；比 :class:`RawItem` 多带对标证据需要的元信息。"""

    adapter: str
    kind: str
    source: str
    ref: str
    title: str
    url: str | None = None
    published_at: str | None = None
    summary: str | None = None
    platform: str | None = None
    heat: float | None = None
    tags: list[str] = field(default_factory=list)
    raw_id: str | None = None
    post_id: str | None = None
    content_type: str | None = None
    duration_sec: float | None = None
    views: float | None = None

    def to_raw(self) -> RawItem:
        return RawItem(
            source=self.source,
            title=self.title,
            url=self.url,
            published_at=self.published_at,
            summary=self.summary,
            platform=self.platform,
            heat=self.heat,
            tags=list(self.tags),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "kind": self.kind,
            "source": self.source,
            "ref": self.ref,
            "title": self.title,
            "url": self.url,
            "published_at": self.published_at,
            "summary": self.summary,
            "platform": self.platform,
            "heat": self.heat,
            "tags": list(self.tags),
            "raw_id": self.raw_id,
            "post_id": self.post_id,
            "content_type": self.content_type,
            "duration_sec": self.duration_sec,
            "views": self.views,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Candidate:
        return cls(
            adapter=str(data.get("adapter") or "local"),
            kind=str(data.get("kind") or "post"),
            source=str(data.get("source") or "benchmark"),
            ref=str(data.get("ref") or ""),
            title=str(data.get("title") or ""),
            url=data.get("url"),
            published_at=data.get("published_at"),
            summary=data.get("summary"),
            platform=data.get("platform"),
            heat=float(data["heat"]) if data.get("heat") is not None else None,
            tags=[str(tag) for tag in (data.get("tags") or [])],
            raw_id=data.get("raw_id"),
            post_id=data.get("post_id"),
            content_type=data.get("content_type"),
            duration_sec=float(data["duration_sec"])
            if data.get("duration_sec") is not None
            else None,
            views=float(data["views"]) if data.get("views") is not None else None,
        )

    def text(self) -> str:
        """参与 jieba 关键词/相似度计算的文本。

        本地对标 fixture 的 caption 是占位文本（见 T05 契约），不参与打分，
        只用标题与 topic_tags；热榜/RSS 的真实摘要参与。
        """
        parts = [self.title, " ".join(self.tags)]
        if self.adapter != "local" and self.summary:
            parts.append(self.summary)
        return " ".join(part for part in parts if part)


def candidate_from_item(item: RawItem, adapter: str) -> Candidate:
    """把只实现 ``fetch`` 的适配器结果补成 Candidate。"""
    kind = "post" if adapter == "local" else adapter
    source = "benchmark" if adapter == "local" else adapter
    ref = item.url or _stable_ref(adapter, item.title)
    return Candidate(
        adapter=adapter,
        kind=kind,
        source=source,
        ref=ref,
        title=item.title,
        url=item.url,
        published_at=item.published_at,
        summary=item.summary,
        platform=item.platform,
        heat=item.heat,
        tags=list(item.tags),
    )


# --------------------------------------------------------------------------- 热榜
class HotlistSource:
    """DailyHotApi 兼容的热榜 JSON 适配器。"""

    name = "hotlist"

    def __init__(
        self,
        *,
        path: str | Path | None = None,
        url: str | None = None,
        offline: bool = True,
        platform: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.path = Path(path).expanduser() if path is not None else None
        self.url = (url or "").strip() or None
        self.offline = offline
        self.platform = platform
        self.timeout = timeout

    def fetch(self, *, since: str | None = None) -> list[RawItem]:
        return [candidate.to_raw() for candidate in self.fetch_candidates(since=since)]

    def fetch_candidates(
        self, *, since: str | None = None, tz: str = "Asia/Shanghai"
    ) -> list[Candidate]:
        payload = self._load()
        items = _extract_hotlist_items(payload)
        root_platform = None
        if isinstance(payload, Mapping):
            root_platform = normalize_platform(
                payload.get("type") or payload.get("name") or payload.get("title"),
                default=self.platform,
            )
        default_platform = self.platform or root_platform
        result: list[Candidate] = []
        for index, raw in enumerate(items, start=1):
            if not isinstance(raw, Mapping):
                continue
            title = _text(_first(raw, ("title", "name", "word", "content")))
            if not title:
                continue
            raw_id = _text(_first(raw, ("id", "guid", "item_id", "docid"))) or None
            url = _text(_first(raw, ("url", "link", "mobileUrl", "mobile_url"))) or None
            published_at = parse_datetime(
                _first(raw, ("timestamp", "published_at", "publish_time", "time", "date")),
                tz,
            )
            platform = normalize_platform(raw.get("platform"), default=default_platform)
            candidate = Candidate(
                adapter=self.name,
                kind="hotlist",
                source="hotlist",
                ref=url or raw_id or _stable_ref(self.name, title, str(index)),
                title=title,
                url=url,
                published_at=published_at,
                summary=_strip_html(_first(raw, ("desc", "description", "summary", "digest"))),
                platform=platform,
                heat=parse_heat(
                    _first(raw, ("hot", "hotness", "heat", "score", "num", "hot_value"))
                ),
                tags=parse_tags(_first(raw, ("tags", "keywords", "topic_tags"))),
                raw_id=raw_id,
            )
            if _within_since(candidate.published_at, since, tz):
                result.append(candidate)
        return result

    def _load(self) -> Any:
        if self.offline or not self.url:
            path = self.path or default_hotlist_path()
            if not path.is_file():
                raise TopicError(f"热榜 fixture 不存在：{path}")
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise TopicError(f"热榜 JSON 解析失败：{path}（{exc}）") from exc
            except OSError as exc:
                raise TopicError(f"热榜 fixture 读取失败：{path}（{exc}）") from exc
        return self._fetch_url()

    def _fetch_url(self) -> Any:
        import httpx

        try:
            response = httpx.get(self.url or "", timeout=self.timeout, follow_redirects=True)
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001 - 统一转成可预期错误
            raise TopicError(f"热榜请求失败：{self.url}（{type(exc).__name__}: {exc}）") from exc


def _extract_hotlist_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        data = payload.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, Mapping):
            for key in ("list", "items", "data"):
                nested = data.get(key)
                if isinstance(nested, list):
                    return nested
        for key in ("list", "items", "result", "data"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return nested
    raise TopicError("无法识别的热榜 JSON 形状：需要根数组或 {data: [...]}")


# --------------------------------------------------------------------------- RSS
class RssSource:
    """RSS / Atom 适配器；摘要用 BeautifulSoup 去 HTML。"""

    name = "rss"

    def __init__(
        self,
        *,
        path: str | Path | None = None,
        url: str | None = None,
        offline: bool = True,
        platform: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.path = Path(path).expanduser() if path is not None else None
        self.url = (url or "").strip() or None
        self.offline = offline
        self.platform = platform
        self.timeout = timeout

    def fetch(self, *, since: str | None = None) -> list[RawItem]:
        return [candidate.to_raw() for candidate in self.fetch_candidates(since=since)]

    def fetch_candidates(
        self, *, since: str | None = None, tz: str = "Asia/Shanghai"
    ) -> list[Candidate]:
        feed = feedparser.parse(self._load())
        entries = list(getattr(feed, "entries", []))
        if not entries and getattr(feed, "bozo", 0):
            raise TopicError("RSS 解析失败：没有可读条目")
        result: list[Candidate] = []
        for index, entry in enumerate(entries, start=1):
            title = _text(entry.get("title"))
            if not title:
                continue
            raw_id = _text(entry.get("id") or entry.get("guid")) or None
            url = _text(entry.get("link")) or None
            published_at = _entry_datetime(entry, tz)
            tags = [
                _text(tag.get("term"))
                for tag in entry.get("tags") or []
                if _text(tag.get("term"))
            ]
            candidate = Candidate(
                adapter=self.name,
                kind="rss",
                source="rss",
                ref=url or raw_id or _stable_ref(self.name, title, str(index)),
                title=title,
                url=url,
                published_at=published_at,
                summary=_strip_html(entry.get("summary") or entry.get("description")),
                platform=self.platform,
                heat=None,
                tags=tags,
                raw_id=raw_id,
            )
            if _within_since(candidate.published_at, since, tz):
                result.append(candidate)
        return result

    def _load(self) -> bytes:
        if self.offline or not self.url:
            path = self.path or default_rss_path()
            if not path.is_file():
                raise TopicError(f"RSS fixture 不存在：{path}")
            try:
                return path.read_bytes()
            except OSError as exc:
                raise TopicError(f"RSS fixture 读取失败：{path}（{exc}）") from exc
        import httpx

        try:
            response = httpx.get(self.url or "", timeout=self.timeout, follow_redirects=True)
            response.raise_for_status()
            return response.content
        except Exception as exc:  # noqa: BLE001
            raise TopicError(f"RSS 请求失败：{self.url}（{type(exc).__name__}: {exc}）") from exc


def _entry_datetime(entry: Any, tz: str) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                stamp = datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
            return stamp.astimezone(_resolve_zone(tz)).isoformat(timespec="seconds")
    for key in ("published", "updated"):
        parsed = parse_datetime(entry.get(key), tz)
        if parsed:
            return parsed
    return None


# --------------------------------------------------------------------------- 本地
class LocalSource:
    """本地 JSON / JSONL fixture；默认读对标作品库。"""

    name = "local"

    def __init__(
        self,
        paths: Sequence[str | Path] | None = None,
        *,
        platform: str | None = None,
    ) -> None:
        if paths:
            self.paths = [Path(path).expanduser() for path in paths]
        else:
            self.paths = [default_benchmark_dir()]
        self.platform = platform

    def fetch(self, *, since: str | None = None) -> list[RawItem]:
        return [candidate.to_raw() for candidate in self.fetch_candidates(since=since)]

    def fetch_candidates(
        self, *, since: str | None = None, tz: str = "Asia/Shanghai"
    ) -> list[Candidate]:
        result: list[Candidate] = []
        for path in self._expand_paths():
            for payload in _read_local_payloads(path):
                candidate = self._to_candidate(payload, path, tz)
                if candidate is None:
                    continue
                if _within_since(candidate.published_at, since, tz):
                    result.append(candidate)
        return result

    def _expand_paths(self) -> list[Path]:
        expanded: list[Path] = []
        for path in self.paths:
            if path.is_dir():
                expanded.extend(sorted(path.glob("*.jsonl")))
                expanded.extend(sorted(path.glob("*.json")))
            else:
                expanded.append(path)
        if not expanded:
            raise TopicError(f"本地 fixture 不存在或为空：{[str(path) for path in self.paths]}")
        return expanded

    def _to_candidate(
        self, payload: Mapping[str, Any], path: Path, tz: str
    ) -> Candidate | None:
        post = payload.get("post")
        account = payload.get("account") if isinstance(payload.get("account"), Mapping) else {}
        metric = payload.get("metric") if isinstance(payload.get("metric"), Mapping) else {}
        if isinstance(post, Mapping):
            post_data: Mapping[str, Any] = post
        else:
            post_data = payload
        title = _text(_first(post_data, ("title", "name")))
        if not title:
            return None
        raw_id = _text(_first(post_data, ("id", "post_id"))) or None
        url = _text(_first(post_data, ("url", "link"))) or None
        views = metric.get("views")
        if views is None:
            views = post_data.get("views")
        heat = parse_heat(views) if views is not None else parse_heat(post_data.get("heat"))
        platform = normalize_platform(
            post_data.get("platform") or account.get("platform"), default=self.platform
        )
        return Candidate(
            adapter=self.name,
            kind="post",
            source="benchmark",
            ref=raw_id or url or _stable_ref("local", path.name, title),
            title=title,
            url=url,
            published_at=parse_datetime(
                _first(post_data, ("published_at", "published", "date")), tz
            ),
            summary=_strip_html(_first(post_data, ("caption", "desc", "summary"))),
            platform=platform,
            heat=heat,
            tags=parse_tags(_first(post_data, ("topic_tags", "tags", "keywords"))),
            raw_id=raw_id,
            post_id=raw_id,
            content_type=_text(post_data.get("content_type")) or None,
            duration_sec=float(post_data["duration_sec"])
            if post_data.get("duration_sec") is not None
            else None,
            views=float(views) if views is not None else None,
        )


def _read_local_payloads(path: Path) -> list[Mapping[str, Any]]:
    if not path.is_file():
        raise TopicError(f"本地 fixture 不存在：{path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TopicError(f"本地 fixture 读取失败：{path}（{exc}）") from exc
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise TopicError(f"本地 JSON 解析失败：{path}（{exc}）") from exc
        if not isinstance(data, list):
            raise TopicError(f"本地 JSON 必须是数组：{path}")
        return [item for item in data if isinstance(item, Mapping)]
    if stripped.startswith("{"):
        # 先尝试整份 JSON 对象；失败再按 JSONL 逐行解析。
        try:
            whole = json.loads(text)
        except json.JSONDecodeError:
            whole = None
        if isinstance(whole, Mapping):
            for key in ("items", "posts", "data", "list"):
                nested = whole.get(key)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, Mapping)]
            return [whole]
        payloads: list[Mapping[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TopicError(f"{path.name} 第 {line_number} 行不是合法 JSON：{exc}") from exc
            if isinstance(item, Mapping):
                payloads.append(item)
        return payloads
    raise TopicError(f"无法识别的本地 fixture 格式：{path}（需要 JSON 或 JSONL）")


# --------------------------------------------------------------------------- 工厂
def build_adapters(
    cfg: Config,
    *,
    offline: bool = True,
    platform: str | None = None,
    hotlist_path: str | Path | None = None,
    rss_path: str | Path | None = None,
    local_paths: Sequence[str | Path] | None = None,
    benchmark_dir: str | Path | None = None,
    hotlist_url: str | None = None,
    rss_url: str | None = None,
) -> list[HotlistSource | RssSource | LocalSource]:
    """构造 ``hotlist`` / ``rss`` / ``local`` 三个适配器（ADR §6.4 固定入口）。

    - 默认全部离线读 ``data/seed`` fixture；``offline=False`` 且给了 URL 才走 HTTP。
    - ``local_paths`` 优先于 ``benchmark_dir``，两者都缺省时读 ``data/seed/benchmark``。
    """
    target_platform = platform or cfg.platform
    seed = default_seed_dir()
    hotlist = Path(hotlist_path).expanduser() if hotlist_path else seed / "hotlist_sample.json"
    rss = Path(rss_path).expanduser() if rss_path else seed / "rss_sample.xml"
    if local_paths:
        local = [Path(path).expanduser() for path in local_paths]
    elif benchmark_dir:
        local = [Path(benchmark_dir).expanduser()]
    else:
        local = [seed / "benchmark"]

    return [
        HotlistSource(
            path=hotlist,
            url=hotlist_url,
            offline=offline,
            platform=target_platform,
        ),
        RssSource(
            path=rss,
            url=rss_url,
            offline=offline,
            platform=target_platform,
        ),
        LocalSource(local, platform=target_platform),
    ]


__all__ = [
    "KIND_NAMES",
    "PLATFORM_ALIASES",
    "SOURCE_NAMES",
    "Candidate",
    "HotlistSource",
    "LocalSource",
    "RssSource",
    "build_adapters",
    "candidate_from_item",
    "default_benchmark_dir",
    "default_hotlist_path",
    "default_rss_path",
    "default_seed_dir",
    "normalize_platform",
    "parse_datetime",
    "parse_heat",
    "parse_tags",
]
