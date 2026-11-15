"""分镜 JSON 的解析与校验（T07）。

分镜结构（字段名固定，供 ``VideoComposer.compose`` 消费）::

    {
      "title": "整条视频的主题（也用作页脚）",
      "tts": false,
      "scenes": [
        {"type": "title",  "text": "大标题", "seconds": 3},
        {"type": "points", "text": "要点标题", "points": ["要点 1", "要点 2"], "seconds": 6},
        {"type": "cta",    "text": "结尾引导", "seconds": 3}
      ]
    }

- 总时长超过 60 秒时按比例压缩到 60 秒，并在 ``warnings`` 里给出原始/压缩后时长；
- 每张 ``points`` 卡最多 5 条要点，``points`` 卡至少要有一条；
- 空 ``title`` / 空 ``text`` / 非法 ``type`` / 非正 ``seconds`` 抛 ``ValidationError``。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workbench.errors import WorkbenchError

MAX_SECONDS = 60.0
MAX_POINTS = 5
CARD_TYPES: tuple[str, ...] = ("title", "points", "cta")

_REPO_ROOT = Path(__file__).resolve().parents[2]


class ValidationError(WorkbenchError):
    """分镜 JSON 不合法；CLI 退出码为 1。"""


@dataclass(slots=True)
class Scene:
    """一张卡片。``seconds`` 已按 60 秒上限缩放。"""

    type: str
    text: str
    seconds: float
    points: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Storyboard:
    """校验、截断后的分镜。"""

    title: str
    scenes: list[Scene]
    tts: bool = False
    original_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        """缩放后的总时长（秒）。"""
        return sum(scene.seconds for scene in self.scenes)


def _clean_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} 不能为空")
    return value.strip()


def _positive_seconds(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{label} 必须是数字（秒）")
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValidationError(f"{label} 必须大于 0 秒")
    return seconds


def parse_storyboard(data: Any) -> Storyboard:
    """校验分镜 dict 并返回 ``Storyboard``；超长时按比例压到 60 秒。"""
    if not isinstance(data, dict):
        raise ValidationError("分镜 JSON 顶层必须是对象")

    title = _clean_text(data.get("title"), "分镜 title")
    raw_scenes = data.get("scenes")
    if not isinstance(raw_scenes, list) or not raw_scenes:
        raise ValidationError("分镜 scenes 必须是非空数组")

    scenes: list[Scene] = []
    for index, raw in enumerate(raw_scenes, start=1):
        if not isinstance(raw, dict):
            raise ValidationError(f"第 {index} 个场景必须是对象")
        card_type = raw.get("type")
        if card_type not in CARD_TYPES:
            raise ValidationError(
                f"第 {index} 个场景 type 不合法：{card_type!r}；"
                f"可选：{', '.join(CARD_TYPES)}"
            )
        text = _clean_text(raw.get("text"), f"第 {index} 个场景 text")
        seconds = _positive_seconds(raw.get("seconds"), f"第 {index} 个场景 seconds")

        raw_points = raw.get("points", [])
        if raw_points is None:
            raw_points = []
        if not isinstance(raw_points, list):
            raise ValidationError(f"第 {index} 个场景 points 必须是数组")
        if len(raw_points) > MAX_POINTS:
            raise ValidationError(
                f"第 {index} 个场景 points 最多 {MAX_POINTS} 条，当前 {len(raw_points)} 条"
            )
        points = [
            _clean_text(item, f"第 {index} 个场景 points 第 {point_index} 条")
            for point_index, item in enumerate(raw_points, start=1)
        ]
        if card_type == "points" and not points:
            raise ValidationError(f"第 {index} 个场景是 points 卡，points 不能为空")

        scenes.append(Scene(type=card_type, text=text, seconds=seconds, points=points))

    tts = data.get("tts", False)
    if tts is None:
        tts = False
    if not isinstance(tts, bool):
        raise ValidationError("分镜 tts 必须是布尔值")

    original_seconds = sum(scene.seconds for scene in scenes)
    warnings: list[str] = []
    if original_seconds > MAX_SECONDS:
        factor = MAX_SECONDS / original_seconds
        scaled = [scene.seconds * factor for scene in scenes]
        # 把浮点误差收回最后一张卡，保证总和精确等于 60 秒。
        scaled[-1] = MAX_SECONDS - sum(scaled[:-1])
        for scene, seconds in zip(scenes, scaled, strict=True):
            scene.seconds = seconds
        warnings.append(
            f"分镜总时长 {original_seconds:.1f}s 超过上限 {MAX_SECONDS:.0f}s，"
            f"已按比例压缩到 {MAX_SECONDS:.1f}s"
        )

    return Storyboard(
        title=title,
        scenes=scenes,
        tts=tts,
        original_seconds=original_seconds,
        warnings=warnings,
    )


def narration_text(storyboard: Storyboard) -> str:
    """把分镜文字拼成配音稿：场景标题 + 要点，按句读拼接。"""
    chunks: list[str] = []
    for scene in storyboard.scenes:
        text = scene.text.strip()
        if scene.type == "points" and scene.points:
            text = f"{text}。" + "；".join(scene.points)
        if text and text[-1] not in "。！？!?；;":
            text += "。"
        chunks.append(text)
    return "\n".join(chunks)


def _repository_templates() -> Path:
    raw = os.environ.get("WB_TEMPLATE_DIR")
    if raw is not None and raw.strip():
        return Path(raw).expanduser().resolve()
    return _REPO_ROOT / "templates"


def default_template_path() -> Path:
    """内置分镜模板路径：``WB_TEMPLATE_DIR`` 或仓库 ``templates/``。"""
    return _repository_templates() / "video" / "default.json"


def load_default_template() -> dict[str, Any]:
    """读取 ``templates/video/default.json``（``wb video template`` 的来源）。"""
    path = default_template_path()
    if not path.is_file():
        raise WorkbenchError(f"找不到内置分镜模板：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WorkbenchError(f"读取内置分镜模板失败：{path}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise WorkbenchError(f"内置分镜模板不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(data, dict):
        raise WorkbenchError(f"内置分镜模板顶层必须是对象：{path}")
    return data


def load_storyboard_file(path: Path | str) -> dict[str, Any]:
    """读取分镜 JSON 文件；返回未校验的 dict，交给 ``parse_storyboard``。"""
    target = Path(path).expanduser()
    if not target.is_file():
        raise WorkbenchError(f"分镜文件不存在：{target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WorkbenchError(f"读取分镜文件失败：{target}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"分镜文件不是合法 JSON：{target}（{exc}）") from exc
    if not isinstance(data, dict):
        raise ValidationError(f"分镜文件顶层必须是对象：{target}")
    return data


__all__ = [
    "CARD_TYPES",
    "MAX_POINTS",
    "MAX_SECONDS",
    "Scene",
    "Storyboard",
    "ValidationError",
    "default_template_path",
    "load_default_template",
    "load_storyboard_file",
    "narration_text",
    "parse_storyboard",
]
