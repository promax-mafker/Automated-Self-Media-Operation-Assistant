"""发布包 ``manifest.json``、SHA-256 校验和与建议发布时间（T08）。

manifest 必含字段以 ``tickets/T08.md`` §4 为准；``checksums`` 覆盖包内除
``manifest.json`` 自身之外的全部文件（manifest 不能包含自己的哈希）。
建议发布时间复用 T02 的时段口径：每篇作品取最新一条 ``metric``，按
``(星期, 时段)`` 分组比较 ``interaction_rate``，样本量 ≥ 3 才采纳；无可用数据时
给出固定默认并在原因里写明「无数据，按默认」。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

from workbench.metrics.compute import (
    MIN_GROUP_SAMPLE,
    TIME_SLOTS,
    latest_metric,
    parse_iso,
    resolve_zone,
    time_slot,
)
from workbench.publish import PublishError
from workbench.store import repo

MANIFEST_FILENAME = "manifest.json"
CHECKSUM_EXCLUDED: frozenset[str] = frozenset({MANIFEST_FILENAME})
PLATFORMS: tuple[str, ...] = ("xhs", "douyin", "shipinhao", "wechat")

REQUIRED_MANIFEST_FIELDS: tuple[str, ...] = (
    "pack_id",
    "platform",
    "copy_set_id",
    "title",
    "body",
    "tags",
    "assets",
    "ai_generated",
    "compliance",
    "suggested_publish_at",
    "checksums",
    "created_at",
)

WEEKDAY_LABELS: tuple[str, ...] = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
#: 各时段的代表发布小时；只用于把「最佳时段」落到具体时间。
SLOT_HOURS: dict[str, int] = {"早": 9, "午": 13, "晚": 19, "深夜": 2}
DEFAULT_HOUR = 19

AI_DISCLOSURE = (
    "本内容由 AI 辅助生成，已完成人工审核与编辑；发布时请按平台要求勾选或声明"
    "「AI 生成」标识。"
)


@dataclass(frozen=True, slots=True)
class PublishSuggestion:
    """一次建议发布时间及其可追溯依据。"""

    at: str
    reason: str
    default_used: bool
    sample_posts: int
    weekday: str | None
    slot: str | None
    mean_interaction_rate: float | None


def sha256_file(path: Path) -> str:
    """返回文件的 SHA-256 十六进制摘要。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_checksums(
    pack_dir: Path, *, exclude: frozenset[str] = CHECKSUM_EXCLUDED
) -> dict[str, str]:
    """计算包目录下全部文件的校验和，路径用 POSIX 相对路径。"""
    root = Path(pack_dir)
    if not root.is_dir():
        raise PublishError(f"发布包目录不存在：{root}")
    files = [path for path in root.rglob("*") if path.is_file()]
    files.sort(key=lambda path: path.relative_to(root).as_posix())
    checksums: dict[str, str] = {}
    for path in files:
        relative = path.relative_to(root).as_posix()
        if relative in exclude:
            continue
        checksums[relative] = sha256_file(path)
    return checksums


def verify_checksums(pack_dir: Path, manifest: dict[str, Any]) -> list[str]:
    """校验包内文件与 manifest.checksums 是否一致；返回问题列表，空列表表示通过。"""
    expected = manifest.get("checksums")
    if not isinstance(expected, dict):
        return ["manifest.checksums 不是对象"]
    actual = compute_checksums(pack_dir)
    problems: list[str] = []
    for relative, digest in expected.items():
        if relative not in actual:
            problems.append(f"缺少文件：{relative}")
        elif actual[relative] != digest:
            problems.append(f"校验和不一致：{relative}")
    for relative in actual:
        if relative not in expected:
            problems.append(f"未覆盖文件：{relative}")
    return problems


def normalize_compliance(value: Any) -> dict[str, Any]:
    """把 ``copy_set.compliance`` 归一成 manifest 需要的 ``{passed, hits}``。"""
    source = value if isinstance(value, dict) else {}
    hits = source.get("hits")
    return {
        "passed": bool(source.get("passed", False)),
        "hits": list(hits) if isinstance(hits, list) else [],
    }


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _best_group(
    groups: dict[Any, list[float]],
) -> tuple[Any, float] | None:
    """样本量 ≥ ``MIN_GROUP_SAMPLE`` 的分组里取均值最高者；并列时取样本更多者。"""
    candidates: list[tuple[Any, float, int]] = [
        (key, _mean(values) or 0.0, len(values))
        for key, values in groups.items()
        if len(values) >= MIN_GROUP_SAMPLE
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[1], -item[2], str(item[0])))
    key, mean_value, _ = candidates[0]
    return key, mean_value


