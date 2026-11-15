"""T09 Web 控制台与一键 demo 的离线测试。

覆盖：6 个页面 200、空态引导、``/kb/ask`` 无命中与空查询、``/files`` 路径穿越、
首页关键数字与最近一次 job、选题「生成文案」POST→303、详情页、demo 产物清单函数。
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workbench.config import Config, now_iso
from workbench.ids import new_id
from workbench.metrics.compute import parse_week, week_key
from workbench.store import repo
from workbench.store.models import (
    Account,
    Asset,
    CopySet,
    Job,
    Metric,
    Pack,
    Post,
    Topic,
)
from workbench.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]

PAGES = ["/", "/topics", "/copies", "/assets", "/packs", "/kb"]


@pytest.fixture
def client(cfg: Config, allow_network: None) -> TestClient:
    # Starlette 1.7 的 TestClient 需要事件循环自管道的 socket；conftest 只允许
    # 通过 allow_network 显式放行。测试本身不访问外网。
    return TestClient(create_app(cfg))


def _account(conn, cfg: Config) -> Account:
    account = Account(
        id=new_id("acc"),
        platform="xhs",
        handle="demo_web",
        display_name="演示账号（虚构）",
        created_at=now_iso(cfg.tz),
    )
    repo.insert_account(conn, account)
    return account


def _post(conn, cfg: Config, account: Account, *, views: int, published_at: datetime) -> Post:
    post = Post(
        id=new_id("post"),
        account_id=account.id,
        platform=account.platform,
        title="本周作品（虚构）",
        published_at=published_at.isoformat(timespec="seconds"),
        content_type="video",
        created_at=now_iso(cfg.tz),
    )
    repo.insert_post(conn, post)
    repo.insert_metric(
        conn,
        Metric(
            id=new_id("metric"),
            post_id=post.id,
            captured_at=now_iso(cfg.tz),
            views=views,
            likes=views // 10,
            comments=2,
            shares=1,
            saves=3,
            follows=1,
            completion_rate=0.5,
        ),
    )
    return post


def _current_week(cfg: Config):
    return parse_week(week_key(datetime.now(cfg.timezone()), cfg.tz), cfg.tz)


# --------------------------------------------------------------------------- 空态
def test_pages_200_with_empty_state(client: TestClient) -> None:
    for url in PAGES:
        response = client.get(url)
        assert response.status_code == 200, url
        assert "wb run demo" in response.text, url
        assert "超级个体获客工作台" in response.text


def test_kb_ask_without_hits_and_empty_query(client: TestClient) -> None:
    response = client.post("/kb/ask", data={"q": "完全不存在的主题词"})
    assert response.status_code == 200
    assert "未找到相关片段" in response.text

    response = client.post("/kb/ask", data={"q": ""})
    assert response.status_code == 200
    assert "少于 2 个字" in response.text


# --------------------------------------------------------------------------- 首页
def test_index_shows_week_numbers_topics_and_latest_job(
    cfg: Config, conn, client: TestClient
) -> None:
    account = _account(conn, cfg)
    week = _current_week(cfg)
    _post(conn, cfg, account, views=120, published_at=week.start + timedelta(hours=1))
    _post(conn, cfg, account, views=480, published_at=week.start + timedelta(hours=2))
    _post(conn, cfg, account, views=9999, published_at=week.start - timedelta(days=3))

    topic = Topic(
        id=new_id("topic"),
        platform="xhs",
        title="高分选题：把周报写成三行（虚构）",
        score=0.88,
        source="hotlist",
        created_at=now_iso(cfg.tz),
        evidence=[{"kind": "hotlist", "ref": "https://example.invalid/hot/1"}],
    )
    repo.insert_topic(conn, topic)

    copy_set = CopySet(
        id=new_id("copy"),
        topic_id=topic.id,
        kind="note",
        length="short",
        title="发布包用文案（虚构）",
        body="正文占位。",
        created_at=now_iso(cfg.tz),
        compliance={"passed": True, "hits": []},
    )
    repo.insert_copy_set(conn, copy_set)
    repo.insert_pack(
        conn,
        Pack(
            id=new_id("pack"),
            copy_set_id=copy_set.id,
            platform="xhs",
            dir="packs/demo",
            manifest={"title": "待发布包（虚构）"},
            status="exported",
            created_at=now_iso(cfg.tz),
        ),
    )
    repo.insert_job(
        conn,
        Job(
            id=new_id("job"),
            pipeline="pack",
            stage="pack_build",
            status="done",
            started_at=now_iso(cfg.tz),
            ended_at=now_iso(cfg.tz),
        ),
    )

    response = client.get("/")
    assert response.status_code == 200
    text = response.text
    assert "本周发布数" in text
    assert "播放中位数" in text
    assert "300" in text  # (120 + 480) / 2
    assert "Top3 选题" in text
    assert topic.title in text
    assert "待发布包数" in text
    assert "pack_build" in text


# --------------------------------------------------------------------------- 文件路由
def test_files_route_serves_home_and_rejects_traversal(cfg: Config, client: TestClient) -> None:
    png = cfg.home / "artifacts" / "demo.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(b"\x89PNG\r\n\x1a\n")

    response = client.get("/files/artifacts/demo.png")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")

    assert client.get("/files/artifacts/missing.png").status_code == 404
    assert client.get("/files/../../etc/passwd").status_code == 404
    assert client.get("/files/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.get("/files/%2Fetc%2Fpasswd").status_code == 404

    outside = cfg.home.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = cfg.home / "escape.png"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("当前文件系统不支持软链，跳过软链越界用例")
    assert client.get("/files/escape.png").status_code == 404


def test_assets_page_links_local_files(cfg: Config, conn, client: TestClient) -> None:
    png = cfg.home / "artifacts" / "poster.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    repo.insert_asset(
        conn,
        Asset(
            id=new_id("asset"),
            kind="poster",
            template="poster-quote",
            path="artifacts/poster.png",
            width=1080,
            height=1080,
            bytes=png.stat().st_size,
            created_at=now_iso(cfg.tz),
        ),
    )
    response = client.get("/assets")
    assert response.status_code == 200
    assert "/files/artifacts/poster.png" in response.text
    assert "1080×1080" in response.text


# --------------------------------------------------------------------------- 写操作
def test_topic_copy_post_redirects_and_generates_ten_copies(
    cfg: Config, conn, client: TestClient
) -> None:
    topic = Topic(
        id=new_id("topic"),
        platform="xhs",
        title="生成文案用的选题（虚构）",
        score=0.7,
        source="manual",
        created_at=now_iso(cfg.tz),
    )
    repo.insert_topic(conn, topic)

    response = client.post(f"/topics/{topic.id}/copy", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/copies"

    copies = repo.list_copy_sets(conn)
    assert len(copies) == 10
    assert {copy.kind for copy in copies} == {"spoken", "note", "seed", "explain", "hook"}

    page = client.get("/copies")
    assert page.status_code == 200
    assert "查看全文" in page.text

    assert client.post("/topics/topic_missing/copy").status_code == 404


def test_copy_detail_and_pack_manifest(conn, client: TestClient, cfg: Config) -> None:
    copy_set = CopySet(
        id=new_id("copy"),
        kind="note",
        length="long",
        title="详情页文案（虚构）",
        body="正文：一周复盘先看三个数字。",
        tags=["复盘", "演示"],
        citations=[{"doc_id": "doc_demo", "chunk_id": "chunk_demo", "quote": "引用一句虚构原文"}],
        compliance={
            "passed": False,
            "needs_human": True,
            "hits": [{"kind": "absolute", "word": "最好"}],
        },
        model="stub",
        created_at=now_iso(cfg.tz),
    )
    repo.insert_copy_set(conn, copy_set)
    repo.insert_pack(
        conn,
        Pack(
            id=new_id("pack"),
            copy_set_id=copy_set.id,
            platform="xhs",
            dir="packs/demo",
            manifest={"title": "发布包标题（虚构）", "ai_generated": True},
            status="exported",
            created_at=now_iso(cfg.tz),
        ),
    )

    copies = client.get(f"/copies?copy_id={copy_set.id}")
    assert copies.status_code == 200
    assert "正文：一周复盘先看三个数字。" in copies.text
    assert "引用一句虚构原文" in copies.text
    assert "未通过" in copies.text

    packs = client.get("/packs?pack_id=" + repo.list_packs(conn)[0].id)
    assert packs.status_code == 200
    assert "发布包标题（虚构）" in packs.text
    assert "该发布包含 AI 生成内容" in packs.text


# --------------------------------------------------------------------------- demo 清单
def _make_demo_tree(root: Path) -> None:
    (root / "artifacts").mkdir(parents=True)
    (root / "packs" / "pack_demo").mkdir(parents=True)
    (root / "wb.sqlite3").write_bytes(b"demo")
    (root / "topics.json").write_text("{}", encoding="utf-8")
    (root / "copies.json").write_text("{}", encoding="utf-8")
    (root / "report.md").write_text("# demo report\n", encoding="utf-8")
    (root / "packs" / "pack_demo" / "manifest.json").write_text("{}", encoding="utf-8")
    (root / "artifacts" / "poster.png").write_bytes(b"png")
    (root / "artifacts" / "video.mp4").write_bytes(b"mp4")


def test_demo_script_artifact_listing(tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    _make_demo_tree(demo)

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "demo.sh"), "--list", str(demo)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "demo 产物清单" in result.stdout
    assert "artifacts/**/*.png" in result.stdout
    assert "1 张" in result.stdout

    (demo / "report.md").unlink()
    failed = subprocess.run(
        ["bash", str(ROOT / "scripts" / "demo.sh"), "--list", str(demo)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert failed.returncode != 0
    assert "report.md" in failed.stderr


# --------------------------------------------------------------------------- CLI
def test_wb_serve_help_lists_options() -> None:
    from typer.testing import CliRunner

    from workbench.cli import app

    result = CliRunner().invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "--host" in result.output
    assert "--port" in result.output
    assert "8077" in result.output
