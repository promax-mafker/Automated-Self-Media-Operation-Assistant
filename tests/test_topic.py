"""T05 选题与爆款对标：适配器解析、画像、打分、CLI 与边界。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from workbench.cli import app
from workbench.config import Config
from workbench.contracts import get_source_adapters
from workbench.store import repo
from workbench.store.db import init_db
from workbench.store.models import Account, Metric, Post
from workbench.topics import TopicError
from workbench.topics.benchmark import (
    build_platform_profile,
    build_profile,
    burst_ratio,
    seed_benchmark,
)
from workbench.topics.score import (
    WEIGHTS,
    build_own_context,
    heat_score,
    score_candidates,
    timing_score,
)
from workbench.topics.sources import (
    HotlistSource,
    LocalSource,
    RssSource,
    build_adapters,
)

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data" / "seed"
BENCHMARK = SEED / "benchmark"


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("WB_HOME", str(home))
    monkeypatch.setenv("WB_LLM", "stub")
    monkeypatch.delenv("WB_LLM_API_KEY", raising=False)
    return home


def _invoke(args: list[str]) -> Result:
    return CliRunner().invoke(app, args)


def _combined(result: Result) -> str:
    text = result.output
    stderr = getattr(result, "stderr", "")
    return text + stderr if stderr else text


def _suggest_args(out: Path, *, count: int = 10) -> list[str]:
    return [
        "topic",
        "suggest",
        "--platform",
        "xhs",
        "--count",
        str(count),
        "--offline",
        "1",
        "--out",
        str(out),
        "--sources",
        "hotlist,rss,local",
        "--hotlist",
        str(SEED / "hotlist_sample.json"),
        "--rss",
        str(SEED / "rss_sample.xml"),
        "--local",
        str(BENCHMARK),
        "--benchmark-dir",
        str(BENCHMARK),
    ]


# --------------------------------------------------------------------------- 信源
def test_hotlist_adapter_parses_dailyhot_shape() -> None:
    adapter = HotlistSource(
        path=SEED / "hotlist_sample.json", offline=True, platform="xhs"
    )
    items = adapter.fetch()
    assert len(items) == 18
    assert {item.source for item in items} == {"hotlist"}
    assert all(item.title for item in items)
    assert items[0].heat == pytest.approx(51800)
    assert items[0].published_at is not None
    assert items[0].published_at.endswith("+08:00")
    assert (items[0].url or "").startswith("https://example.invalid/hot/xhs/")

    filtered = adapter.fetch(since="2026-11-15T09:30:00+08:00")
    assert 0 < len(filtered) < len(items)
    assert all(
        item.published_at is not None and item.published_at >= "2026-11-15T09:30:00"
        for item in filtered
    )


def test_hotlist_adapter_tolerates_missing_fields(tmp_path: Path) -> None:
    fixture = tmp_path / "hot.json"
    fixture.write_text(
        json.dumps(
            {
                "code": 200,
                "data": [
                    {"title": "只有标题"},
                    {
                        "name": "用 name 字段",
                        "hot": "1.2万",
                        "url": "https://example.invalid/hot/a",
                        "timestamp": 1763200000000,
                    },
                    {"desc": "没有标题，应跳过"},
                    {"title": "坏时间", "timestamp": "not-a-time"},
                    {"title": ""},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    items = HotlistSource(path=fixture, offline=True).fetch()
    assert [item.title for item in items] == ["只有标题", "用 name 字段", "坏时间"]
    assert items[1].heat == pytest.approx(12000)
    assert items[1].published_at is not None
    assert items[0].published_at is None
    assert items[2].published_at is None

    root_list = tmp_path / "root.json"
    root_list.write_text(
        json.dumps(
            [{"title": "根数组形状", "url": "https://example.invalid/r"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    assert [item.title for item in HotlistSource(path=root_list).fetch()] == ["根数组形状"]


def test_rss_adapter_parses_feed_and_strips_html() -> None:
    items = RssSource(path=SEED / "rss_sample.xml", offline=True, platform="xhs").fetch()
    assert len(items) == 12
    assert {item.source for item in items} == {"rss"}
    assert all(item.title for item in items)
    assert all(item.url and item.url.startswith("https://example.invalid/rss/") for item in items)
    assert items[0].published_at is not None
    assert items[0].published_at.endswith("+08:00")
    assert items[0].summary is not None
    assert "<p>" not in items[0].summary
    assert "输出" in items[0].tags

    filtered = RssSource(path=SEED / "rss_sample.xml", offline=True).fetch(
        since="2026-11-12T00:00:00+08:00"
    )
    assert 0 < len(filtered) < len(items)


def test_rss_adapter_tolerates_missing_fields(tmp_path: Path) -> None:
    fixture = tmp_path / "feed.xml"
    fixture.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>测试</title><link>https://example.invalid</link>
<item><title>只有标题的条目</title></item>
<item><title>带 HTML 摘要</title><link>https://example.invalid/x</link>
<description><![CDATA[<p>摘要<strong>正文</strong></p>]]></description></item>
</channel></rss>
""",
        encoding="utf-8",
    )
    items = RssSource(path=fixture, offline=True).fetch()
    assert [item.title for item in items] == ["只有标题的条目", "带 HTML 摘要"]
    assert items[0].published_at is None
    assert items[1].summary == "摘要 正文"


