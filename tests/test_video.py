"""T07 验收测试：分镜模板/校验、时长截断、ffmpeg 参数、产物探测、静音降级、asset 入库。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from workbench.cli import app
from workbench.config import Config
from workbench.contracts import get_composer
from workbench.pipeline.runner import run_pipeline
from workbench.store import repo
from workbench.store.db import init_db
from workbench.video import build as video_build
from workbench.video import tts
from workbench.video.build import CardVideoComposer
from workbench.video.compose import (
    build_ffmpeg_args,
    ffmpeg_exe,
    probe_video,
    run_command,
)
from workbench.video.frame import Segment
from workbench.video.storyboard import (
    MAX_POINTS,
    MAX_SECONDS,
    ValidationError,
    load_default_template,
    parse_storyboard,
)


def _invoke(args: list[str]) -> Result:
    return CliRunner().invoke(app, args)


def _combined(result: Result) -> str:
    text = result.output
    stderr = getattr(result, "stderr", "")
    if stderr:
        text += stderr
    return text


def _write_storyboard(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _short_storyboard(*, tts_enabled: bool = False) -> dict[str, Any]:
    return {
        "title": "测试分镜",
        "tts": tts_enabled,
        "scenes": [
            {"type": "title", "text": "第一张卡片", "seconds": 1},
            {"type": "cta", "text": "最后一句话", "seconds": 1},
        ],
    }


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("WB_HOME", str(home))
    monkeypatch.setenv("WB_LLM", "stub")
    monkeypatch.delenv("WB_LLM_API_KEY", raising=False)
    monkeypatch.delenv("WB_FONT_DIR", raising=False)
    monkeypatch.delenv("WB_TEMPLATE_DIR", raising=False)
    return home


def test_default_template_covers_three_card_types() -> None:
    data = load_default_template()
    board = parse_storyboard(data)

    assert len(board.scenes) >= 3
    assert {scene.type for scene in board.scenes} == {"title", "points", "cta"}
    assert board.seconds <= MAX_SECONDS
    assert all(len(scene.points) <= MAX_POINTS for scene in board.scenes)


def test_cli_help_lists_video_subcommands() -> None:
    result = _invoke(["video", "--help"])
    output = _combined(result)

    assert result.exit_code == 0, output
    assert "template" in output
    assert "build" in output


def test_cli_template_prints_usable_json(cli_env: Path) -> None:
    result = _invoke(["video", "template"])
    output = _combined(result)

    assert result.exit_code == 0, output
    data = json.loads(result.output)
    board = parse_storyboard(data)
    assert len(board.scenes) >= 3


def _empty_text(data: dict[str, Any]) -> None:
    data["scenes"][0]["text"] = "   "


def _too_many_points(data: dict[str, Any]) -> None:
    data["scenes"][0]["points"] = [f"要点 {index}" for index in range(MAX_POINTS + 1)]


def _unknown_card_type(data: dict[str, Any]) -> None:
    data["scenes"][0]["type"] = "quote"


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (_empty_text, "text"),
        (_too_many_points, "points"),
        (_unknown_card_type, "type"),
    ],
    ids=["empty-text", "too-many-points", "unknown-type"],
)
def test_storyboard_validation_rejects_bad_input(
    mutate: Any, match: str
) -> None:
    data: dict[str, Any] = {
        "title": "校验失败",
        "scenes": [
            {"type": "points", "text": "标题", "points": ["一条"], "seconds": 2}
        ],
    }
    mutate(data)

    with pytest.raises(ValidationError, match=match):
        parse_storyboard(data)


def test_storyboard_scales_90_seconds_to_60() -> None:
    data = {
        "title": "长分镜",
        "scenes": [
            {"type": "title", "text": "第一段", "seconds": 30},
            {"type": "points", "text": "第二段", "points": ["要点"], "seconds": 30},
            {"type": "cta", "text": "第三段", "seconds": 30},
        ],
    }

    board = parse_storyboard(data)

    assert board.original_seconds == pytest.approx(90.0)
    assert board.seconds == pytest.approx(MAX_SECONDS)
    assert all(scene.seconds == pytest.approx(20.0) for scene in board.scenes)
    assert any("90" in warning and "60" in warning for warning in board.warnings)


def test_points_card_requires_points() -> None:
    data = {
        "title": "空要点",
        "scenes": [{"type": "points", "text": "标题", "points": [], "seconds": 2}],
    }

    with pytest.raises(ValidationError, match="points 不能为空"):
        parse_storyboard(data)


def test_ffmpeg_command_assembly_with_fake_binary(tmp_path: Path) -> None:
    first = tmp_path / "frame_01.png"
    second = tmp_path / "frame_02.png"
    first.write_bytes(b"fake")
    second.write_bytes(b"fake")
    segments = [Segment(path=first, seconds=1.5), Segment(path=second, seconds=2.5)]
    out = tmp_path / "out.mp4"

    args = build_ffmpeg_args(segments=segments, out=out, duration_sec=4.0)

    record = tmp_path / "argv.txt"
    fake = tmp_path / "fake-ffmpeg"
    fake.write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$@" > "{record}"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    run_command(str(fake), args, cwd=tmp_path)

    recorded = record.read_text(encoding="utf-8").splitlines()
    assert recorded.count("-loop") == 2
    assert recorded.count("-i") == 2
    assert recorded[recorded.index("-t") + 1] == "1.500"
    filter_chain = recorded[recorded.index("-filter_complex") + 1]
    assert "scale=1080:1920" in filter_chain
    assert "concat=n=2:v=1:a=0[outv]" in filter_chain
    assert recorded[recorded.index("-c:v") + 1] == "libx264"
    assert recorded[recorded.index("-crf") + 1] == "23"
    assert recorded[recorded.index("-preset") + 1] == "veryfast"
    assert recorded[recorded.index("-r") + 1] == "25"
    assert recorded[-1] == str(out)
    assert not out.exists()


def test_ffmpeg_args_include_audio_duration_cap() -> None:
    segments = [
        Segment(path=Path("frame_01.png"), seconds=2.0),
        Segment(path=Path("frame_02.png"), seconds=3.0),
    ]

    args = build_ffmpeg_args(
        segments=segments,
        out="out.mp4",
        duration_sec=5.0,
        audio="narration.mp3",
    )

    assert "narration.mp3" in args
    assert args[args.index("2:a") - 1] == "-map"
    assert args[args.index("-af") + 1] == "apad"
    last_t = len(args) - 1 - args[::-1].index("-t")
    assert args[last_t + 1] == "5.000"


def test_default_template_produces_vertical_h264_video(tmp_path: Path) -> None:
    data = load_default_template()
    expected_seconds = sum(float(scene["seconds"]) for scene in data["scenes"])
    out = tmp_path / "default.mp4"

    result = CardVideoComposer().compose(storyboard=data, out=out)
    info = probe_video(out)

    assert result["bytes"] > 50 * 1024
    assert (info.width, info.height) == (1080, 1920)
    assert info.codec == "h264"
    assert info.fps == pytest.approx(25.0)
    assert info.duration_sec == pytest.approx(expected_seconds, abs=0.5)
    assert result["meta"] == {"scenes": 3, "fps": 25, "tts": False}


def test_cli_tts_failure_degrades_to_silent(
    cli_env: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def unavailable(*_args: Any, **_kwargs: Any) -> None:
        raise FileNotFoundError("edge-tts")

    monkeypatch.setattr(tts, "subprocess_run", unavailable)
    storyboard = _write_storyboard(
        tmp_path / "sb.json", _short_storyboard(tts_enabled=True)
    )
    out = tmp_path / "tts.mp4"

    with caplog.at_level(logging.WARNING, logger="workbench.video"):
        result = _invoke(
            ["video", "build", "--storyboard", str(storyboard), "--out", str(out), "--tts"]
        )
    output = _combined(result)

    assert result.exit_code == 0, output
    assert "配音失败" in output
    assert any("配音失败" in record.message for record in caplog.records)
    info = probe_video(out)
    assert info.has_audio is False
    assert info.duration_sec == pytest.approx(2.0, abs=0.5)


def test_tts_audio_is_padded_or_trimmed_to_storyboard_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_synthesize(
        _text: str, out: Path | str, **_kwargs: Any
    ) -> tuple[bool, str | None]:
        audio = Path(out)
        run_command(
            ffmpeg_exe(),
            [
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=5",
                "-c:a",
                "libmp3lame",
                str(audio),
            ],
            cwd=audio.parent,
        )
        return True, None

    monkeypatch.setattr(video_build, "synthesize", fake_synthesize)
    data = _short_storyboard(tts_enabled=True)  # 分镜 2 秒，配音 5 秒
    out = tmp_path / "tts-ok.mp4"

    result = CardVideoComposer().compose(storyboard=data, out=out)
    info = probe_video(out)

    assert result["meta"]["tts"] is True
    assert info.has_audio is True
    assert info.duration_sec == pytest.approx(2.0, abs=0.5)


def test_contract_factory_returns_composer() -> None:
    assert isinstance(get_composer(), CardVideoComposer)


def test_video_stage_persists_asset(tmp_path: Path) -> None:
    cfg = Config.from_env({"WB_HOME": str(tmp_path / "home"), "WB_LLM": "stub"})
    storyboard = _write_storyboard(tmp_path / "sb.json", _short_storyboard())
    out = cfg.home / "runs" / "video"

    run = run_pipeline(cfg, "video", params={"storyboard": str(storyboard)}, out=out)
    output = run.outputs["video_build"]

    conn = init_db(cfg.db_path)
    try:
        assets = repo.list_assets(conn, kind="video")
    finally:
        conn.close()

    assert len(assets) == 1
    asset = assets[0]
    assert asset.width == 1080
    assert asset.height == 1920
    assert asset.meta == {"scenes": 2, "fps": 25, "tts": False}
    assert output["asset_id"] == asset.id

    video = Path(asset.path)
    if not video.is_absolute():
        video = cfg.home / video
    info = probe_video(video)
    assert asset.duration_sec == pytest.approx(info.duration_sec, abs=0.2)
    assert asset.bytes == video.stat().st_size
    assert info.duration_sec == pytest.approx(2.0, abs=0.5)
    assert info.codec == "h264"
