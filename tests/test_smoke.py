"""端到端冒烟：CLI doctor / demo 流水线 / 缺失 stage / runner 失败 / Null 适配器。"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest
from click.testing import Result
from PIL import Image
from typer.testing import CliRunner

from workbench.cli import app
from workbench.config import Config
from workbench.contracts import (
    get_composer,
    get_publisher,
    get_source_adapters,
)
from workbench.errors import PipelineFailed, Unavailable
from workbench.pipeline import runner
from workbench.pipeline.runner import run_pipeline
from workbench.stages import load_stages
from workbench.store import repo
from workbench.store.db import init_db


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
    if stderr:
        text += stderr
    return text


def _count(path: Path, sql: str) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute(sql).fetchone()[0])
    finally:
        connection.close()


def test_doctor_reports_environment_and_pipeline_coverage(cli_env: Path) -> None:
    result = _invoke(["doctor"])
    output = _combined(result)

    assert result.exit_code == 0, output
    assert "WB_HOME:" in output
    assert "user_version:" in output
    assert "LLM: stub" in output
    assert "model: stub" in output
    for pipeline in ("review", "topic", "copy", "visual", "video", "pack", "demo"):
        assert pipeline in output
    assert "demo_source" in output
    assert "missing" in output
    assert "ok" in output


def test_run_demo_writes_manifest_db_rows_and_valid_png(cli_env: Path, tmp_path: Path) -> None:
    out = tmp_path / "wbdemo"
    result = _invoke(["run", "demo", "--out", str(out)])
    output = _combined(result)
    assert result.exit_code == 0, output

    manifest_path = out / "manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["pipeline"] == "demo"
    assert manifest["run_id"].startswith("run_")
    assert len(manifest["stages"]) == 5
    assert [item["name"] for item in manifest["stages"]] == [
        "demo_source",
        "demo_topic",
        "demo_copy",
        "demo_asset",
        "demo_report",
    ]
    assert all(item["status"] == "done" for item in manifest["stages"])

    report = out / "report.md"
    assert report.is_file()
    assert report.read_text(encoding="utf-8").strip()

    db_path = cli_env / "wb.sqlite3"
    assert _count(db_path, "SELECT COUNT(*) FROM job WHERE status = 'done'") == 5
    assert _count(db_path, "SELECT COUNT(*) FROM copy_set") == 3
    assert _count(db_path, "SELECT COUNT(*) FROM asset") == 1

    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute("SELECT path, width, height FROM asset").fetchone()
    finally:
        connection.close()
    asset_path = Path(row[0])
    if not asset_path.is_absolute():
        asset_path = cli_env / asset_path
    assert asset_path.is_file()
    assert (row[1], row[2]) == (1080, 1440)
    with Image.open(asset_path) as image:
        assert image.format == "PNG"
        assert image.size == (1080, 1440)

    first_run_id = manifest["run_id"]
    second = _invoke(["run", "demo", "--out", str(out)])
    assert second.exit_code == 0, _combined(second)
    second_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert second_manifest["run_id"] != first_run_id
    assert len(second_manifest["stages"]) == 5
    assert _count(db_path, "SELECT COUNT(*) FROM job WHERE status = 'done'") == 10
    assert _count(db_path, "SELECT COUNT(*) FROM copy_set") == 6
    assert _count(db_path, "SELECT COUNT(*) FROM asset") == 2


def test_run_missing_stage_exits_2_and_marks_job_failed(cli_env: Path) -> None:
    result = _invoke(["run", "pack"])
    output = _combined(result)
    assert result.exit_code == 2, output
    assert "MissingStage: pack/pack_build 尚未实现（T08）" in output

    connection = sqlite3.connect(cli_env / "wb.sqlite3")
    try:
        rows = connection.execute(
            "SELECT pipeline, stage, status, error FROM job"
        ).fetchall()
    finally:
        connection.close()
    assert len(rows) == 1
    pipeline, stage, status, error = rows[0]
    assert (pipeline, stage, status) == ("pack", "pack_build", "failed")
    assert "MissingStage: pack/pack_build 尚未实现（T08）" in error


def test_runner_stage_exception_marks_job_failed(
    cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    load_stages()
    real_get_stage = runner.get_stage

    class Boom:
        name = "demo_asset"
        needs: tuple[str, ...] = ()

        def run(self, ctx: object) -> dict[str, object]:
            raise RuntimeError("boom 故意失败")

    def fake_get_stage(name: str) -> object:
        if name == "demo_asset":
            return Boom()
        return real_get_stage(name)

    monkeypatch.setattr(runner, "get_stage", fake_get_stage)

    with pytest.raises(PipelineFailed) as excinfo:
        run_pipeline(cfg, "demo", out=tmp_path / "failed-run")
    assert "boom 故意失败" in str(excinfo.value)

    connection = init_db(cfg.db_path)
    try:
        jobs = repo.list_jobs(connection, pipeline="demo")
    finally:
        connection.close()
    failed = [job for job in jobs if job.status == "failed"]
    assert len(failed) == 1
    assert failed[0].stage == "demo_asset"
    assert failed[0].error
    assert "boom 故意失败" in failed[0].error


def test_null_adapters_degrade_without_crashing(
    cfg: Config, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # T05 实现 source adapters 后，这里改为覆盖仍未实现的 T07/T08 适配器。
    with caplog.at_level(logging.WARNING):
        with pytest.raises(Unavailable):
            get_composer().compose(storyboard={}, out=tmp_path / "never.mp4")
        with pytest.raises(Unavailable):
            get_publisher().build(pack={}, out=tmp_path / "never.zip")

        assert [adapter.name for adapter in get_source_adapters(cfg)] == [
            "hotlist",
            "rss",
            "local",
        ]

    assert "退回 Null 实现" in caplog.text


def test_cli_help_lists_commands_and_run_options(cli_env: Path) -> None:
    root_help = _combined(_invoke(["--help"]))
    assert "doctor" in root_help
    assert "db" in root_help
    assert "run" in root_help

    run_help = _combined(_invoke(["run", "--help"]))
    assert "--out" in run_help
    assert "--stop-after" in run_help
    assert "--input" in run_help

    db_help = _combined(_invoke(["db", "--help"]))
    assert "init" in db_help


def test_cli_db_init_is_idempotent(cli_env: Path) -> None:
    first = _invoke(["db", "init"])
    assert first.exit_code == 0, _combined(first)
    assert "user_version: 1" in _combined(first)
    first_count = next(
        line for line in _combined(first).splitlines() if line.startswith("table_count:")
    )

    second = _invoke(["db", "init"])
    assert second.exit_code == 0, _combined(second)
    assert "user_version: 1" in _combined(second)
    second_count = next(
        line for line in _combined(second).splitlines() if line.startswith("table_count:")
    )
    assert first_count == second_count
    assert (cli_env / "wb.sqlite3").is_file()
