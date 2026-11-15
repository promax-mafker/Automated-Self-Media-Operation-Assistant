"""T06 验收测试：10 条组合、裁剪、字段错误、极限词、数字出处、版本号与真实模型跳过。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path

import pytest
from click.testing import Result
from pydantic import ValidationError
from typer.testing import CliRunner

from workbench.cli import app
from workbench.config import Config, now_iso
from workbench.copy.generate import (
    COPIES_FILENAME,
    generate_copies,
    load_copies,
)
from workbench.copy.grounding import Grounding, retrieve
from workbench.copy.lint import (
    check_text,
    lint_payload,
    load_compliance_terms,
    relint_payload,
)
from workbench.copy.prompts import PromptError, load_all_prompts, load_prompt
from workbench.copy.schema import (
    KINDS,
    LENGTHS,
    CopyValidationError,
    HookCopy,
    finalize_payload,
    normalize_kinds,
    validate_payload,
)
from workbench.knowledge.build import build_kb
from workbench.llm.stub import StubLLM
from workbench.pipeline.registry import has_stage
from workbench.stages import load_stages
from workbench.store import repo
from workbench.store.db import init_db
from workbench.store.models import CopySet, Topic

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data" / "kb" / "seed"
DOC = ROOT / "docs" / "copy.md"
PROMPT_VERSION_RE = re.compile(r"^v[0-9]+\+[0-9a-f]{8}$")


def _invoke(args: list[str]) -> Result:
    return CliRunner().invoke(app, args)


def _combined(result: Result) -> str:
    text = result.output
    stderr = getattr(result, "stderr", "")
    if stderr:
        text += stderr
    return text


def _count(db_path: Path, sql: str) -> int:
    connection = sqlite3.connect(db_path)
    try:
        return int(connection.execute(sql).fetchone()[0])
    finally:
        connection.close()


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("WB_HOME", str(home))
    monkeypatch.setenv("WB_LLM", "stub")
    monkeypatch.delenv("WB_LLM_API_KEY", raising=False)
    return home


def _seed_topic(
    *,
    topic_id: str = "topic_test",
    title: str = "通勤袋泡茶怎么泡",
) -> str:
    cfg = Config.from_env()
    cfg.ensure_dirs()
    conn = init_db(cfg.db_path)
    try:
        topic = repo.get_topic(conn, topic_id)
        if topic is None:
            topic = Topic(
                id=topic_id,
                platform="xhs",
                title=title,
                angle="办公室与通勤场景",
                hook="先讲清楚怎么泡，再讲品牌",
                score=0.82,
                score_parts={"heat": 0.7, "fit": 0.9, "benchmark": 0.8, "timing": 0.9},
                source="manual",
                source_ref="t06-test",
                evidence=[{"kind": "manual", "ref": "seed", "note": "虚构测试选题"}],
                status="picked",
                created_at=now_iso(cfg.tz),
            )
            repo.insert_topic(conn, topic)
    finally:
        conn.close()
    return topic_id


def _write_bad_copies(
    path: Path,
    *,
    copy_id: str = "copy_fixture_bad",
    title: str = "全网最低价的通勤茶",
    body: str = "全网最低价，只要 99 元，今天下单更划算。",
    evidence: list[dict] | None = None,
) -> Path:
    payload = {
        "schema_version": 1,
        "topic_id": "topic_test",
        "platform": "xhs",
        "brand": None,
        "evidence": evidence or [],
        "copies": [
            {
                "id": copy_id,
                "topic_id": "topic_test",
                "kind": "note",
                "length": "short",
                "title": title,
                "hook": "办公室提神",
                "body": body,
                "tags": ["通勤"],
                "citations": [],
                "compliance": {},
                "created_at": now_iso("Asia/Shanghai"),
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- prompt
def test_prompt_files_have_versions_and_docs_explain_the_flow() -> None:
    specs = load_all_prompts()
    assert set(specs) == set(KINDS)
    for kind, spec in specs.items():
        assert spec.version.isdigit(), kind
        assert spec.prompt_version != ""
        assert PROMPT_VERSION_RE.match(spec.prompt_version)
        assert 0.0 <= spec.temperature <= 2.0
        assert spec.max_tokens > 0
        assert "{brief}" in spec.template
        assert "{evidence}" in spec.template
        assert "{brand}" in spec.template
        assert spec.path.name == f"{kind}.md"

    docs = DOC.read_text(encoding="utf-8")
    assert "改 prompt 的流程" in docs
    for kind in KINDS:
        assert kind in docs
    assert "绝对化" in docs or "极限词" in docs
    assert "数字" in docs


def test_prompt_rejects_unknown_placeholder_and_missing_front_matter(tmp_path: Path) -> None:
    directory = tmp_path / "prompts"
    directory.mkdir()
    bad_placeholder = directory / "note.md"
    bad_placeholder.write_text(
        "---\nversion: 1\nmodel: deepseek-chat\ntemperature: 0.8\nmax_tokens: 100\n---\n"
        "正文 {evil}\n",
        encoding="utf-8",
    )
    with pytest.raises(PromptError, match="非法占位符"):
        load_prompt("note", prompt_dir=directory)

    no_front_matter = directory / "note.md"
    no_front_matter.write_text("正文只有模板，没有 front-matter。\n", encoding="utf-8")
    with pytest.raises(PromptError, match="front-matter"):
        load_prompt("note", prompt_dir=directory)


# --------------------------------------------------------------------------- schema
def test_schema_missing_field_is_reported_with_kind() -> None:
    with pytest.raises(CopyValidationError) as excinfo:
        validate_payload(
            "spoken",
            "short",
            {"title": "标题", "hook": "钩子", "body": "正文", "tags": []},
        )
    message = str(excinfo.value)
    assert "kind=spoken" in message
    assert "length=short" in message
    assert "shots" in message

    with pytest.raises(ValidationError):
        HookCopy(title="标题", hook="钩子", body="正文", tags=[])


def test_hook_requires_five_variants_for_real_model_and_pads_stub() -> None:
    short = HookCopy(title="标题", hook="钩子", body="正文", tags=[], variants=["只有一个"])
    with pytest.raises(CopyValidationError, match="至少 5 条"):
        finalize_payload("hook", "short", short, is_stub=False)

    padded = finalize_payload("hook", "short", short, is_stub=True)
    assert len(padded.variants) == 5
    assert len(set(padded.variants)) == 5


def test_normalize_kinds_trims_invalid_and_keeps_canonical_order() -> None:
    assert normalize_kinds(None) == list(KINDS)
    assert normalize_kinds("note") == ["note"]
    assert normalize_kinds("hook, spoken") == ["spoken", "hook"]
    with pytest.raises(CopyValidationError, match="未知 kind"):
        normalize_kinds("other")


# --------------------------------------------------------------------------- lint
def test_lint_number_without_evidence_fails_and_with_evidence_passes() -> None:
    absolute, sensitive = load_compliance_terms()

    missing = lint_payload(
        "note",
        "short",
        {"title": "价格", "hook": "钩子", "body": "今天只要 99 元。", "tags": []},
        "",
        absolute_terms=absolute,
        sensitive_terms=sensitive,
        checked_at="2026-11-15T21:00:00+08:00",
    )
    assert missing.passed is False
    hits = [hit for hit in missing.hits if hit["kind"] == "ungrounded_number"]
    assert len(hits) == 1
    assert hits[0]["word"] == "99"
    assert hits[0]["note"] == "数字无出处"
    assert "出处" in hits[0]["suggestion"]

    grounded = lint_payload(
        "note",
        "short",
        {"title": "价格", "hook": "钩子", "body": "今天只要 99 元。", "tags": []},
        "示例价 99 元，属于虚构测算。",
        absolute_terms=absolute,
        sensitive_terms=sensitive,
        checked_at="2026-11-15T21:00:00+08:00",
    )
    assert grounded.passed is True
    assert grounded.hits == []


def test_lint_absolute_terms_suggest_replacement_and_sensitive_needs_human() -> None:
    absolute, sensitive = load_compliance_terms()
    result = check_text(
        "全网最低价，只要 99 元",
        "",
        absolute_terms=absolute,
        sensitive_terms=sensitive,
        checked_at="2026-11-15T21:00:00+08:00",
    )
    assert result.passed is False
    absolute_hits = [hit for hit in result.hits if hit["kind"] == "absolute"]
    assert any(hit["word"] == "全网最低价" for hit in absolute_hits)
    assert any(hit["suggestion"] == "更有性价比" for hit in absolute_hits)
    assert any(hit["kind"] == "ungrounded_number" for hit in result.hits)

    sensitive_result = check_text(
        "这款茶可以助眠",
        "",
        absolute_terms=absolute,
        sensitive_terms=sensitive,
        checked_at="2026-11-15T21:00:00+08:00",
    )
    assert sensitive_result.passed is False
    assert sensitive_result.needs_human is True
    assert any(hit["kind"] == "sensitive" for hit in sensitive_result.hits)


# --------------------------------------------------------------------------- generate
def test_generate_all_combinations_with_stub(cfg: Config) -> None:
    cfg.ensure_dirs()
    conn = init_db(cfg.db_path)
    topic = Topic(
        id="topic_direct",
        platform="xhs",
        title="通勤袋泡茶怎么泡",
        angle="办公室场景",
        hook="三分钟",
        score=0.8,
        score_parts={},
        source="manual",
        evidence=[],
        status="picked",
        created_at=now_iso(cfg.tz),
    )
    try:
        result = generate_copies(
            StubLLM(),
            cfg,
            topic,
            platform="xhs",
            grounding=Grounding(query=""),
        )
        assert len(result.copies) == 10
        assert result.failed == 0
        combos = {(copy.entry["kind"], copy.entry["length"]) for copy in result.copies}
        assert combos == {(kind, length) for kind in KINDS for length in LENGTHS}
        assert len({copy.entry["id"] for copy in result.copies}) == 10
        for copy in result.copies:
            assert copy.entry["prompt_version"]
            assert PROMPT_VERSION_RE.match(copy.entry["prompt_version"])
            assert copy.entry["compliance"]["passed"] is True
            if copy.entry["kind"] == "hook":
                assert len(copy.entry["variants"]) >= 5
            if copy.entry["kind"] == "spoken":
                assert copy.entry["shots"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- CLI
def test_cli_copy_gen_writes_ten_rows_and_prompt_versions(
    cli_env: Path, tmp_path: Path
) -> None:
    topic_id = _seed_topic()
    out = tmp_path / "copies"
    result = _invoke(["copy", "gen", "--topic", topic_id, "--out", str(out)])
    output = _combined(result)
    assert result.exit_code == 0, output

    payload = load_copies(out / COPIES_FILENAME)
    assert payload["count"] == 10
    assert len(payload["copies"]) == 10
    combos = {(item["kind"], item["length"]) for item in payload["copies"]}
    assert combos == {(kind, length) for kind in KINDS for length in LENGTHS}
    assert payload["model"] == "stub"

    db_path = cli_env / "wb.sqlite3"
    assert _count(db_path, "SELECT COUNT(*) FROM copy_set") == 10
    assert _count(db_path, "SELECT COUNT(DISTINCT created_at) FROM copy_set") == 1
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT kind, prompt_version, compliance FROM copy_set ORDER BY rowid"
        ).fetchall()
        stages = connection.execute(
            "SELECT stage, status FROM job WHERE pipeline = 'copy' ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()

    expected = {kind: load_prompt(kind).prompt_version for kind in KINDS}
    for kind, prompt_version, compliance in rows:
        assert prompt_version == expected[kind]
        assert json.loads(compliance)["passed"] is True
    assert stages == [("copy_generate", "done"), ("copy_lint", "done")]


def test_cli_copy_gen_kinds_filter_and_kb_citations(cli_env: Path, tmp_path: Path) -> None:
    added = _invoke(["kb", "add", str(SEED)])
    assert added.exit_code == 0, _combined(added)

    topic_id = _seed_topic()
    out = tmp_path / "copies-note"
    result = _invoke(
        ["copy", "gen", "--topic", topic_id, "--kinds", "note", "--out", str(out)]
    )
    assert result.exit_code == 0, _combined(result)

    payload = load_copies(out / COPIES_FILENAME)
    assert payload["count"] == 2
    assert {(item["kind"], item["length"]) for item in payload["copies"]} == {
        ("note", "short"),
        ("note", "long"),
    }
    assert 1 <= len(payload["evidence"]) <= 5
    assert payload["brand"]
    for item in payload["copies"]:
        assert len(item["citations"]) == len(payload["evidence"])
        required = {"doc_id", "chunk_id", "quote"}
        assert all(required <= set(citation) for citation in item["citations"])

    connection = sqlite3.connect(cli_env / "wb.sqlite3")
    try:
        citations = connection.execute(
            "SELECT citations FROM copy_set ORDER BY rowid"
        ).fetchall()
    finally:
        connection.close()
    assert len(citations) == 2
    assert all(json.loads(row[0]) for row in citations)


def test_cli_copy_lint_absolute_terms_exit_3_and_updates_copy_set(
    cli_env: Path, tmp_path: Path
) -> None:
    copy_id = "copy_fixture_bad"
    cfg = Config.from_env()
    cfg.ensure_dirs()
    conn = init_db(cfg.db_path)
    try:
        repo.insert_copy_set(
            conn,
            CopySet(
                id=copy_id,
                topic_id=None,
                kind="note",
                length="short",
                title="全网最低价的通勤茶",
                hook="办公室提神",
                body="全网最低价，只要 99 元。",
                tags=["通勤"],
                citations=[],
                compliance={},
                created_at=now_iso(cfg.tz),
            ),
        )
    finally:
        conn.close()

    file = _write_bad_copies(tmp_path / "bad-copies.json", copy_id=copy_id)
    result = _invoke(["copy", "lint", "--file", str(file)])
    output = _combined(result)
    assert result.exit_code == 3, output
    assert "全网最低价" in output
    assert "更有性价比" in output
    assert "99" in output
    assert "ungrounded_number" in output

    payload = json.loads(file.read_text(encoding="utf-8"))
    compliance = payload["copies"][0]["compliance"]
    assert compliance["passed"] is False
    assert any(hit["word"] == "全网最低价" for hit in compliance["hits"])

    connection = sqlite3.connect(cli_env / "wb.sqlite3")
    try:
        stored = connection.execute(
            "SELECT compliance FROM copy_set WHERE id = ?", (copy_id,)
        ).fetchone()[0]
    finally:
        connection.close()
    assert json.loads(stored)["passed"] is False


def test_cli_copy_lint_grounded_number_passes(cli_env: Path, tmp_path: Path) -> None:
    evidence = [
        {
            "doc_id": "kbdoc_test",
            "chunk_id": "kbchunk_test",
            "title": "价格说明（虚构）",
            "heading": "示例",
            "score": 1.0,
            "body": "假设组合装成交金额 95 元，属于虚构测算。",
        }
    ]
    file = _write_bad_copies(
        tmp_path / "good-copies.json",
        title="组合装价格说明",
        body="按假设测算，组合装 95 元。",
        evidence=evidence,
    )
    result = _invoke(["copy", "lint", "--file", str(file)])
    assert result.exit_code == 0, _combined(result)
    payload = json.loads(file.read_text(encoding="utf-8"))
    assert payload["copies"][0]["compliance"]["passed"] is True


def test_cli_copy_gen_missing_topic_and_invalid_kind(cli_env: Path, tmp_path: Path) -> None:
    missing = _invoke(["copy", "gen", "--topic", "topic_missing", "--out", str(tmp_path / "x")])
    assert missing.exit_code == 1, _combined(missing)
    assert "找不到 topic" in _combined(missing)

    topic_id = _seed_topic()
    invalid = _invoke(
        ["copy", "gen", "--topic", topic_id, "--kinds", "other", "--out", str(tmp_path / "y")]
    )
    assert invalid.exit_code == 1, _combined(invalid)
    assert "未知 kind" in _combined(invalid)


def test_cli_copy_help_and_doctor_registration(cli_env: Path) -> None:
    copy_help = _combined(_invoke(["copy", "--help"]))
    assert "gen" in copy_help
    assert "lint" in copy_help

    gen_help = _combined(_invoke(["copy", "gen", "--help"]))
    assert "--topic" in gen_help
    assert "--kinds" in gen_help
    assert "--out" in gen_help

    lint_help = _combined(_invoke(["copy", "lint", "--help"]))
    assert "--file" in lint_help

    root_help = _combined(_invoke(["--help"]))
    assert "copy" in root_help

    doctor = _combined(_invoke(["doctor"]))
    assert "copy_generate" in doctor
    assert "copy_lint" in doctor

    load_stages()
    assert has_stage("copy_generate")
    assert has_stage("copy_lint")


def test_relint_payload_keeps_evidence_and_updates_compliance(tmp_path: Path) -> None:
    payload = json.loads(
        _write_bad_copies(tmp_path / "relint.json").read_text(encoding="utf-8")
    )
    payload["evidence"] = [
        {
            "doc_id": "kbdoc_relint",
            "chunk_id": "kbchunk_relint",
            "title": "虚构资料",
            "heading": "价格",
            "score": 1.0,
            "body": "示例价 99 元。",
        }
    ]
    report = relint_payload(payload)
    assert report.failed == 1
    assert report.items[0]["passed"] is False
    # 极限词仍会失败，但 99 元已有出处，不再产生数字类 hit。
    hits = payload["copies"][0]["compliance"]["hits"]
    assert not any(hit["kind"] == "ungrounded_number" for hit in hits)


def test_retrieve_limits_evidence_to_five(cfg: Config, tmp_path: Path) -> None:
    cfg.ensure_dirs()
    kb = build_kb(cfg)
    try:
        for index in range(8):
            path = tmp_path / f"demo_copy_{index}.md"
            path.write_text(
                f"# 文案资料 {index}（虚构）\n\n## 通勤\n\n通勤袋泡茶的虚构说明 {index}。\n",
                encoding="utf-8",
            )
            kb.add([path])
        topic = {
            "id": "topic_grounding",
            "title": "通勤袋泡茶怎么泡",
            "angle": "办公室场景",
            "hook": "三分钟",
        }
        grounding = retrieve(kb, topic, k=5)
        assert len(grounding.hits) <= 5
        assert grounding.query
        assert all("通勤" in hit.body or "通勤" in (hit.heading or "") for hit in grounding.hits)
    finally:
        kb.close()


# --------------------------------------------------------------------------- real model (opt-in)
@pytest.mark.skipif(
    not os.environ.get("WB_LLM_API_KEY"),
    reason="需要真实 WB_LLM_API_KEY；CI 不跑真实模型路径",
)
def test_real_model_generation_path(
    cli_env: Path,
    allow_network: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("WB_LLM", "openai")
    cfg = Config.from_env()
    cfg.ensure_dirs()
    conn = init_db(cfg.db_path)
    try:
        topic = Topic(
            id="topic_real",
            platform="xhs",
            title="通勤袋泡茶怎么泡",
            angle="办公室场景",
            hook="三分钟",
            score=0.8,
            score_parts={},
            source="manual",
            evidence=[],
            status="picked",
            created_at=now_iso(cfg.tz),
        )
        repo.insert_topic(conn, topic)
        kb = build_kb(cfg)
        try:
            kb.add([SEED])
            grounding = retrieve(kb, topic, k=5)
        finally:
            kb.close()
        from workbench.llm.factory import build_llm

        result = generate_copies(
            build_llm(cfg),
            cfg,
            topic,
            platform="xhs",
            grounding=grounding,
        )
        assert len(result.copies) == 10
        for copy in result.copies:
            assert copy.entry["title"]
            assert copy.entry["body"]
    finally:
        conn.close()
