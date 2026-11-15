"""选题打分（T05）。

四个分项各自归一化到 0–1，再按固定权重加权：

``score = 0.35*heat + 0.25*fit + 0.25*benchmark + 0.15*timing``

- ``heat``：信源热度，``ln(1+h) / ln(1+100000)``；缺失按 0.3 中性值。
- ``fit``：与自己的定位 / 历史高表现作品契合度（jieba 词集 Jaccard）。
- ``benchmark``：与对标画像的标题长度、关键词、内容类型、时长区间匹配度。
- ``timing``：``0.5 ** (age_hours / 36)``；``since`` 或发布时间缺失时 age 按 0。

全部计算确定性：无随机数、不调 LLM；分数与分项都保留 6 位小数。
"""

from __future__ import annotations

import hashlib
import math
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from workbench.metrics.compute import latest_metric, parse_iso, quantile
from workbench.store import repo
from workbench.topics.benchmark import (
    CONTENT_TYPE_LABELS,
    BenchmarkProfile,
    HitPost,
    RangeStat,
    tokenize,
)
from workbench.topics.sources import Candidate

WEIGHTS: dict[str, float] = {
    "heat": 0.35,
    "fit": 0.25,
    "benchmark": 0.25,
    "timing": 0.15,
}
HALF_LIFE_HOURS = 36.0
HEAT_SCALE = 100_000.0
DEFAULT_HEAT = 0.3
MAX_EVIDENCE = 5
MIN_EVIDENCE = 3


# --------------------------------------------------------------------------- 分项
def heat_score(heat: float | None) -> float:
    """热榜热度 → 0–1；缺失用中性值 0.3，非正数按 0。"""
    if heat is None:
        return DEFAULT_HEAT
    if heat <= 0:
        return 0.0
    return round(min(1.0, math.log1p(heat) / math.log1p(HEAT_SCALE)), 6)


def timing_score(
    published_at: str | None, *, since: str | None, tz: str = "Asia/Shanghai"
) -> float:
    """半衰期时效分：36 小时衰减一半；``since``/发布时间缺失时 age=0。"""
    if not published_at or not since:
        return 1.0
    try:
        reference = parse_iso(since, tz)
        published = parse_iso(published_at, tz)
    except Exception:  # noqa: BLE001 - 单条坏时间按 age=0 处理
        return 1.0
    age_hours = max(0.0, (reference - published).total_seconds() / 3600.0)
    return round(0.5 ** (age_hours / HALF_LIFE_HOURS), 6)


def _range_component(value: float | None, stat: RangeStat, *, neutral: float = 0.5) -> float:
    if value is None or stat.n == 0:
        return neutral
    if stat.p25 is not None and stat.p75 is not None and stat.p25 <= value <= stat.p75:
        return 1.0
    if stat.minimum is not None and stat.maximum is not None:
        if stat.minimum <= value <= stat.maximum:
            return 0.7
    return 0.3


def benchmark_score(candidate: Candidate, profile: BenchmarkProfile) -> float:
    """候选与爆款画像的匹配度（0–1）。"""
    tokens = set(tokenize(candidate.text()))
    keyword_tokens = {token for token, _count in profile.keywords}
    tag_component = (
        min(1.0, len(tokens & keyword_tokens) / 2.0) if keyword_tokens else 0.5
    )
    title_component = _range_component(
        float(len(candidate.title.strip())), profile.title_length
    )
    top_type = profile.top_content_type()
    if candidate.content_type is None or top_type is None:
        content_component = 0.5
    elif candidate.content_type == top_type:
        content_component = 1.0
    else:
        content_component = 0.4
    if candidate.content_type == "video":
        duration_component = _range_component(candidate.duration_sec, profile.duration)
    else:
        duration_component = 0.5
    return round(
        0.30 * title_component
        + 0.35 * tag_component
        + 0.20 * content_component
        + 0.15 * duration_component,
        6,
    )


# --------------------------------------------------------------------------- 自身画像
@dataclass(slots=True)
class OwnContext:
    """自己的定位与历史高表现作品，用于 ``fit`` 分项。"""

    account: str | None
    niche_tokens: frozenset[str] = field(default_factory=frozenset)
    history_tokens: frozenset[str] = field(default_factory=frozenset)
    history_posts: int = 0
    fallback: bool = False


