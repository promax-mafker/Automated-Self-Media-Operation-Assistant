"""统一异常类型；CLI 依据 ``exit_code`` 决定退出码。"""

from __future__ import annotations


class WorkbenchError(Exception):
    """所有可预期错误的基类。"""

    exit_code = 1


class ConfigError(WorkbenchError):
    """环境变量或配置不合法。"""


class StoreError(WorkbenchError):
    """数据库迁移或读写失败。"""


class Unavailable(WorkbenchError):
    """某个能力尚未实现（由后续 Ticket 提供）。"""

    def __init__(self, capability: str, ticket: str | None = None) -> None:
        self.capability = capability
        self.ticket = ticket
        suffix = f"（{ticket}）" if ticket else ""
        super().__init__(f"Unavailable: {capability} 尚未实现{suffix}")


class MissingStage(WorkbenchError):
    """流水线声明的 stage 尚未实现。"""

    exit_code = 2

    def __init__(self, pipeline: str, stage: str, ticket: str | None = None) -> None:
        self.pipeline = pipeline
        self.stage = stage
        self.ticket = ticket
        super().__init__(self._message())

    def _message(self) -> str:
        suffix = f"（{self.ticket}）" if self.ticket else ""
        return f"MissingStage: {self.pipeline}/{self.stage} 尚未实现{suffix}"

    def __str__(self) -> str:
        return self._message()


class MissingParam(WorkbenchError):
    """stage 声明的必需 ``--input`` 参数缺失。"""

    exit_code = 2

    def __init__(self, pipeline: str, stage: str, key: str) -> None:
        self.pipeline = pipeline
        self.stage = stage
        self.key = key
        super().__init__(
            f"MissingParam: {pipeline}/{stage} 缺少必需参数 {key!r}，请用 --input {key}=<值> 传入"
        )


class PipelineFailed(WorkbenchError):
    """stage 执行抛异常（job 已标记 failed）。"""

    def __init__(self, pipeline: str, stage: str, cause: BaseException | str) -> None:
        self.pipeline = pipeline
        self.stage = stage
        self.cause = cause
        super().__init__(f"PipelineFailed: {pipeline}/{stage} 执行失败: {cause}")


class LLMError(WorkbenchError):
    """模型调用或解析失败；``raw`` 保留原始响应前 500 字。"""

    def __init__(self, message: str, raw: str | None = None) -> None:
        self.raw = (raw or "")[:500]
        text = message if not self.raw else f"{message}；原始响应: {self.raw}"
        super().__init__(text)
