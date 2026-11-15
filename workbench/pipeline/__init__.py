"""Stage 注册表、流水线定义与 runner。"""

from workbench.pipeline.definitions import PIPELINES, stages_for
from workbench.pipeline.registry import get_stage, has_stage, register
from workbench.pipeline.runner import RunResult, run_pipeline
from workbench.pipeline.stage import RunContext, Stage

__all__ = [
    "PIPELINES",
    "RunContext",
    "RunResult",
    "Stage",
    "get_stage",
    "has_stage",
    "register",
    "run_pipeline",
    "stages_for",
]