def build_own_context(
    conn: sqlite3.Connection,
    *,
    platform: str,
    profile: BenchmarkProfile,
    tz: str = "Asia/Shanghai",
) -> OwnContext:
    """找自己的账号，提取定位词与历史高表现标签；没有账号时用画像关键词兜底。"""
    accounts = [
        account
        for account in repo.list_accounts(conn, platform=platform)
        if not account.is_benchmark
    ]
    accounts.sort(key=lambda account: account.id)
    if not accounts:
        # 没有自己的账号时，用平台内对标账号的赛道词兜底（不是画像关键词）。
        fallback_tokens: set[str] = set()
        for benchmark in repo.list_accounts(conn, platform=platform, is_benchmark=True):
            fallback_tokens.update(tokenize(benchmark.niche or ""))
        if not fallback_tokens:
            for token, _count in profile.keywords:
                fallback_tokens.add(token)
        return OwnContext(
            account=None,
            niche_tokens=frozenset(fallback_tokens),
            history_tokens=frozenset(),
            history_posts=0,
            fallback=True,
        )

    account = accounts[0]
    niche_tokens = frozenset(tokenize(account.niche or ""))
    posts = repo.list_posts(conn, account_id=account.id)
    views: list[float] = []
    metric_by_post: dict[str, float] = {}
    for post in posts:
        metric = latest_metric(conn, post.id, tz)
        if metric is not None and metric.views is not None:
            metric_by_post[post.id] = float(metric.views)
            views.append(float(metric.views))
    median = quantile(views, 0.5)
    if median is None:
        selected = posts
    else:
        selected = [post for post in posts if metric_by_post.get(post.id, 0.0) >= median]
    history_tokens: set[str] = set()
    for post in selected:
        history_tokens.update(tokenize(" ".join(post.topic_tags)))
    return OwnContext(
        account=account.handle,
        niche_tokens=niche_tokens,
        history_tokens=frozenset(history_tokens),
        history_posts=len(selected),
    )


def _coverage(tokens: Iterable[str], reference: Iterable[str]) -> float:
    """候选覆盖了多少「自己的定位/历史标签」；比 Jaccard 更不受长标题稀释。"""
    reference_set = set(reference)
    if not reference_set:
        return 0.0
    return len(set(tokens) & reference_set) / len(reference_set)


def fit_score(candidate: Candidate, own: OwnContext) -> float:
    tokens = tokenize(candidate.text())
    niche_similarity = _coverage(tokens, own.niche_tokens)
    if own.history_tokens:
        history_similarity = _coverage(tokens, own.history_tokens)
    else:
        history_similarity = niche_similarity
    return round(0.6 * niche_similarity + 0.4 * history_similarity, 6)


# --------------------------------------------------------------------------- 选题卡
@dataclass(slots=True)
class ScoredCandidate:
    candidate: Candidate
    parts: dict[str, float]
    score: float
    angle: str
    hook: str
    evidence: list[dict[str, str]] = field(default_factory=list)


def _clip(text: str, limit: int = 24) -> str:
    value = text.strip().rstrip("。！？!?")
    return value if len(value) <= limit else value[:limit] + "…"


def _fallback_focus(words: list[str], exclude: str) -> str:
    """挑一个不与信号词重复、信息量更大的落点。"""
    candidates = [word for word in words if word != exclude]
    return max(candidates, key=lambda word: (len(word), word)) if candidates else exclude


def _signals(candidate: Candidate, profile: BenchmarkProfile) -> tuple[str, str]:
    """从候选自身与画像里挑一个「信号词」和一个「落点」，避免角度千篇一律。"""
    words = list(dict.fromkeys(tokenize(candidate.text())))
    word_set = set(words)
    matched = next(
        (token for token, _count in profile.keywords if token in word_set), None
    )
    tag = candidate.tags[0] if candidate.tags else None
    if tag:
        signal = matched or tag
        return signal, _fallback_focus(words, signal)
    if matched:
        return matched, _fallback_focus(words, matched)
    title_words = list(dict.fromkeys(tokenize(candidate.title)))
    if title_words:
        signal = title_words[0]
        return signal, _fallback_focus(title_words, signal)
    clipped = _clip(candidate.title, 22)
    return _clip(candidate.title, 12), clipped


def _variant(text: str, count: int) -> int:
    """按标题的 sha1 稳定选模板；不用 Python 内置 hash（逐进程随机）。"""
    return int(hashlib.sha1(text.encode()).hexdigest(), 16) % count


def make_angle(candidate: Candidate, profile: BenchmarkProfile) -> str:
    """用固定模板生成角度（真实文案由 T06 负责，这里只给方向）。"""
    signal, focus = _signals(candidate, profile)
    variant = _variant(f"angle|{candidate.title}", 3)
    if candidate.content_type:
        label = CONTENT_TYPE_LABELS.get(candidate.content_type, candidate.content_type)
        duration = (
            f"{int(round(candidate.duration_sec))} 秒"
            if candidate.duration_sec
            else "对标时长"
        )
        templates = (
            f"对标{label}爆款结构（{duration}），围绕「{signal}」"
            f"把「{focus}」拆成 3 个可执行步骤",
            f"复刻{label}爆款的{signal}题材，用 {duration} 讲清「{focus}」的 3 个做法",
            f"参考{label}爆款的节奏（{duration}），从「{signal}」切入给「{focus}」一份清单",
        )
        return templates[variant]
    templates = (
        f"借热榜信号「{signal}」，把「{focus}」拆成 3 个可执行动作",
        f"从「{signal}」这个热榜词切入，用「{focus}」做一篇带模板的实操",
        f"先回答「{focus}」，再用「{signal}」做一层延伸",
    )
    return templates[variant]


