"""Stage 与 RunContext 契约（ADR §6.1）。"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from workbench.config import Config
from workbench.llm.base import LLMClient


@dataclass(slots=True)
class RunContext:
    run_id: str
    pipeline: str
    home: Path
    out: Path
    db: sqlite3.Connection
    llm: LLMClient
    cfg: Config
    params: dict[str, str]
    log: Callable[[str], None]


class Stage(Protocol):
    name: str
    needs: tuple[str, ...]

    def run(self, ctx: RunContext) -> dict[str, Any]: ...


__all__ = ["RunContext", "Stage"]
