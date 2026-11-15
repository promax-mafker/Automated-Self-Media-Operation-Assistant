"""读取 ``prompts/copy/<kind>.md``：front-matter + 正文模板（带版本号）。

模板只允许三个占位符：

- ``{brief}``：选题卡 JSON（含本次 kind / length / target_chars）
- ``{evidence}``：知识库命中片段，最多 5 条
- ``{brand}``：品牌摘要，可为空

``prompt_version`` = front-matter 的 ``version`` + 文件 sha256 前 8 位，
例如 ``v1+3f2a9c01``；写入 ``copy_set.prompt_version``，便于回滚到旧 prompt。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from workbench.copy.schema import KINDS
from workbench.errors import WorkbenchError
from workbench.llm.base import Message

PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts" / "copy"
ALLOWED_PLACEHOLDERS = frozenset({"brief", "evidence", "brand"})
_REQUIRED_KEYS = ("version", "model", "temperature", "max_tokens")
_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")
SYSTEM_PROMPT = (
    "你是中文自媒体文案助手。只输出一个 JSON 对象，不要输出 Markdown、"
    "解释或代码块围栏；字段名与类型必须符合本次任务要求。"
)


class PromptError(WorkbenchError):
    """prompt 文件缺失、front-matter 不完整或模板占位符非法。"""


@dataclass(frozen=True, slots=True)
class PromptSpec:
    kind: str
    path: Path
    version: str
    model: str
    temperature: float
    max_tokens: int
    template: str
    sha256: str

    @property
    def prompt_version(self) -> str:
        return f"v{self.version}+{self.sha256[:8]}"

    def render(
        self,
        *,
        brief: dict[str, Any],
        evidence: str,
        brand: str | None,
    ) -> list[Message]:
        payload = {
            "brief": json.dumps(brief, ensure_ascii=False, separators=(",", ":")),
            "evidence": evidence or "（知识库无可引用片段；不要写具体数字与事实）",
            "brand": brand or "（无品牌摘要；不要编造品牌信息）",
        }
        try:
            content = self.template.format(**payload)
        except (KeyError, ValueError) as exc:
            raise PromptError(f"prompt {self.path} 渲染失败：{exc}") from exc
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ]


def _parse_front_matter(text: str, path: Path) -> tuple[dict[str, str], str]:
    match = _FRONT_MATTER_RE.match(text)
    if match is None:
        raise PromptError(f"prompt {path} 缺少 --- 包裹的 front-matter")
    raw_meta, template = match.groups()
    meta: dict[str, str] = {}
    for line in raw_meta.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            raise PromptError(f"prompt {path} 的 front-matter 行不合法：{stripped!r}")
        key, value = stripped.split(":", 1)
        meta[key.strip()] = value.strip().strip("'\"")
    missing = [key for key in _REQUIRED_KEYS if not meta.get(key)]
    if missing:
        raise PromptError(f"prompt {path} 缺少 front-matter 字段：{', '.join(missing)}")
    if not template.strip():
        raise PromptError(f"prompt {path} 的正文模板为空")

    placeholders = set(_PLACEHOLDER_RE.findall(template))
    unknown = placeholders - ALLOWED_PLACEHOLDERS
    if unknown:
        raise PromptError(
            f"prompt {path} 含非法占位符：{', '.join(sorted(unknown))}；"
            f"只允许 {', '.join(sorted(ALLOWED_PLACEHOLDERS))}"
        )
    return meta, template


def _parse_float(raw: str, path: Path) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise PromptError(f"prompt {path} 的 temperature 不是数字：{raw!r}") from exc
    if not 0.0 <= value <= 2.0:
        raise PromptError(f"prompt {path} 的 temperature 必须在 0-2 之间：{value}")
    return value


def _parse_int(raw: str, path: Path) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise PromptError(f"prompt {path} 的 max_tokens 不是整数：{raw!r}") from exc
    if value <= 0:
        raise PromptError(f"prompt {path} 的 max_tokens 必须大于 0：{value}")
    return value


def load_prompt(kind: str, *, prompt_dir: Path | None = None) -> PromptSpec:
    """读取单个 kind 的 prompt；版本、模型参数与模板一次解析完成。"""
    if kind not in KINDS:
        raise PromptError(f"未知 kind：{kind!r}；可选：{', '.join(KINDS)}")
    directory = Path(prompt_dir) if prompt_dir is not None else PROMPT_DIR
    path = directory / f"{kind}.md"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PromptError(f"读取 prompt 失败：{path}（{exc}）") from exc
    text = raw.decode("utf-8")
    meta, template = _parse_front_matter(text, path)

    version = meta["version"].strip()
    if version.lower().startswith("v"):
        version = version[1:]
    if not version:
        raise PromptError(f"prompt {path} 的 version 不能为空")

    return PromptSpec(
        kind=kind,
        path=path,
        version=version,
        model=meta["model"],
        temperature=_parse_float(meta["temperature"], path),
        max_tokens=_parse_int(meta["max_tokens"], path),
        template=template.strip(),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def load_all_prompts(*, prompt_dir: Path | None = None) -> dict[str, PromptSpec]:
    """读取并校验五个 prompt；缺任何一个都报错。"""
    return {kind: load_prompt(kind, prompt_dir=prompt_dir) for kind in KINDS}


__all__ = [
    "ALLOWED_PLACEHOLDERS",
    "PROMPT_DIR",
    "SYSTEM_PROMPT",
    "PromptError",
    "PromptSpec",
    "load_all_prompts",
    "load_prompt",
]
