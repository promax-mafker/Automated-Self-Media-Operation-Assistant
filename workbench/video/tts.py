"""可选配音：调用 ``edge-tts`` 生成 MP3；任何失败都返回 ``(False, 原因)``。

上游（``build.CardVideoComposer``）据此降级为静音视频，不抛异常、不改变退出码。
真实配音需要网络；离线环境只会走降级分支。``subprocess_run`` 是模块级别名，
测试可以只替换它而不影响 ffmpeg 调用。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("workbench.video")

VOICE = "zh-CN-XiaoxiaoNeural"
DEFAULT_TIMEOUT = 120
subprocess_run = subprocess.run


def synthesize(
    text: str,
    out: Path | str,
    *,
    voice: str = VOICE,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bool, str | None]:
    """用 ``edge-tts`` 把文本合成为 MP3。

    返回 ``(是否成功, 失败原因)``；成功时 ``out`` 是可用的音频文件。
    """
    audio = Path(out)
    if not text.strip():
        return False, "配音文本为空"
    audio.parent.mkdir(parents=True, exist_ok=True)
    executable = shutil.which("edge-tts") or "edge-tts"
    command = [
        executable,
        "--voice",
        voice,
        "--text",
        text,
        "--write-media",
        str(audio),
    ]
    try:
        result = subprocess_run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"edge-tts 调用失败：{type(exc).__name__}: {exc}"

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return False, f"edge-tts 返回码 {result.returncode}：{detail[:300]}"
    if not audio.is_file() or audio.stat().st_size == 0:
        return False, "edge-tts 没有生成音频文件"
    logger.info("配音已生成：%s（%d bytes）", audio, audio.stat().st_size)
    return True, None


__all__ = ["DEFAULT_TIMEOUT", "VOICE", "synthesize"]