def make_hook(candidate: Candidate) -> str:
    title = _clip(candidate.title, 30)
    style = "video" if candidate.content_type == "video" else "text"
    templates = (
        f"从一个真实场景切入：{title}。先给结论，再列 3 个可执行动作，结尾留一个互动问题。",
        f"先抛一个反常识结论：{title}。正文给 3 个证据，结尾请读者对号入座。",
        f"用一句读者熟悉的吐槽开场：{title}。再给 3 个今天就做的动作。",
        f"开头先讲一个失败案例：{title}。然后给出 3 步修正方案。",
    )
    index = _variant(f"hook|{style}|{candidate.title}", len(templates))
    return templates[index]


def build_evidence(
    candidate: Candidate, profile: BenchmarkProfile
) -> list[dict[str, str]]:
    """选题证据：信源本身 + 对标爆款/样本作品（ref 为真实 id 或 URL）。"""
    if candidate.kind == "hotlist":
        note = f"热榜信源：{candidate.summary or candidate.title}"
    elif candidate.kind == "rss":
        note = f"RSS 信源：{candidate.summary or candidate.title}"
    else:
        views = f"views={int(candidate.views)}，" if candidate.views is not None else ""
        note = f"对标作品：{candidate.title}（{views}来自本地 fixture）"
    evidence: list[dict[str, str]] = [
        {"kind": candidate.kind, "ref": candidate.ref, "note": note}
    ]
    candidate_tokens = set(tokenize(candidate.text()))

    def relevance(post: HitPost) -> tuple[int, int, float, str]:
        overlap = len(candidate_tokens & set(tokenize(f"{post.title} {' '.join(post.tags)}")))
        # 先看与候选的词重叠，再看是否爆款、burst；最后用 id 保证确定性。
        return (-overlap, -int(post.is_hit), -post.burst, post.post_id)

    for post in sorted(profile.top_posts, key=relevance):
        if len(evidence) >= MAX_EVIDENCE:
            break
        if post.post_id == candidate.post_id:
            continue
        label = "对标爆款" if post.is_hit else "对标候选"
        evidence.append(
            {
                "kind": "post",
                "ref": post.post_id,
                "note": (
                    f"{label}：{post.title}（views={post.views}，"
                    f"burst={post.burst:.2f}，{post.content_type}）"
                ),
            }
        )
    return evidence


def score_candidates(
    candidates: Sequence[Candidate],
    *,
    profile: BenchmarkProfile,
    own: OwnContext,
    since: str | None,
    count: int,
    tz: str = "Asia/Shanghai",
) -> list[ScoredCandidate]:
    """打分、去重、按分数降序取前 ``count`` 条；同分用标题与 ref 稳定排序。"""
    scored: list[ScoredCandidate] = []
    seen_refs: set[str] = set()
    seen_titles: set[str] = set()
    for candidate in candidates:
        title = candidate.title.strip()
        if not title or not candidate.ref:
            continue
        ref_key = candidate.ref
        title_key = re.sub(r"\s+", "", title).casefold()
        if ref_key in seen_refs or title_key in seen_titles:
            continue
        seen_refs.add(ref_key)
        seen_titles.add(title_key)
        parts = {
            "heat": heat_score(candidate.heat),
            "fit": fit_score(candidate, own),
            "benchmark": benchmark_score(candidate, profile),
            "timing": timing_score(candidate.published_at, since=since, tz=tz),
        }
        total = round(sum(WEIGHTS[name] * value for name, value in parts.items()), 6)
        evidence = build_evidence(candidate, profile)
        if len(evidence) < MIN_EVIDENCE:
            # 契约要求每张选题卡至少 3 条可核对证据；不足时不产出。
            continue
        scored.append(
            ScoredCandidate(
                candidate=candidate,
                parts=parts,
                score=total,
                angle=make_angle(candidate, profile),
                hook=make_hook(candidate),
                evidence=evidence,
            )
        )
    scored.sort(
        key=lambda item: (-item.score, item.candidate.title, item.candidate.ref)
    )
    return scored[: max(0, int(count))]


def latest_candidate_time(candidates: Sequence[Candidate], tz: str) -> datetime | None:
    """候选里最新的发布时间，作为没有对标快照时的时效基准。"""
    times = []
    for candidate in candidates:
        if not candidate.published_at:
            continue
        try:
            times.append(parse_iso(candidate.published_at, tz))
        except Exception:  # noqa: BLE001
            continue
    return max(times) if times else None


__all__ = [
    "DEFAULT_HEAT",
    "HALF_LIFE_HOURS",
    "HEAT_SCALE",
    "MAX_EVIDENCE",
    "MIN_EVIDENCE",
    "OwnContext",
    "ScoredCandidate",
    "WEIGHTS",
    "benchmark_score",
    "build_evidence",
    "build_own_context",
    "fit_score",
    "heat_score",
    "latest_candidate_time",
    "make_angle",
    "make_hook",
    "score_candidates",
    "timing_score",
]
