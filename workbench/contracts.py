"""适配器协议、数据类、Null 实现与懒加载工厂（ADR §6.4）。

其余 Ticket 只实现固定路径下的 ``build_*`` 工厂函数，不修改本文件：

- ``workbench.knowledge.build:build_kb(cfg)``
- ``workbench.render.build:build_renderer()``
- ``workbench.video.build:build_composer()``
- ``workbench.publish.build:build_publisher()``
- ``workbench.topics.sources:build_adapters(cfg)``

实现模块不存在或工厂抛异常时，返回 Null 实现并记一条 warning，调用方不会崩。
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from workbench.errors import Unavailable

if TYPE_CHECKING:
    from workbench.config import Config

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RawItem:
    """信源适配器返回的原始条目（T05 消费）。"""

    source: str
    title: str
    url: str | None = None
    published_at: str | None = None
    summary: str | None = None
    platform: str | None = None
    tags: list[str] = field(default_factory=list)


@dataclass(slots=True)
class KBHit:
    """知识库检索命中（T03 产出、T06 引用）。"""

    doc_id: str
    chunk_id: str
    title: str
    body: str
    score: float = 0.0


@dataclass(slots=True)
class RenderSpec:
    """渲染请求（T04 消费）。``out`` 是绝对路径。"""

    kind: str
    out: Path
    template: str | None = None
    width: int = 1080
    height: int = 1440
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RenderResult:
    """渲染产物（T04 返回）。``path`` 建议存相对 WB_HOME 的路径。"""

    path: Path
    width: int
    height: int
    bytes: int
    meta: dict[str, Any] = field(default_factory=dict)


class SourceAdapter(Protocol):
    """信源：热榜 / RSS / 对标库。"""

    name: str

    def fetch(self, *, since: str | None = None) -> list[RawItem]: ...


class KB(Protocol):
    """知识库：检索与导入。"""

    def search(self, query: str, *, k: int = 5) -> list[KBHit]: ...

    def add(self, paths: Sequence[Path]) -> dict[str, int]: ...


class Renderer(Protocol):
    """图文 / 海报渲染器。"""

    def render(self, spec: RenderSpec) -> RenderResult: ...


class VideoComposer(Protocol):
    """分镜 → 视频合成器。"""

    def compose(self, *, storyboard: dict[str, Any], out: Path) -> dict[str, Any]: ...


class Publisher(Protocol):
    """发布包构建器（只打包，不登录、不发布）。"""

    def build(self, *, pack: dict[str, Any], out: Path) -> Path: ...


class NullKB:
    """知识库缺失时的降级实现：检索为空，导入跳过。"""

    def search(self, query: str, *, k: int = 5) -> list[KBHit]:
        return []

    def add(self, paths: Sequence[Path]) -> dict[str, int]:
        return {}


class NullSourceAdapter:
    """信源缺失时的降级实现：返回空列表。"""

    name = "null"

    def fetch(self, *, since: str | None = None) -> list[RawItem]:
        return []


class NullRenderer:
    def render(self, spec: RenderSpec) -> RenderResult:
        raise Unavailable("renderer", "T04")


class NullVideoComposer:
    def compose(self, *, storyboard: dict[str, Any], out: Path) -> dict[str, Any]:
        raise Unavailable("video composer", "T07")


class NullPublisher:
    def build(self, *, pack: dict[str, Any], out: Path) -> Path:
        raise Unavailable("publisher", "T08")


def _log_missing(module_path: str, exc: BaseException) -> None:
    logger.warning("适配器 %s 不可用，退回 Null 实现：%s: %s", module_path, type(exc).__name__, exc)


def get_kb(cfg: Config | None = None) -> KB:
    """加载知识库实现；缺失时返回 NullKB。"""
    try:
        module = importlib.import_module("workbench.knowledge.build")
        return module.build_kb(cfg)  # type: ignore[no-any-return]
    except Exception as exc:  # noqa: BLE001 - 缺实现/实现初始化失败都必须降级
        _log_missing("workbench.knowledge.build", exc)
        return NullKB()


def get_renderer() -> Renderer:
    try:
        module = importlib.import_module("workbench.render.build")
        return module.build_renderer()  # type: ignore[no-any-return]
    except Exception as exc:  # noqa: BLE001
        _log_missing("workbench.render.build", exc)
        return NullRenderer()


def get_composer() -> VideoComposer:
    try:
        module = importlib.import_module("workbench.video.build")
        return module.build_composer()  # type: ignore[no-any-return]
    except Exception as exc:  # noqa: BLE001
        _log_missing("workbench.video.build", exc)
        return NullVideoComposer()


def get_publisher() -> Publisher:
    try:
        module = importlib.import_module("workbench.publish.build")
        return module.build_publisher()  # type: ignore[no-any-return]
    except Exception as exc:  # noqa: BLE001
        _log_missing("workbench.publish.build", exc)
        return NullPublisher()


def get_source_adapters(cfg: Config | None = None) -> list[SourceAdapter]:
    try:
        module = importlib.import_module("workbench.topics.sources")
        adapters = module.build_adapters(cfg)
        return list(adapters)
    except Exception as exc:  # noqa: BLE001
        _log_missing("workbench.topics.sources", exc)
        return []


__all__ = [
    "KB",
    "KBHit",
    "NullKB",
    "NullPublisher",
    "NullRenderer",
    "NullSourceAdapter",
    "NullVideoComposer",
    "Publisher",
    "RawItem",
    "RenderResult",
    "RenderSpec",
    "Renderer",
    "SourceAdapter",
    "VideoComposer",
    "get_composer",
    "get_kb",
    "get_publisher",
    "get_renderer",
    "get_source_adapters",
]