def _next_at(
    now: datetime,
    zone: Any,
    *,
    weekday: int | None = None,
    hour: int,
) -> datetime:
    if weekday is None:
        candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    days = (weekday - now.weekday()) % 7
    candidate = datetime.combine(
        now.date() + timedelta(days=days), time(hour), tzinfo=zone
    )
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.2f}%"


def suggest_publish_time(
    conn: sqlite3.Connection,
    platform: str,
    *,
    tz: str = "Asia/Shanghai",
    now: datetime | None = None,
) -> PublishSuggestion:
    """按 T02 指标给出下一次建议发布时间。

    - 每篇作品取最新 ``metric``；``interaction_rate = (likes+comments+saves+shares)/views``。
    - 优先比较 ``(星期, 时段)``；同星期样本不足时退到「时段」；都无 ≥3 样本时用默认。
    """
    zone = resolve_zone(tz)
    moment = now or datetime.now(zone)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    else:
        moment = moment.astimezone(zone)

    exact: dict[tuple[int, str], list[float]] = {}
    slots: dict[str, list[float]] = {}
    sample_posts = 0
    for post in repo.list_posts(conn, platform=platform):
        metric = latest_metric(conn, post.id, tz)
        if metric is None:
            continue
        views = int(metric.views or 0)
        if views <= 0:
            continue
        interaction = (
            (metric.likes or 0)
            + (metric.comments or 0)
            + (metric.saves or 0)
            + (metric.shares or 0)
        ) / views
        published = parse_iso(post.published_at, tz)
        slot = time_slot(published)
        exact.setdefault((published.weekday(), slot), []).append(interaction)
        slots.setdefault(slot, []).append(interaction)
        sample_posts += 1

    best_exact = _best_group(exact)
    if best_exact is not None:
        (weekday, slot), rate = best_exact
        label = WEEKDAY_LABELS[weekday]
        at = _next_at(moment, zone, weekday=weekday, hour=SLOT_HOURS[slot])
        reason = (
            f"基于 {sample_posts} 篇同平台作品的最新指标：{label} / {slot} 的 "
            f"interaction_rate 均值 {_pct(rate)} 最高（该组 {len(exact[(weekday, slot)])} 篇，"
            f"≥{MIN_GROUP_SAMPLE} 篇才采纳）"
        )
        return PublishSuggestion(
            at=at.isoformat(timespec="seconds"),
            reason=reason,
            default_used=False,
            sample_posts=sample_posts,
            weekday=label,
            slot=slot,
            mean_interaction_rate=rate,
        )

    best_slot = _best_group(slots)
    if best_slot is not None:
        slot, rate = best_slot
        at = _next_at(moment, zone, hour=SLOT_HOURS[slot])
        reason = (
            f"基于 {sample_posts} 篇同平台作品的最新指标：{slot} 时段的 interaction_rate "
            f"均值 {_pct(rate)} 最高（该时段 {len(slots[slot])} 篇）；同星期样本不足，"
            "按最佳时段推荐"
        )
        return PublishSuggestion(
            at=at.isoformat(timespec="seconds"),
            reason=reason,
            default_used=False,
            sample_posts=sample_posts,
            weekday=None,
            slot=slot,
            mean_interaction_rate=rate,
        )

    default_day = (moment + timedelta(days=1)).date()
    at = datetime.combine(default_day, time(DEFAULT_HOUR), tzinfo=zone)
    reason = (
        f"无数据，按默认：{default_day.isoformat()} {DEFAULT_HOUR:02d}:00"
        f"（同平台没有 ≥{MIN_GROUP_SAMPLE} 篇可用指标）"
    )
    return PublishSuggestion(
        at=at.isoformat(timespec="seconds"),
        reason=reason,
        default_used=True,
        sample_posts=sample_posts,
        weekday=None,
        slot=None,
        mean_interaction_rate=None,
    )


