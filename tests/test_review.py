"""T02 验收测试：周计算、指标手算、分组样本不足、幂等导入、缺列报错与周报结构。"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import Result
from typer.testing import CliRunner

from workbench.cli import app
from workbench.metrics import MetricsError, compute
from workbench.metrics.import_csv import import_metrics
from workbench.metrics.report import build_actions, render_report
from workbench.store import repo
from workbench.store.db import init_db
from workbench.store.models import Account, Metric, Post

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data" / "seed"
WEEK = "2026-W45"
TZ = "Asia/Shanghai"


def _invoke(args: list[str]) -> Result:
    return CliRunner().invoke(app, args)


def _combined(result: Result) -> str:
    text = result.output
    stderr = getattr(result, "stderr", "")
    if stderr:
        text += stderr
    return text


def _section(text: str, heading: str) -> str:
    start = text.index(heading) + len(heading)
    rest = text[start:]
    match = re.search(r"\n## ", rest)
    return rest[: match.start()] if match else rest


def _subsection(text: str, heading: str) -> str:
    start = text.index(heading) + len(heading)
    rest = text[start:]
    match = re.search(r"\n### |\n## ", rest)
    return rest[: match.start()] if match else rest


def _table_rows(block: str) -> list[str]:
    lines = [line.strip() for line in block.splitlines() if line.strip().startswith("|")]
    return lines[2:]


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("WB_HOME", str(home))
    monkeypatch.setenv("WB_LLM", "stub")
    monkeypatch.delenv("WB_LLM_API_KEY", raising=False)
    return home


def test_parse_week_and_week_key_respects_timezone() -> None:
    week = compute.parse_week(WEEK, TZ)
    assert week.key() == WEEK
    assert week.start.isoformat() == "2026-11-02T00:00:00+08:00"
    assert week.end.isoformat() == "2026-11-09T00:00:00+08:00"
    assert (
        compute.week_key(datetime(2026, 11, 8, 23, 59, tzinfo=ZoneInfo(TZ)), TZ)
        == WEEK
    )
    # UTC 2026-11-01 17:00 = 北京时间 2026-11-02 01:00，属于 W45。
    assert (
        compute.week_key(datetime(2026, 11, 1, 17, 0, tzinfo=timezone.utc), TZ)
        == WEEK
    )

    with pytest.raises(MetricsError):
        compute.parse_week("2026-45", TZ)
    with pytest.raises(MetricsError):
        compute.parse_week("2026-W54", TZ)


def _insert_hand_fixture(conn: object) -> None:
    repo.insert_account(
        conn,
        Account(
            id="acc_hand",
            platform="xhs",
            handle="demo_hand",
            display_name="手算账号（虚构）",
            is_benchmark=False,
            niche=None,
            note=None,
            created_at="2026-11-01T00:00:00+08:00",
        ),
    )
    rows = [
        ("post_hand_001", "2026-11-02T08:00:00+08:00", 100, 10, 5, 20, 5, 4, 0.8),
        ("post_hand_002", "2026-11-03T12:00:00+08:00", 200, 20, 10, 40, 10, 8, 0.6),
        ("post_hand_003", "2026-11-04T20:00:00+08:00", 300, 30, 15, 60, 15, 12, 0.4),
    ]
    for index, row in enumerate(rows, start=1):
        post_id, published, views, likes, comments, saves, shares, follows, rate = row
        repo.insert_post(
            conn,
            Post(
                id=post_id,
                account_id="acc_hand",
                platform="xhs",
                title=f"手算作品 {index}（虚构）",
                url=None,
                published_at=published,
                content_type="image_text",
                topic_tags=["手算"],
                duration_sec=None,
                caption=None,
                created_at="2026-11-01T00:00:00+08:00",
            ),
        )
        repo.insert_metric(
            conn,
            Metric(
                id=f"metric_hand_{index}",
                post_id=post_id,
                captured_at="2026-11-05T10:00:00+08:00",
                views=views,
                likes=likes,
                comments=comments,
                shares=shares,
                saves=saves,
                follows=follows,
                completion_rate=rate,
                avg_watch_sec=None,
                raw=None,
            ),
        )


def test_metrics_match_hand_calculated_fixture(tmp_path: Path) -> None:
    conn = init_db(tmp_path / "hand.sqlite3")
    try:
        _insert_hand_fixture(conn)
        data = compute.build_weekly_data(
            conn, "demo_hand", WEEK, platform="xhs", tz=TZ
        )
        summary = data.summary
        assert summary.publish_count == 3
        # 手算：中位数 200；P90 在线性插值下 = 200 + 0.8 * (300 - 200) = 280。
        assert summary.median_views == pytest.approx(200)
        assert summary.p90_views == pytest.approx(280)
        # 三篇互动率都是 0.4；收藏率 0.2；关注率 0.04；完播率均值 0.6。
        assert summary.mean_interaction_rate == pytest.approx(0.4)
        assert summary.mean_save_rate == pytest.approx(0.2)
        assert summary.mean_follow_rate == pytest.approx(0.04)
        assert summary.mean_completion_rate == pytest.approx(0.6)
        # 08:00 → 次日 12:00 = 28h，次日 12:00 → 第三日 20:00 = 32h，中位数 30h。
        assert summary.publish_gap_hours == pytest.approx(30.0)
        assert summary.history_p90_views == pytest.approx(280)
        assert summary.anomaly_count == 0
    finally:
        conn.close()


def test_seed_import_is_idempotent(conn: object) -> None:
    first = import_metrics(
        conn, SEED / "metrics_sample.csv", account="demo_xhs", platform="xhs", tz=TZ
    )
    assert (first.imported, first.accounts, first.skipped) == (30, 3, 0)
    assert len(repo.list_accounts(conn)) == 3
    assert len(repo.list_posts(conn)) == 30
    assert len(repo.list_metrics(conn)) == 30

    benchmark = {account.handle: account.is_benchmark for account in repo.list_accounts(conn)}
    assert benchmark == {
        "demo_xhs": False,
        "demo_xhs_bench_a": True,
        "demo_xhs_bench_b": True,
    }

    second = import_metrics(
        conn, SEED / "metrics_sample.csv", account="demo_xhs", platform="xhs", tz=TZ
    )
    assert (second.imported, second.accounts, second.skipped) == (0, 3, 30)
    assert len(repo.list_posts(conn)) == 30
    assert len(repo.list_metrics(conn)) == 30


def test_group_insufficient_samples_are_marked_and_not_recommended(conn: object) -> None:
    import_metrics(
        conn, SEED / "metrics_sample.csv", account="demo_xhs", platform="xhs", tz=TZ
    )
    data = compute.build_weekly_data(conn, "demo_xhs", WEEK, platform="xhs", tz=TZ)

    slots = {group.key: group for group in data.groups["time_slot"]}
    assert slots["深夜"].n == 1
    assert slots["深夜"].insufficient
    assert not slots["午"].insufficient

    durations = {group.key: group for group in data.groups["duration"]}
    assert durations["30–60s"].n == 0
    assert durations["30–60s"].insufficient
    assert durations["15–30s"].insufficient
    assert not durations["≤15s"].insufficient

    contents = {group.key: group for group in data.groups["content_type"]}
    assert contents["文章"].insufficient

    rendered = render_report(data)
    assert "样本不足" in rendered

    actions = build_actions(data)
    assert len(actions) == 3
    insufficient_names = ("深夜", "文章", "15–30s", "30–60s", ">60s", "无时长")
    for action in actions:
        assert not any(name in action for name in insufficient_names)


def test_missing_columns_exit_2_and_name_aliases(
    cli_env: Path, tmp_path: Path
) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "post_id,published_at,title,content_type,captured_at,likes,comments,shares,saves,follows\n"
        "post_bad_1,2026-11-02T08:00:00+08:00,坏样例,image_text,"
        "2026-11-03T08:00:00+08:00,1,1,1,1,1\n",
        encoding="utf-8",
    )
    result = _invoke(["review", "import", "--csv", str(bad)])
    output = _combined(result)

    assert result.exit_code == 2, output
    assert "views" in output
    assert "播放量" in output
    assert "支持别名" in output


def test_invalid_cell_reports_line_and_column(cli_env: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad_value.csv"
    bad.write_text(
        "post_id,published_at,title,content_type,captured_at,views,likes,comments,shares,saves,follows\n"
        "post_bad_2,2026-11-02T08:00:00+08:00,坏样例,image_text,"
        "2026-11-03T08:00:00+08:00,not-a-number,1,1,1,1,1\n",
        encoding="utf-8",
    )
    result = _invoke(["review", "import", "--csv", str(bad), "--account", "demo_xhs"])
    output = _combined(result)

    assert result.exit_code == 2, output
    assert "第 2 行" in output
    assert "views" in output


def test_import_without_account_column_uses_account_flag(
    cli_env: Path, tmp_path: Path
) -> None:
    single = tmp_path / "single.csv"
    single.write_text(
        "post_id,published_at,title,content_type,captured_at,views,likes,comments,"
        "shares,saves,follows\n"
        "post_single,2026-11-02T08:00:00+08:00,单账号样例,image_text,"
        "2026-11-03T08:00:00+08:00,100,10,2,1,5,3\n",
        encoding="utf-8",
    )
    result = _invoke(
        ["review", "import", "--csv", str(single), "--account", "demo_single"]
    )
    output = _combined(result)
    assert result.exit_code == 0, output
    assert "imported=1 accounts=1" in output

    connection = sqlite3.connect(cli_env / "wb.sqlite3")
    try:
        row = connection.execute(
            "SELECT handle, is_benchmark FROM account"
        ).fetchone()
    finally:
        connection.close()
    assert row == ("demo_single", 0)


def test_import_multi_account_without_account_exits_2(cli_env: Path) -> None:
    result = _invoke(
        ["review", "import", "--csv", str(SEED / "metrics_sample.csv")]
    )
    output = _combined(result)
    assert result.exit_code == 2, output
    assert "--account" in output
    assert "多个账号" in output


def test_cli_import_and_week_report_acceptance(
    cli_env: Path, tmp_path: Path
) -> None:
    seed = str(SEED / "metrics_sample.csv")
    first = _invoke(["review", "import", "--csv", seed, "--account", "demo_xhs"])
    first_output = _combined(first)
    assert first.exit_code == 0, first_output
    assert "imported=30" in first_output
    assert "accounts=3" in first_output

    second = _invoke(["review", "import", "--csv", seed, "--account", "demo_xhs"])
    assert second.exit_code == 0, _combined(second)
    assert "skipped=30" in _combined(second)

    report_dir = tmp_path / "rev"
    week_result = _invoke(
        [
            "review",
            "week",
            "--account",
            "demo_xhs",
            "--week",
            WEEK,
            "--out",
            str(report_dir),
        ]
    )
    week_output = _combined(week_result)
    assert week_result.exit_code == 0, week_output
    assert "report=" in week_output
    assert (report_dir / "report.md").is_file()

    text = (report_dir / "report.md").read_text(encoding="utf-8")
    for heading in (
        "## 概览",
        "## 指标表",
        "## 分组对比",
        "## Top5 与 Bottom5",
        "## 下周三条动作",
    ):
        assert heading in text

    metrics_rows = _table_rows(_section(text, "## 指标表"))
    assert len(metrics_rows) >= 8

    top_block = _subsection(_section(text, "## Top5 与 Bottom5"), "### Top5")
    bottom_block = _subsection(_section(text, "## Top5 与 Bottom5"), "### Bottom5")
    top_rows = _table_rows(top_block)
    bottom_rows = _table_rows(bottom_block)
    assert len(top_rows) == 5
    assert len(bottom_rows) == 5
    assert "views" in top_block and "interaction_rate" in top_block
    assert "views" in bottom_block and "interaction_rate" in bottom_block
    for row in top_rows + bottom_rows:
        assert re.search(r"post_[a-z0-9_]+", row)
        assert "%" in row

    actions_block = _section(text, "## 下周三条动作")
    action_lines = [
        line.strip()
        for line in actions_block.splitlines()
        if re.match(r"^\d+\. ", line.strip())
    ]
    assert len(action_lines) == 3
    group_names = (
        "早",
        "午",
        "晚",
        "深夜",
        "图文",
        "视频",
        "文章",
        "≤15s",
        "15–30s",
        "30–60s",
        ">60s",
        "无时长",
    )
    for line in action_lines:
        assert re.search(r"\d", line)
        assert re.search(r"post_[a-z0-9_]+", line) or any(
            name in line for name in group_names
        )


def test_review_pipeline_runs_both_stages(cli_env: Path, tmp_path: Path) -> None:
    out = tmp_path / "run"
    result = _invoke(
        [
            "run",
            "review",
            "--input",
            f"csv={SEED / 'metrics_sample.csv'}",
            "--input",
            "account=demo_xhs",
            "--input",
            f"week={WEEK}",
            "--out",
            str(out),
        ]
    )
    assert result.exit_code == 0, _combined(result)

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert [stage["name"] for stage in manifest["stages"]] == [
        "metrics_import",
        "review_report",
    ]
    assert all(stage["status"] == "done" for stage in manifest["stages"])
    assert manifest["stages"][0]["output"]["imported"] == 30
    assert (out / "report.md").is_file()
    assert "## 下周三条动作" in (out / "report.md").read_text(encoding="utf-8")


def test_week_without_posts_exits_2(cli_env: Path, tmp_path: Path) -> None:
    import_result = _invoke(
        [
            "review",
            "import",
            "--csv",
            str(SEED / "metrics_sample.csv"),
            "--account",
            "demo_xhs",
        ]
    )
    assert import_result.exit_code == 0, _combined(import_result)

    result = _invoke(
        [
            "review",
            "week",
            "--account",
            "demo_xhs",
            "--week",
            "2026-W30",
            "--out",
            str(tmp_path / "empty"),
        ]
    )
    output = _combined(result)
    assert result.exit_code == 2, output
    assert "没有可用的 metric" in output


def test_cli_help_lists_review_commands(cli_env: Path) -> None:
    root_help = _combined(_invoke(["--help"]))
    assert "review" in root_help
    review_help = _combined(_invoke(["review", "--help"]))
    assert "import" in review_help
    assert "week" in review_help


def test_seed_accounts_are_fictional_and_consistent() -> None:
    with (SEED / "accounts.csv").open(encoding="utf-8", newline="") as handle:
        accounts = list(csv.DictReader(handle))
    assert len(accounts) == 3
    for account in accounts:
        assert account["handle"].startswith("demo_")
        assert "虚构" in account["display_name"]

    with (SEED / "metrics_sample.csv").open(encoding="utf-8", newline="") as handle:
        metrics = list(csv.DictReader(handle))
    assert len(metrics) == 30
    assert {row["account"] for row in metrics} == {row["handle"] for row in accounts}

    with (SEED / "posts.csv").open(encoding="utf-8", newline="") as handle:
        posts = {row["post_id"]: row for row in csv.DictReader(handle)}
    assert set(posts) == {row["post_id"] for row in metrics}
    for row in metrics:
        post = posts[row["post_id"]]
        for column in (
            "account",
            "platform",
            "title",
            "published_at",
            "content_type",
            "topic_tags",
            "duration_sec",
            "caption",
        ):
            assert post[column] == row[column]
