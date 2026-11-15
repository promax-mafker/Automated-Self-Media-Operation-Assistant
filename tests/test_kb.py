"""T03 知识库：切块、FTS5 检索、幂等导入、CLI 与评测。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from workbench.cli import app
from workbench.config import Config
from workbench.errors import Unavailable
from workbench.knowledge.answer import EMPTY_QUERY_HINT, NO_HIT_HINT, ask, evaluate, load_eval
from workbench.knowledge.build import build_kb
from workbench.knowledge.chunk import MAX_CHARS, parse_markdown
from workbench.knowledge.index import KnowledgeBase, build_seg, tokenize
from workbench.llm.stub import StubLLM
from workbench.pipeline.registry import has_stage
from workbench.pipeline.stage import RunContext
from workbench.stages import load_stages
from workbench.stages.kb import KbAsk, KbIngest
from workbench.store.db import init_db

SEED_DIR = Path("data/kb/seed")
EVAL_FILE = Path("data/kb/eval.jsonl")
PRICE_DOC = "价格与客单价（虚构）"


def _invoke(args: list[str]) -> Result:
    return CliRunner().invoke(app, args)


def _combined(result: Result) -> str:
    text = result.output
    stderr = getattr(result, "stderr", "")
    if stderr:
        text += stderr
    return text


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _count(db_path: Path, sql: str) -> int:
    connection = sqlite3.connect(db_path)
    try:
        return int(connection.execute(sql).fetchone()[0])
    finally:
        connection.close()


@pytest.fixture
def kb(cfg: Config):
    knowledge = build_kb(cfg)
    yield knowledge
    knowledge.close()


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("WB_HOME", str(home))
    monkeypatch.setenv("WB_LLM", "stub")
    monkeypatch.delenv("WB_LLM_API_KEY", raising=False)
    return home


# --------------------------------------------------------------------------- 切块
def test_chunk_long_paragraph_is_split_by_sentence_into_limit() -> None:
    long_paragraph = "这是一句用于测试切块的文本。" * 200
    parsed = parse_markdown(f"# 长文测试\n\n{long_paragraph}", fallback_title="demo_long")

    assert parsed.title == "长文测试"
    assert len(parsed.chunks) >= 2
    assert all(chunk.body.strip() for chunk in parsed.chunks)
    assert all(len(chunk.body) <= MAX_CHARS for chunk in parsed.chunks)
    assert all(chunk.body.endswith("。") for chunk in parsed.chunks)
    assert len(parsed.chunks[0].body) >= 300
    assert "这是一句用于测试切块的文本" in parsed.chunks[0].body


def test_chunk_without_heading_uses_filename_title() -> None:
    text = "第一段内容，没有标题。\n\n第二段内容，仍然没有标题。"
    parsed = parse_markdown(text, fallback_title="demo_plain")

    assert parsed.title == "demo_plain"
    assert parsed.chunks
    assert all(chunk.heading is None for chunk in parsed.chunks)
    assert "第一段内容" in parsed.chunks[0].body


def test_chunk_multilevel_heading_keeps_path_and_excludes_h1() -> None:
    text = (
        "# 产品信息（虚构）\n\n"
        "## 产品\n\n产品介绍。\n\n"
        "### 价格\n\n价格说明，客单价等于成交总额除以成交客户数。\n\n"
        "## 服务\n\n服务说明。"
    )
    parsed = parse_markdown(text)

    assert parsed.title == "产品信息（虚构）"
    headings = [chunk.heading for chunk in parsed.chunks]
    assert "产品" in headings
    assert "产品/价格" in headings
    assert "服务" in headings
    assert all(heading != "产品信息（虚构）" for heading in headings)
    price = next(chunk for chunk in parsed.chunks if chunk.heading == "产品/价格")
    assert "客单价" in price.body
    assert "#" not in price.body


# --------------------------------------------------------------------------- 索引与检索
def test_tokenize_and_seg_keep_chinese_words_and_drop_punctuation() -> None:
    tokens = tokenize("客单价，怎么解释？")
    # jieba 的搜索模式会把「客单价」切成「客 / 单价」；单字会被过滤。
    assert "单价" in tokens
    assert all(len(token) >= 2 for token in tokens)
    assert "，" not in tokens

    seg = build_seg("客单价 与 成交总额")
    assert "单价" in seg
    assert "成交" in seg
    assert " " in seg


def test_index_and_search_return_citable_hits(kb: KnowledgeBase, tmp_path: Path) -> None:
    doc = _write(
        tmp_path / "demo_price_unit.md",
        "# 价格说明（虚构）\n\n## 客单价\n\n"
        "客单价等于成交总额除以成交客户数，用于解释一次购买的平均金额。\n\n"
        "## 毛利率\n\n毛利率用于观察成交金额里扣除商品成本后的比例。",
    )
    stats = kb.add([doc])
    assert stats["docs"] == 1
    assert stats["chunks"] >= 2

    hits = kb.search("客单价怎么解释", k=3)
    assert hits
    assert hits[0].title == "价格说明（虚构）"
    assert hits[0].heading == "客单价"
    assert 0.0 <= hits[0].score <= 1.0
    assert hits[0].body
    assert hits[0].chunk_id.startswith("kbchunk_")


def test_search_empty_single_and_no_hit_return_empty_with_hint(
    kb: KnowledgeBase, tmp_path: Path
) -> None:
    _write(tmp_path / "demo_topic.md", "# 主题\n\n## 内容\n\n这里只讲冲泡方法。")
    kb.add([tmp_path / "demo_topic.md"])

    assert kb.search("") == []
    assert kb.search("价") == []
    assert kb.search("量子纠缠") == []

    empty = ask(kb, "")
    assert empty.hits == []
    assert empty.hint == EMPTY_QUERY_HINT

    single = ask(kb, "价")
    assert single.hits == []
    assert single.hint == EMPTY_QUERY_HINT

    missing = ask(kb, "量子纠缠")
    assert missing.hits == []
    assert missing.hint == NO_HIT_HINT


# --------------------------------------------------------------------------- 幂等导入
def test_add_is_idempotent_and_writes_fts_rows(
    kb: KnowledgeBase, cfg: Config, tmp_path: Path
) -> None:
    docs = tmp_path / "docs"
    _write(docs / "demo_a.md", "# 文档 A\n\n## 小节\n\n客单价与毛利率的说明。")
    _write(docs / "demo_b.md", "# 文档 B\n\n## 小节\n\n冲泡方法与水温说明。")

    first = kb.add([docs])
    assert first["docs"] == 2
    assert first["skipped"] == 0
    assert first["chunks"] >= 2

    second = kb.add([docs])
    assert second == {"docs": 0, "skipped": 2, "chunks": 0}

    assert _count(cfg.db_path, "SELECT COUNT(*) FROM kb_doc") == 2
    chunks = _count(cfg.db_path, "SELECT COUNT(*) FROM kb_chunk")
    fts_rows = _count(cfg.db_path, "SELECT COUNT(*) FROM kb_fts")
    assert chunks >= 2
    assert fts_rows == chunks


def test_add_replaces_changed_source_path(kb: KnowledgeBase, cfg: Config, tmp_path: Path) -> None:
    doc = _write(tmp_path / "demo_change.md", "# 旧标题\n\n## 旧小节\n\n旧内容，苹果。")
    assert kb.add([doc])["docs"] == 1
    old_chunks = _count(cfg.db_path, "SELECT COUNT(*) FROM kb_chunk")

    _write(doc, "# 新标题\n\n## 新小节\n\n新内容，香蕉，客单价说明。")
    assert kb.add([doc])["docs"] == 1

    assert _count(cfg.db_path, "SELECT COUNT(*) FROM kb_doc") == 1
    new_chunks = _count(cfg.db_path, "SELECT COUNT(*) FROM kb_chunk")
    assert new_chunks != old_chunks or new_chunks == 1
    assert _count(cfg.db_path, "SELECT COUNT(*) FROM kb_fts") == new_chunks
    hits = kb.search("客单价说明", k=3)
    assert hits
    assert hits[0].title == "新标题"
    assert kb.search("苹果", k=3) == []
    assert kb.search("香蕉", k=3)


def test_build_kb_turns_storage_failure_into_unavailable(tmp_path: Path) -> None:
    cfg = Config.from_env({"WB_HOME": str(tmp_path / "bad"), "WB_LLM": "stub"})
    # 让数据库路径是一个目录，SQLite 无法打开 → 工厂必须抛 Unavailable 供上层降级。
    cfg.db_path.mkdir(parents=True)

    with pytest.raises(Unavailable):
        build_kb(cfg)


# --------------------------------------------------------------------------- stage
def test_kb_stages_are_registered_and_run(cfg: Config, tmp_path: Path) -> None:
    load_stages()
    assert has_stage("kb_ingest")
    assert has_stage("kb_ask")

    conn = init_db(cfg.db_path)
    ctx = RunContext(
        run_id="run_test",
        pipeline="copy",
        home=cfg.home,
        out=tmp_path / "out",
        db=conn,
        llm=StubLLM(),
        cfg=cfg,
        params={"path": str(SEED_DIR), "q": "客单价怎么解释", "k": "3"},
        log=lambda _message: None,
    )
    try:
        ingest = KbIngest().run(ctx)
        assert ingest["docs"] == 7
        assert ingest["chunks"] > 0

        answer = KbAsk().run(ctx)
        assert answer["query"] == "客单价怎么解释"
        assert len(answer["hits"]) >= 3
        assert answer["hits"][0]["doc_title"] == PRICE_DOC
    finally:
        conn.close()


# --------------------------------------------------------------------------- CLI 与评测
def test_cli_kb_add_ask_json_and_idempotent(cli_env: Path) -> None:
    first = _invoke(["kb", "add", str(SEED_DIR)])
    assert first.exit_code == 0, _combined(first)
    assert "docs=7 chunks=" in _combined(first)

    second = _invoke(["kb", "add", str(SEED_DIR)])
    assert second.exit_code == 0, _combined(second)
    assert "docs=0 skipped=7" in _combined(second)

    question = _invoke(["kb", "ask", "客单价怎么解释", "--json", "--k", "3"])
    assert question.exit_code == 0, _combined(question)
    payload = json.loads(question.output)
    assert payload["query"] == "客单价怎么解释"
    assert len(payload["hits"]) >= 3
    for hit in payload["hits"]:
        assert set(hit) == {"doc_id", "chunk_id", "doc_title", "heading", "score", "snippet"}
        assert 0.0 <= hit["score"] <= 1.0
        assert len(hit["snippet"]) <= 80
    assert payload["hits"][0]["doc_title"] == PRICE_DOC

    table = _invoke(["kb", "ask", "客单价怎么解释"])
    output = _combined(table)
    assert table.exit_code == 0, output
    assert "doc_title" in output
    assert "score" in output
    assert "片段" in output
    assert PRICE_DOC in output


def test_cli_kb_ask_handles_empty_single_and_no_hit(cli_env: Path) -> None:
    for query in ("", "价", "量子纠缠"):
        result = _invoke(["kb", "ask", query, "--json"])
        assert result.exit_code == 0, _combined(result)
        payload = json.loads(result.output)
        assert payload["hits"] == []
        assert payload["hint"]


def test_cli_kb_eval_passes_seed_and_fails_at_threshold_one(cli_env: Path) -> None:
    added = _invoke(["kb", "add", str(SEED_DIR)])
    assert added.exit_code == 0, _combined(added)

    passed = _invoke(["kb", "eval"])
    output = _combined(passed)
    assert passed.exit_code == 0, output
    assert "recall@3=0." in output
    assert "(>=0.70)" in output
    assert "通过" in output

    strict = _invoke(["kb", "eval", "--threshold", "1.0"])
    strict_output = _combined(strict)
    assert strict.exit_code == 1, strict_output
    assert "(>=1.00)" in strict_output
    assert "未通过" in strict_output


def test_cli_kb_eval_custom_pass_and_fail_paths(cli_env: Path, tmp_path: Path) -> None:
    added = _invoke(["kb", "add", str(SEED_DIR)])
    assert added.exit_code == 0, _combined(added)

    passing = _write(
        tmp_path / "eval_pass.jsonl",
        json.dumps({"q": "客单价怎么解释", "doc": PRICE_DOC}, ensure_ascii=False) + "\n",
    )
    ok = _invoke(["kb", "eval", "--eval", str(passing)])
    assert ok.exit_code == 0, _combined(ok)
    assert "recall@3=1.00" in _combined(ok)

    failing = _write(
        tmp_path / "eval_fail.jsonl",
        json.dumps({"q": "客单价怎么解释", "doc": "不存在的文档"}, ensure_ascii=False) + "\n",
    )
    bad = _invoke(["kb", "eval", "--eval", str(failing)])
    assert bad.exit_code == 1, _combined(bad)
    assert "recall@3=0.00" in _combined(bad)
    assert "不存在的文档" in _combined(bad)


def test_load_eval_and_evaluate_report_misses(kb: KnowledgeBase) -> None:
    kb.add([SEED_DIR])
    items = load_eval(EVAL_FILE)
    assert len(items) == 20
    assert all(item.query and item.doc_title for item in items)

    report = evaluate(kb, items, k=3)
    assert report.total == 20
    assert 0.7 <= report.recall < 1.0
    assert report.hits + len(report.misses) == report.total
    assert report.misses


def test_cli_kb_help_lists_subcommands(cli_env: Path) -> None:
    help_text = _combined(_invoke(["kb", "--help"]))
    assert "add" in help_text
    assert "ask" in help_text
    assert "eval" in help_text


def test_cli_root_help_lists_kb(cli_env: Path) -> None:
    help_text = _combined(_invoke(["--help"]))
    assert "kb" in help_text
