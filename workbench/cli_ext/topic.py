"""``wb topic``：选题建议、对标画像、选题列表与 fixture 导入（T05）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.table import Table

from workbench.config import Config
from workbench.errors import WorkbenchError
from workbench.ids import new_run_id
from workbench.pipeline.runner import run_pipeline
from workbench.store import repo
from workbench.store.db import init_db
from workbench.topics import TopicError
from workbench.topics.benchmark import (
    CONTENT_TYPE_LABELS,
    WINDOW_DAYS,
    BenchmarkProfile,
    build_profile,
    seed_benchmark,
)
from workbench.topics.sources import SOURCE_NAMES, default_benchmark_dir

app = typer.Typer(
    help="选题与爆款对标：热榜/RSS/本地 fixture → 10 条带证据的选题卡",
    no_args_is_help=True,
)


def _fail(exc: WorkbenchError) -> NoReturn:
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=exc.exit_code) from None


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:g}"


def _profile_lines(profile: BenchmarkProfile) -> list[str]:
    duration = profile.duration
    title_length = profile.title_length
    lines = [
        f"对标账号：{profile.account}（平台 {profile.platform}）",
        (
            f"样本：{profile.post_count} 篇（近 {profile.window_days} 天）  "
            f"爆款：{profile.hit_count} 条（burst>={profile.burst_threshold:g}，Top 10%）  "
            f"P50 views：{_fmt(profile.p50_views)}"
        ),
        (
            "时长（秒）："
            + (
                "n=0（该画像没有视频时长样本）"
                if duration.n == 0
                else (
                    f"n={duration.n} 中位 {_fmt(duration.median)} "
                    f"P25–P75 {_fmt(duration.p25)}–{_fmt(duration.p75)} "
                    f"全距 {_fmt(duration.minimum)}–{_fmt(duration.maximum)}"
                )
            )
        ),
        (
            "标题长度（字）："
            + (
                "n=0（该画像没有标题样本）"
                if title_length.n == 0
                else (
                    f"n={title_length.n} 中位 {_fmt(title_length.median)} "
                    f"P25–P75 {_fmt(title_length.p25)}–{_fmt(title_length.p75)} "
                    f"全距 {_fmt(title_length.minimum)}–{_fmt(title_length.maximum)}"
                )
            )
        ),
    ]
    if profile.keywords:
        tags = "、".join(f"{token}×{count}" for token, count in profile.keywords[:5])
        lines.append(f"Top5 标签：{tags}（基于 {profile.hit_count} 条爆款）")
    else:
        lines.append(f"Top5 标签：无（n={profile.hit_count}）")
    slots = "  ".join(
        f"{bucket.key} {bucket.n}（{bucket.share * 100:.1f}%）" for bucket in profile.time_slots
    )
    types = "  ".join(
        f"{CONTENT_TYPE_LABELS.get(bucket.key, bucket.key)} {bucket.n}"
        f"（{bucket.share * 100:.1f}%）"
        for bucket in profile.content_types
    )
    lines.append(f"发布时段（n={profile.hit_count}）：{slots}")
    lines.append(f"内容类型（n={profile.hit_count}）：{types}")
    if profile.top_posts:
        lines.append("Top 爆款/候选：")
        for post in profile.top_posts:
            tag = "爆款" if post.is_hit else "候选"
            lines.append(
                f"  - [{tag}] {post.title}（{post.post_id}，views={post.views}，"
                f"burst={post.burst:.2f}）"
            )
    return lines


@app.command("suggest")
def suggest(
    platform: str | None = typer.Option(None, "--platform", help="平台；默认取 WB_PLATFORM"),
    count: int = typer.Option(10, "--count", min=1, max=50, help="选题条数（默认 10）"),
    offline: str = typer.Option(
        "1",
        "--offline",
        help="1=只读本地 fixture（默认）；0=允许真实 HTTP（需在代码里提供 URL）",
    ),
    out: Path | None = typer.Option(
        None,
        "--out",
        help="输出目录（写 topics.json）；默认 WB_HOME/runs/<run_id>",
    ),
    sources: str = typer.Option(
        "hotlist,rss,local",
        "--sources",
        help="启用的信源，逗号分隔：hotlist,rss,local",
    ),
    hotlist: Path | None = typer.Option(None, "--hotlist", help="热榜 JSON fixture 路径"),
    rss_path: Path | None = typer.Option(None, "--rss", help="RSS fixture 路径"),
    local: str | None = typer.Option(
        None, "--local", help="本地 fixture 文件或目录，逗号分隔；默认 data/seed/benchmark"
    ),
    benchmark_dir: Path | None = typer.Option(
        None, "--benchmark-dir", help="对标 fixture 目录（*.jsonl）"
    ),
    since: str | None = typer.Option(None, "--since", help="只取该时间之后发布的条目"),
    days: int = typer.Option(
        WINDOW_DAYS, "--days", min=1, help="对标画像回看天数（默认 90）"
    ),
    as_of: str | None = typer.Option(
        None, "--as-of", help="画像/时效基准时间；默认取对标库最新采集时间"
    ),
    json_output: bool = typer.Option(False, "--json", help="输出完整 topics.json"),
) -> None:
    """从三个信源生成选题卡：`<out>/topics.json`。"""
    try:
        selected = [part.strip() for part in sources.split(",") if part.strip()]
        unknown = [name for name in selected if name not in SOURCE_NAMES]
        if unknown:
            raise TopicError(
                f"未知信源：{', '.join(unknown)}；可选：{' / '.join(SOURCE_NAMES)}"
            )
        if offline.strip().lower() not in {
            "1", "0", "true", "false", "yes", "no", "on", "off", "y", "n",
        }:
            raise TopicError(f"--offline 只接受 1/0、true/false，收到 {offline!r}")
        cfg = Config.from_env()
        target = Path(out).expanduser() if out is not None else cfg.runs_dir / new_run_id()
        params: dict[str, str] = {
            "count": str(count),
            "offline": offline,
            "sources": sources,
            "days": str(days),
        }
        if platform:
            params["platform"] = platform
        if hotlist:
            params["hotlist"] = str(hotlist)
        if rss_path:
            params["rss"] = str(rss_path)
        if local:
            params["local"] = local
        if benchmark_dir:
            params["benchmark_dir"] = str(benchmark_dir)
        if since:
            params["since"] = since
        if as_of:
            params["as_of"] = as_of
        result = run_pipeline(cfg, "topic", params=params, out=target, log=typer.echo)
    except WorkbenchError as exc:
        _fail(exc)

    topics_path = result.out / "topics.json"
    try:
        payload = json.loads(topics_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        typer.echo(f"读取 {topics_path} 失败：{exc}", err=True)
        raise typer.Exit(code=1) from None
    topics = payload.get("topics") or []
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if not topics:
        typer.echo(payload.get("message") or "信源无新内容")
        typer.echo(f"topics=0 out={topics_path}")
        return

    table = Table(title=f"本周选题（{len(topics)} 条）")
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("score", justify="right", no_wrap=True)
    table.add_column("heat", justify="right", no_wrap=True)
    table.add_column("fit", justify="right", no_wrap=True)
    table.add_column("bench", justify="right", no_wrap=True)
    table.add_column("timing", justify="right", no_wrap=True)
    table.add_column("source", no_wrap=True)
    table.add_column("标题", max_width=42)
    for index, topic in enumerate(topics, start=1):
        parts = topic.get("score_parts") or {}
        table.add_row(
            str(index),
            f"{float(topic.get('score', 0)):.3f}",
            f"{float(parts.get('heat', 0)):.2f}",
            f"{float(parts.get('fit', 0)):.2f}",
            f"{float(parts.get('benchmark', 0)):.2f}",
            f"{float(parts.get('timing', 0)):.2f}",
            str(topic.get("source") or "-"),
            str(topic.get("title") or ""),
        )
    Console().print(table)
    typer.echo(f"topics={len(topics)} out={topics_path}")


@app.command("benchmark")
def benchmark(
    account: str = typer.Option(..., "--account", help="对标账号 handle，例如 demo_bench_a"),
    platform: str | None = typer.Option(None, "--platform", help="平台；默认取 WB_PLATFORM"),
    benchmark_dir: Path | None = typer.Option(
        None, "--benchmark-dir", help="对标 fixture 目录（*.jsonl）"
    ),
    days: int = typer.Option(WINDOW_DAYS, "--days", min=1, help="回看天数（默认 90）"),
    as_of: str | None = typer.Option(None, "--as-of", help="画像基准时间"),
    out: Path | None = typer.Option(None, "--out", help="把画像写入该目录的 profile.json"),
    json_output: bool = typer.Option(False, "--json", help="输出结构化画像 JSON"),
) -> None:
    """输出某个对标账号的爆款画像（每项带样本数）。"""
    conn = None
    try:
        cfg = Config.from_env()
        cfg.ensure_dirs()
        conn = init_db(cfg.db_path)
        seed_benchmark(conn, benchmark_dir or default_benchmark_dir())
        profile = build_profile(
            conn,
            account,
            platform=platform or cfg.platform,
            as_of=as_of,
            days=days,
            tz=cfg.tz,
        )
    except WorkbenchError as exc:
        _fail(exc)
    finally:
        if conn is not None:
            conn.close()

    payload = profile.to_dict()
    if out is not None:
        target = Path(out).expanduser().resolve()
        target.mkdir(parents=True, exist_ok=True)
        (target / "profile.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    for line in _profile_lines(profile):
        typer.echo(line)
    if out is not None:
        typer.echo(f"profile={Path(out).expanduser().resolve() / 'profile.json'}")


@app.command("list")
def list_topics(
    platform: str | None = typer.Option(None, "--platform", help="平台；默认取 WB_PLATFORM"),
    status: str | None = typer.Option(None, "--status", help="new / picked / used / dropped"),
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="最多显示条数"),
    json_output: bool = typer.Option(False, "--json", help="输出结构化 JSON"),
) -> None:
    """按分数降序查看已落库的选题。"""
    conn = None
    try:
        cfg = Config.from_env()
        cfg.ensure_dirs()
        conn = init_db(cfg.db_path)
        topics = repo.list_topics(conn, platform=platform or cfg.platform, status=status)
    except WorkbenchError as exc:
        _fail(exc)
    finally:
        if conn is not None:
            conn.close()
    topics.sort(key=lambda topic: (-topic.score, topic.title))
    topics = topics[:limit]
    if json_output:
        typer.echo(
            json.dumps(
                [topic.model_dump() for topic in topics], ensure_ascii=False, indent=2
            )
        )
        return
    if not topics:
        typer.echo("暂无选题；先运行 wb topic suggest")
        return
    table = Table(title=f"选题库（{len(topics)} 条）")
    table.add_column("score", justify="right", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("source", no_wrap=True)
    table.add_column("id", no_wrap=True)
    table.add_column("标题", max_width=48)
    for topic in topics:
        table.add_row(
            f"{topic.score:.3f}",
            topic.status,
            topic.source,
            topic.id,
            topic.title,
        )
    Console().print(table)


@app.command("seed")
def seed(
    benchmark_dir: Path | None = typer.Option(
        None, "--benchmark-dir", help="对标 fixture 目录（*.jsonl）"
    ),
) -> None:
    """把 data/seed/benchmark 的对标 fixture 幂等导入 account / post / metric。"""
    conn = None
    try:
        cfg = Config.from_env()
        cfg.ensure_dirs()
        conn = init_db(cfg.db_path)
        result = seed_benchmark(conn, benchmark_dir or default_benchmark_dir())
    except WorkbenchError as exc:
        _fail(exc)
    finally:
        if conn is not None:
            conn.close()
    typer.echo(result.summary())


__all__ = ["app"]
