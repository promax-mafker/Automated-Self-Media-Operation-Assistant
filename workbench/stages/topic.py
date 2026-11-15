"""T05 stage：``topic_source`` 与 ``topic_score``（topic 流水线）。

- ``topic_source``：调用 hotlist / rss / local 三个适配器，把候选写入
  ``<out>/candidates.json``。
- ``topic_score``：导入对标 fixture（幂等）、计算平台爆款画像、确定性打分、
  upsert ``topic`` 行，并写出 ``<out>/topics.json``。

空信源不是异常：``topics.json`` 里 ``topics=[]`` 且带「信源无新内容」提示。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from workbench.config import now_iso
from workbench.ids import new_id
from workbench.pipeline.registry import register
from workbench.pipeline.stage import RunContext
from workbench.store import repo
from workbench.store.models import Topic
from workbench.topics import TopicError
from workbench.topics.benchmark import (
    WINDOW_DAYS,
    build_platform_profile,
    seed_benchmark,
)
from workbench.topics.score import (
    ScoredCandidate,
    build_own_context,
    latest_candidate_time,
    score_candidates,
)
from workbench.topics.sources import (
    SOURCE_NAMES,
    Candidate,
    build_adapters,
    candidate_from_item,
    default_benchmark_dir,
)

DEFAULT_COUNT = 10
MAX_COUNT = 50
CANDIDATES_FILE = "candidates.json"
TOPICS_FILE = "topics.json"


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "y"}:
        return True
    if normalized in {"0", "false", "no", "off", "n"}:
        return False
    raise TopicError(f"布尔参数只接受 1/0、true/false，收到 {value!r}")


def _parse_int(value: str | None, key: str, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise TopicError(f"{key} 必须是整数，收到 {value!r}") from exc


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _fetch_candidates(
    adapter: Any, *, since: str | None, tz: str
) -> list[Candidate]:
    if hasattr(adapter, "fetch_candidates"):
        return list(adapter.fetch_candidates(since=since, tz=tz))
    return [
        candidate_from_item(item, adapter.name) for item in adapter.fetch(since=since)
    ]


def _platform_matches(candidate: Candidate, platform: str) -> bool:
    return not candidate.platform or candidate.platform == platform


@register("topic_source")
class TopicSource:
    """从三个信源拉取候选并落盘；不写库、不调模型。"""

    name = "topic_source"
    needs: tuple[str, ...] = ()

    def run(self, ctx: RunContext) -> dict[str, Any]:
        platform = (ctx.params.get("platform") or ctx.cfg.platform).strip().lower()
        offline = _parse_bool(ctx.params.get("offline"), True)
        since = ctx.params.get("since") or None
        wanted = _split_csv(ctx.params.get("sources")) or list(SOURCE_NAMES)
        unknown = [name for name in wanted if name not in SOURCE_NAMES]
        if unknown:
            raise TopicError(
                f"未知信源：{', '.join(unknown)}；可选：{' / '.join(SOURCE_NAMES)}"
            )

        adapters = build_adapters(
            ctx.cfg,
            offline=offline,
            platform=platform,
            hotlist_path=ctx.params.get("hotlist") or None,
            rss_path=ctx.params.get("rss") or None,
            local_paths=_split_csv(ctx.params.get("local")) or None,
            benchmark_dir=ctx.params.get("benchmark_dir") or None,
        )
        candidates: list[Candidate] = []
        counts: dict[str, int] = {}
        for adapter in adapters:
            if adapter.name not in wanted:
                continue
            fetched = _fetch_candidates(adapter, since=since, tz=ctx.cfg.tz)
            kept = [item for item in fetched if _platform_matches(item, platform)]
            counts[adapter.name] = len(kept)
            candidates.extend(kept)

        payload = {
            "platform": platform,
            "since": since,
            "offline": offline,
            "sources": counts,
            "items": [candidate.to_dict() for candidate in candidates],
        }
        path = ctx.out / CANDIDATES_FILE
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        ctx.log(
            "topic_source: items="
            f"{len(candidates)} "
            + " ".join(f"{name}={count}" for name, count in counts.items())
        )
        return {
            "items": len(candidates),
            "sources": counts,
            "offline": offline,
            "path": str(path),
        }


def _benchmark_dir(ctx: RunContext) -> Path:
    raw = ctx.params.get("benchmark_dir")
    return Path(raw).expanduser() if raw else default_benchmark_dir()


def _upsert_topics(
    ctx: RunContext, scored: list[ScoredCandidate], *, platform: str
) -> list[Topic]:
    existing_by_ref = {
        topic.source_ref: topic
        for topic in repo.list_topics(ctx.db, platform=platform)
        if topic.source_ref
    }
    rows: list[Topic] = []
    for item in scored:
        candidate = item.candidate
        existing = existing_by_ref.get(candidate.ref)
        topic = Topic(
            id=existing.id if existing is not None else new_id("topic"),
            platform=platform,
            title=candidate.title.strip(),
            angle=item.angle,
            hook=item.hook,
            score=item.score,
            score_parts={name: float(value) for name, value in item.parts.items()},
            source=candidate.source,
            source_ref=candidate.ref,
            evidence=item.evidence,
            status=existing.status if existing is not None else "new",
            created_at=existing.created_at if existing is not None else now_iso(ctx.cfg.tz),
        )
        if existing is None:
            repo.insert_topic(ctx.db, topic)
        else:
            ctx.db.execute(
                """
                UPDATE topic
                SET title = ?, angle = ?, hook = ?, score = ?, score_parts = ?,
                    source = ?, evidence = ?
                WHERE id = ?
                """,
                (
                    topic.title,
                    topic.angle,
                    topic.hook,
                    topic.score,
                    json.dumps(topic.score_parts, ensure_ascii=False, separators=(",", ":")),
                    topic.source,
                    json.dumps(topic.evidence, ensure_ascii=False, separators=(",", ":")),
                    topic.id,
                ),
            )
            ctx.db.commit()
        existing_by_ref[candidate.ref] = topic
        rows.append(topic)
    return rows


@register("topic_score")
class TopicScore:
    """构建对标画像、确定性打分、upsert topic 并写出 topics.json。"""

    name = "topic_score"
    needs: tuple[str, ...] = ()

    def run(self, ctx: RunContext) -> dict[str, Any]:
        path = ctx.out / CANDIDATES_FILE
        if not path.is_file():
            raise TopicError(
                f"topic_score 缺少 {CANDIDATES_FILE}；请先执行 topic_source"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TopicError(f"{CANDIDATES_FILE} 不是合法 JSON：{exc}") from exc
        platform = (
            ctx.params.get("platform") or payload.get("platform") or ctx.cfg.platform
        ).strip().lower()
        count = _parse_int(ctx.params.get("count"), "count", DEFAULT_COUNT)
        if not 1 <= count <= MAX_COUNT:
            raise TopicError(f"count 应在 1–{MAX_COUNT} 之间，收到 {count}")
        days = _parse_int(ctx.params.get("days"), "days", WINDOW_DAYS)
        if days <= 0:
            raise TopicError(f"days 必须为正整数，收到 {days}")
        as_of = ctx.params.get("as_of") or None

        candidates = [
            Candidate.from_dict(item)
            for item in payload.get("items") or []
            if isinstance(item, dict)
        ]
        topics_path = ctx.out / TOPICS_FILE

        if not candidates:
            # 空信源按契约返回 0 条并提示，不依赖对标库是否存在。
            output: dict[str, Any] = {
                "platform": platform,
                "count": 0,
                "topics": [],
                "message": "信源无新内容",
            }
            topics_path.write_text(
                json.dumps(output, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            ctx.log("topic_score: topics=0 message=信源无新内容")
            return {
                "topics": 0,
                "path": str(topics_path),
                "message": "信源无新内容",
                "benchmark": None,
                "seed": None,
            }

        seed = seed_benchmark(ctx.db, _benchmark_dir(ctx))
        profile = build_platform_profile(
            ctx.db,
            platform=platform,
            as_of=as_of,
            days=days,
            tz=ctx.cfg.tz,
        )
        own = build_own_context(
            ctx.db, platform=platform, profile=profile, tz=ctx.cfg.tz
        )
        if profile.post_count > 0:
            since = profile.as_of
        else:
            latest = latest_candidate_time(candidates, ctx.cfg.tz)
            since = latest.isoformat(timespec="seconds") if latest else None
        scored = score_candidates(
            candidates,
            profile=profile,
            own=own,
            since=since,
            count=count,
            tz=ctx.cfg.tz,
        )
        message = ""
        if not scored:
            message = "对标证据不足：请先导入对标 fixture（wb topic seed）"

        topics = _upsert_topics(ctx, scored, platform=platform) if scored else []
        output = {
            "platform": platform,
            "count": len(topics),
            "benchmark": profile.to_dict(),
            "topics": [topic.model_dump() for topic in topics],
        }
        if message:
            output["message"] = message
        topics_path.write_text(
            json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        ctx.log(
            f"topic_score: topics={len(topics)} "
            f"benchmark_hits={profile.hit_count} as_of={profile.as_of}"
            + (f" message={message}" if message else "")
        )
        return {
            "topics": len(topics),
            "path": str(topics_path),
            "message": message,
            "benchmark": {
                "account": profile.account,
                "hit_count": profile.hit_count,
                "as_of": profile.as_of,
            },
            "seed": seed.summary(),
        }


__all__ = [
    "CANDIDATES_FILE",
    "DEFAULT_COUNT",
    "MAX_COUNT",
    "TOPICS_FILE",
    "TopicScore",
    "TopicSource",
]
