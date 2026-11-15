"""T06 文案输出模型：五类 kind 各一个 pydantic 模型。

- 所有 kind 共用 ``title / hook / body / tags``。
- ``spoken`` 额外 ``shots``（镜头提示），``hook`` 额外 ``variants``（至少 5 条；
  最小条数在 ``generate`` 层校验，方便 stub 模式补齐确定性占位变体）。
- 字段缺失或类型不对时，``validate_payload`` 抛出带 ``kind`` / ``length`` 的
  ``CopyValidationError``，CLI 能直接告诉用户是哪一类文案不可用。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from workbench.errors import WorkbenchError

KINDS: tuple[str, ...] = ("spoken", "note", "seed", "explain", "hook")
LENGTHS: tuple[str, ...] = ("short", "long")

# 目标长度只作为 prompt 里的写作目标与产出统计口径；不阻断落库。
LENGTH_TARGET_CHARS: dict[str, int] = {"short": 100, "long": 600}
LENGTH_RANGE: dict[str, tuple[int, int]] = {"short": (1, 100), "long": (400, 800)}

HOOK_MIN_VARIANTS = 5


class CopyValidationError(WorkbenchError):
    """某一条文案不满足输出 schema；消息必须带 kind 与 length。"""

    def __init__(self, kind: str, length: str, detail: str) -> None:
        self.kind = kind
        self.length = length
        self.detail = detail
        super().__init__(f"文案校验失败（kind={kind}，length={length}）：{detail}")


class CopyBase(BaseModel):
    """五类文案的公共字段。"""

    model_config = ConfigDict(extra="ignore")

    title: str = Field(min_length=1)
    hook: str = Field(min_length=1)
    body: str = Field(min_length=1)
    tags: list[str]


class SpokenCopy(CopyBase):
    """口播脚本：body 是可直接念的正文，shots 是镜头提示。"""

    shots: list[str]


class NoteCopy(CopyBase):
    """小红书图文笔记正文。"""


class SeedCopy(CopyBase):
    """种草/带货：卖点 + 异议处理。"""


class ExplainCopy(CopyBase):
    """知识科普/干货。"""


class HookCopy(CopyBase):
    """引流钩子：标题、评论区话术、私信与 5 条以上变体。"""

    variants: list[str]


KIND_SCHEMAS: dict[str, type[CopyBase]] = {
    "spoken": SpokenCopy,
    "note": NoteCopy,
    "seed": SeedCopy,
    "explain": ExplainCopy,
    "hook": HookCopy,
}


def normalize_kinds(raw: str | None) -> list[str]:
    """把 ``--kinds spoken,note`` 解析成去重后的标准顺序。

    空值表示五类全要；未知 kind 直接报错并列出可选项。
    """
    if raw is None or not raw.strip():
        return list(KINDS)
    requested: set[str] = set()
    for item in raw.split(","):
        kind = item.strip()
        if not kind:
            continue
        if kind not in KINDS:
            raise CopyValidationError(
                kind,
                "-",
                f"未知 kind，可选：{', '.join(KINDS)}",
            )
        requested.add(kind)
    if not requested:
        raise CopyValidationError("-", "-", f"--kinds 不能为空，可选：{', '.join(KINDS)}")
    return [kind for kind in KINDS if kind in requested]


def schema_for(kind: str) -> type[CopyBase]:
    try:
        return KIND_SCHEMAS[kind]
    except KeyError as exc:
        raise CopyValidationError(
            kind, "-", f"未知 kind，可选：{', '.join(KINDS)}"
        ) from exc


def _format_errors(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error.get("loc", ())) or "（根）"
        parts.append(f"{location}: {error.get('msg', '校验失败')}")
    return "；".join(parts) or str(exc)


def validate_payload(kind: str, length: str, payload: Any) -> CopyBase:
    """按 kind 校验模型输出；任何缺口都转成带 kind/length 的清晰错误。"""
    model = schema_for(kind)
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise CopyValidationError(kind, length, _format_errors(exc)) from exc


def finalize_payload(kind: str, length: str, payload: CopyBase, *, is_stub: bool) -> CopyBase:
    """补齐/校验 kind 专属字段。

    - stub 模式：``variants`` 不足 5 条时追加确定性占位变体，保证离线产物结构合法。
    - 真实模型：不足 5 条直接报错，不代替模型编造变体。
    """
    if kind == "hook":
        variants = list(getattr(payload, "variants", []) or [])
        if len(variants) < HOOK_MIN_VARIANTS:
            if not is_stub:
                raise CopyValidationError(
                    kind,
                    length,
                    f"variants 至少 {HOOK_MIN_VARIANTS} 条，实际 {len(variants)} 条",
                )
            base = variants[-1] if variants else payload.hook
            labels = ("一", "二", "三", "四", "五")
            for index in range(len(variants), HOOK_MIN_VARIANTS):
                variants.append(f"{base}（备选{labels[index]}）")
            payload = payload.model_copy(update={"variants": variants})
    if kind == "spoken":
        shots = list(getattr(payload, "shots", []) or [])
        if not shots:
            if not is_stub:
                raise CopyValidationError(kind, length, "shots 不能为空")
            payload = payload.model_copy(update={"shots": ["【stub】镜头提示"]})
    return payload


def payload_text(payload: CopyBase) -> str:
    """把一条文案的全部可读文本拼起来，供合规与数字取证检查。"""
    parts: list[str] = [payload.title, payload.hook, payload.body, *payload.tags]
    for name in ("shots", "variants"):
        extra = getattr(payload, name, None)
        if extra:
            parts.extend(str(item) for item in extra)
    return "\n".join(part for part in parts if part)


__all__ = [
    "HOOK_MIN_VARIANTS",
    "KINDS",
    "KIND_SCHEMAS",
    "LENGTHS",
    "LENGTH_RANGE",
    "LENGTH_TARGET_CHARS",
    "CopyBase",
    "CopyValidationError",
    "ExplainCopy",
    "HookCopy",
    "NoteCopy",
    "SeedCopy",
    "SpokenCopy",
    "finalize_payload",
    "normalize_kinds",
    "payload_text",
    "schema_for",
    "validate_payload",
]
