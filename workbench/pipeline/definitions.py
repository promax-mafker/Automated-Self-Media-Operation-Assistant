"""流水线定义（ADR §6.2，T01 固定）。"""

from __future__ import annotations

from workbench.errors import MissingStage, WorkbenchError

PIPELINES: dict[str, list[str]] = {
    "review": ["metrics_import", "review_report"],  # T02
    "topic": ["topic_source", "topic_score"],  # T05
    "copy": ["copy_generate", "copy_lint"],  # T06
    "visual": ["render_assets"],  # T04
    "video": ["video_build"],  # T07
    "pack": ["pack_build"],  # T08
    "demo": ["demo_source", "demo_topic", "demo_copy", "demo_asset", "demo_report"],  # T01
}

# stage → 负责实现的 Ticket，用于 ``wb doctor`` 与 MissingStage 提示。
STAGE_TICKETS: dict[str, str] = {
    "metrics_import": "T02",
    "review_report": "T02",
    "render_assets": "T04",
    "topic_source": "T05",
    "topic_score": "T05",
    "copy_generate": "T06",
    "copy_lint": "T06",
    "video_build": "T07",
    "pack_build": "T08",
    "demo_source": "T01",
    "demo_topic": "T01",
    "demo_copy": "T01",
    "demo_asset": "T01",
    "demo_report": "T01",
}


def stages_for(pipeline: str) -> list[str]:
    try:
        return list(PIPELINES[pipeline])
    except KeyError as exc:
        raise WorkbenchError(
            f"未知流水线：{pipeline}；可选：{', '.join(PIPELINES)}"
        ) from exc


def stage_ticket(stage: str) -> str | None:
    return STAGE_TICKETS.get(stage)


def missing_stage(pipeline: str, stage: str) -> MissingStage:
    return MissingStage(pipeline, stage, stage_ticket(stage))


__all__ = [
    "PIPELINES",
    "STAGE_TICKETS",
    "missing_stage",
    "stage_ticket",
    "stages_for",
]
