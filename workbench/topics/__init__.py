"""选题与爆款对标（T05）。

本包提供三块能力：

- :mod:`workbench.topics.sources`：热榜 JSON / RSS / 本地 fixture 三个信源适配器。
- :mod:`workbench.topics.benchmark`：对标账号爆款画像（burst 阈值 + Top 10%）。
- :mod:`workbench.topics.score`：确定性打分（heat / fit / benchmark / timing）与选题卡。

错误统一继承 :class:`TopicError`；它属于用户输入/数据问题，CLI 默认退出码 2。
"""

from __future__ import annotations

from workbench.errors import WorkbenchError


class TopicError(WorkbenchError):
    """T05 的可预期错误（信源缺失、账号不存在、参数非法等）；退出码 2。"""

    exit_code = 2


__all__ = ["TopicError"]
