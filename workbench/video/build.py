"""``VideoComposer`` 的 Pillow + ffmpeg 实现（T07，契约入口 ``build_composer``）。"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from workbench.errors import WorkbenchError
from workbench.video.compose import (
    FPS,
    build_ffmpeg_args,
    ffmpeg_exe,
    probe_video,
    run_command,
)
from workbench.video.frame import render_storyboard
from workbench.video.storyboard import (
    Storyboard,
    narration_text,
    parse_storyboard,
)
from workbench.video.tts import synthesize

logger = logging.getLogger("workbench.video")
logger.addHandler(logging.NullHandler())


class CardVideoComposer:
    """卡片式分镜 → 竖版 MP4；配音失败自动降级静音。"""

    def compose(self, *, storyboard: dict[str, Any], out: Path) -> dict[str, Any]:
        board = storyboard if isinstance(storyboard, Storyboard) else parse_storyboard(storyboard)
        target = Path(out).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        warnings = list(board.warnings)
        tts_used = False
        executable = ffmpeg_exe()

        with tempfile.TemporaryDirectory(
            prefix="wb-video-", dir=str(target.parent)
        ) as raw_work_dir:
            work_dir = Path(raw_work_dir)
            segments = render_storyboard(board, work_dir)

            audio_name: str | None = None
            if board.tts:
                audio_path = work_dir / "narration.mp3"
                succeeded, reason = synthesize(narration_text(board), audio_path)
                if succeeded:
                    audio_name = audio_path.name
                    tts_used = True
                else:
                    message = f"配音失败，已降级为静音：{reason}"
                    warnings.append(message)
                    logger.warning(message)

            args = build_ffmpeg_args(
                segments=segments,
                out=str(target),
                duration_sec=board.seconds,
                audio=audio_name,
            )
            try:
                run_command(executable, args, cwd=work_dir)
            except WorkbenchError as exc:
                if audio_name is None:
                    raise
                message = f"配音合入失败，已降级为静音：{exc}"
                warnings.append(message)
                logger.warning(message)
                tts_used = False
                run_command(
                    executable,
                    build_ffmpeg_args(
                        segments=segments,
                        out=str(target),
                        duration_sec=board.seconds,
                    ),
                    cwd=work_dir,
                )

        info = probe_video(target)
        return {
            "path": str(target),
            "width": info.width,
            "height": info.height,
            "codec": info.codec,
            "fps": info.fps,
            "duration_sec": info.duration_sec,
            "bytes": target.stat().st_size,
            "meta": {"scenes": len(board.scenes), "fps": FPS, "tts": tts_used},
            "warnings": warnings,
            "has_audio": info.has_audio,
        }


def build_composer() -> CardVideoComposer:
    """契约固定工厂（``workbench.contracts.get_composer`` 懒加载入口）。"""
    return CardVideoComposer()


__all__ = ["CardVideoComposer", "build_composer"]