def build_manifest(
    *,
    pack_id: str,
    platform: str,
    copy_set_id: str,
    title: str,
    body: str,
    tags: list[str],
    assets: list[dict[str, Any]],
    compliance: Any,
    suggestion: PublishSuggestion,
    created_at: str,
) -> dict[str, Any]:
    """组装 manifest 的静态部分；``checksums`` 由打包器在文件落盘后补上。"""
    return {
        "pack_id": pack_id,
        "platform": platform,
        "copy_set_id": copy_set_id,
        "title": title,
        "body": body,
        "tags": list(tags),
        "assets": list(assets),
        "ai_generated": True,
        "compliance": normalize_compliance(compliance),
        "suggested_publish_at": suggestion.at,
        "suggested_publish_reason": suggestion.reason,
        "suggested_publish_default": suggestion.default_used,
        "created_at": created_at,
    }


def validate_manifest(manifest: dict[str, Any]) -> None:
    """校验 manifest 必填字段与基本类型；不合格时抛 :class:`PublishError`。"""
    if not isinstance(manifest, dict):
        raise PublishError("manifest 必须是 JSON 对象")
    missing = [field for field in REQUIRED_MANIFEST_FIELDS if field not in manifest]
    if missing:
        raise PublishError(f"manifest 缺少必填字段：{', '.join(missing)}")
    for field in ("pack_id", "platform", "copy_set_id", "title", "body", "created_at"):
        if not isinstance(manifest[field], str) or not manifest[field]:
            raise PublishError(f"manifest.{field} 必须是非空字符串")
    if manifest["platform"] not in PLATFORMS:
        raise PublishError(
            f"manifest.platform 无法识别：{manifest['platform']!r}；"
            f"支持 {' / '.join(PLATFORMS)}"
        )
    if not isinstance(manifest["tags"], list) or not all(
        isinstance(tag, str) for tag in manifest["tags"]
    ):
        raise PublishError("manifest.tags 必须是字符串数组")
    if manifest["ai_generated"] is not True:
        raise PublishError("manifest.ai_generated 必须为 true")
    compliance = manifest["compliance"]
    if not isinstance(compliance, dict) or not isinstance(compliance.get("passed"), bool):
        raise PublishError("manifest.compliance.passed 必须是布尔值")
    if not isinstance(compliance.get("hits"), list):
        raise PublishError("manifest.compliance.hits 必须是数组")
    if not isinstance(manifest["assets"], list):
        raise PublishError("manifest.assets 必须是数组")
    for index, asset in enumerate(manifest["assets"], start=1):
        if not isinstance(asset, dict):
            raise PublishError(f"manifest.assets[{index}] 必须是对象")
        missing_asset = [
            field
            for field in ("id", "path", "width", "height", "duration_sec")
            if field not in asset
        ]
        if missing_asset:
            raise PublishError(
                f"manifest.assets[{index}] 缺少字段：{', '.join(missing_asset)}"
            )
    if not isinstance(manifest["checksums"], dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in manifest["checksums"].items()
    ):
        raise PublishError("manifest.checksums 必须是字符串到字符串的对象")
    if not isinstance(manifest["suggested_publish_at"], str) or not manifest[
        "suggested_publish_at"
    ]:
        raise PublishError("manifest.suggested_publish_at 必须是非空字符串")


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """校验并写出格式化的 ``manifest.json``。"""
    validate_manifest(manifest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_manifest(path: Path) -> dict[str, Any]:
    """读取并校验 ``manifest.json``。"""
    target = Path(path)
    if not target.is_file():
        raise PublishError(f"manifest 不存在：{target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PublishError(f"manifest 不是合法 JSON：{target}（{exc}）") from exc
    if not isinstance(data, dict):
        raise PublishError(f"manifest 顶层必须是对象：{target}")
    validate_manifest(data)
    return data


__all__ = [
    "AI_DISCLOSURE",
    "CHECKSUM_EXCLUDED",
    "DEFAULT_HOUR",
    "MANIFEST_FILENAME",
    "PLATFORMS",
    "REQUIRED_MANIFEST_FIELDS",
    "SLOT_HOURS",
    "TIME_SLOTS",
    "WEEKDAY_LABELS",
    "PublishSuggestion",
    "build_manifest",
    "compute_checksums",
    "load_manifest",
    "normalize_compliance",
    "sha256_file",
    "suggest_publish_time",
    "validate_manifest",
    "verify_checksums",
    "write_manifest",
]
