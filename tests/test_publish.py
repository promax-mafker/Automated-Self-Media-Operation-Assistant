"""T08 验收测试：发布包结构、manifest 校验和、幂等 log、backfill 三分支与发布适配位。"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import Result
from PIL import Image
from typer.testing import CliRunner

from workbench.cli import app
from workbench.config import Config
from workbench.contracts import get_publisher
from workbench.ids import new_id
from workbench.publish import PublishError
from workbench.publish.backfill import backfill
from workbench.publish.build import (
    PUBLISH_NOT_IMPLEMENTED,
    LocalPublisher,
    SocialAutoUploadPublisher,
    build_publisher,
)
from workbench.publish.bundle import create_pack, pack_path
from workbench.publish.manifest import (
    MANIFEST_FILENAME,
    REQUIRED_MANIFEST_FIELDS,
    load_manifest,
    suggest_publish_time,
    validate_manifest,
    verify_checksums,
)
from workbench.store import repo
from workbench.store.db import init_db
from workbench.store.models import Account, Asset, CopySet, Metric, Post

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "export_sample.csv"
SEED = ROOT / "data" / "seed" / "metrics_sample.csv"
TZ = "Asia/Shanghai"
CREATED_AT = "2026-11-15T10:00:00+08:00"


def _invoke(args: list[str]) -> Result:
    return CliRunner().invoke(app, args)


def _combined(result: Result) -> str:
    text = result.output
    stderr = getattr(result, "stderr", "")
    if stderr:
        text += stderr
    return text


def _open(home: Path) -> tuple[Config, sqlite3.Connection]:
    cfg = Config.from_env({"WB_HOME": str(home), "WB_LLM": "stub"})
    cfg.ensure_dirs()
    return cfg, init_db(cfg.db_path)


def _seed_copy(
    conn: sqlite3.Connection,
    home: Path,
    *,
    assets: int = 2,
    compliance: dict | None = None,
) -> CopySet:
    copy = CopySet(
        id=new_id("copy"),
        kind="note",
        length="long",
        title="发布包验收标题",
        body="第一段正文\n第二段正文",
        tags=["内容运营", "效率工具"],
        citations=[],
        model="stub",
        prompt_version="test",
        compliance=compliance if compliance is not None else {"passed": True, "hits": []},
        created_at=CREATED_AT,
    )
    repo.insert_copy_set(conn, copy)
    for index in range(1, assets + 1):
        path = home / "artifacts" / f"image-{index}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1080, 1440), "#FFFFFF").save(path)
        repo.insert_asset(
            conn,
            Asset(
                id=new_id("asset"),
                copy_set_id=copy.id,
                kind="carousel",
                template="xhs-nine",
                path=path.relative_to(home).as_posix(),
                width=1080,
                height=1440,
                duration_sec=None,
                bytes=path.stat().st_size,
                meta={},
                created_at=CREATED_AT,
            ),
        )
    return copy


def _ensure_account(conn: sqlite3.Connection, handle: str = "demo_xhs") -> Account:
    for account in repo.list_accounts(conn, platform="xhs"):
        if account.handle == handle:
            return account
    account = Account(
        id=new_id("acc"),
        platform="xhs",
        handle=handle,
        display_name="示例账号（虚构）",
        is_benchmark=False,
        created_at=CREATED_AT,
    )
    repo.insert_account(conn, account)
    return account


def _add_post_with_metric(
    conn: sqlite3.Connection,
    account: Account,
    *,
    published_at: str,
    views: int,
    likes: int,
    comments: int = 0,
    saves: int = 0,
    shares: int = 0,
) -> Post:
    post = Post(
        id=new_id("post"),
        account_id=account.id,
        platform="xhs",
        title="建议发布时间测试",
        url=f"https://example.invalid/demo/{new_id('p')}",
        published_at=published_at,
        content_type="image_text",
        topic_tags=[],
        created_at=CREATED_AT,
    )
    repo.insert_post(conn, post)
    repo.insert_metric(
        conn,
        Metric(
            id=new_id("metric"),
            post_id=post.id,
            captured_at="2026-11-15T10:00:00+08:00",
            views=views,
            likes=likes,
            comments=comments,
            saves=saves,
            shares=shares,
        ),
    )
    return post


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("WB_HOME", str(home))
    monkeypatch.setenv("WB_LLM", "stub")
    monkeypatch.delenv("WB_LLM_API_KEY", raising=False)
    monkeypatch.delenv("WB_FONT_DIR", raising=False)
    return home


def test_cli_help_lists_pack_subcommands() -> None:
    result = _invoke(["pack", "--help"])
    output = _combined(result)

    assert result.exit_code == 0, output
    for name in ("build", "list", "show", "log", "backfill"):
        assert name in output


def test_build_creates_structure_manifest_and_checksums(cli_env: Path) -> None:
    cfg, conn = _open(cli_env)
    try:
        copy = _seed_copy(conn, cli_env)
        result = _invoke(["pack", "build", "--copy", copy.id, "--platform", "xhs"])
        output = _combined(result)
        assert result.exit_code == 0, output

        packs = repo.list_packs(conn)
        assert len(packs) == 1
        pack = packs[0]
        assert pack.status == "exported"
        assert pack.platform == "xhs"
        assert pack.copy_set_id == copy.id

        pack_dir = pack_path(cfg, pack)
        expected_files = {
            "文案.md",
            "发布说明.md",
            "图片/01.png",
            "图片/02.png",
            MANIFEST_FILENAME,
        }
        actual_files = {
            path.relative_to(pack_dir).as_posix()
            for path in pack_dir.rglob("*")
            if path.is_file()
        }
        assert actual_files == expected_files
        assert (pack_dir.parent / f"{pack_dir.name}.zip").is_file()

        manifest = load_manifest(pack_dir / MANIFEST_FILENAME)
        assert set(REQUIRED_MANIFEST_FIELDS) <= set(manifest)
        assert manifest["ai_generated"] is True
        assert manifest["compliance"] == {"passed": True, "hits": []}
        assert manifest["tags"] == ["内容运营", "效率工具"]
        assert len(manifest["assets"]) == 2
        for asset in manifest["assets"]:
            assert {"id", "path", "width", "height", "duration_sec"} <= set(asset)
            assert (pack_dir / asset["path"]).is_file()
        assert set(manifest["checksums"]) == actual_files - {MANIFEST_FILENAME}
        assert verify_checksums(pack_dir, manifest) == []
        assert manifest == pack.manifest

        copy_text = (pack_dir / "文案.md").read_text(encoding="utf-8")
        assert copy_text.splitlines()[0] == "发布包验收标题"
        assert "#内容运营" in copy_text
        assert "#效率工具" in copy_text
        assert "第一段正文" in copy_text

        notes = (pack_dir / "发布说明.md").read_text(encoding="utf-8")
        assert "AI 辅助生成" in notes
        assert "`compliance.passed`: **true**" in notes
        assert "无数据，按默认" in notes
        assert manifest["suggested_publish_at"] in notes

        with zipfile.ZipFile(pack_dir.parent / f"{pack_dir.name}.zip") as archive:
            names = set(archive.namelist())
        assert f"{pack_dir.name}/manifest.json" in names
        assert f"{pack_dir.name}/图片/01.png" in names
    finally:
        conn.close()


def test_build_without_assets_and_failed_compliance(cli_env: Path) -> None:
    cfg, conn = _open(cli_env)
    try:
        copy = _seed_copy(
            conn,
            cli_env,
            assets=0,
            compliance={
                "passed": False,
                "hits": [{"word": "第一", "kind": "absolute", "index": 5}],
            },
        )
        result = _invoke(["pack", "build", "--copy", copy.id, "--platform", "xhs"])
        output = _combined(result)

        assert result.exit_code == 0, output
        assert "纯文案包" in output
        assert "compliance.passed=false" in output
        pack = repo.list_packs(conn)[0]
        manifest = load_manifest(pack_path(cfg, pack) / MANIFEST_FILENAME)
        assert manifest["assets"] == []
        assert manifest["compliance"]["passed"] is False
        notes = (pack_path(cfg, pack) / "发布说明.md").read_text(encoding="utf-8")
        assert "`compliance.passed`: **false**" in notes
        assert "第一" in notes
        assert "未通过" in notes
    finally:
        conn.close()


def test_build_rejects_missing_copy_and_non_empty_out(cli_env: Path, tmp_path: Path) -> None:
    cfg, conn = _open(cli_env)
    try:
        missing = _invoke(["pack", "build", "--copy", "copy_missing"])
        assert missing.exit_code == 2
        assert "找不到 copy_set" in _combined(missing)

        copy = _seed_copy(conn, cli_env)
        out = tmp_path / "custom-pack"
        out.mkdir()
        (out / "keep.txt").write_text("keep", encoding="utf-8")
        result = _invoke(
            ["pack", "build", "--copy", copy.id, "--platform", "xhs", "--out", str(out)]
        )
        assert result.exit_code == 2
        assert "已存在且非空" in _combined(result)
        assert repo.list_packs(conn) == []
    finally:
        conn.close()


def test_pack_log_is_idempotent_and_overwrites_manifest(cli_env: Path) -> None:
    cfg, conn = _open(cli_env)
    try:
        copy = _seed_copy(conn, cli_env, assets=1)
        build = _invoke(["pack", "build", "--copy", copy.id, "--platform", "xhs"])
        assert build.exit_code == 0, _combined(build)
        pack_id = repo.list_packs(conn)[0].id

        first = _invoke(
            [
                "pack",
                "log",
                "--pack",
                pack_id,
                "--url",
                "https://example.com/p/1",
                "--at",
                "2026-11-15T20:00:00+08:00",
            ]
        )
        assert first.exit_code == 0, _combined(first)
        second = _invoke(
            [
                "pack",
                "log",
                "--pack",
                pack_id,
                "--url",
                "https://example.com/p/2",
                "--at",
                "2026-11-15T21:00:00+08:00",
            ]
        )
        assert second.exit_code == 0, _combined(second)

        packs = repo.list_packs(conn)
        assert len(packs) == 1
        assert packs[0].status == "published"
        assert packs[0].manifest["published_url"] == "https://example.com/p/2"
        assert packs[0].manifest["published_at"] == "2026-11-15T21:00:00+08:00"
        assert "published_urls" not in packs[0].manifest

        on_disk = load_manifest(pack_path(cfg, packs[0]) / MANIFEST_FILENAME)
        assert on_disk["published_url"] == "https://example.com/p/2"
        assert on_disk["published_at"] == "2026-11-15T21:00:00+08:00"
        zip_path = pack_path(cfg, packs[0]).parent / f"{pack_path(cfg, packs[0]).name}.zip"
        with zipfile.ZipFile(zip_path) as archive:
            zipped = json.loads(
                archive.read(f"{pack_path(cfg, packs[0]).name}/manifest.json")
            )
        assert zipped["published_url"] == "https://example.com/p/2"

        listed = _invoke(["pack", "list"])
        assert listed.exit_code == 0, _combined(listed)
        assert pack_id in _combined(listed)
        assert "published" in _combined(listed)

        shown = _invoke(["pack", "show", "--pack", pack_id, "--json"])
        assert shown.exit_code == 0, _combined(shown)
        assert json.loads(shown.output)["published_url"] == "https://example.com/p/2"
    finally:
        conn.close()


def test_pack_show_missing_returns_2(cli_env: Path) -> None:
    result = _invoke(["pack", "show", "--pack", "pack_missing"])
    assert result.exit_code == 2
    assert "找不到发布包" in _combined(result)


def test_pack_log_rejects_invalid_url_and_at(cli_env: Path) -> None:
    _cfg, conn = _open(cli_env)
    try:
        copy = _seed_copy(conn, cli_env, assets=0)
        build = _invoke(["pack", "build", "--copy", copy.id, "--platform", "xhs"])
        assert build.exit_code == 0, _combined(build)
        pack_id = repo.list_packs(conn)[0].id

        bad_url = _invoke(
            ["pack", "log", "--pack", pack_id, "--url", "ftp://example.com/p/1"]
        )
        assert bad_url.exit_code == 2
        assert "http(s)" in _combined(bad_url)
        assert repo.list_packs(conn)[0].status == "exported"

        bad_at = _invoke(
            [
                "pack",
                "log",
                "--pack",
                pack_id,
                "--url",
                "https://example.com/p/1",
                "--at",
                "not-a-time",
            ]
        )
        assert bad_at.exit_code == 2
        assert "ISO 8601" in _combined(bad_at)
    finally:
        conn.close()


def test_backfill_matches_creates_and_is_idempotent(cli_env: Path) -> None:
    imported = _invoke(
        ["review", "import", "--csv", str(SEED), "--account", "demo_xhs"]
    )
    assert imported.exit_code == 0, _combined(imported)

    first = _invoke(["pack", "backfill", "--csv", str(FIXTURE)])
    assert first.exit_code == 0, _combined(first)
    assert "matched=2 created=1 metrics=3" in _combined(first)

    second = _invoke(["pack", "backfill", "--csv", str(FIXTURE)])
    assert second.exit_code == 0, _combined(second)
    assert "matched=3 created=0 metrics=0" in _combined(second)

    cfg, conn = _open(cli_env)
    try:
        posts = repo.list_posts(conn)
        assert len(posts) == 31
        created = [post for post in posts if post.url.endswith("post_xhs_099")]
        assert len(created) == 1
        assert len(repo.list_metrics(conn, post_id=created[0].id)) == 1
        assert len(repo.list_metrics(conn)) == 33
        assert len(repo.list_accounts(conn, platform="xhs")) == 3
    finally:
        conn.close()


def test_backfill_duplicate_rows_are_skipped(cli_env: Path, tmp_path: Path) -> None:
    csv_path = tmp_path / "duplicate.csv"
    row = (
        "https://example.invalid/demo/post_xhs_100,xhs,demo_xhs,重复行,"
        "2026-11-12T19:00:00+08:00,video,2026-11-13T10:00:00+08:00,100\n"
    )
    csv_path.write_text(
        "url,platform,account,title,published_at,content_type,captured_at,views\n"
        + row
        + row,
        encoding="utf-8",
    )
    _cfg, conn = _open(cli_env)
    try:
        result = backfill(conn, csv_path)
        assert result.matched == 1
        assert result.created == 1
        assert result.metrics == 1
        assert result.skipped == 1
        assert len(repo.list_posts(conn)) == 1
        assert len(repo.list_metrics(conn)) == 1
    finally:
        conn.close()


def test_backfill_uses_csv_post_id_for_new_post(cli_env: Path, tmp_path: Path) -> None:
    csv_path = tmp_path / "with-post-id.csv"
    csv_path.write_text(
        "post_id,url,platform,account,title,published_at,content_type,captured_at,views\n"
        "post_backfill_custom,https://example.invalid/demo/post_xhs_101,xhs,demo_xhs,"
        "指定 post_id,2026-11-12T19:00:00+08:00,video,2026-11-13T10:00:00+08:00,100\n",
        encoding="utf-8",
    )
    _cfg, conn = _open(cli_env)
    try:
        result = backfill(conn, csv_path)
        assert result.created == 1
        post = repo.get_post(conn, "post_backfill_custom")
        assert post is not None
        assert post.url == "https://example.invalid/demo/post_xhs_101"
    finally:
        conn.close()


def test_backfill_requires_url_column(cli_env: Path, tmp_path: Path) -> None:
    csv_path = tmp_path / "no-url.csv"
    csv_path.write_text("title,platform\n示例,xhs\n", encoding="utf-8")
    result = _invoke(["pack", "backfill", "--csv", str(csv_path)])

    assert result.exit_code == 2
    assert "缺少 url 列" in _combined(result)


def test_suggest_publish_time_uses_exact_weekday_and_slot(cli_env: Path) -> None:
    _cfg, conn = _open(cli_env)
    try:
        account = _ensure_account(conn)
        for published in (
            "2026-11-02T19:00:00+08:00",
            "2026-11-09T19:00:00+08:00",
            "2026-11-16T19:00:00+08:00",
        ):
            _add_post_with_metric(
                conn, account, published_at=published, views=100, likes=20, comments=5
            )
        for published in (
            "2026-11-03T09:00:00+08:00",
            "2026-11-10T09:00:00+08:00",
            "2026-11-17T09:00:00+08:00",
        ):
            _add_post_with_metric(
                conn, account, published_at=published, views=100, likes=1, comments=0
            )

        suggestion = suggest_publish_time(
            conn,
            "xhs",
            now=datetime(2026, 11, 15, 10, 0, tzinfo=ZoneInfo(TZ)),
        )
        assert suggestion.default_used is False
        assert suggestion.weekday == "周一"
        assert suggestion.slot == "晚"
        assert suggestion.at == "2026-11-16T19:00:00+08:00"
        assert "周一 / 晚" in suggestion.reason
        assert suggestion.sample_posts == 6
    finally:
        conn.close()


def test_suggest_publish_time_falls_back_to_slot(cli_env: Path) -> None:
    _cfg, conn = _open(cli_env)
    try:
        account = _ensure_account(conn)
        for published in (
            "2026-11-02T19:00:00+08:00",
            "2026-11-04T19:00:00+08:00",
            "2026-11-06T19:00:00+08:00",
        ):
            _add_post_with_metric(
                conn, account, published_at=published, views=100, likes=10
            )

        suggestion = suggest_publish_time(
            conn,
            "xhs",
            now=datetime(2026, 11, 15, 10, 0, tzinfo=ZoneInfo(TZ)),
        )
        assert suggestion.default_used is False
        assert suggestion.weekday is None
        assert suggestion.slot == "晚"
        assert suggestion.at == "2026-11-15T19:00:00+08:00"
        assert "同星期样本不足" in suggestion.reason
    finally:
        conn.close()


def test_suggest_publish_time_defaults_without_data(cli_env: Path) -> None:
    _cfg, conn = _open(cli_env)
    try:
        suggestion = suggest_publish_time(
            conn,
            "xhs",
            now=datetime(2026, 11, 15, 10, 0, tzinfo=ZoneInfo(TZ)),
        )
        assert suggestion.default_used is True
        assert suggestion.at == "2026-11-16T19:00:00+08:00"
        assert "无数据，按默认" in suggestion.reason
        assert suggestion.sample_posts == 0
    finally:
        conn.close()


def test_manifest_validation_rejects_missing_and_false_ai(cli_env: Path) -> None:
    _cfg, conn = _open(cli_env)
    try:
        copy = _seed_copy(conn, cli_env, assets=0)
        result = create_pack(conn, _cfg, copy.id, platform="xhs")
        manifest = dict(result.pack.manifest)

        broken = dict(manifest)
        del broken["checksums"]
        with pytest.raises(PublishError, match="缺少必填字段"):
            validate_manifest(broken)

        broken = dict(manifest)
        broken["ai_generated"] = False
        with pytest.raises(PublishError, match="ai_generated"):
            validate_manifest(broken)
    finally:
        conn.close()


def test_publisher_auto_publish_is_explicitly_not_implemented() -> None:
    publisher = build_publisher()
    assert isinstance(publisher, LocalPublisher)
    assert isinstance(get_publisher(), LocalPublisher)

    with pytest.raises(NotImplementedError) as local_error:
        publisher.publish()
    assert PUBLISH_NOT_IMPLEMENTED in str(local_error.value)
    assert "本项目不自动发布，请人工发布后用 wb pack log 记录" in str(local_error.value)

    with pytest.raises(NotImplementedError) as adapter_error:
        SocialAutoUploadPublisher().publish(
            pack_id="pack_test", platform="xhs", bundle_dir=Path("/tmp")
        )
    assert PUBLISH_NOT_IMPLEMENTED in str(adapter_error.value)


def test_run_pack_pipeline_writes_pack_row(cli_env: Path, tmp_path: Path) -> None:
    _cfg, conn = _open(cli_env)
    try:
        copy = _seed_copy(conn, cli_env, assets=1)
        result = _invoke(
            [
                "run",
                "pack",
                "--input",
                f"copy={copy.id}",
                "--out",
                str(tmp_path / "run"),
            ]
        )
        assert result.exit_code == 0, _combined(result)
        assert "pack_build" in _combined(result)
        packs = repo.list_packs(conn)
        assert len(packs) == 1
        assert packs[0].status == "exported"
        jobs = repo.list_jobs(conn, pipeline="pack")
        assert len(jobs) == 1
        assert jobs[0].status == "done"
        assert jobs[0].output_ref["pack_id"] == packs[0].id
    finally:
        conn.close()
