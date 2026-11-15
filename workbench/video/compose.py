"""ffmpeg 组装与产物探测（T07）。

只用 ``imageio_ffmpeg.get_ffmpeg_exe()` 返回的二进制，不依赖系统 ffmpeg。

组装方式：每张卡片作为独立输入（``-loop 1 -t <seconds>``），再用 concat
filter 串起来，最后统一 ``scale=1080:1920,format=yuv420p`` 并以 H.264 / 25fps
编码。这比 concat demuxer 的 ``duration`` 指令更可控——后者在部分 ffmpeg 版本
里会让末帧多播约 1 秒，导致总时长偏移。

``build_ffmpeg_args`` 是纯函数，测试用假二进制断言参数；``probe_video`` 通过
``ffmpeg -i`` 的 stderr 解析宽高、编码、帧率与时长。
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import imageio_ffmpeg

from workbench.errors import WorkbenchError
from workbench.video.frame import Segment

FPS = 25
WIDTH = 1080
HEIGHT = 1920

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_CODEC_RE = re.compile(r"Video:\s*([A-Za-z0-9_\-]+)")
_SIZE_RE = re.compile(r"(\d{2,5})x(\d{2,5})")
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*fps")


@dataclass(frozen=True, slots=True)
class VideoInfo:
    """``ffmpeg -i`` 解析出的实际产物信息。"""

    width: int
    height: int
    codec: str
    fps: float
    duration_sec: float
    has_audio: bool


def ffmpeg_exe() -> str:
    """``imageio-ffmpeg`` 自带的 ffmpeg 路径。"""
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as exc:  # noqa: BLE001 - 缺二进制时给出可读错误
        raise WorkbenchError(f"找不到 ffmpeg（imageio-ffmpeg）：{exc}") from exc


def _video_filter(segment_count: int) -> str:
    """先把每路缩放到目标画布，再 concat；所有输入格式/SAR 必须一致。"""
    chains = [
        f"[{index}:v]scale={WIDTH}:{HEIGHT},format=yuv420p,setsar=1[v{index}]"
        for index in range(segment_count)
    ]
    labels = "".join(f"[v{index}]" for index in range(segment_count))
    chains.append(f"{labels}concat=n={segment_count}:v=1:a=0[outv]")
    return ";".join(chains)


def build_ffmpeg_args(
    *,
    segments: Sequence[Segment],
    out: Path | str,
    duration_sec: float,
    audio: str | Path | None = None,
) -> list[str]:
    """拼装 ffmpeg 参数；``audio`` 存在时合入 AAC 并按分镜时长封顶。"""
    if not segments:
        raise WorkbenchError("没有可拼接的卡片帧")

    args: list[str] = ["-y"]
    for segment in segments:
        args += ["-loop", "1", "-t", f"{segment.seconds:.3f}", "-i", segment.path.name]
    if audio is not None:
        args += ["-i", str(audio)]

    args += [
        "-filter_complex",
        _video_filter(len(segments)),
        "-map",
        "[outv]",
    ]
    if audio is not None:
        args += ["-map", f"{len(segments)}:a"]
    args += [
        "-r",
        str(FPS),
        "-c:v",
        "libx264",
        "-crf",
        "23",
        "-preset",
        "veryfast",
    ]
    if audio is not None:
        # apad 补足短音频，-t 截断长音频：视频时长始终以分镜为准。
        args += [
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-af",
            "apad",
            "-t",
            f"{max(0.04, duration_sec):.3f}",
        ]
    args.append(str(out))
    return args


def run_command(
    executable: str,
    args: Sequence[str],
    *,
    cwd: Path | str | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess[str]:
    """执行外部命令；非零退出码转成带 stderr 摘要的 ``WorkbenchError``。"""
    try:
        result = subprocess.run(
            [executable, *args],
            cwd=None if cwd is None else str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkbenchError(f"命令执行失败：{type(exc).__name__}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-1500:]
        raise WorkbenchError(f"命令返回码 {result.returncode}：{detail}")
    return result


def _parse_duration(text: str) -> float:
    match = _DURATION_RE.search(text)
    if match is None:
        raise WorkbenchError("无法从 ffmpeg 输出解析时长")
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def probe_video(path: Path | str) -> VideoInfo:
    """用 ``ffmpeg -i`` 解析产物；``-i`` 无输出文件时退出码为 1，属正常。"""
    target = Path(path).expanduser()
    if not target.is_file():
        raise WorkbenchError(f"视频文件不存在：{target}")
    result = subprocess.run(
        [ffmpeg_exe(), "-hide_banner", "-i", str(target)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    text = result.stderr or ""
    if "No such file" in text:
        raise WorkbenchError(f"ffmpeg 无法读取视频：{target}")

    stream_line = next(
        (line for line in text.splitlines() if "Video:" in line),
        "",
    )
    codec_match = _CODEC_RE.search(stream_line)
    size_match = _SIZE_RE.search(stream_line)
    fps_match = _FPS_RE.search(stream_line)
    if codec_match is None or size_match is None or fps_match is None:
        detail = text.strip()[-800:]
        raise WorkbenchError(f"无法解析 ffmpeg 输出（{target}）：{detail}")

    return VideoInfo(
        width=int(size_match.group(1)),
        height=int(size_match.group(2)),
        codec=codec_match.group(1),
        fps=float(fps_match.group(1)),
        duration_sec=_parse_duration(text),
        has_audio="Audio:" in text,
    )


__all__ = [
    "FPS",
    "HEIGHT",
    "WIDTH",
    "VideoInfo",
    "build_ffmpeg_args",
    "ffmpeg_exe",
    "probe_video",
    "run_command",
]