def test_local_adapter_reads_benchmark_jsonl() -> None:
    adapter = LocalSource([BENCHMARK], platform="xhs")
    items = adapter.fetch()
    assert len(items) == 75
    candidates = adapter.fetch_candidates()
    assert len(candidates) == 75
    assert {candidate.source for candidate in candidates} == {"benchmark"}
    assert {candidate.kind for candidate in candidates} == {"post"}
    first = candidates[0]
    assert first.post_id == "post_bench_a_001"
    assert first.content_type == "video"
    assert first.views == pytest.approx(1450)
    assert first.adapter == "local"
    assert first.ref == "post_bench_a_001"


def test_local_adapter_accepts_json_list_and_missing_fields(tmp_path: Path) -> None:
    fixture = tmp_path / "local.json"
    fixture.write_text(
        json.dumps(
            [
                {"title": "本地选题 A", "post_id": "local_a"},
                {
                    "post": {
                        "title": "本地选题 B",
                        "id": "local_b",
                        "content_type": "video",
                        "duration_sec": 30,
                        "topic_tags": ["效率", "复盘"],
                        "published_at": "2026-11-01T08:00:00+08:00",
                    }
                },
                {"desc": "没有标题，应跳过"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    candidates = LocalSource([fixture]).fetch_candidates()
    assert [candidate.title for candidate in candidates] == ["本地选题 A", "本地选题 B"]
    assert candidates[0].post_id == "local_a"
    assert candidates[1].content_type == "video"
    assert candidates[1].tags == ["效率", "复盘"]


def test_build_adapters_and_lazy_factory(cfg: Config) -> None:
    adapters = build_adapters(cfg)
    assert [adapter.name for adapter in adapters] == ["hotlist", "rss", "local"]
    lazy = get_source_adapters(cfg)
    assert [adapter.name for adapter in lazy] == ["hotlist", "rss", "local"]


def test_benchmark_fixtures_are_fictional_and_complete() -> None:
    files = sorted(BENCHMARK.glob("*.jsonl"))
    assert [path.name for path in files] == [
        "demo_bench_a.jsonl",
        "demo_bench_b.jsonl",
        "demo_bench_c.jsonl",
    ]
    handles: set[str] = set()
    for path in files:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert 20 <= len(lines) <= 30
        for line in lines:
            record = json.loads(line)
            account = record["account"]
            handle = account["handle"]
            handles.add(handle)
            assert handle.startswith("demo_bench_")
            assert "虚构" in account["display_name"]
            assert "虚构" in account["note"]
            assert record["post"]["caption"].startswith("占位文案")
            assert record["metric"]["captured_at"] == "2026-11-10T10:00:00+08:00"
    assert handles == {"demo_bench_a", "demo_bench_b", "demo_bench_c"}


# --------------------------------------------------------------------------- 画像
def test_seed_and_profiles_have_sample_counts(conn: sqlite3.Connection) -> None:
    first = seed_benchmark(conn, BENCHMARK)
    assert (first.accounts, first.posts, first.metrics) == (3, 75, 75)
    second = seed_benchmark(conn, BENCHMARK)
    assert (second.accounts, second.posts, second.metrics) == (0, 0, 0)
    assert second.skipped == 150

    profile = build_profile(conn, "demo_bench_a", platform="xhs")
    assert profile.post_count == 25
    assert profile.hit_count == 3
    assert profile.p50_views == pytest.approx(1310)
    assert profile.as_of == "2026-11-10T10:00:00+08:00"
    assert profile.duration.n == 3
    assert profile.title_length.n == 3
    assert 1 <= len(profile.keywords) <= 10
    assert sum(bucket.n for bucket in profile.time_slots) == 3
    assert sum(bucket.n for bucket in profile.content_types) == 3
    assert profile.top_posts
    for post in profile.top_posts:
        assert repo.get_post(conn, post.post_id) is not None
    assert all(post.is_hit for post in profile.top_posts[:3])

    aggregate = build_platform_profile(conn, platform="xhs")
    assert aggregate.account == "all"
    assert aggregate.post_count == 75
    assert aggregate.hit_count == 9
    assert aggregate.top_posts
    assert sum(bucket.n for bucket in aggregate.time_slots) == 9
    assert sum(bucket.n for bucket in aggregate.content_types) == 9

    with pytest.raises(TopicError, match="对标账号不存在"):
        build_profile(conn, "demo_bench_missing", platform="xhs")


def test_burst_threshold_and_top_ten_percent(conn: sqlite3.Connection) -> None:
    created = "2026-11-10T10:00:00+08:00"
    account = repo.insert_account(
        conn,
        Account(
            id="acct_control",
            platform="xhs",
            handle="demo_control",
            display_name="控制组（虚构）",
            is_benchmark=True,
            niche="测试",
            note="测试用虚构账号",
            created_at=created,
        ),
    )
    views = [200, 150, 140] + [100] * 17
    for index, value in enumerate(views, start=1):
        post_id = f"post_control_{index:03d}"
        repo.insert_post(
            conn,
            Post(
                id=post_id,
                account_id=account.id,
                platform="xhs",
                title=f"控制样本 {index}",
                url=f"https://example.invalid/control/{index:03d}",
                published_at="2026-10-01T08:00:00+08:00",
                content_type="video",
                topic_tags=[],
                duration_sec=30,
                caption=None,
                created_at=created,
            ),
        )
        repo.insert_metric(
            conn,
            Metric(
                id=f"metric_control_{index:03d}",
                post_id=post_id,
                captured_at=created,
                views=value,
            ),
        )

    assert burst_ratio(150, 100) == pytest.approx(1.5)
    profile = build_profile(
        conn,
        "demo_control",
        platform="xhs",
        as_of=created,
        threshold=1.5,
        top_pct=0.1,
    )
    assert profile.post_count == 20
    assert profile.p50_views == pytest.approx(100)
    assert profile.hit_count == 2
    hit_ids = {post.post_id for post in profile.top_posts if post.is_hit}
    assert hit_ids == {"post_control_001", "post_control_002"}
    excluded = next(
        post for post in profile.top_posts if post.post_id == "post_control_003"
    )
    assert excluded.is_hit is False
    assert excluded.burst == pytest.approx(1.4)


def test_empty_benchmark_profile_is_zero(conn: sqlite3.Connection) -> None:
    profile = build_platform_profile(conn, platform="xhs")
    assert profile.account == "all"
    assert profile.post_count == 0
    assert profile.hit_count == 0
    assert profile.keywords == []
    assert profile.top_posts == []


# --------------------------------------------------------------------------- 打分
def test_timing_decay_half_life() -> None:
    reference = "2026-11-15T00:00:00+08:00"
    stamp = datetime.fromisoformat(reference)
    assert timing_score(reference, since=reference) == pytest.approx(1.0, abs=0.01)
    for hours, expected in ((36, 0.5), (72, 0.25)):
        published = (stamp - timedelta(hours=hours)).isoformat()
        assert timing_score(published, since=reference) == pytest.approx(expected, abs=0.01)
    assert timing_score(None, since=reference) == pytest.approx(1.0)
    assert timing_score(reference, since=None) == pytest.approx(1.0)


def test_heat_score_is_bounded_and_monotonic() -> None:
    assert heat_score(None) == pytest.approx(0.3)
    assert heat_score(0) == pytest.approx(0.0)
    low = heat_score(1_000)
    high = heat_score(50_000)
    assert 0 < low < high <= 1.0
    assert heat_score(1_000_000) == pytest.approx(1.0)


def test_score_candidates_is_deterministic_and_exactly_count(
    conn: sqlite3.Connection,
) -> None:
    seed_benchmark(conn, BENCHMARK)
    profile = build_platform_profile(conn, platform="xhs")
    candidates = LocalSource([BENCHMARK], platform="xhs").fetch_candidates()
    own = build_own_context(conn, platform="xhs", profile=profile)
    first = score_candidates(
        candidates, profile=profile, own=own, since=profile.as_of, count=10
    )
    second = score_candidates(
        candidates, profile=profile, own=own, since=profile.as_of, count=10
    )
    assert [item.score for item in first] == [item.score for item in second]
    assert len(first) == 10
    for item in first:
        assert set(item.parts) == set(WEIGHTS)
        assert all(0.0 <= value <= 1.0 for value in item.parts.values())
        expected = round(sum(WEIGHTS[name] * value for name, value in item.parts.items()), 6)
        assert item.score == pytest.approx(expected)
        assert item.angle and item.hook
        assert len(item.evidence) >= 3


# --------------------------------------------------------------------------- CLI
def test_topic_suggest_is_deterministic_and_evidenced(
    cli_env: Path, tmp_path: Path
) -> None:
    first_out = tmp_path / "first"
    result = _invoke(_suggest_args(first_out))
    assert result.exit_code == 0, _combined(result)
    payload = json.loads((first_out / "topics.json").read_text(encoding="utf-8"))
    assert payload["count"] == 10
    assert len(payload["topics"]) == 10

    candidates = json.loads((first_out / "candidates.json").read_text(encoding="utf-8"))
    assert candidates["sources"] == {"hotlist": 18, "rss": 12, "local": 75}

    connection = init_db(cli_env / "wb.sqlite3")
    try:
        for topic in payload["topics"]:
            assert set(topic["score_parts"]) == set(WEIGHTS)
            assert all(0.0 <= value <= 1.0 for value in topic["score_parts"].values())
            assert topic["title"] and topic["angle"] and topic["hook"]
            assert len(topic["evidence"]) >= 3
            assert topic["source"] in {"hotlist", "rss", "benchmark"}
            for evidence in topic["evidence"]:
                assert evidence["kind"] in {"hotlist", "rss", "post"}
                ref = evidence["ref"]
                assert ref
                if ref.startswith("post_"):
                    assert repo.get_post(connection, ref) is not None
                else:
                    assert ref.startswith("https://example.invalid/")
        assert repo.list_topics(connection, platform="xhs")
    finally:
        connection.close()

    first_scores = [topic["score"] for topic in payload["topics"]]
    first_ids = [topic["id"] for topic in payload["topics"]]
    second_out = tmp_path / "second"
    second = _invoke(_suggest_args(second_out))
    assert second.exit_code == 0, _combined(second)
    second_payload = json.loads((second_out / "topics.json").read_text(encoding="utf-8"))
    assert [topic["score"] for topic in second_payload["topics"]] == first_scores
    assert [topic["id"] for topic in second_payload["topics"]] == first_ids

    connection = sqlite3.connect(cli_env / "wb.sqlite3")
    try:
        count = connection.execute("SELECT COUNT(*) FROM topic").fetchone()[0]
        duplicates = connection.execute(
            "SELECT platform, source_ref, COUNT(*) FROM topic "
            "GROUP BY platform, source_ref HAVING COUNT(*) > 1"
        ).fetchall()
    finally:
        connection.close()
    assert count == 10
    assert duplicates == []


def test_topic_suggest_empty_source_exits_zero(
    cli_env: Path, tmp_path: Path
) -> None:
    empty_hotlist = tmp_path / "empty_hot.json"
    empty_hotlist.write_text('{"code": 200, "data": []}\n', encoding="utf-8")
    out = tmp_path / "empty"
    result = _invoke(
        [
            "topic",
            "suggest",
            "--platform",
            "xhs",
            "--count",
            "10",
            "--offline",
            "1",
            "--sources",
            "hotlist",
            "--hotlist",
            str(empty_hotlist),
            "--benchmark-dir",
            str(BENCHMARK),
            "--out",
            str(out),
        ]
    )
    output = _combined(result)
    assert result.exit_code == 0, output
    assert "信源无新内容" in output
    payload = json.loads((out / "topics.json").read_text(encoding="utf-8"))
    assert payload["topics"] == []
    assert payload["message"] == "信源无新内容"


def test_topic_benchmark_cli_reports_samples(cli_env: Path, tmp_path: Path) -> None:
    out = tmp_path / "profile"
    result = _invoke(
        [
            "topic",
            "benchmark",
            "--account",
            "demo_bench_a",
            "--platform",
            "xhs",
            "--benchmark-dir",
            str(BENCHMARK),
            "--out",
            str(out),
        ]
    )
    output = _combined(result)
    assert result.exit_code == 0, output
    for expected in ("时长（秒）", "标题长度（字）", "Top5 标签", "发布时段", "内容类型", "n=3"):
        assert expected in output
    profile = json.loads((out / "profile.json").read_text(encoding="utf-8"))
    assert profile["hit_count"] == 3
    assert profile["duration"]["n"] == 3
    assert profile["title_length"]["n"] == 3
    assert 5 <= len(profile["keywords"]) <= 10
    assert sum(bucket["n"] for bucket in profile["time_slots"]) == 3
    assert sum(bucket["n"] for bucket in profile["content_types"]) == 3

    missing = _invoke(
        [
            "topic",
            "benchmark",
            "--account",
            "demo_bench_missing",
            "--platform",
            "xhs",
            "--benchmark-dir",
            str(BENCHMARK),
        ]
    )
    assert missing.exit_code == 2
    assert "对标账号不存在" in _combined(missing)


def test_topic_help_list_and_run_pipeline(cli_env: Path, tmp_path: Path) -> None:
    root_help = _combined(_invoke(["--help"]))
    assert "topic" in root_help
    topic_help = _combined(_invoke(["topic", "--help"]))
    for command in ("suggest", "benchmark", "list", "seed"):
        assert command in topic_help

    out = tmp_path / "run"
    result = _invoke(
        [
            "run",
            "topic",
            "--input",
            "platform=xhs",
            "--input",
            "count=10",
            "--input",
            "offline=1",
            "--input",
            f"hotlist={SEED / 'hotlist_sample.json'}",
            "--input",
            f"rss={SEED / 'rss_sample.xml'}",
            "--input",
            f"local={BENCHMARK}",
            "--input",
            f"benchmark_dir={BENCHMARK}",
            "--out",
            str(out),
        ]
    )
    assert result.exit_code == 0, _combined(result)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert [stage["name"] for stage in manifest["stages"]] == [
        "topic_source",
        "topic_score",
    ]
    assert all(stage["status"] == "done" for stage in manifest["stages"])
    payload = json.loads((out / "topics.json").read_text(encoding="utf-8"))
    assert len(payload["topics"]) == 10

    listed = _invoke(["topic", "list", "--limit", "5"])
    assert listed.exit_code == 0, _combined(listed)
    assert "选题库" in _combined(listed)
